from src.derive import derive_events
from src.models import Observation, RunLedgerRow


def _obs(observed_at: str, session_id: str = "S1") -> Observation:
    return Observation(
        schema_version=1,
        run_id="run-x",
        observed_at_utc=observed_at,
        venue_id="9505",
        venue_name="Cinepolis BSR Mall OMR",
        screen_name="AUDI01",
        film_id="F1",
        film_title="Idhayam Murali",
        language="Tamil",
        format="2D",
        show_date="2026-08-01",
        show_time_local="2026-08-01T17:25",
        session_id=session_id,
        booking_open=True,
        availability_status="G",
        seats_total=180,
        seats_available=154,
        price_min=54.35,
        price_max=183.8,
        price_tiers_json="[]",
        raw_hash="deadbeef",
    )


def _ledger_row(started_at: str, status: str = "ok") -> RunLedgerRow:
    return RunLedgerRow(
        run_id=f"run-{started_at}",
        started_at_utc=started_at,
        finished_at_utc=started_at,
        trigger="dispatch",
        scheduled_for_utc=None,
        trigger_latency_ms=None,
        venue_id="9505",
        status=status,
        http_status=200 if status == "ok" else 500,
        error_class=None,
        error_message=None,
        rows_written=0,
        duration_ms=100,
        parser_version="1",
        runner_region=None,
    )


def test_clean_release_no_gap():
    ledger = [
        _ledger_row("2026-08-01T10:00:00.000Z", "ok"),   # absent
        _ledger_row("2026-08-01T10:30:00.000Z", "ok"),   # absent -- last one before release
        _ledger_row("2026-08-01T11:00:00.000Z", "ok"),   # first present
    ]
    observations = [_obs("2026-08-01T11:00:00.000Z")]

    events = derive_events(observations, ledger)

    assert len(events) == 1
    event = events[0]
    assert event.last_absent_at_utc == "2026-08-01T10:30:00.000Z"
    assert event.first_present_at_utc == "2026-08-01T11:00:00.000Z"
    assert event.uncertainty_seconds == 1800
    assert event.preceded_by_gap is False


def test_missed_poll_sets_preceded_by_gap():
    ledger = [
        _ledger_row("2026-08-01T10:00:00.000Z", "ok"),          # confirmed absent
        _ledger_row("2026-08-01T10:30:00.000Z", "http_error"),  # missed poll -- can't confirm absence here
        _ledger_row("2026-08-01T11:00:00.000Z", "ok"),          # first present
    ]
    observations = [_obs("2026-08-01T11:00:00.000Z")]

    events = derive_events(observations, ledger)

    assert len(events) == 1
    event = events[0]
    assert event.last_absent_at_utc == "2026-08-01T10:00:00.000Z"
    assert event.preceded_by_gap is True
    assert event.uncertainty_seconds == 3600


def test_no_prior_poll_means_unknown_uncertainty():
    ledger = [_ledger_row("2026-08-01T11:00:00.000Z", "ok")]
    observations = [_obs("2026-08-01T11:00:00.000Z")]

    events = derive_events(observations, ledger)

    assert events[0].last_absent_at_utc is None
    assert events[0].uncertainty_seconds is None


def test_derive_is_deterministic_on_rerun():
    ledger = [
        _ledger_row("2026-08-01T10:00:00.000Z", "ok"),
        _ledger_row("2026-08-01T11:00:00.000Z", "ok"),
    ]
    observations = [_obs("2026-08-01T11:00:00.000Z", "S1"), _obs("2026-08-01T11:00:00.000Z", "S2")]

    first_run = [e.model_dump_json() for e in derive_events(observations, ledger)]
    second_run = [e.model_dump_json() for e in derive_events(list(reversed(observations)), ledger)]

    assert first_run == second_run
