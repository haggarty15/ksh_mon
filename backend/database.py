"""
database.py – SQLite helpers for the Vanguard System Monitor backend.

All DB access is async via aiosqlite.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT    NOT NULL,          -- driver | network | error | crash | shell_ext
    timestamp   TEXT    NOT NULL,          -- ISO-8601
    source      TEXT,
    event_id    INTEGER,
    level       TEXT,
    message     TEXT,
    extra       TEXT    DEFAULT '{}'       -- JSON blob for type-specific fields
);

CREATE INDEX IF NOT EXISTS idx_events_type      ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp);
"""


async def get_db_path() -> Path:
    """Return the DB path, resolved in priority order:

    1. ``VANGUARD_DB_PATH`` environment variable (useful for containers / cloud).
    2. ``db_path`` field in ``config.json``, resolved relative to the repo root.
    3. Fallback: ``data/vanguard_monitor.db`` next to the repo root.
    """
    env_path = os.environ.get("VANGUARD_DB_PATH", "")
    if env_path:
        db_path = Path(env_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        return db_path

    repo_root = Path(__file__).resolve().parent.parent
    config_path = repo_root / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
        raw = cfg.get("db_path", "data/vanguard_monitor.db")
    else:
        raw = "data/vanguard_monitor.db"

    db_path = Path(raw) if Path(raw).is_absolute() else repo_root / raw
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


async def init_db(db_path: Path | None = None) -> None:
    """Create tables if they don't exist yet."""
    path = db_path or await get_db_path()
    async with aiosqlite.connect(path) as db:
        await db.executescript(_DDL)
        await db.commit()


async def insert_events(events: list[dict[str, Any]], db_path: Path | None = None) -> int:
    """
    Insert a list of event dicts into the DB.
    Returns the number of rows inserted.
    """
    if not events:
        return 0

    # Known scalar columns
    scalar_cols = {"event_type", "timestamp", "source", "event_id", "level", "message"}
    path = db_path or await get_db_path()

    rows: list[tuple] = []
    for ev in events:
        extra = {k: v for k, v in ev.items() if k not in scalar_cols}
        rows.append((
            ev.get("event_type", "unknown"),
            ev.get("timestamp", ""),
            ev.get("source"),
            ev.get("event_id"),
            ev.get("level"),
            ev.get("message"),
            json.dumps(extra),
        ))

    async with aiosqlite.connect(path) as db:
        await db.executemany(
            """
            INSERT INTO events (event_type, timestamp, source, event_id, level, message, extra)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await db.commit()

    return len(rows)


async def query_events(
    event_type: str | None = None,
    date: str | None = None,
    limit: int = 500,
    offset: int = 0,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """
    Query events with optional filters.

    :param event_type: Filter by event_type column.
    :param date:       ISO date string (YYYY-MM-DD) to filter by day.
    :param limit:      Max rows to return.
    :param offset:     Pagination offset.
    """
    path = db_path or await get_db_path()

    conditions: list[str] = []
    params: list[Any] = []

    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)

    if date:
        conditions.append("date(timestamp) = ?")
        params.append(date)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    sql = f"""
        SELECT id, event_type, timestamp, source, event_id, level, message, extra
        FROM events
        {where}
        ORDER BY timestamp DESC
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])

    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()

    result = []
    for row in rows:
        d = dict(row)
        try:
            d["extra"] = json.loads(d.get("extra") or "{}")
        except (json.JSONDecodeError, TypeError):
            d["extra"] = {}
        result.append(d)

    return result


async def get_available_dates(db_path: Path | None = None) -> list[str]:
    """Return sorted list of distinct dates (YYYY-MM-DD) that have events."""
    path = db_path or await get_db_path()
    async with aiosqlite.connect(path) as db:
        async with db.execute(
            "SELECT DISTINCT date(timestamp) AS d FROM events ORDER BY d DESC"
        ) as cursor:
            rows = await cursor.fetchall()
    return [row[0] for row in rows if row[0]]


async def get_event_counts_by_type(
    date: str | None = None,
    db_path: Path | None = None,
) -> dict[str, int]:
    """Return a dict of {event_type: count} for the given date (or all time)."""
    path = db_path or await get_db_path()
    params: list[Any] = []
    where = ""
    if date:
        where = "WHERE date(timestamp) = ?"
        params.append(date)

    sql = f"""
        SELECT event_type, COUNT(*) AS cnt
        FROM events
        {where}
        GROUP BY event_type
    """
    async with aiosqlite.connect(path) as db:
        async with db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
    return {row[0]: row[1] for row in rows}


async def get_24h_summary(db_path: Path | None = None) -> dict[str, Any]:
    """
    Return aggregated stats for the last 24 hours.

    Keys: network_count, unique_ips (list[str]), crash_count, error_count, driver_count.
    """
    path = db_path or await get_db_path()

    async with aiosqlite.connect(path) as db:
        # Event-type counts in the last 24 hours
        async with db.execute(
            """
            SELECT event_type, COUNT(*) AS cnt
            FROM events
            WHERE timestamp >= datetime('now', '-1 day')
            GROUP BY event_type
            """
        ) as cursor:
            type_counts = {row[0]: row[1] for row in await cursor.fetchall()}

        # Distinct remote IPs from network events in the last 24 hours
        async with db.execute(
            """
            SELECT DISTINCT json_extract(extra, '$.remote_ip') AS ip
            FROM events
            WHERE event_type = 'network'
              AND timestamp >= datetime('now', '-1 day')
              AND json_extract(extra, '$.remote_ip') IS NOT NULL
            """
        ) as cursor:
            unique_ips = [row[0] for row in await cursor.fetchall() if row[0]]

    return {
        "network_count": type_counts.get("network", 0),
        "unique_ips": unique_ips,
        "crash_count": type_counts.get("crash", 0),
        "error_count": type_counts.get("error", 0),
        "driver_count": type_counts.get("driver", 0),
    }


async def get_baseline_ips(days_back: int = 7, db_path: Path | None = None) -> set[str]:
    """
    Return the set of distinct remote IPs seen in network events from the last
    *days_back* days, excluding today.  Used as the anomaly-detection baseline.
    """
    path = db_path or await get_db_path()

    async with aiosqlite.connect(path) as db:
        async with db.execute(
            """
            SELECT DISTINCT json_extract(extra, '$.remote_ip') AS ip
            FROM events
            WHERE event_type = 'network'
              AND date(timestamp) < date('now')
              AND timestamp >= datetime('now', :offset)
              AND json_extract(extra, '$.remote_ip') IS NOT NULL
            """,
            {"offset": f"-{days_back} days"},
        ) as cursor:
            rows = await cursor.fetchall()

    return {row[0] for row in rows if row[0]}
