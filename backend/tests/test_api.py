"""
test_api.py – Unit/integration tests for the Vanguard System Monitor backend.

Uses an in-memory/temp SQLite database so tests are fully self-contained.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Point to a temp DB before importing the app.
# Use mkstemp (not the insecure mktemp) to create the path safely.
_fd, _TMP_DB = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ.setdefault("TEST_DB_PATH", _TMP_DB)

# We monkey-patch get_db_path so all DB calls in tests use the temp file.
import backend.database as db_module

_ORIGINAL_GET_DB_PATH = db_module.get_db_path


async def _test_db_path() -> Path:
    p = Path(_TMP_DB)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


db_module.get_db_path = _test_db_path  # type: ignore[assignment]

from backend.main import app  # noqa: E402 – import after patch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(autouse=True)
async def _init_test_db():
    """Initialise (and wipe) the temp DB before each test."""
    import aiosqlite

    # Drop and recreate tables
    async with aiosqlite.connect(_TMP_DB) as conn:
        await conn.execute("DROP TABLE IF EXISTS events")
        await conn.commit()

    await db_module.init_db(db_path=Path(_TMP_DB))
    yield


@pytest_asyncio.fixture(autouse=True)
async def _clear_api_key_env():
    """Ensure no leftover API key env var bleeds between tests."""
    os.environ.pop("VANGUARD_API_KEY", None)
    yield
    os.environ.pop("VANGUARD_API_KEY", None)


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def authed_client() -> AsyncGenerator[AsyncClient, None]:
    """Client pre-configured with the test API key header."""
    os.environ["VANGUARD_API_KEY"] = "test-secret"
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"x-api-key": "test-secret"},
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

SAMPLE_EVENT = {
    "event_type": "driver",
    "timestamp": "2025-05-01T12:00:00+00:00",
    "source": "Service Control Manager",
    "event_id": 7036,
    "level": "Information",
    "message": "The vgk service entered the running state.",
}

SAMPLE_PAYLOAD = {
    "collected_at": "2025-05-01T12:00:00+00:00",
    "event_count": 1,
    "events": [SAMPLE_EVENT],
}


# ---------------------------------------------------------------------------
# Tests – /api/ingest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ingest_single_event(client: AsyncClient):
    resp = await client.post("/api/ingest", json=SAMPLE_PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert body["inserted"] == 1
    assert body["received"] == 1


@pytest.mark.asyncio
async def test_ingest_empty_payload(client: AsyncClient):
    resp = await client.post("/api/ingest", json={"events": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["inserted"] == 0
    assert body["received"] == 0


@pytest.mark.asyncio
async def test_ingest_multiple_types(client: AsyncClient):
    events = [
        {**SAMPLE_EVENT, "event_type": "driver"},
        {**SAMPLE_EVENT, "event_type": "network", "remote_ip": "1.2.3.4", "remote_port": "443"},
        {**SAMPLE_EVENT, "event_type": "crash", "process": "explorer.exe"},
        {**SAMPLE_EVENT, "event_type": "shell_ext", "status": "active"},
        {**SAMPLE_EVENT, "event_type": "error", "level": "Critical"},
    ]
    resp = await client.post("/api/ingest", json={"events": events})
    assert resp.status_code == 200
    assert resp.json()["inserted"] == 5


# ---------------------------------------------------------------------------
# Tests – /api/events
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_events_empty(client: AsyncClient):
    resp = await client.get("/api/events")
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_get_events_after_ingest(client: AsyncClient):
    await client.post("/api/ingest", json=SAMPLE_PAYLOAD)
    resp = await client.get("/api/events")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    ev = body["events"][0]
    assert ev["event_type"] == "driver"
    assert ev["event_id"] == 7036


@pytest.mark.asyncio
async def test_get_events_filter_by_type(client: AsyncClient):
    events = [
        {**SAMPLE_EVENT, "event_type": "driver"},
        {**SAMPLE_EVENT, "event_type": "network"},
    ]
    await client.post("/api/ingest", json={"events": events})

    resp_driver = await client.get("/api/events?event_type=driver")
    assert resp_driver.status_code == 200
    body = resp_driver.json()
    assert body["total"] == 1
    assert body["events"][0]["event_type"] == "driver"

    resp_network = await client.get("/api/events?event_type=network")
    assert resp_network.status_code == 200
    assert resp_network.json()["total"] == 1


@pytest.mark.asyncio
async def test_get_events_filter_by_date(client: AsyncClient):
    events = [
        {**SAMPLE_EVENT, "timestamp": "2025-05-01T10:00:00+00:00"},
        {**SAMPLE_EVENT, "timestamp": "2025-05-02T10:00:00+00:00"},
    ]
    await client.post("/api/ingest", json={"events": events})

    resp = await client.get("/api/events?date=2025-05-01")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1

    resp2 = await client.get("/api/events?date=2025-05-02")
    assert resp2.status_code == 200
    assert resp2.json()["total"] == 1


@pytest.mark.asyncio
async def test_get_events_pagination(client: AsyncClient):
    events = [{**SAMPLE_EVENT} for _ in range(5)]
    await client.post("/api/ingest", json={"events": events})

    resp = await client.get("/api/events?limit=2&offset=0")
    body = resp.json()
    assert len(body["events"]) == 2
    assert body["total"] == 5

    resp2 = await client.get("/api/events?limit=2&offset=4")
    body2 = resp2.json()
    assert len(body2["events"]) == 1


@pytest.mark.asyncio
async def test_event_extra_fields_stored(client: AsyncClient):
    """Extra fields (e.g. remote_ip for network events) should be in 'extra'."""
    event = {
        **SAMPLE_EVENT,
        "event_type": "network",
        "remote_ip": "8.8.8.8",
        "remote_port": "443",
    }
    await client.post("/api/ingest", json={"events": [event]})
    resp = await client.get("/api/events?event_type=network")
    ev = resp.json()["events"][0]
    assert ev["extra"].get("remote_ip") == "8.8.8.8"
    assert ev["extra"].get("remote_port") == "443"


# ---------------------------------------------------------------------------
# Tests – /api/dates
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_dates_empty(client: AsyncClient):
    resp = await client.get("/api/dates")
    assert resp.status_code == 200
    assert resp.json()["dates"] == []


@pytest.mark.asyncio
async def test_get_dates_after_ingest(client: AsyncClient):
    events = [
        {**SAMPLE_EVENT, "timestamp": "2025-05-01T10:00:00+00:00"},
        {**SAMPLE_EVENT, "timestamp": "2025-05-02T10:00:00+00:00"},
        {**SAMPLE_EVENT, "timestamp": "2025-05-02T11:00:00+00:00"},  # same date, different time
    ]
    await client.post("/api/ingest", json={"events": events})
    resp = await client.get("/api/dates")
    dates = resp.json()["dates"]
    assert len(dates) == 2
    assert "2025-05-01" in dates
    assert "2025-05-02" in dates


# ---------------------------------------------------------------------------
# Tests – /api/counts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_counts_empty(client: AsyncClient):
    resp = await client.get("/api/counts")
    assert resp.status_code == 200
    assert resp.json()["counts"] == {}


@pytest.mark.asyncio
async def test_get_counts_after_ingest(client: AsyncClient):
    events = [
        {**SAMPLE_EVENT, "event_type": "driver"},
        {**SAMPLE_EVENT, "event_type": "driver"},
        {**SAMPLE_EVENT, "event_type": "network"},
    ]
    await client.post("/api/ingest", json={"events": events})
    resp = await client.get("/api/counts")
    counts = resp.json()["counts"]
    assert counts["driver"] == 2
    assert counts["network"] == 1


@pytest.mark.asyncio
async def test_get_counts_filtered_by_date(client: AsyncClient):
    events = [
        {**SAMPLE_EVENT, "event_type": "driver", "timestamp": "2025-05-01T10:00:00+00:00"},
        {**SAMPLE_EVENT, "event_type": "network", "timestamp": "2025-05-02T10:00:00+00:00"},
    ]
    await client.post("/api/ingest", json={"events": events})

    resp = await client.get("/api/counts?date=2025-05-01")
    counts = resp.json()["counts"]
    assert "driver" in counts
    assert "network" not in counts


# ---------------------------------------------------------------------------
# Tests – /api/trigger auth
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_trigger_no_auth_when_key_not_configured(client: AsyncClient):
    """When api_key is empty, /api/trigger should not require auth."""
    # Ensure no key is set
    os.environ.pop("VANGUARD_API_KEY", None)
    # The endpoint will fail with 404/500 because PowerShell isn't available,
    # but it must NOT return 403.
    resp = await client.post("/api/trigger")
    assert resp.status_code != 403


@pytest.mark.asyncio
async def test_trigger_missing_api_key_returns_403(client: AsyncClient):
    """Missing x-api-key header returns 403 when a key is configured."""
    os.environ["VANGUARD_API_KEY"] = "secret123"
    resp = await client.post("/api/trigger")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_trigger_wrong_api_key_returns_403(client: AsyncClient):
    """Wrong x-api-key header returns 403."""
    os.environ["VANGUARD_API_KEY"] = "secret123"
    resp = await client.post("/api/trigger", headers={"x-api-key": "wrong"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_trigger_correct_api_key_passes_auth(client: AsyncClient):
    """Correct x-api-key header passes auth (may still fail for other reasons)."""
    os.environ["VANGUARD_API_KEY"] = "secret123"
    resp = await client.post("/api/trigger", headers={"x-api-key": "secret123"})
    # 403 must not be returned; 404/500/504 is acceptable in a test environment
    assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Tests – /api/summary/latest
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summary_no_auth_when_key_not_configured(client: AsyncClient):
    """When api_key is empty, /api/summary/latest should not require auth."""
    os.environ.pop("VANGUARD_API_KEY", None)
    resp = await client.get("/api/summary/latest")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_summary_missing_api_key_returns_403(client: AsyncClient):
    """Verify a 403 response is returned when the API key is required but the header is absent."""
    os.environ["VANGUARD_API_KEY"] = "secret123"
    resp = await client.get("/api/summary/latest")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_summary_wrong_api_key_returns_403(client: AsyncClient):
    """Verify a 403 response is returned when the API key is required but an incorrect value is provided."""
    os.environ["VANGUARD_API_KEY"] = "secret123"
    resp = await client.get("/api/summary/latest", headers={"x-api-key": "wrong"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_summary_empty_db(authed_client: AsyncClient):
    resp = await authed_client.get("/api/summary/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["network_count"] == 0
    assert body["new_ip_count"] == 0
    assert body["crash_count"] == 0
    assert "plain_text" in body
    assert "as_of" in body


@pytest.mark.asyncio
async def test_summary_plain_text_format(authed_client: AsyncClient):
    """plain_text must contain key phrase and counts."""
    # Insert a network event with timestamp = now (last 24h)
    import datetime
    now_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    events = [
        {
            "event_type": "network",
            "timestamp": now_ts,
            "remote_ip": "5.5.5.5",
            "remote_port": "443",
            "source": "test",
        },
        {
            "event_type": "crash",
            "timestamp": now_ts,
            "source": "test",
            "message": "explorer.exe crash",
        },
    ]
    await authed_client.post("/api/ingest", json={"events": events})

    resp = await authed_client.get("/api/summary/latest")
    assert resp.status_code == 200
    body = resp.json()
    assert body["network_count"] == 1
    assert body["crash_count"] == 1
    text = body["plain_text"]
    assert text.startswith("Vanguard report:")
    assert "1 network connection" in text
    assert "1 crash detected" in text


# ---------------------------------------------------------------------------
# Tests – database helpers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_events_returns_count():
    n = await db_module.insert_events([SAMPLE_EVENT], db_path=Path(_TMP_DB))
    assert n == 1


@pytest.mark.asyncio
async def test_insert_events_empty_list():
    n = await db_module.insert_events([], db_path=Path(_TMP_DB))
    assert n == 0


@pytest.mark.asyncio
async def test_query_events_returns_dict():
    await db_module.insert_events([SAMPLE_EVENT], db_path=Path(_TMP_DB))
    rows = await db_module.query_events(db_path=Path(_TMP_DB))
    assert len(rows) == 1
    assert isinstance(rows[0], dict)
    assert rows[0]["event_type"] == "driver"


@pytest.mark.asyncio
async def test_get_24h_summary_empty():
    summary = await db_module.get_24h_summary(db_path=Path(_TMP_DB))
    assert summary["network_count"] == 0
    assert summary["unique_ips"] == []
    assert summary["crash_count"] == 0


@pytest.mark.asyncio
async def test_get_24h_summary_counts():
    import datetime
    now_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    events = [
        {**SAMPLE_EVENT, "event_type": "network", "timestamp": now_ts, "remote_ip": "1.1.1.1"},
        {**SAMPLE_EVENT, "event_type": "network", "timestamp": now_ts, "remote_ip": "2.2.2.2"},
        {**SAMPLE_EVENT, "event_type": "crash", "timestamp": now_ts},
    ]
    await db_module.insert_events(events, db_path=Path(_TMP_DB))
    summary = await db_module.get_24h_summary(db_path=Path(_TMP_DB))
    assert summary["network_count"] == 2
    assert summary["crash_count"] == 1
    assert set(summary["unique_ips"]) == {"1.1.1.1", "2.2.2.2"}


@pytest.mark.asyncio
async def test_get_baseline_ips_excludes_today():
    """IPs from today should not appear in the baseline."""
    import datetime
    now_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    old_ts = "2020-01-01T00:00:00+00:00"
    events = [
        {**SAMPLE_EVENT, "event_type": "network", "timestamp": now_ts, "remote_ip": "today.ip"},
        {**SAMPLE_EVENT, "event_type": "network", "timestamp": old_ts, "remote_ip": "old.ip"},
    ]
    await db_module.insert_events(events, db_path=Path(_TMP_DB))
    baseline = await db_module.get_baseline_ips(days_back=3650, db_path=Path(_TMP_DB))
    assert "old.ip" in baseline
    assert "today.ip" not in baseline


# ---------------------------------------------------------------------------
# Tests – anomaly detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detect_anomalies_no_anomaly():
    from backend.anomaly import detect_anomalies
    cfg = {"anomaly": {"new_ip_threshold": 1, "connection_count_threshold": 50, "alert_on_crash": True, "baseline_days_back": 7}}
    result = await detect_anomalies(cfg, db_path=Path(_TMP_DB))
    assert not result.is_anomaly
    assert result.new_ips == []
    assert result.crash_count == 0


@pytest.mark.asyncio
async def test_detect_anomalies_new_ip_flagged():
    from backend.anomaly import detect_anomalies
    import datetime
    now_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    events = [
        {**SAMPLE_EVENT, "event_type": "network", "timestamp": now_ts, "remote_ip": "9.9.9.9"},
    ]
    await db_module.insert_events(events, db_path=Path(_TMP_DB))
    cfg = {"anomaly": {"new_ip_threshold": 1, "connection_count_threshold": 50, "alert_on_crash": True, "baseline_days_back": 7}}
    result = await detect_anomalies(cfg, db_path=Path(_TMP_DB))
    assert result.is_anomaly
    assert "9.9.9.9" in result.new_ips


@pytest.mark.asyncio
async def test_detect_anomalies_crash_flagged():
    from backend.anomaly import detect_anomalies
    import datetime
    now_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    events = [{**SAMPLE_EVENT, "event_type": "crash", "timestamp": now_ts}]
    await db_module.insert_events(events, db_path=Path(_TMP_DB))
    cfg = {"anomaly": {"new_ip_threshold": 1, "connection_count_threshold": 50, "alert_on_crash": True, "baseline_days_back": 7}}
    result = await detect_anomalies(cfg, db_path=Path(_TMP_DB))
    assert result.is_anomaly
    assert result.crash_count == 1


@pytest.mark.asyncio
async def test_detect_anomalies_known_ip_not_flagged():
    """An IP that appears in baseline should NOT trigger a new-IP anomaly."""
    from backend.anomaly import detect_anomalies
    import datetime
    now_ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    old_ts = "2020-01-01T00:00:00+00:00"
    events = [
        {**SAMPLE_EVENT, "event_type": "network", "timestamp": old_ts, "remote_ip": "known.ip"},
        {**SAMPLE_EVENT, "event_type": "network", "timestamp": now_ts, "remote_ip": "known.ip"},
    ]
    await db_module.insert_events(events, db_path=Path(_TMP_DB))
    cfg = {"anomaly": {"new_ip_threshold": 1, "connection_count_threshold": 50, "alert_on_crash": False, "baseline_days_back": 3650}}
    result = await detect_anomalies(cfg, db_path=Path(_TMP_DB))
    assert not result.is_anomaly
    assert result.new_ips == []
