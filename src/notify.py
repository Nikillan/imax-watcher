"""Telegram notifications. Off by default (config.notify.enabled: false).

Never raises -- a notification failure must never fail the data-collection run.
Secrets come from environment variables (repo secrets in CI); if they're absent
this module no-ops rather than erroring, per the brief.
"""
from __future__ import annotations

import logging
import os

import httpx

from src.models import DerivedEvent

logger = logging.getLogger(__name__)


def send_telegram_message(text: str, *, bot_token_env: str, chat_id_env: str) -> bool:
    token = os.environ.get(bot_token_env)
    chat_id = os.environ.get(chat_id_env)
    if not token or not chat_id:
        logger.info("telegram secrets not set (%s / %s), skipping notification", bot_token_env, chat_id_env)
        return False

    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10.0,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning("telegram notification failed: %s", exc)
        return False


def format_first_seen_bookable(event: DerivedEvent, *, venue_name: str, film_title: str) -> str:
    window = (
        f"{event.uncertainty_seconds // 60} min"
        if event.uncertainty_seconds is not None
        else "unknown (no prior confirmed-absent poll)"
    )
    gap_note = " (a poll gap overlaps this window -- take it with caution)" if event.preceded_by_gap else ""
    return (
        f"*Booking opened*: {film_title}\n"
        f"Venue: {venue_name}\n"
        f"Show date: {event.show_date}\n"
        f"First seen: {event.first_present_at_utc}\n"
        f"Uncertainty window: {window}{gap_note}"
    )


def notify_first_seen_bookable(
    events: list[DerivedEvent],
    *,
    config: dict,
    venue_names: dict[str, str],
    film_titles: dict[str, str],
    watchlist_film_ids: set[str],
) -> None:
    notify_cfg = config.get("notify", {})
    if not notify_cfg.get("enabled", False):
        return

    telegram_cfg = notify_cfg.get("telegram", {})
    for event in events:
        if event.event_type != "first_seen_bookable":
            continue
        if watchlist_film_ids and event.film_id not in watchlist_film_ids:
            continue
        text = format_first_seen_bookable(
            event,
            venue_name=venue_names.get(event.venue_id, event.venue_id),
            film_title=film_titles.get(event.film_id, event.film_id),
        )
        send_telegram_message(
            text,
            bot_token_env=telegram_cfg.get("bot_token_env", "TELEGRAM_BOT_TOKEN"),
            chat_id_env=telegram_cfg.get("chat_id_env", "TELEGRAM_CHAT_ID"),
        )
