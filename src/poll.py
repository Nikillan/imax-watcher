"""Orchestrate a single poll run: fetch all venues x N days ahead, write
observations, raw archive, and ledger rows. Per-venue failures are isolated --
one venue erroring never stops the others from being attempted.

    python -m src.poll [--dry-run] [--venues phoenix,vr] [--days-ahead 5] [--verbose]
                        [--trigger dispatch|schedule|manual] [--scheduled-for ISO8601]
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import yaml

from src.client import Client, RequestBudgetExceeded
from src.models import Observation, RunLedgerRow
from src.parse import PARSER_VERSION, RegionMismatchError, SchemaDriftError, parse_observations

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
CONFIG_PATH = REPO_ROOT / "src" / "config.yaml"
LOCK_PATH = DATA_DIR / ".run_lock"
LOCK_STALE_SECONDS = 25 * 60  # longer than the 8-minute hard runtime cap, plus margin


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def acquire_lock(run_id: str) -> bool:
    """Return True if the lock was acquired, False if another run holds it."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if LOCK_PATH.exists():
        try:
            held = json.loads(LOCK_PATH.read_text())
            held_at = datetime.strptime(held["started_at_utc"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc
            )
            age = (datetime.now(timezone.utc) - held_at).total_seconds()
        except Exception:
            age = LOCK_STALE_SECONDS + 1  # unreadable lock, treat as stale
        if age < LOCK_STALE_SECONDS:
            return False
        logger.warning("stale lock (age %.0fs) from run %s, taking over", age, held.get("run_id"))
    LOCK_PATH.write_text(json.dumps({"run_id": run_id, "started_at_utc": utc_now_iso()}))
    return True


def release_lock() -> None:
    LOCK_PATH.unlink(missing_ok=True)


def poll_venue(
    client: Client,
    venue: dict,
    *,
    days_ahead: int,
    expected_city_id: str,
    run_id: str,
    dry_run: bool,
) -> tuple[list[Observation], RunLedgerRow, list[tuple[str, bytes]]]:
    """Poll one venue for today + days_ahead future dates.

    Returns (observations, ledger_row, raw_archive_entries).
    A single ledger row summarizes the whole venue attempt (all dates); the
    first failure encountered aborts remaining dates for that venue but never
    affects other venues.
    """
    started = time.monotonic()
    observations: list[Observation] = []
    raw_entries: list[tuple[str, bytes]] = []

    today = datetime.now(timezone.utc).date()
    dates = [today] + [today + timedelta(days=i) for i in range(1, days_ahead + 1)]

    status = "ok"
    http_status: int | None = None
    error_class: str | None = None
    error_message: str | None = None

    for d in dates:
        observed_at_utc = utc_now_iso()
        params = None if d == today else {"fromdate": d.isoformat()}
        try:
            resp = client.get(f"/movies/{venue['slug']}", params=params)
        except RequestBudgetExceeded as exc:
            status, error_class, error_message = "skipped_by_policy", type(exc).__name__, str(exc)
            break
        except httpx.TimeoutException as exc:
            status, error_class, error_message = "timeout", type(exc).__name__, str(exc)
            break
        except httpx.TransportError as exc:
            status, error_class, error_message = "http_error", type(exc).__name__, str(exc)
            break

        http_status = resp.status_code
        if resp.status_code == 403:
            status, error_class, error_message = "blocked", "HTTPStatusError", f"403 for {resp.url}"
            break
        if resp.status_code >= 400:
            status, error_class, error_message = (
                "http_error",
                "HTTPStatusError",
                f"{resp.status_code} for {resp.url}",
            )
            break

        raw_entries.append((f"{venue['id']}-{d.isoformat()}", resp.content))

        try:
            day_obs = parse_observations(
                resp.text,
                venue_id=venue["id"],
                venue_label=venue["label"],
                expected_city_id=expected_city_id,
                run_id=run_id,
                observed_at_utc=observed_at_utc,
            )
        except RegionMismatchError as exc:
            status, error_class, error_message = "parse_error", type(exc).__name__, str(exc)
            break
        except SchemaDriftError as exc:
            status, error_class, error_message = "parse_error", type(exc).__name__, str(exc)
            break

        observations.extend(day_obs)

    duration_ms = int((time.monotonic() - started) * 1000)
    ledger_row = RunLedgerRow(
        run_id=run_id,
        started_at_utc=utc_now_iso(),
        finished_at_utc=utc_now_iso(),
        trigger="manual",  # overwritten by caller
        scheduled_for_utc=None,  # overwritten by caller
        trigger_latency_ms=None,  # overwritten by caller
        venue_id=venue["id"],
        status=status,
        http_status=http_status,
        error_class=error_class,
        error_message=error_message,
        rows_written=0 if dry_run else len(observations),
        duration_ms=duration_ms,
        parser_version=PARSER_VERSION,
        runner_region=None,
    )
    return observations, ledger_row, raw_entries


def dedupe(observations: list[Observation]) -> list[Observation]:
    """Dedupe key: (run_id, venue_id, session_id) -- keeps first occurrence."""
    seen: set[tuple[str, str, str]] = set()
    out = []
    for obs in observations:
        key = (obs.run_id, obs.venue_id, obs.session_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(obs)
    return out


def write_outputs(
    run_id: str,
    observations: list[Observation],
    raw_entries: list[tuple[str, bytes]],
    raw_retention_days: int,
) -> None:
    dt = datetime.now(timezone.utc).date().isoformat()
    obs_dir = DATA_DIR / "observations" / f"dt={dt}"
    raw_dir = DATA_DIR / "raw" / f"dt={dt}"
    obs_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    obs_path = obs_dir / f"part-{run_id}.jsonl"
    with open(obs_path, "w") as f:
        for obs in observations:
            f.write(obs.model_dump_json() + "\n")

    for name, content in raw_entries:
        raw_path = raw_dir / f"{run_id}-{name}.json.gz"
        with gzip.open(raw_path, "wb") as f:
            f.write(content)

    _prune_raw(raw_retention_days)


def _prune_raw(retention_days: int) -> None:
    raw_root = DATA_DIR / "raw"
    if not raw_root.exists():
        return
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=retention_days)
    for dt_dir in raw_root.glob("dt=*"):
        try:
            dt = datetime.strptime(dt_dir.name, "dt=%Y-%m-%d").date()
        except ValueError:
            continue
        if dt < cutoff:
            for f in dt_dir.glob("*"):
                f.unlink()
            dt_dir.rmdir()


def append_ledger(rows: list[RunLedgerRow]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "runs.jsonl", "a") as f:
        for row in rows:
            f.write(row.model_dump_json() + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--venues", default=None, help="comma-separated venue ids or labels")
    parser.add_argument("--days-ahead", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--trigger", default="manual", choices=["dispatch", "schedule", "manual"])
    parser.add_argument("--scheduled-for", default=None, help="ISO8601, from client_payload.scheduled_for")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    config = load_config()
    run_id = uuid.uuid4().hex[:12]
    run_started_at = utc_now_iso()

    venues = config["venues"]
    if args.venues:
        wanted = set(args.venues.split(","))
        venues = [v for v in venues if v["id"] in wanted or v["label"] in wanted]

    days_ahead = args.days_ahead if args.days_ahead is not None else config["polling"]["days_ahead"]

    scheduled_for = args.scheduled_for
    trigger_latency_ms = None
    if scheduled_for:
        try:
            sched = datetime.strptime(scheduled_for, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        except ValueError:
            sched = datetime.strptime(scheduled_for, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        started = datetime.strptime(run_started_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
        trigger_latency_ms = int((started - sched).total_seconds() * 1000)

    if args.dry_run:
        print(f"[dry-run] would poll {len(venues)} venues x {days_ahead + 1} dates (today + {days_ahead} ahead):")
        for v in venues:
            print(f"  - {v['label']} ({v['id']}) -> /movies/{v['slug']}")
        return 0

    if not acquire_lock(run_id):
        row = RunLedgerRow(
            run_id=run_id,
            started_at_utc=run_started_at,
            finished_at_utc=utc_now_iso(),
            trigger=args.trigger,
            scheduled_for_utc=scheduled_for,
            trigger_latency_ms=trigger_latency_ms,
            venue_id="__all__",
            status="skipped_concurrent",
            http_status=None,
            error_class=None,
            error_message="another run holds the lock",
            rows_written=0,
            duration_ms=0,
            parser_version=PARSER_VERSION,
            runner_region=None,
        )
        append_ledger([row])
        logger.warning("skipped: concurrent run in progress")
        return 0

    try:
        source_cfg = config["source"]
        polling_cfg = config["polling"]
        client = Client(
            base_url=source_cfg["base_url"],
            user_agent=source_cfg["user_agent"],
            inter_request_delay_seconds=polling_cfg["inter_request_delay_seconds"],
            request_budget_per_run=polling_cfg["request_budget_per_run"],
            max_attempts=polling_cfg["retries"]["max_attempts"],
        )

        all_observations: list[Observation] = []
        all_raw: list[tuple[str, bytes]] = []
        ledger_rows: list[RunLedgerRow] = []
        any_schema_drift = False

        with client:
            for venue in venues:
                try:
                    obs, ledger_row, raw_entries = poll_venue(
                        client,
                        venue,
                        days_ahead=days_ahead,
                        expected_city_id=source_cfg["city_id"],
                        run_id=run_id,
                        dry_run=args.dry_run,
                    )
                except Exception as exc:  # noqa: BLE001 -- per-venue isolation is the point
                    logger.exception("unhandled error polling venue %s", venue["id"])
                    obs, raw_entries = [], []
                    ledger_row = RunLedgerRow(
                        run_id=run_id,
                        started_at_utc=run_started_at,
                        finished_at_utc=utc_now_iso(),
                        trigger=args.trigger,
                        scheduled_for_utc=scheduled_for,
                        trigger_latency_ms=trigger_latency_ms,
                        venue_id=venue["id"],
                        status="http_error",
                        http_status=None,
                        error_class=type(exc).__name__,
                        error_message=str(exc),
                        rows_written=0,
                        duration_ms=0,
                        parser_version=PARSER_VERSION,
                        runner_region=None,
                    )

                ledger_row.trigger = args.trigger
                ledger_row.scheduled_for_utc = scheduled_for
                ledger_row.trigger_latency_ms = trigger_latency_ms

                if ledger_row.status == "parse_error":
                    any_schema_drift = True

                all_observations.extend(obs)
                all_raw.extend(raw_entries)
                ledger_rows.append(ledger_row)
                logger.info(
                    "venue %s: status=%s rows=%d", venue["id"], ledger_row.status, ledger_row.rows_written
                )

        all_observations = dedupe(all_observations)
        write_outputs(run_id, all_observations, all_raw, config["raw_retention_days"])
        append_ledger(ledger_rows)

        print(
            f"run {run_id}: {len(venues)} venues attempted, "
            f"{sum(1 for r in ledger_rows if r.status == 'ok')} ok, "
            f"{len(all_observations)} rows written"
        )

        # Fail loudly on schema drift (after every venue was attempted) so CI sends a failure email.
        return 1 if any_schema_drift else 0
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
