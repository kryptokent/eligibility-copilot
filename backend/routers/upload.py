from __future__ import annotations

import os
import re
import sqlite3
import time
import traceback
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import dotenv_values
from fastapi import APIRouter, File, HTTPException, UploadFile
from pypdf import PdfReader

# Load config from .env if present (local dev); otherwise use os.environ (e.g. production)
_env_path = Path(__file__).resolve().parent.parent / ".env"
_config = dotenv_values(_env_path) if _env_path.exists() else dict(os.environ)

# Startup log: show whether AWS credentials and region are set (visible in terminal on launch)
_aws_key = _config.get("AWS_ACCESS_KEY_ID")
_aws_secret = _config.get("AWS_SECRET_ACCESS_KEY")
_aws_region = _config.get("AWS_DEFAULT_REGION")
_creds_status = (
    "found"
    if (_aws_key and _aws_secret and _aws_key != "PLACEHOLDER" and _aws_secret != "PLACEHOLDER")
    else "NOT FOUND or still PLACEHOLDER"
)
print(f"[upload] AWS credentials: {_creds_status}; AWS_DEFAULT_REGION={_aws_region or '(not set)'}")

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOADS_DIR = BASE_DIR / "uploads"
MAX_FILE_BYTES = 25 * 1024 * 1024  # keep demo safe; adjust later if needed
DB_PATH = BASE_DIR / "overrides.db"

SPANISH_COMMON_WORDS = {
    "el",
    "la",
    "los",
    "las",
    "de",
    "del",
    "y",
    "que",
    "por",
    "para",
    "con",
    "sin",
    "una",
    "un",
    "su",
    "sus",
    "se",
    "es",
    "son",
    "como",
    "pero",
    "más",
    "menos",
    "sí",
    "no",
}


def _get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_documents_schema() -> None:
    with _get_connection() as conn:
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
        conn.commit()


def _ensure_uploads_dir() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _cleanup_old_uploads(max_age_seconds: int = 24 * 60 * 60) -> None:
    """Delete any file in uploads/ older than max_age_seconds."""
    _ensure_uploads_dir()
    now = time.time()
    for p in UPLOADS_DIR.iterdir():
        if not p.is_file():
            continue
        try:
            age = now - p.stat().st_mtime
            if age > max_age_seconds:
                p.unlink(missing_ok=True)
        except OSError:
            # Best-effort cleanup; don't block uploads if a file is locked.
            continue


def _safe_filename(original: str) -> str:
    # Keep letters/numbers/._- and replace everything else with _
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", original or "document.pdf").strip("._")
    if not cleaned.lower().endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned[:120]  # avoid crazy-long names


def _detect_language(text: str) -> str:
    """
    Very lightweight Spanish detection:
    count common Spanish words; if enough matches, label as spanish.
    """
    tokens = re.findall(r"[a-záéíóúñü]+", (text or "").lower())
    hits = sum(1 for t in tokens if t in SPANISH_COMMON_WORDS)
    return "spanish" if hits >= 6 else "english"


def _textract_extract_text(document_bytes: bytes, *, s3_key_hint: str | None = None) -> str:
    """
    Extract text using AWS Textract.

    Textract's synchronous DetectDocumentText often rejects PDFs with
    UnsupportedDocumentException. When that happens, we switch to asynchronous
    StartDocumentTextDetection + GetDocumentTextDetection (requires S3).
    """
    config = _config

    textract = boto3.client(
        "textract",
        region_name=config.get("AWS_DEFAULT_REGION", "us-east-1"),
        aws_access_key_id=config.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=config.get("AWS_SECRET_ACCESS_KEY"),
    )

    blocks: list[dict] = []
    try:
        # Always send bytes correctly for synchronous OCR attempts.
        resp = textract.detect_document_text(Document={"Bytes": document_bytes})
        blocks = resp.get("Blocks", []) or []
    except (ClientError, BotoCoreError) as e:
        msg = str(e)
        code = None
        if isinstance(e, ClientError):
            err = e.response.get("Error", {})
            code = err.get("Code")

        # If it's not UnsupportedDocumentException, surface the underlying error.
        if code != "UnsupportedDocumentException":
            if isinstance(e, ClientError):
                err = e.response.get("Error", {})
                message = err.get("Message")
                if code or message:
                    msg = f"{code or 'TextractError'}: {message or ''}".strip()
            raise RuntimeError(msg) from e

        # Async fallback for PDFs (requires an S3 bucket).
        bucket = config.get("TEXTRACT_S3_BUCKET") or config.get("AWS_TEXTRACT_S3_BUCKET")
        prefix = (config.get("TEXTRACT_S3_PREFIX") or "textract-uploads").strip("/")
        if not bucket:
            raise RuntimeError(
                "Textract rejected this PDF via the synchronous API. "
                "To process PDFs reliably, configure TEXTRACT_S3_BUCKET for async Textract OCR."
            ) from e

        s3 = boto3.client(
            "s3",
            region_name=config.get("AWS_DEFAULT_REGION", "us-east-1"),
            aws_access_key_id=config.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=config.get("AWS_SECRET_ACCESS_KEY"),
        )

        safe_hint = re.sub(r"[^A-Za-z0-9._-]+", "_", (s3_key_hint or "document.pdf"))[:80]
        object_key = f"{prefix}/{uuid.uuid4()}_{safe_hint}"

        try:
            s3.put_object(
                Bucket=bucket,
                Key=object_key,
                Body=document_bytes,
                ContentType="application/pdf",
            )

            start = textract.start_document_text_detection(
                DocumentLocation={"S3Object": {"Bucket": bucket, "Name": object_key}}
            )
            job_id = start.get("JobId")
            if not job_id:
                raise RuntimeError("Textract async job did not return a JobId.")

            max_wait_seconds = int(config.get("TEXTRACT_POLL_MAX_SECONDS") or 120)
            sleep_seconds = 1.0
            waited = 0.0
            status = "IN_PROGRESS"

            while status == "IN_PROGRESS" and waited < max_wait_seconds:
                time.sleep(sleep_seconds)
                waited += sleep_seconds
                sleep_seconds = min(sleep_seconds * 1.5, 5.0)

                status_resp = textract.get_document_text_detection(JobId=job_id, MaxResults=1)
                status = status_resp.get("JobStatus") or "IN_PROGRESS"

            if status != "SUCCEEDED":
                raise RuntimeError(f"Textract async OCR did not succeed (status={status}).")

            next_token = None
            while True:
                page = (
                    textract.get_document_text_detection(JobId=job_id, NextToken=next_token)
                    if next_token
                    else textract.get_document_text_detection(JobId=job_id)
                )
                blocks.extend(page.get("Blocks", []) or [])
                next_token = page.get("NextToken")
                if not next_token:
                    break
        except (ClientError, BotoCoreError) as async_err:
            raise RuntimeError(f"Textract async OCR failed: {async_err}") from async_err
        finally:
            try:
                s3.delete_object(Bucket=bucket, Key=object_key)
            except Exception:
                pass
    lines_by_page: dict[int, list[str]] = {}
    for b in blocks:
        if b.get("BlockType") != "LINE":
            continue
        page = int(b.get("Page") or 1)
        lines_by_page.setdefault(page, []).append(b.get("Text", ""))

    if not lines_by_page:
        return ""

    parts: list[str] = []
    for page in sorted(lines_by_page.keys()):
        if len(lines_by_page) > 1:
            parts.append(f"--- Page {page} ---")
        parts.extend([ln for ln in lines_by_page[page] if ln])
        parts.append("")  # spacer
    return "\n".join(parts).strip()


@router.post("/api/upload-document")
async def upload_document(file: UploadFile = File(...)):
    """
    Accept a PDF, save it temporarily, run Textract OCR, and return extracted text + metadata.
    """
    try:
        if not file:
            raise HTTPException(status_code=400, detail="No file was provided.")

        filename_original = file.filename or ""
        content_type = (file.content_type or "").lower()

        if content_type != "application/pdf" and not filename_original.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail="Only PDF files are supported.")

        _cleanup_old_uploads()
        _ensure_uploads_dir()

        pdf_bytes = await file.read()
        if not pdf_bytes:
            raise HTTPException(status_code=400, detail="The uploaded file is empty.")
        if len(pdf_bytes) > MAX_FILE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File is too large for this demo (max {MAX_FILE_BYTES // (1024 * 1024)} MB).",
            )

        document_id = str(uuid.uuid4())
        safe_name = _safe_filename(filename_original)
        stored_filename = f"{document_id}_{safe_name}"
        stored_path = UPLOADS_DIR / stored_filename

        try:
            stored_path.write_bytes(pdf_bytes)
        except OSError:
            raise HTTPException(status_code=500, detail="Failed to save the uploaded file on the server.")

        # Page count (best-effort)
        try:
            reader = PdfReader(str(stored_path))
            page_count = len(reader.pages)
        except Exception:
            page_count = None

        # Textract OCR
        try:
            extracted_text = _textract_extract_text(pdf_bytes, s3_key_hint=stored_filename)
        except RuntimeError as e:
            raise HTTPException(
                status_code=502,
                detail=(
                    "AWS Textract failed to process this PDF. "
                    f"Details: {str(e)}"
                ),
            )

        detected_language = _detect_language(extracted_text)

        # Persist extracted text for later governance reporting.
        try:
            _ensure_documents_schema()
            with _get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO documents (document_id, filename, extracted_text, detected_language)
                    VALUES (?, ?, ?, ?)
                    """,
                    (document_id, stored_filename, extracted_text, detected_language),
                )
                conn.commit()
        except sqlite3.Error:
            # Best-effort; don't block the main upload flow.
            pass

        return {
            "document_id": document_id,
            "filename": stored_filename,
            "page_count": page_count,
            "detected_language": detected_language,
            "extracted_text": extracted_text,
        }
    except Exception:
        traceback.print_exc()
        raise

