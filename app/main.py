from __future__ import annotations

import csv
import io
import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .models import ScrapeRequest, ScrapeResult
from .scraper import run_scraper
from .storage import finish_job, get_job, init_db, list_jobs, save_job

BASE_DIR = Path(__file__).resolve().parent


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="Scrapify Agent MVP",
    version="1.0.0",
    description="Agente local para extraer datos públicos de páginas web.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/scrape", response_model=ScrapeResult)
async def scrape(request: ScrapeRequest) -> ScrapeResult:
    job_id = uuid.uuid4().hex[:12]
    created_at = utc_now()
    save_job(job_id, "running", str(request.url), request.model_dump(mode="json"), created_at)

    try:
        data, pages_visited, errors = await run_scraper(request)
        finished_at = utc_now()
        result = ScrapeResult(
            job_id=job_id,
            status="completed",
            source_url=str(request.url),
            pages_visited=pages_visited,
            item_count=len(data),
            data=data,
            errors=errors,
            created_at=created_at,
            finished_at=finished_at,
        )
        finish_job(job_id, "completed", result.model_dump(mode="json"), finished_at)
        return result
    except PermissionError as exc:
        finished_at = utc_now()
        result = ScrapeResult(
            job_id=job_id,
            status="failed",
            source_url=str(request.url),
            errors=[str(exc)],
            created_at=created_at,
            finished_at=finished_at,
        )
        finish_job(job_id, "failed", result.model_dump(mode="json"), finished_at)
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        finished_at = utc_now()
        result = ScrapeResult(
            job_id=job_id,
            status="failed",
            source_url=str(request.url),
            errors=[str(exc)],
            created_at=created_at,
            finished_at=finished_at,
        )
        finish_job(job_id, "failed", result.model_dump(mode="json"), finished_at)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        finished_at = utc_now()
        message = f"{type(exc).__name__}: {exc}"
        result = ScrapeResult(
            job_id=job_id,
            status="failed",
            source_url=str(request.url),
            errors=[message],
            created_at=created_at,
            finished_at=finished_at,
        )
        finish_job(job_id, "failed", result.model_dump(mode="json"), finished_at)
        raise HTTPException(status_code=500, detail=message) from exc


@app.get("/api/jobs")
async def jobs() -> list[dict[str, Any]]:
    return list_jobs()


@app.get("/api/jobs/{job_id}")
async def job(job_id: str) -> JSONResponse:
    stored = get_job(job_id)
    if not stored:
        raise HTTPException(status_code=404, detail="Trabajo no encontrado")
    return JSONResponse(stored)


def flatten_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


@app.get("/api/jobs/{job_id}/export.csv")
async def export_csv(job_id: str) -> StreamingResponse:
    stored = get_job(job_id)
    if not stored or not stored.get("result"):
        raise HTTPException(status_code=404, detail="Resultado no encontrado")
    rows = stored["result"].get("data", [])
    if not rows:
        raise HTTPException(status_code=404, detail="No hay filas para exportar")

    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: flatten_value(row.get(key)) for key in fieldnames})

    content = "\ufeff" + buffer.getvalue()
    headers = {"Content-Disposition": f'attachment; filename="{job_id}.csv"'}
    return StreamingResponse(iter([content]), media_type="text/csv; charset=utf-8", headers=headers)
