"""
main.py – FastAPI backend for the Vanguard System Monitor.

Endpoints
---------
POST /api/ingest          – Receive a collector payload and store in DB
GET  /api/events          – Query stored events (filter by type, date, pagination)
GET  /api/dates           – List of dates that have events
GET  /api/counts          – Event counts per type (optionally filtered by date)
POST /api/trigger         – Trigger the PowerShell collector on-demand
GET  /                    – Serve the frontend dashboard (index.html)
"""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.database import (
    get_available_dates,
    get_event_counts_by_type,
    init_db,
    insert_events,
    query_events,
)
from backend.models import (
    CountsResponse,
    DatesResponse,
    EventListResponse,
    IngestPayload,
    TriggerResponse,
)

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

REPO_ROOT    = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"
COLLECTOR    = REPO_ROOT / "collector" / "collect.ps1"
CONFIG_PATH  = REPO_ROOT / "config.json"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Vanguard System Monitor",
    description="Purpose-built dashboard for monitoring Riot Vanguard activity on Windows.",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Static files / frontend
# ---------------------------------------------------------------------------

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def serve_frontend():
    index = FRONTEND_DIR / "index.html"
    if not index.exists():
        return JSONResponse({"error": "Frontend not found"}, status_code=404)
    return FileResponse(str(index))


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.post("/api/ingest", summary="Ingest events from PowerShell collector")
async def ingest(payload: IngestPayload):
    events = [ev.to_flat_dict() for ev in payload.events]
    count = await insert_events(events)
    return {"inserted": count, "received": len(events)}


@app.get("/api/events", response_model=EventListResponse, summary="Query stored events")
async def get_events(
    event_type: str | None = Query(default=None, description="Filter by type: driver, network, error, crash, shell_ext"),
    date: str | None       = Query(default=None, description="Filter by date YYYY-MM-DD"),
    limit: int             = Query(default=100, ge=1, le=1000),
    offset: int            = Query(default=0, ge=0),
):
    rows = await query_events(event_type=event_type, date=date, limit=limit, offset=offset)
    # Get total count for this filter combination (re-query without limit)
    all_rows = await query_events(event_type=event_type, date=date, limit=100_000, offset=0)
    return EventListResponse(
        events=rows,
        total=len(all_rows),
        limit=limit,
        offset=offset,
    )


@app.get("/api/dates", response_model=DatesResponse, summary="List dates with events")
async def get_dates():
    dates = await get_available_dates()
    return DatesResponse(dates=dates)


@app.get("/api/counts", response_model=CountsResponse, summary="Event counts per type")
async def get_counts(
    date: str | None = Query(default=None, description="Limit to YYYY-MM-DD"),
):
    counts = await get_event_counts_by_type(date=date)
    return CountsResponse(counts=counts, date=date)


@app.post("/api/trigger", response_model=TriggerResponse, summary="Trigger collector on-demand")
async def trigger_collector():
    """
    Run collect.ps1 immediately and return its output.
    The collector will POST results back to this API via /api/ingest,
    or the results can be read from the output JSON file.
    """
    if not COLLECTOR.exists():
        raise HTTPException(status_code=404, detail=f"Collector script not found: {COLLECTOR}")

    # Determine the PowerShell executable
    ps_exe = "pwsh" if sys.platform != "win32" else "powershell"

    # Build the API base URL from config
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        host = cfg.get("server", {}).get("host", "127.0.0.1")
        port = cfg.get("server", {}).get("port", 8000)
        api_base = f"http://{host}:{port}"
    except Exception:
        api_base = "http://127.0.0.1:8000"

    cmd = [
        ps_exe, "-NonInteractive", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", str(COLLECTOR),
        "-ConfigPath", str(CONFIG_PATH),
        "-ApiBaseUrl", api_base,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
        success = result.returncode == 0
        return TriggerResponse(
            status="success" if success else "error",
            message=f"Collector exited with code {result.returncode}",
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail=f"PowerShell executable '{ps_exe}' not found. Ensure PowerShell is installed.",
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Collector timed out after 120 seconds.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
