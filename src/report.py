"""Build reports/summary.md from data/events.jsonl and data/runs.jsonl.

Coverage stats matter as much as the findings -- see brief §10. Everything
here is a straight read of committed data; no new polling happens.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.derive import load_ledger, load_observations
from src.models import DerivedEvent

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


def load_events(path: Path) -> list[DerivedEvent]:
    if not path.exists():
        return []
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(DerivedEvent.model_validate_json(line))
    return events


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def build_report() -> str:
    events = load_events(DATA_DIR / "events.jsonl")
    ledger = load_ledger(DATA_DIR / "runs.jsonl")
    observations = load_observations(DATA_DIR / "observations")

    venue_names = {o.venue_id: o.venue_name for o in observations}
    film_titles = {o.film_id: o.film_title for o in observations}

    now = datetime.now(timezone.utc)
    cutoff_7d = now - timedelta(days=7)

    lines = ["# Coverage & Findings Summary", "", f"_Generated {now.strftime('%Y-%m-%d %H:%M UTC')}_", ""]

    # --- Release events in the last 7 days, per venue ---
    lines.append("## Release events (last 7 days)")
    recent = [e for e in events if e.event_type == "first_seen_bookable" and _parse_ts(e.first_present_at_utc) >= cutoff_7d]
    if not recent:
        lines.append("\nNone recorded in the last 7 days.")
    else:
        by_venue: dict[str, list[DerivedEvent]] = defaultdict(list)
        for e in recent:
            by_venue[e.venue_id].append(e)
        for venue_id, venue_events in sorted(by_venue.items()):
            lines.append(f"\n### {venue_names.get(venue_id, venue_id)}")
            for e in sorted(venue_events, key=lambda e: e.first_present_at_utc):
                window = f"{e.uncertainty_seconds // 60}m" if e.uncertainty_seconds is not None else "unknown"
                gap = " [gap in window]" if e.preceded_by_gap else ""
                lines.append(
                    f"- {film_titles.get(e.film_id, e.film_id)} — show {e.show_date}, "
                    f"first seen {e.first_present_at_utc}, uncertainty ±{window}{gap}"
                )

    # --- Coverage stats ---
    lines.append("\n## Coverage")
    total_attempts = len([r for r in ledger if r.venue_id != "__all__"])
    ok_attempts = len([r for r in ledger if r.venue_id != "__all__" and r.status == "ok"])
    pct = f"{100 * ok_attempts / total_attempts:.1f}%" if total_attempts else "n/a"
    lines.append(f"- Polls attempted: {total_attempts}, succeeded: {ok_attempts} ({pct})")

    by_venue_times: dict[str, list[datetime]] = defaultdict(list)
    for r in ledger:
        if r.venue_id != "__all__" and r.status == "ok":
            by_venue_times[r.venue_id].append(_parse_ts(r.started_at_utc))

    longest_gap = timedelta(0)
    longest_gap_venue = None
    for venue_id, times in by_venue_times.items():
        times.sort()
        for a, b in zip(times, times[1:]):
            gap = b - a
            if gap > longest_gap:
                longest_gap, longest_gap_venue = gap, venue_id
    if longest_gap_venue:
        lines.append(
            f"- Longest gap between successful polls: {longest_gap} "
            f"({venue_names.get(longest_gap_venue, longest_gap_venue)})"
        )
    else:
        lines.append("- Longest gap between successful polls: n/a (insufficient history)")

    lines.append("- Hot-window / Tier 2 polls run: n/a (Tier 2 deferred, `tiers.tier2_enabled: false`)")

    # --- Lead time distribution per venue ---
    lines.append("\n## Lead time (booking-open → show date), per venue")
    lead_times: dict[str, list[int]] = defaultdict(list)
    for e in events:
        if e.event_type != "first_seen_bookable":
            continue
        try:
            show_date = datetime.strptime(e.show_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        first_present = _parse_ts(e.first_present_at_utc)
        days = (show_date - first_present).days
        lead_times[e.venue_id].append(days)
    if not lead_times:
        lines.append("\nNo data yet.")
    else:
        for venue_id, days_list in sorted(lead_times.items()):
            days_list.sort()
            n = len(days_list)
            median = days_list[n // 2]
            lines.append(
                f"- {venue_names.get(venue_id, venue_id)}: n={n}, "
                f"min={min(days_list)}d, median={median}d, max={max(days_list)}d"
            )

    # --- Films seen at one venue but never another ---
    lines.append("\n## Films seen at one venue but not others")
    film_to_venues: dict[str, set[str]] = defaultdict(set)
    for o in observations:
        film_to_venues[o.film_id].add(o.venue_id)
    all_venue_ids = set(venue_names.keys())
    partial = {
        fid: venues for fid, venues in film_to_venues.items() if venues and venues != all_venue_ids and len(all_venue_ids) > 1
    }
    if not partial:
        lines.append("\nNone, or insufficient data.")
    else:
        for fid, venues in sorted(partial.items()):
            seen_at = ", ".join(sorted(venue_names.get(v, v) for v in venues))
            lines.append(f"- {film_titles.get(fid, fid)}: seen at {seen_at}")

    return "\n".join(lines) + "\n"


def main() -> int:
    report = build_report()
    out_path = REPO_ROOT / "reports" / "summary.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    print(f"wrote {out_path}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
