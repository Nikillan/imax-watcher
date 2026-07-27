"""Pydantic schemas for the append-only observation log and run ledger.

Field names and shapes follow the data model in the project brief (README §Data model).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int
    run_id: str
    observed_at_utc: str  # ISO 8601 UTC

    venue_id: str
    venue_name: str
    screen_name: str | None

    film_id: str
    film_title: str
    language: str | None
    format: str | None  # 2D / 3D / IMAX / 4DX / Dolby ...

    show_date: str  # YYYY-MM-DD, local (IST)
    show_time_local: str  # ISO 8601, no offset (source is IST wall-clock)
    session_id: str

    booking_open: bool
    availability_status: str  # raw categorical code from source (e.g. statusColor "G"/"Y"/"D")

    seats_total: int | None
    seats_available: int | None
    price_min: float | None
    price_max: float | None
    price_tiers_json: str  # JSON-encoded list of {label, price, seats_total, seats_available}

    raw_hash: str


class RunLedgerRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    started_at_utc: str
    finished_at_utc: str | None

    trigger: Literal["dispatch", "schedule", "manual"]
    scheduled_for_utc: str | None
    trigger_latency_ms: int | None

    venue_id: str
    status: Literal[
        "ok",
        "http_error",
        "parse_error",
        "timeout",
        "blocked",
        "skipped_concurrent",
        "skipped_by_policy",
    ]

    http_status: int | None
    error_class: str | None
    error_message: str | None

    rows_written: int
    duration_ms: int
    parser_version: str
    runner_region: str | None


class DerivedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_type: Literal[
        "first_seen_bookable",
        "new_session_added",
        "sold_out",
        "show_removed",
        "price_changed",
    ]
    venue_id: str
    film_id: str
    show_date: str
    session_id: str

    last_absent_at_utc: str | None
    first_present_at_utc: str
    uncertainty_seconds: int | None
    preceded_by_gap: bool
