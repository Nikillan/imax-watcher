from src.poll import dedupe
from src.models import Observation


def _obs(run_id: str, venue_id: str, session_id: str) -> Observation:
    return Observation(
        schema_version=1,
        run_id=run_id,
        observed_at_utc="2026-08-01T11:00:00.000Z",
        venue_id=venue_id,
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


def test_dedupe_by_run_venue_session():
    observations = [
        _obs("run-1", "9505", "S1"),
        _obs("run-1", "9505", "S1"),  # exact duplicate -- e.g. a re-run with the same run_id
        _obs("run-1", "9505", "S2"),
        _obs("run-1", "1020779", "S1"),  # different venue, same session id -- not a duplicate
        _obs("run-2", "9505", "S1"),  # different run -- not a duplicate
    ]

    result = dedupe(observations)

    keys = {(o.run_id, o.venue_id, o.session_id) for o in result}
    assert len(result) == 4
    assert keys == {
        ("run-1", "9505", "S1"),
        ("run-1", "9505", "S2"),
        ("run-1", "1020779", "S1"),
        ("run-2", "9505", "S1"),
    }


def test_dedupe_keeps_first_occurrence():
    a = _obs("run-1", "9505", "S1")
    a.seats_available = 100
    b = _obs("run-1", "9505", "S1")
    b.seats_available = 999  # would-be duplicate with different payload

    result = dedupe([a, b])

    assert len(result) == 1
    assert result[0].seats_available == 100
