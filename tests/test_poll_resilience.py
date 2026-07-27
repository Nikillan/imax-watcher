import httpx

from src.poll import poll_venue

VENUE = {"id": "9505", "label": "BSR Mall", "slug": "cinepolis-bsr-mall-omr-thoraipakkam-chennai-in-chennai-CD9505"}


class _DeadClient:
    """Simulates the network dying mid-request."""

    def get(self, path, params=None):
        raise httpx.ConnectError("connection refused")


def test_network_failure_produces_clean_http_error_ledger_row_not_partial_data():
    observations, ledger_row, raw_entries = poll_venue(
        _DeadClient(),
        VENUE,
        days_ahead=2,
        expected_city_id="34",
        run_id="run-1",
        dry_run=False,
    )

    assert observations == []
    assert raw_entries == []
    assert ledger_row.status == "http_error"
    assert ledger_row.error_class == "ConnectError"
    assert ledger_row.venue_id == "9505"
    assert ledger_row.rows_written == 0


class _FlakyClient:
    """First date succeeds, second date's network dies -- must keep the first date's rows."""

    def __init__(self, good_response_text: str):
        self._good_text = good_response_text
        self.calls = 0

    def get(self, path, params=None):
        self.calls += 1
        if self.calls == 1:
            return httpx.Response(200, text=self._good_text, request=httpx.Request("GET", "https://x/"))
        raise httpx.ConnectError("connection refused")


def test_partial_success_before_failure_keeps_rows_already_parsed():
    import json
    from pathlib import Path

    real = json.loads((Path(__file__).parent / "fixtures" / "real_response_sample.json").read_text())
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(real)}</script>'

    observations, ledger_row, raw_entries = poll_venue(
        _FlakyClient(html),
        VENUE,
        days_ahead=2,
        expected_city_id="34",
        run_id="run-1",
        dry_run=False,
    )

    assert len(observations) == 39  # from the one successful date before the network died
    assert len(raw_entries) == 1
    assert ledger_row.status == "http_error"
    assert ledger_row.rows_written == 39
