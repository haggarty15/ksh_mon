"""
models.py – Pydantic models for the Vanguard System Monitor API.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Ingest models
# ---------------------------------------------------------------------------

class EventIn(BaseModel):
    """A single collected event from the PowerShell collector."""
    event_type: str
    timestamp: str
    source: str | None = None
    event_id: int | None = None
    level: str | None = None
    message: str | None = None
    # Additional fields are captured in 'extra'
    model_config = {"extra": "allow"}

    def to_flat_dict(self) -> dict[str, Any]:
        return self.model_dump()


class IngestPayload(BaseModel):
    """Payload from collect.ps1 or any JSON ingest."""
    collected_at: str | None = None
    event_count: int | None = None
    events: list[EventIn] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Query / response models
# ---------------------------------------------------------------------------

class EventOut(BaseModel):
    """An event row returned from the DB."""
    id: int
    event_type: str
    timestamp: str
    source: str | None = None
    event_id: int | None = None
    level: str | None = None
    message: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class EventListResponse(BaseModel):
    events: list[EventOut]
    total: int
    limit: int
    offset: int


class DatesResponse(BaseModel):
    dates: list[str]


class CountsResponse(BaseModel):
    counts: dict[str, int]
    date: str | None = None


class TriggerResponse(BaseModel):
    status: str
    message: str
    stdout: str = ""
    stderr: str = ""
