"""HTTP layer: retries with backoff+jitter, per-run request budget, politeness delay.

Retries only on connection errors, 5xx, and 429. Any other 4xx is not retried —
it means our request was wrong, not that the server is struggling.
"""
from __future__ import annotations

import logging
import random
import time

import httpx

logger = logging.getLogger(__name__)

RETRY_STATUS_CODES = {429, 500, 502, 503, 504}


class RequestBudgetExceeded(RuntimeError):
    pass


class Client:
    def __init__(
        self,
        base_url: str,
        user_agent: str,
        inter_request_delay_seconds: float = 2.0,
        request_budget_per_run: int = 40,
        max_attempts: int = 3,
        request_timeout_seconds: float = 20.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.inter_request_delay_seconds = inter_request_delay_seconds
        self.request_budget_per_run = request_budget_per_run
        self.max_attempts = max_attempts
        self._requests_made = 0
        self._http = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=request_timeout_seconds,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def get(self, path: str, params: dict | None = None) -> httpx.Response:
        if self._requests_made >= self.request_budget_per_run:
            raise RequestBudgetExceeded(
                f"request budget of {self.request_budget_per_run} exhausted for this run"
            )

        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            if self._requests_made > 0:
                time.sleep(self.inter_request_delay_seconds)
            self._requests_made += 1

            try:
                resp = self._http.get(url, params=params)
            except httpx.TransportError as exc:
                last_exc = exc
                logger.warning("attempt %d/%d transport error for %s: %s", attempt, self.max_attempts, url, exc)
            else:
                if resp.status_code not in RETRY_STATUS_CODES:
                    return resp
                last_exc = None
                logger.warning(
                    "attempt %d/%d got retryable status %d for %s", attempt, self.max_attempts, resp.status_code, url
                )

            if attempt < self.max_attempts:
                backoff = (2 ** (attempt - 1)) + random.uniform(0, 1)
                time.sleep(backoff)

        if last_exc is not None:
            raise last_exc
        return resp  # last (retryable-status) response, exhausted retries
