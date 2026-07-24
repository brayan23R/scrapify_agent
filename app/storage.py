from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "jobs.db"


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                source_url TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT
            )
            """
        )
        conn.commit()


def save_job(job_id: str, status: str, source_url: str, request: dict[str, Any], created_at: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO jobs (id, status, source_url, request_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, status, source_url, json.dumps(request, ensure_ascii=False), created_at),
        )
        conn.commit()


def finish_job(job_id: str, status: str, result: dict[str, Any], finished_at: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE jobs SET status = ?, result_json = ?, finished_at = ? WHERE id = ?",
            (status, json.dumps(result, ensure_ascii=False), finished_at, job_id),
        )
        conn.commit()


def get_job(job_id: str) -> dict[str, Any] | None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["request"] = json.loads(result.pop("request_json"))
    result["result"] = json.loads(result.pop("result_json")) if result.get("result_json") else None
    return result


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, status, source_url, created_at, finished_at FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]
