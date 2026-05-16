"""
anomaly.py – Anomaly detection for the Vanguard System Monitor.

Compares today's network activity and crash events against a rolling baseline
stored in SQLite and returns a structured result indicating whether anything
unusual was detected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.database import get_24h_summary, get_baseline_ips


@dataclass
class AnomalyResult:
    is_anomaly: bool
    new_ips: list[str] = field(default_factory=list)
    connection_count: int = 0
    crash_count: int = 0
    message: str = ""


async def detect_anomalies(
    cfg: dict[str, Any],
    db_path: Path | None = None,
) -> AnomalyResult:
    """
    Detect anomalies by comparing today's activity against baseline thresholds.

    :param cfg:     Parsed config.json as a dict.
    :param db_path: Optional override for the SQLite database path.
    :returns:       AnomalyResult describing what (if anything) was found.
    """
    anomaly_cfg = cfg.get("anomaly", {})
    new_ip_threshold: int = int(anomaly_cfg.get("new_ip_threshold", 1))
    conn_threshold: int = int(anomaly_cfg.get("connection_count_threshold", 50))
    alert_on_crash: bool = bool(anomaly_cfg.get("alert_on_crash", True))
    baseline_days: int = int(anomaly_cfg.get("baseline_days_back", 7))

    summary = await get_24h_summary(db_path=db_path)
    baseline_ips = await get_baseline_ips(days_back=baseline_days, db_path=db_path)

    today_ips: set[str] = set(summary["unique_ips"])
    new_ips: list[str] = sorted(today_ips - baseline_ips)
    connection_count: int = summary["network_count"]
    crash_count: int = summary["crash_count"]

    reasons: list[str] = []

    if len(new_ips) >= new_ip_threshold:
        reasons.append(
            f"{len(new_ips)} new IP(s) not seen in the last {baseline_days} days"
        )

    if connection_count > conn_threshold:
        reasons.append(
            f"connection count {connection_count} exceeds threshold {conn_threshold}"
        )

    if alert_on_crash and crash_count > 0:
        reasons.append(f"{crash_count} crash(es) detected")

    is_anomaly = bool(reasons)
    message = "; ".join(reasons) if reasons else "no anomalies detected"

    return AnomalyResult(
        is_anomaly=is_anomaly,
        new_ips=new_ips,
        connection_count=connection_count,
        crash_count=crash_count,
        message=message,
    )
