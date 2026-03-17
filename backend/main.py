"""
Eligibility Copilot — FastAPI backend.
Serves the API that the frontend will call for health checks and (later) document processing.
"""
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from routers.upload import router as upload_router
from routers.analyze import router as analyze_router
from routers.overrides import router as overrides_router
from routers.governance import router as governance_router

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
UPLOADS_DIR = BASE_DIR / "uploads"
DB_PATH = BASE_DIR / "overrides.db"

if ENV_PATH.exists():
    load_dotenv(ENV_PATH, override=False)

app = FastAPI(title="Eligibility Copilot API")

# Allow the React app (running on port 5173) to call this API from the browser.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _init_database() -> None:
    """Create SQLite database and tables if they do not exist."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
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
    finally:
        conn.close()


@app.on_event("startup")
def startup_event() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    _init_database()
    print("Eligibility Copilot backend is running")

app.include_router(upload_router)
app.include_router(analyze_router)
app.include_router(overrides_router)
app.include_router(governance_router)


@app.get("/")
def root():
    """Root endpoint — returns a welcome message."""
    return {"message": "Eligibility Copilot API"}


@app.get("/health")
def health():
    """Health check — used to verify the API is up and its version."""
    return {"status": "ok", "version": "1.0"}
