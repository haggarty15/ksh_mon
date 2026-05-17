"""
main.py – FastAPI backend for the Vanguard System Monitor.

Endpoints
---------
POST /api/ingest          – Receive a collector payload and store in DB
GET  /api/events          – Query stored events (filter by type, date, pagination)
GET  /api/dates           – List of dates that have events
GET  /api/counts          – Event counts per type (optionally filtered by date)
POST /api/trigger         – Trigger the PowerShell collector on-demand (auth required)
GET  /api/trigger/pending – Check and consume the pending-trigger flag (auth required)
GET  /api/summary/latest  – Last 24 h digest as plain text for TTS (auth required)
GET  /                    – Serve the frontend dashboard (index.html)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.anomaly import AnomalyResult, detect_anomalies
from backend.database import (
    get_24h_summary,
    get_available_dates,
    get_baseline_ips,
    get_event_counts_by_type,
    get_and_clear_trigger_pending,
    init_db,
    insert_events,
    query_events,
    set_trigger_pending,
)
from backend.models import (
    CountsResponse,
    DatesResponse,
    EventListResponse,
    IngestPayload,
    SummaryResponse,
    TriggerPendingResponse,
    TriggerResponse,
)
from backend.tts import speak_on_google_home

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

REPO_ROOT    = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "frontend"
COLLECTOR    = REPO_ROOT / "collector" / "collect.ps1"
CONFIG_PATH  = REPO_ROOT / "config.json"


def load_config() -> dict[str, Any]:
    """Load and return config.json as a dict.  Returns ``{}`` on any error."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


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
# Auth dependency
# ---------------------------------------------------------------------------

def _get_configured_api_key() -> str:
    """
    Return the API key that inbound requests must present.

    Priority:
    1. ``VANGUARD_API_KEY`` environment variable (useful for testing / containers).
    2. ``api_key`` field in ``config.json``.

    An empty string means authentication is **disabled** (local-only mode).
    """
    env_key = os.environ.get("VANGUARD_API_KEY", "")
    if env_key:
        return env_key
    return load_config().get("api_key", "")


async def verify_api_key(x_api_key: str | None = Header(default=None)) -> None:
    """
    FastAPI dependency that validates the ``x-api-key`` header.

    If no API key is configured (empty string), authentication is skipped so
    that the server remains usable in local-only mode without any setup.
    """
    configured_key = _get_configured_api_key()
    if not configured_key:
        return  # auth disabled — local-only mode
    if x_api_key != configured_key:
        raise HTTPException(status_code=403, detail="Invalid or missing API key.")


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.post("/api/ingest", summary="Ingest events from PowerShell collector",
          dependencies=[Depends(verify_api_key)])
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


@app.post(
    "/api/trigger",
    response_model=TriggerResponse,
    summary="Trigger collector on-demand",
    dependencies=[Depends(verify_api_key)],
)
async def trigger_collector():
    """
    Run collect.ps1 immediately and return its output.

    Requires a valid ``x-api-key`` header when an API key is configured.
    After a successful collection, anomaly detection runs automatically; if
    an anomaly is found and a Google Home device IP is configured, the summary
    is spoken aloud via TTS.

    **Cloud mode** (``CLOUD_MODE=1``): the server cannot run PowerShell locally,
    so this endpoint queues a trigger instead.  The Windows collector must be
    running with ``-PollMode`` so it picks up the request within ~30 seconds.
    """
    # Cloud mode: queue the trigger for the Windows collector to pick up
    if os.environ.get("CLOUD_MODE", "").lower() in ("1", "true", "yes"):
        await set_trigger_pending()
        return TriggerResponse(
            status="queued",
            message=(
                "Trigger queued. Your Windows collector will pick this up on its "
                "next poll cycle (within ~30 s). Make sure collect.ps1 is running "
                "with -PollMode -ApiBaseUrl <url> -ApiKey <key> on your Windows machine."
            ),
        )

    if not COLLECTOR.exists():
        raise HTTPException(status_code=404, detail=f"Collector script not found: {COLLECTOR}")

    # Determine the PowerShell executable
    ps_exe = "pwsh" if sys.platform != "win32" else "powershell"

    # Build the API base URL from config
    cfg = load_config()
    host = cfg.get("server", {}).get("host", "127.0.0.1")
    port = cfg.get("server", {}).get("port", 8000)
    api_base = f"http://{host}:{port}"

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
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail=f"PowerShell executable '{ps_exe}' not found. Ensure PowerShell is installed.",
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Collector timed out after 120 seconds.")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Run anomaly detection after a successful collection
    anomaly_detected = False
    anomaly_summary = ""
    tts_spoken = False
    tts_message = ""
    if success:
        try:
            anomaly = await detect_anomalies(cfg)
            anomaly_detected = anomaly.is_anomaly
            anomaly_summary = anomaly.message

            gh_cfg = cfg.get("google_home", {})
            device_ip = gh_cfg.get("device_ip", "")
            language = gh_cfg.get("tts_language", "en")
            announce_summary_on_trigger = gh_cfg.get(
                "announce_summary_on_trigger",
                True,
            )

            if anomaly.is_anomaly:
                tts_message = _build_spoken_summary(anomaly)
            elif announce_summary_on_trigger:
                latest = await _build_latest_summary_response(cfg)
                tts_message = f"Vanguard run complete. {latest.plain_text}"

            if tts_message:
                tts_spoken = speak_on_google_home(
                    tts_message,
                    device_ip=device_ip,
                    language=language,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Anomaly detection failed: %s", exc)

    return TriggerResponse(
        status="success" if success else "error",
        message=f"Collector exited with code {result.returncode}",
        stdout=result.stdout,
        stderr=result.stderr,
        anomaly_detected=anomaly_detected,
        anomaly_summary=anomaly_summary,
        tts_spoken=tts_spoken,
        tts_message=tts_message,
    )


@app.get(
    "/api/trigger/pending",
    response_model=TriggerPendingResponse,
    summary="Check and consume the pending-trigger flag",
    dependencies=[Depends(verify_api_key)],
)
async def get_trigger_pending():
    """
    Return whether a remote trigger is pending and immediately clear the flag
    (consume-on-read semantics).

    Called by the Windows collector running in ``-PollMode``.  When this
    returns ``{"pending": true}`` the collector should run a full collection
    and POST the results to ``/api/ingest``.

    Requires a valid ``x-api-key`` header when an API key is configured.
    """
    pending = await get_and_clear_trigger_pending()
    return TriggerPendingResponse(pending=pending)


@app.get(
    "/api/summary/latest",
    response_model=SummaryResponse,
    summary="Last 24 h digest (plain text for TTS)",
    dependencies=[Depends(verify_api_key)],
)
async def get_latest_summary():
    """
    Return a 24-hour digest of collected activity as both structured data and
    a human-readable plain-text string suitable for text-to-speech.

    Requires a valid ``x-api-key`` header when an API key is configured.
    """
    cfg = load_config()
    return await _build_latest_summary_response(cfg)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _build_spoken_summary(anomaly: AnomalyResult) -> str:
    """Format an AnomalyResult into a short spoken sentence."""
    parts: list[str] = []
    if anomaly.new_ips:
        n = len(anomaly.new_ips)
        parts.append(f"{n} new IP{'s' if n != 1 else ''} not seen before")
    if anomaly.connection_count:
        parts.append(f"{anomaly.connection_count} network connections")
    if anomaly.crash_count:
        parts.append(f"{anomaly.crash_count} crash{'es' if anomaly.crash_count != 1 else ''} detected")

    detail = ", ".join(parts) if parts else anomaly.message
    return f"Vanguard anomaly alert: {detail}"


async def _build_latest_summary_response(cfg: dict[str, Any]) -> SummaryResponse:
    """Build the latest 24-hour summary payload used by API and trigger TTS."""
    baseline_days: int = int(cfg.get("anomaly", {}).get("baseline_days_back", 7))

    summary = await get_24h_summary()
    baseline_ips = await get_baseline_ips(days_back=baseline_days)

    today_ips: set[str] = set(summary["unique_ips"])
    new_ip_count = len(today_ips - baseline_ips)
    crash_count = summary["crash_count"]
    network_count = summary["network_count"]

    crash_text = (
        f"{crash_count} crash{'es' if crash_count != 1 else ''} detected"
        if crash_count > 0
        else "no crashes detected"
    )
    new_ip_text = (
        f"{new_ip_count} new IP{'s' if new_ip_count != 1 else ''} not seen before"
        if new_ip_count > 0
        else "no new IPs"
    )
    plain_text = (
        f"Vanguard report: {network_count} network "
        f"connection{'s' if network_count != 1 else ''}, "
        f"{new_ip_text}, {crash_text}"
    )
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return SummaryResponse(
        plain_text=plain_text,
        network_count=network_count,
        new_ip_count=new_ip_count,
        crash_count=crash_count,
        as_of=as_of,
    )
