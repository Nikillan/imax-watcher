"""Pure, deterministic derivation: data/observations/**/*.jsonl + data/runs.jsonl
-> data/events.jsonl. Running this twice on the same input must produce a
byte-identical output file.

Approach: for each (venue_id, film_id, show_date, session_id), find the first
poll where it was observed (first_present_at_utc) and the last successful poll
before that where it was *not* observed at that venue (last_absent_at_utc).
The gap between them is the uncertainty window. If any non-"ok" ledger row for
that venue falls inside the window, preceded_by_gap is True -- the outage could
be hiding an earlier release we simply failed to observe.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.models import DerivedEvent, Observation, RunLedgerRow

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def load_observations(observations_dir: Path) -> list[Observation]:
    rows: list[Observation] = []
    for path in sorted(observations_dir.glob("dt=*/part-*.jsonl")):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(Observation.model_validate_json(line))
    return rows


def load_ledger(ledger_path: Path) -> list[RunLedgerRow]:
    if not ledger_path.exists():
        return []
    rows = []
    with open(ledger_path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(RunLedgerRow.model_validate_json(line))
    return rows


def _run_poll_times(ledger: list[RunLedgerRow]) -> dict[str, list[tuple[datetime, RunLedgerRow]]]:
    """venue_id -> sorted list of (poll_time, ledger_row) for every attempt, ok or not."""
    by_venue: dict[str, list[tuple[datetime, RunLedgerRow]]] = defaultdict(list)
    for row in ledger:
        if row.venue_id == "__all__":
            continue
        by_venue[row.venue_id].append((_parse_ts(row.started_at_utc), row))
    for rows in by_venue.values():
        rows.sort(key=lambda t: t[0])
    return by_venue


def derive_events(observations: list[Observation], ledger: list[RunLedgerRow]) -> list[DerivedEvent]:
    # key: (venue_id, film_id, show_date, session_id) -> sorted list of (observed_at, obs)
    by_key: dict[tuple[str, str, str, str], list[tuple[datetime, Observation]]] = defaultdict(list)
    for obs in observations:
        key = (obs.venue_id, obs.film_id, obs.show_date, obs.session_id)
        by_key[key].append((_parse_ts(obs.observed_at_utc), obs))
    for rows in by_key.values():
        rows.sort(key=lambda t: t[0])

    poll_times_by_venue = _run_poll_times(ledger)

    events: list[DerivedEvent] = []
    for (venue_id, film_id, show_date, session_id), rows in by_key.items():
        first_present_at, first_obs = rows[0]

        venue_polls = poll_times_by_venue.get(venue_id, [])
        last_absent_at: datetime | None = None
        preceded_by_gap = False
        for poll_time, row in venue_polls:
            if poll_time >= first_present_at:
                break
            if row.status == "ok":
                last_absent_at = poll_time
                preceded_by_gap = False
            else:
                # a failed/skipped poll sits in the window -- we can't confirm absence here
                preceded_by_gap = True

        uncertainty_seconds = (
            int((first_present_at - last_absent_at).total_seconds()) if last_absent_at else None
        )

        events.append(
            DerivedEvent(
                event_type="first_seen_bookable",
                venue_id=venue_id,
                film_id=film_id,
                show_date=show_date,
                session_id=session_id,
                last_absent_at_utc=last_absent_at.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
                if last_absent_at
                else None,
                first_present_at_utc=first_obs.observed_at_utc,
                uncertainty_seconds=uncertainty_seconds,
                preceded_by_gap=preceded_by_gap,
            )
        )

    # Deterministic order: sort by the natural key, not insertion order.
    events.sort(key=lambda e: (e.venue_id, e.film_id, e.show_date, e.session_id))
    return events


def write_events(events: list[DerivedEvent], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        for event in events:
            f.write(event.model_dump_json() + "\n")


def main() -> int:
    observations = load_observations(DATA_DIR / "observations")
    ledger = load_ledger(DATA_DIR / "runs.jsonl")
    events = derive_events(observations, ledger)
    write_events(events, DATA_DIR / "events.jsonl")
    print(f"derived {len(events)} events from {len(observations)} observations and {len(ledger)} ledger rows")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
