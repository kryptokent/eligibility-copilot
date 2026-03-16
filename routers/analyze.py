from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import List, Literal, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# Load config from .env if present (local dev); otherwise use os.environ (e.g. production)
_env_path = Path(__file__).resolve().parent.parent / ".env"
_config = dotenv_values(_env_path) if _env_path.exists() else dict(os.environ)

_aws_region = _config.get("AWS_DEFAULT_REGION", "us-east-1")
_aws_key = _config.get("AWS_ACCESS_KEY_ID")
_aws_secret = _config.get("AWS_SECRET_ACCESS_KEY")

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

    client = boto3.client(
        "bedrock-runtime",
        region_name=_aws_region,
        aws_access_key_id=_aws_key,
        aws_secret_access_key=_aws_secret,
    )

    language_instruction = (
        "Write all explanations and missing_information values in clear English suitable for a front-line caseworker."
        if response_language == "english"
        else "Write all explanations and missing_information values in clear Spanish suitable for a front-line caseworker."
    )

    system_prompt = (
        "You are an expert U.S. public benefits intake screener. "
        "Given an intake document, you will assess likely eligibility for three programs: "
        "SNAP (food assistance), Medicaid, and CHIP. "
        "Base your assessment ONLY on the information in the document; if key data is missing, "
        "respond with 'maybe' and explicitly list what data is missing.\n\n"
        f"{language_instruction}\n\n"
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
            "max_tokens": 1024,
            "temperature": 0,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": system_prompt},
                        {"type": "text", "text": user_prompt},
                    ],
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
        msg = str(e)
        if isinstance(e, ClientError):
            err = e.response.get("Error", {})
            code = err.get("Code")
            message = err.get("Message")
            if code or message:
                msg = f"{code or 'BedrockError'}: {message or ''}".strip()
        raise RuntimeError(msg) from e

    try:
        raw = response.get("body")
        if hasattr(raw, "read"):
            raw = raw.read()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Failed to parse Bedrock response JSON: {e}") from e

    programs = data.get("programs") or []
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


