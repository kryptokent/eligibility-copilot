from __future__ import annotations

import json
import os
import re
import sqlite3
import traceback
from pathlib import Path
from typing import Any, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "overrides.db"

# Load config from .env if present (local dev); otherwise use os.environ (e.g. production)
_env_path = Path(__file__).resolve().parent.parent / ".env"
_config = dotenv_values(_env_path) if _env_path.exists() else dict(os.environ)
_aws_region = _config.get("AWS_DEFAULT_REGION", "us-east-1")
_aws_key = _config.get("AWS_ACCESS_KEY_ID")
_aws_secret = _config.get("AWS_SECRET_ACCESS_KEY")


class GenerateGovernanceRequest(BaseModel):
    document_id: str = Field(..., description="Document ID to generate a governance report for.")


class GovernanceReport(BaseModel):
    document_id: str
    document_summary: str
    ai_determinations: str
    human_overrides: str
    language_parity_status: str
    audit_trail: str


def _get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_governance_schema() -> None:
    with _get_connection() as conn:
        # Documents table (also created from upload router; kept identical here).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                extracted_text TEXT NOT NULL,
                detected_language TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # Parity reports table (also created from analyze router; schema must match).
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
        # Overrides table (also created from overrides router; schema must match).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                document_id TEXT NOT NULL,
                program TEXT NOT NULL,
                original_determination TEXT NOT NULL,
                override_decision TEXT NOT NULL,
                override_reason TEXT NOT NULL,
                caseworker_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        # Governance reports table.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS governance_reports (
                document_id TEXT PRIMARY KEY,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.commit()


def _load_document(document_id: str) -> Optional[sqlite3.Row]:
    with _get_connection() as conn:
        return conn.execute(
            "SELECT * FROM documents WHERE document_id = ?",
            (document_id,),
        ).fetchone()


def _load_parity(document_id: str) -> Optional[sqlite3.Row]:
    with _get_connection() as conn:
        return conn.execute(
            "SELECT * FROM parity_reports WHERE document_id = ?",
            (document_id,),
        ).fetchone()


def _load_overrides(document_id: str) -> List[sqlite3.Row]:
    with _get_connection() as conn:
        return conn.execute(
            "SELECT * FROM overrides WHERE document_id = ? ORDER BY created_at ASC, id ASC",
            (document_id,),
        ).fetchall()


def _store_governance_report(document_id: str, report: GovernanceReport) -> None:
    _ensure_governance_schema()
    with _get_connection() as conn:
        conn.execute(
            """
            INSERT INTO governance_reports (document_id, report_json)
            VALUES (?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                report_json=excluded.report_json
            """,
            (document_id, report.model_dump_json()),
        )
        conn.commit()


def _invoke_bedrock_for_governance(context: dict[str, Any]) -> GovernanceReport:
    client = boto3.client(
        "bedrock-runtime",
        region_name=_aws_region,
        aws_access_key_id=_aws_key,
        aws_secret_access_key=_aws_secret,
    )

    system_prompt = (
        "You are an AI governance and audit specialist for public benefits programs. "
        "Using the provided context, you will generate a concise but complete governance report "
        "suitable for compliance, audit, and human review.\n\n"
        "Return your answer strictly as JSON with this exact shape:\n"
        "{\n"
        '  "document_id": "<same as input>",\n'
        '  "document_summary": "<narrative summary of the intake document>",\n'
        '  "ai_determinations": "<summary of AI eligibility determinations across programs>",\n'
        '  "human_overrides": "<summary of any human overrides and their rationale>",\n'
        '  "language_parity_status": "<summary of language parity status and any gaps>",\n'
        '  "audit_trail": "<chronological audit trail of key events and decisions>"\n'
        "}\n"
        "Use clear professional language. Do not include any additional keys or commentary outside this JSON object."
    )

    user_prompt = (
        "Here is the full context you should base the report on, as JSON:\n\n"
        f"{json.dumps(context, ensure_ascii=False)}"
    )

    body = json.dumps(
        {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 2048,
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
        response_body = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Failed to parse Bedrock response JSON: {e}") from e

    # Bedrock Messages API returns { "content": [ {"type": "text", "text": "..."} ], "stop_reason": ... }
    # The model's JSON report is inside content[].text, not at the top level.
    text_parts = [
        block.get("text", "")
        for block in response_body.get("content", [])
        if block.get("type") == "text"
    ]
    report_text = "".join(text_parts).strip()
    if not report_text:
        raise RuntimeError("Bedrock response contained no text content.")

    # Claude may wrap JSON in markdown code blocks; strip them if present
    code_block = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", report_text)
    if code_block:
        report_text = code_block.group(1).strip()

    try:
        data = json.loads(report_text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Failed to parse model output as JSON: {e}") from e

    try:
        return GovernanceReport(**data)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Bedrock returned an invalid governance report payload: {e}") from e


@router.post("/api/generate-governance-report", response_model=GovernanceReport)
async def generate_governance_report(payload: GenerateGovernanceRequest) -> GovernanceReport:
    """
    Compose a governance report for a document by aggregating whatever data is available:
    - extracted document text (if stored)
    - AI eligibility determinations (if parity report exists)
    - human overrides
    and passing the bundle to AWS Bedrock (Claude) for narrative synthesis.
    Generates a report with whatever data exists rather than failing when document row is missing.
    """
    try:
        _ensure_governance_schema()
    except sqlite3.Error as e:
        raise HTTPException(
            status_code=500,
            detail=f"Database error while preparing governance schema: {e}",
        ) from e

    try:
        doc_row = _load_document(payload.document_id)
        parity_row = _load_parity(payload.document_id)
        overrides_rows = _load_overrides(payload.document_id)

        # Build document context from stored row if present; otherwise use placeholders
        if doc_row is not None:
            extracted_text = (doc_row["extracted_text"] or "")[:20000]
            filename = doc_row["filename"] or "(unknown)"
            detected_language = doc_row["detected_language"]
        else:
            extracted_text = "(No extracted text was stored for this document. Report is based on eligibility and override data only.)"
            filename = "(not stored)"
            detected_language = None

        english_programs = []
        spanish_programs = []
        if parity_row:
            try:
                english_programs = json.loads(parity_row["english_json"] or "[]")
                spanish_programs = json.loads(parity_row["spanish_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                english_programs = []
                spanish_programs = []

        overrides_serialized = []
        for row in overrides_rows:
            overrides_serialized.append({
                "program": row["program"],
                "original_determination": row["original_determination"],
                "override_decision": row["override_decision"],
                "override_reason": row["override_reason"],
                "caseworker_id": row["caseworker_id"],
                "created_at": row["created_at"],
            })

        context = {
            "document": {
                "document_id": payload.document_id,
                "filename": filename,
                "detected_language": detected_language,
                "extracted_text": extracted_text,
            },
            "ai_determinations": {
                "english_programs": english_programs,
                "spanish_programs": spanish_programs,
            },
            "overrides": overrides_serialized,
        }

        report = _invoke_bedrock_for_governance(context)

        try:
            _store_governance_report(payload.document_id, report)
        except sqlite3.Error:
            pass

        return report

    except HTTPException:
        traceback.print_exc()
        raise
    except RuntimeError as e:
        traceback.print_exc()
        raise HTTPException(
            status_code=502,
            detail=f"AWS Bedrock failed to generate the governance report: {str(e)}",
        ) from e
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(
            status_code=500,
            detail=f"Error generating governance report: {type(e).__name__}: {e}",
        ) from e


@router.get("/api/governance-report/{document_id}", response_model=GovernanceReport)
async def get_governance_report(document_id: str) -> GovernanceReport:
    """
    Retrieve a previously generated governance report for the given document.
    """
    _ensure_governance_schema()
    with _get_connection() as conn:
        row = conn.execute(
            "SELECT report_json FROM governance_reports WHERE document_id = ?",
            (document_id,),
        ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="No governance report found for this document_id.",
        )

    try:
        data = json.loads(row["report_json"])
        return GovernanceReport(**data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=500,
            detail=f"Stored governance report is corrupted or invalid: {e}",
        ) from e

