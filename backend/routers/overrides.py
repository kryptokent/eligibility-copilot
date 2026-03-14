from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "overrides.db"


class OverrideLogRequest(BaseModel):
    document_id: str = Field(..., description="ID of the processed document")
    program: Literal["SNAP", "Medicaid", "CHIP"]
    original_determination: str = Field(
        ..., description="The AI's original determination for this program"
    )
    override_decision: str = Field(
        ..., description="Caseworker's final decision for this program"
    )
    override_reason: str = Field(..., description="Free-text explanation from caseworker")
    caseworker_id: str = Field(..., description="Identifier for the caseworker")


class OverrideLogResponse(BaseModel):
    id: int
    document_id: str
    program: str
    original_determination: str
    override_decision: str
    override_reason: str
    caseworker_id: str
    created_at: str


def _get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema() -> None:
    with _get_connection() as conn:
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
        conn.commit()


@router.on_event("startup")
def startup_event() -> None:
    _ensure_schema()


@router.post("/api/log-override", response_model=OverrideLogResponse)
async def log_override(payload: OverrideLogRequest) -> OverrideLogResponse:
    _ensure_schema()
    try:
        with _get_connection() as conn:
            cur = conn.execute(
                """
                INSERT INTO overrides (
                    document_id,
                    program,
                    original_determination,
                    override_decision,
                    override_reason,
                    caseworker_id
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.document_id,
                    payload.program,
                    payload.original_determination,
                    payload.override_decision,
                    payload.override_reason,
                    payload.caseworker_id,
                ),
            )
            override_id = cur.lastrowid
            row = conn.execute(
                "SELECT * FROM overrides WHERE id = ?", (override_id,)
            ).fetchone()
    except sqlite3.Error as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to log override: {e}",
        ) from e

    return OverrideLogResponse(
        id=row["id"],
        document_id=row["document_id"],
        program=row["program"],
        original_determination=row["original_determination"],
        override_decision=row["override_decision"],
        override_reason=row["override_reason"],
        caseworker_id=row["caseworker_id"],
        created_at=row["created_at"],
    )


@router.get("/api/overrides", response_model=list[OverrideLogResponse])
async def list_overrides() -> list[OverrideLogResponse]:
    _ensure_schema()
    with _get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM overrides ORDER BY created_at DESC, id DESC"
        ).fetchall()

    return [
        OverrideLogResponse(
            id=row["id"],
            document_id=row["document_id"],
            program=row["program"],
            original_determination=row["original_determination"],
            override_decision=row["override_decision"],
            override_reason=row["override_reason"],
            caseworker_id=row["caseworker_id"],
            created_at=row["created_at"],
        )
        for row in rows
    ]

