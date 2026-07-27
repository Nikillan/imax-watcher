import json
from pathlib import Path

import pytest

from src.parse import RegionMismatchError, SchemaDriftError, parse_observations

FIXTURES = Path(__file__).parent / "fixtures"


def _wrap(next_data: dict) -> str:
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script>'


def _minimal_valid_next_data(**overrides) -> dict:
    base = {
        "props": {
            "pageProps": {
                "data": {
                    "cityName": "chennai",
                    "serverState": {
                        "9505": {
                            "meta": {
                                "cinema": {"id": 9505, "cityId": 34, "name": "Cinepolis BSR Mall OMR"},
                                "movies": [{"id": "OBAA84", "name": "Idhayam Murali"}],
                            },
                            "pageData": {
                                "sessions": [
                                    {
                                        "sid": "9949",
                                        "mid": "OBAA84",
                                        "showTime": "2026-07-27T17:25",
                                        "audi": "AUDI01",
                                        "lang": "Tamil",
                                        "scrnFmt": "2D",
                                        "statusColor": "G",
                                        "areas": [
                                            {"label": "NORMAL", "sTotal": 17, "sAvail": 0, "price": 54.35},
                                            {"label": "EXECUTIVE", "sTotal": 163, "sAvail": 154, "price": 183.8},
                                        ],
                                    }
                                ]
                            },
                        }
                    },
                }
            }
        }
    }
    base.update(overrides)
    return base


def test_happy_path_parse_from_real_fixture():
    real = json.loads((FIXTURES / "real_response_sample.json").read_text())
    html = _wrap(real)

    observations = parse_observations(
        html,
        venue_id="9505",
        venue_label="BSR Mall",
        expected_city_id="34",
        run_id="run-1",
        observed_at_utc="2026-07-27T12:00:00.000Z",
    )

    assert len(observations) == 39
    first = observations[0]
    assert first.venue_id == "9505"
    assert first.schema_version == 1
    assert first.session_id
    assert first.seats_total is not None and first.seats_available is not None
    assert first.availability_status in {"G", "Y", "D"}


def test_happy_path_minimal_fixture():
    html = _wrap(_minimal_valid_next_data())
    observations = parse_observations(
        html,
        venue_id="9505",
        venue_label="BSR Mall",
        expected_city_id="34",
        run_id="run-1",
        observed_at_utc="2026-07-27T12:00:00.000Z",
    )
    assert len(observations) == 1
    obs = observations[0]
    assert obs.film_title == "Idhayam Murali"
    assert obs.screen_name == "AUDI01"
    assert obs.seats_total == 180
    assert obs.seats_available == 154
    assert obs.price_min == 54.35
    assert obs.price_max == 183.8
    assert obs.show_date == "2026-07-27"
    assert obs.booking_open is True


def test_missing_next_data_script_raises_schema_drift():
    with pytest.raises(SchemaDriftError):
        parse_observations(
            "<html><body>not the right page</body></html>",
            venue_id="9505",
            venue_label="BSR Mall",
            expected_city_id="34",
            run_id="run-1",
            observed_at_utc="2026-07-27T12:00:00.000Z",
        )


def test_schema_drift_missing_sessions_key():
    next_data = _minimal_valid_next_data()
    del next_data["props"]["pageProps"]["data"]["serverState"]["9505"]["pageData"]["sessions"]
    with pytest.raises(SchemaDriftError):
        parse_observations(
            _wrap(next_data),
            venue_id="9505",
            venue_label="BSR Mall",
            expected_city_id="34",
            run_id="run-1",
            observed_at_utc="2026-07-27T12:00:00.000Z",
        )


def test_schema_drift_unexpected_session_shape():
    next_data = _minimal_valid_next_data()
    # areas is required to compute seat counts/prices -- drop it to simulate an upstream schema change
    del next_data["props"]["pageProps"]["data"]["serverState"]["9505"]["pageData"]["sessions"][0]["areas"]
    with pytest.raises(SchemaDriftError):
        parse_observations(
            _wrap(next_data),
            venue_id="9505",
            venue_label="BSR Mall",
            expected_city_id="34",
            run_id="run-1",
            observed_at_utc="2026-07-27T12:00:00.000Z",
        )


def test_region_mismatch_wrong_city():
    next_data = _minimal_valid_next_data()
    next_data["props"]["pageProps"]["data"]["cityName"] = "bengaluru"
    with pytest.raises(RegionMismatchError):
        parse_observations(
            _wrap(next_data),
            venue_id="9505",
            venue_label="BSR Mall",
            expected_city_id="34",
            run_id="run-1",
            observed_at_utc="2026-07-27T12:00:00.000Z",
        )


def test_far_future_date_with_no_movies_yet_is_empty_not_an_error():
    """A date far enough ahead that nothing's been scheduled omits 'movies' and
    'sessions' entirely rather than returning empty collections -- this is a real
    response shape observed in production, not a hypothetical."""
    next_data = {
        "props": {
            "pageProps": {
                "data": {
                    "cityName": "chennai",
                    "serverState": {
                        "9505": {
                            "meta": {
                                "cinema": {"id": 9505, "cityId": 34, "name": "Cinepolis BSR Mall OMR"},
                                "amenities": [],
                                "quickFilterData": {},
                            },
                            "pageData": {},
                        }
                    },
                }
            }
        }
    }
    observations = parse_observations(
        _wrap(next_data),
        venue_id="9505",
        venue_label="BSR Mall",
        expected_city_id="34",
        run_id="run-1",
        observed_at_utc="2026-07-27T12:00:00.000Z",
    )
    assert observations == []


def test_inconsistent_absence_is_schema_drift():
    """movies present but sessions missing (or vice versa) is a genuine shape change,
    not the known far-future-absence case -- must still fail loudly."""
    next_data = _minimal_valid_next_data()
    del next_data["props"]["pageProps"]["data"]["serverState"]["9505"]["pageData"]["sessions"]
    with pytest.raises(SchemaDriftError):
        parse_observations(
            _wrap(next_data),
            venue_id="9505",
            venue_label="BSR Mall",
            expected_city_id="34",
            run_id="run-1",
            observed_at_utc="2026-07-27T12:00:00.000Z",
        )


def test_region_mismatch_wrong_venue_id():
    html = _wrap(_minimal_valid_next_data())
    with pytest.raises(RegionMismatchError):
        parse_observations(
            html,
            venue_id="1020779",  # asking for Phoenix Mall but fixture is BSR Mall (9505)
            venue_label="Phoenix Mall",
            expected_city_id="34",
            run_id="run-1",
            observed_at_utc="2026-07-27T12:00:00.000Z",
        )
