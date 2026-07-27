"""district.in venue-page response -> Observation rows.

The site is a Next.js app; every server-rendered page embeds the full API
response it hydrated from in a <script id="__NEXT_DATA__"> tag. We parse that
JSON rather than the surrounding HTML/DOM, so this is not HTML scraping in the
fragile sense -- it's just reading structured data out of a script tag.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import ValidationError

from src.models import Observation

PARSER_VERSION = "1"
SCHEMA_VERSION = 1

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)

# statusColor -> raw categorical label, purely descriptive; availability_status
# stores the raw source code (see models.Observation), this map is not used to
# overwrite it, only for readability. Kept here as documentation.
STATUS_COLOR_HINT = {"G": "Available", "Y": "Filling Fast", "D": "Sold Out / Almost Full"}


class SchemaDriftError(Exception):
    """Raised when the response doesn't match the shape parse.py expects."""


class RegionMismatchError(Exception):
    """Raised when the response's city/venue doesn't match what we requested."""


def extract_next_data(html: str) -> dict[str, Any]:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise SchemaDriftError("__NEXT_DATA__ script tag not found in response")
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise SchemaDriftError(f"__NEXT_DATA__ payload is not valid JSON: {exc}") from exc


def _raw_hash(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


def parse_observations(
    html: str,
    *,
    venue_id: str,
    venue_label: str,
    expected_city_id: str,
    run_id: str,
    observed_at_utc: str,
) -> list[Observation]:
    """Parse one venue-page response (one venue, one show-date) into Observation rows.

    Raises SchemaDriftError on unexpected shape, RegionMismatchError if the
    response's city/venue doesn't match what was requested.
    """
    raw_bytes = html.encode("utf-8")
    raw_hash = _raw_hash(raw_bytes)

    data = extract_next_data(html)

    try:
        page_props = data["props"]["pageProps"]
        page_data = page_props["data"]
    except (KeyError, TypeError) as exc:
        raise SchemaDriftError(f"missing expected top-level key: {exc}") from exc

    city_name = page_data.get("cityName", "")
    if city_name.lower() != "chennai":
        raise RegionMismatchError(f"expected city 'chennai', got {city_name!r}")

    server_state = page_data.get("serverState")
    if not isinstance(server_state, dict) or not server_state:
        raise SchemaDriftError("serverState missing or empty")

    # serverState is keyed by "{cinemaId}{date}" (or bare cinemaId for 'today').
    # There should be exactly one entry for a single (venue, date) request.
    state_key, state = next(iter(server_state.items()))
    if not state_key.startswith(venue_id):
        raise RegionMismatchError(
            f"expected serverState key starting with venue_id {venue_id}, got {state_key!r}"
        )

    try:
        cinema_meta = state["meta"]["cinema"]
    except (KeyError, TypeError) as exc:
        raise SchemaDriftError(f"missing cinema meta: {exc}") from exc

    if str(cinema_meta.get("cityId")) != expected_city_id:
        raise RegionMismatchError(
            f"expected cityId {expected_city_id}, got {cinema_meta.get('cityId')!r}"
        )
    if str(cinema_meta.get("id")) != venue_id:
        raise RegionMismatchError(
            f"expected cinema id {venue_id}, got {cinema_meta.get('id')!r}"
        )
    venue_name = cinema_meta.get("name", venue_label)

    # When nothing is scheduled yet for the requested date (booking not open that far
    # ahead), the source omits "movies" and "sessions" entirely rather than returning
    # empty collections. That's a legitimate absence state, not malformed data -- treat
    # it as zero observations. It's only schema drift if the two disagree (one present,
    # one missing), which would mean the shape actually changed underneath us.
    movies_meta = state.get("meta", {}).get("movies")
    sessions = state.get("pageData", {}).get("sessions")
    if movies_meta is None and sessions is None:
        return []
    if movies_meta is None or sessions is None:
        raise SchemaDriftError(
            f"inconsistent absence: movies={'present' if movies_meta is not None else 'missing'}, "
            f"sessions={'present' if sessions is not None else 'missing'}"
        )

    film_titles = {m["id"]: m.get("name", "") for m in movies_meta if "id" in m}

    observations: list[Observation] = []
    for sess in sessions:
        try:
            observations.append(_session_to_observation(
                sess,
                film_titles=film_titles,
                venue_id=venue_id,
                venue_name=venue_name,
                run_id=run_id,
                observed_at_utc=observed_at_utc,
                raw_hash=raw_hash,
            ))
        except (KeyError, TypeError, ValidationError) as exc:
            raise SchemaDriftError(f"unexpected session shape: {exc}") from exc

    return observations


def _session_to_observation(
    sess: dict[str, Any],
    *,
    film_titles: dict[str, str],
    venue_id: str,
    venue_name: str,
    run_id: str,
    observed_at_utc: str,
    raw_hash: str,
) -> Observation:
    areas = sess["areas"]
    seats_total = sum(a["sTotal"] for a in areas if a.get("sTotal") is not None) or None
    seats_available = sum(a["sAvail"] for a in areas if a.get("sAvail") is not None) or None
    prices = [a["price"] for a in areas if a.get("price") is not None]

    show_time_local: str = sess["showTime"]
    show_date = show_time_local[:10]
    film_id = sess["mid"]

    return Observation(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        observed_at_utc=observed_at_utc,
        venue_id=venue_id,
        venue_name=venue_name,
        screen_name=sess.get("audi"),
        film_id=film_id,
        film_title=film_titles.get(film_id, ""),
        language=sess.get("lang"),
        format=sess.get("scrnFmt"),
        show_date=show_date,
        show_time_local=show_time_local,
        session_id=str(sess["sid"]),
        booking_open=True,  # presence in this listing means it's bookable
        availability_status=str(sess.get("statusColor", "")),
        seats_total=seats_total,
        seats_available=seats_available,
        price_min=min(prices) if prices else None,
        price_max=max(prices) if prices else None,
        price_tiers_json=json.dumps(
            [
                {
                    "label": a.get("label"),
                    "price": a.get("price"),
                    "seats_total": a.get("sTotal"),
                    "seats_available": a.get("sAvail"),
                }
                for a in areas
            ]
        ),
        raw_hash=raw_hash,
    )
