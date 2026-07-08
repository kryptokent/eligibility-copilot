from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Merge process env with .env so platform secrets (e.g. Render) work, and local .env can override when set.
_env_path = Path(__file__).resolve().parent.parent / ".env"
_env_from_file = dotenv_values(_env_path) if _env_path.exists() else {}
_config: Dict[str, Optional[str]] = {**dict(os.environ), **{k: v for k, v in _env_from_file.items() if v}}

_aws_region = (_config.get("AWS_DEFAULT_REGION") or "us-east-1").strip()
_aws_key = (_config.get("AWS_ACCESS_KEY_ID") or "").strip() or None
_aws_secret = (_config.get("AWS_SECRET_ACCESS_KEY") or "").strip() or None

BASE_DIR = Path(__file__).resolve().parent.parent
PARITY_DB_PATH = BASE_DIR / "overrides.db"

router = APIRouter()


class AnalyzeRequest(BaseModel):
    extracted_text: str = Field(
        ...,
        description="Full OCR text extracted from the uploaded document.",
    )
    document_id: Optional[str] = Field(
        default=None,
        description="Document ID from the upload step, used for parity reporting.",
    )
    detected_language: Optional[Literal["english", "spanish"]] = Field(
        default=None,
        description="Language detected during OCR; drives which language is primary in the UI.",
    )


class EligibilityItem(BaseModel):
    program: Literal["SNAP", "Medicaid", "CHIP"]
    eligibility: Literal["yes", "no", "maybe"]
    reason: str
    missing_information: List[str] = Field(
        default_factory=list,
        description="Specific data points needed to complete a high‑confidence determination.",
    )


class ParityProgramDiff(BaseModel):
    program: Literal["SNAP", "Medicaid", "CHIP"]
    english_eligibility: Literal["yes", "no", "maybe"]
    spanish_eligibility: Literal["yes", "no", "maybe"]


class ParityReport(BaseModel):
    document_id: Optional[str]
    detected_language: Optional[str]
    parity_match: bool
    differences: List[ParityProgramDiff] = Field(
        default_factory=list,
        description="Per-program differences between English and Spanish determinations.",
    )
    english_programs: List[EligibilityItem]
    spanish_programs: List[EligibilityItem]


class AnalyzeResponse(BaseModel):
    programs: List[EligibilityItem]
    parity: Optional[ParityReport] = None


@dataclass
class _BedrockChecklist:
    program: str
    eligibility: str
    reason: str
    missing_information: List[str]


def _get_parity_connection() -> sqlite3.Connection:
    PARITY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(PARITY_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_parity_schema() -> None:
    with _get_parity_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS parity_reports (
                document_id TEXT PRIMARY KEY,
                detected_language TEXT,
                english_json TEXT NOT NULL,
                spanish_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()


def _extract_json_object_from_text(text: str) -> Optional[dict]:
    """Parse a JSON object from model output, including optional ```json fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        while lines and lines[-1].strip() in ("```", ""):
            lines.pop()
        text = "\n".join(lines).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if match:
        try:
            obj = json.loads(match.group())
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _extract_programs_payload(data: Any) -> dict:
    """
    Bedrock Claude Messages API returns assistant output in `content` blocks, not top-level `programs`.
    Accept either shape and normalize to a dict that may contain `programs`.
    """
    if not isinstance(data, dict):
        return {}
    if isinstance(data.get("programs"), list):
        return data
    content = data.get("content")
    if isinstance(content, list):
        texts: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                texts.append(str(block.get("text") or ""))
        combined = "\n".join(texts)
        extracted = _extract_json_object_from_text(combined)
        if extracted and isinstance(extracted.get("programs"), list):
            return extracted
    return {}


def _bedrock_runtime_client():
    """Build client; only pass explicit keys when set so boto3 default chain works (e.g. IAM role)."""
    kwargs: dict = {"region_name": _aws_region}
    if _aws_key and _aws_secret:
        kwargs["aws_access_key_id"] = _aws_key
        kwargs["aws_secret_access_key"] = _aws_secret
    return boto3.client("bedrock-runtime", **kwargs)


def _invoke_bedrock_for_checklist(
    extracted_text: str,
    response_language: Literal["english", "spanish"],
) -> List[_BedrockChecklist]:
    """
    Call AWS Bedrock (Claude) to turn free‑form intake text into
    a small, structured eligibility checklist for SNAP, Medicaid, and CHIP.
    The `response_language` flag controls whether the explanation fields
    are written in English or Spanish.
    """
    if not extracted_text or not extracted_text.strip():
        raise ValueError("No extracted text was provided for analysis.")

    client = _bedrock_runtime_client()

    language_instruction = (
        "Write all explanations and missing_information values in clear English suitable for a front-line caseworker."
        if response_language == "english"
        else "Write all explanations and missing_information values in clear Spanish suitable for a front-line caseworker."
    )

    system_prompt = (
        "You are a benefits eligibility specialist. Based on the document provided, you MUST make a clear determination for each program.\n\n"
        "For SNAP in Texas: A household of 3 with gross monthly income under $2,311 is likely ELIGIBLE.\n"
        "For Medicaid in Texas: Adults with income under 138% FPL may qualify. Children under 19 in households under 200% FPL qualify.\n"
        "For CHIP in Texas: Children under 19 in households earning between 100-200% FPL qualify.\n\n"
        'You must respond with "Eligible", "Not Eligible", or "Uncertain" for each program with a clear reason. '
        "Do not say uncertain if you have enough information to make a determination.\n\n"
        f"{language_instruction}\n\n"
        'In the JSON below, encode each determination in the "eligibility" field as exactly '
        '"yes" (Eligible), "no" (Not Eligible), or "maybe" (Uncertain only when information is genuinely insufficient). '
        "If key data is missing, use \"maybe\" and list what is needed in missing_information.\n\n"
        "Return your answer strictly as JSON with this exact shape:\n"
        "{\n"
        '  "programs": [\n'
        '    {\n'
        '      "program": "SNAP" | "Medicaid" | "CHIP",\n'
        '      "eligibility": "yes" | "no" | "maybe",\n'
        '      "reason": "short natural-language explanation",\n'
        '      "missing_information": ["list", "of", "missing", "fields"]\n'
        "    },\n"
        "    ... one item for each of the three programs ...\n"
        "  ]\n"
        "}\n"
        "Do not include any additional commentary or keys outside this JSON object."
    )

    user_prompt = (
        "Intake document text:\n\n"
        f"{extracted_text[:15000]}\n\n"
        "Use only this text for your assessment."
    )

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 4096,
            "temperature": 0,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": user_prompt}],
                }
            ],
        }
    )

    try:
        response = client.invoke_model(
            modelId="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            contentType="application/json",
            accept="application/json",
            body=body,
        )
    except (ClientError, BotoCoreError) as e:
        traceback.print_exc()
        msg = str(e)
        if isinstance(e, ClientError):
            err = e.response.get("Error", {})
            code = err.get("Code")
            message = err.get("Message")
            if code or message:
                msg = f"{code or 'BedrockError'}: {message or ''}".strip()
        raise RuntimeError(msg) from e
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        raise RuntimeError(f"Unexpected error calling Bedrock: {e}") from e

    try:
        raw = response.get("body")
        if hasattr(raw, "read"):
            raw = raw.read()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        raw_str = raw if isinstance(raw, str) else str(raw)
        print(f"[Bedrock] raw response body:\n{raw_str}")
        logger.info("Bedrock raw response body: %s", raw_str)
        data = json.loads(raw_str)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        raise RuntimeError(f"Failed to parse Bedrock response JSON: {e}") from e

    payload = _extract_programs_payload(data)
    programs = payload.get("programs") or []
    if not programs:
        print(
            "[Bedrock] No programs in parsed payload; top-level keys: "
            f"{list(data.keys()) if isinstance(data, dict) else type(data)}"
        )
        logger.warning(
            "Bedrock response had no programs after parsing; keys=%s",
            list(data.keys()) if isinstance(data, dict) else None,
        )
    checklist: List[_BedrockChecklist] = []

    for item in programs:
        try:
            program = str(item.get("program", "")).strip()
            eligibility = str(item.get("eligibility", "")).strip().lower()
            if program not in {"SNAP", "Medicaid", "CHIP"}:
                continue
            if eligibility not in {"yes", "no", "maybe"}:
                eligibility = "maybe"

            reason = str(item.get("reason", "")).strip() or "No explanation provided."
            missing = item.get("missing_information") or []
            if isinstance(missing, str):
                missing_list = [missing]
            else:
                missing_list = [str(m).strip() for m in missing if str(m).strip()]

            checklist.append(
                _BedrockChecklist(
                    program=program,
                    eligibility=eligibility,
                    reason=reason,
                    missing_information=missing_list,
                )
            )
        except Exception:
            continue

    # Ensure all three programs are present, even if Bedrock omitted some.
    for p in ["SNAP", "Medicaid", "CHIP"]:
        if not any(c.program == p for c in checklist):
            checklist.append(
                _BedrockChecklist(
                    program=p,
                    eligibility="maybe",
                    reason="Model did not return a determination for this program.",
                    missing_information=["Household size", "Income", "State of residence"],
                )
            )

    return checklist


def _check_parity(
    english_items: List[EligibilityItem],
    spanish_items: List[EligibilityItem],
) -> ParityReport:
    english_by_program = {p.program: p for p in english_items}
    spanish_by_program = {p.program: p for p in spanish_items}

    differences: List[ParityProgramDiff] = []
    for program in ["SNAP", "Medicaid", "CHIP"]:
        e = english_by_program.get(program)
        s = spanish_by_program.get(program)
        if not e or not s:
            continue
        if e.eligibility != s.eligibility:
            differences.append(
                ParityProgramDiff(
                    program=program,
                    english_eligibility=e.eligibility,
                    spanish_eligibility=s.eligibility,
                )
            )

    parity_match = len(differences) == 0

    return ParityReport(
        document_id=None,
        detected_language=None,
        parity_match=parity_match,
        differences=differences,
        english_programs=english_items,
        spanish_programs=spanish_items,
    )


def _store_parity_report(document_id: str, detected_language: Optional[str], report: ParityReport) -> None:
    _ensure_parity_schema()
    data = {
        "document_id": document_id,
        "detected_language": detected_language,
        "english_json": json.dumps([item.model_dump() for item in report.english_programs]),
        "spanish_json": json.dumps([item.model_dump() for item in report.spanish_programs]),
    }
    with _get_parity_connection() as conn:
        conn.execute(
            """
            INSERT INTO parity_reports (document_id, detected_language, english_json, spanish_json)
            VALUES (:document_id, :detected_language, :english_json, :spanish_json)
            ON CONFLICT(document_id) DO UPDATE SET
                detected_language=excluded.detected_language,
                english_json=excluded.english_json,
                spanish_json=excluded.spanish_json
            """,
            data,
        )
        conn.commit()


@router.post("/api/analyze-document", response_model=AnalyzeResponse)
async def analyze_document(payload: AnalyzeRequest) -> AnalyzeResponse:
    """
    Run the extracted intake text through AWS Bedrock (Claude) to generate
    SNAP / Medicaid / CHIP eligibility checklists in both English and Spanish,
    and compute a language parity report.
    """
    try:
        english_checklist = _invoke_bedrock_for_checklist(payload.extracted_text, "english")
        spanish_checklist = _invoke_bedrock_for_checklist(payload.extracted_text, "spanish")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except RuntimeError as e:
        raise HTTPException(
            status_code=502,
            detail=(
                "AWS Bedrock failed to analyze the document. "
                f"Details: {str(e)}"
            ),
        ) from e

    english_items = [
        EligibilityItem(
            program=c.program,  # type: ignore[arg-type]
            eligibility=c.eligibility,  # type: ignore[arg-type]
            reason=c.reason,
            missing_information=c.missing_information,
        )
        for c in english_checklist
    ]
    spanish_items = [
        EligibilityItem(
            program=c.program,  # type: ignore[arg-type]
            eligibility=c.eligibility,  # type: ignore[arg-type]
            reason=c.reason,
            missing_information=c.missing_information,
        )
        for c in spanish_checklist
    ]

    parity = _check_parity(english_items, spanish_items)
    parity.document_id = payload.document_id
    parity.detected_language = payload.detected_language

    # Persist parity data keyed by document_id so it can be retrieved later.
    if payload.document_id:
        try:
            _store_parity_report(payload.document_id, payload.detected_language, parity)
        except sqlite3.Error:
            # Best-effort; don't fail the main analysis if parity logging fails.
            pass

    # For the UI, show analysis in the same language as the intake when Spanish is detected.
    primary_language = payload.detected_language or "english"
    primary_items = spanish_items if primary_language == "spanish" else english_items

    return AnalyzeResponse(programs=primary_items, parity=parity)


@router.get("/api/parity-report/{document_id}", response_model=ParityReport)
async def get_parity_report(document_id: str) -> ParityReport:
    """
    Fetch a previously-computed parity report for a document.
    """
    _ensure_parity_schema()
    with _get_parity_connection() as conn:
        row = conn.execute(
            "SELECT document_id, detected_language, english_json, spanish_json FROM parity_reports WHERE document_id = ?",
            (document_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="No parity report found for this document_id.")

    english_raw = json.loads(row["english_json"])
    spanish_raw = json.loads(row["spanish_json"])

    english_items = [EligibilityItem(**item) for item in english_raw]
    spanish_items = [EligibilityItem(**item) for item in spanish_raw]

    report = _check_parity(english_items, spanish_items)
    report.document_id = row["document_id"]
    report.detected_language = row["detected_language"]
    return report


