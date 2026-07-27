# imax-watcher

Records, at ~30-minute resolution, when online booking opens for films at four Chennai
cinema venues, and keeps a longitudinal record of how listings evolve after opening.
This is a **data collection system**, not a notification bot — see `data/` for the
append-only record; Telegram notifications are optional and off by default.

Data source: [district.in](https://www.district.in) (Zomato's PVR/INOX/Cinepolis ticketing
platform for Chennai — see "Why district.in, not bookmyshow.com" below).

## Venues tracked

| Label | Venue | Code |
|---|---|---|
| Phoenix Mall | INOX Phoenix Market City, Velachery | `1020779` |
| BSR Mall | Cinepolis BSR Mall OMR, Thoraipakkam | `9505` |
| PVR Palazzo | PVR Palazzo, The Nexus Vijaya Mall | `1022274` |
| VR Mall | PVR VR Mall, Anna Nagar | `1022304` |

(Authoritative list: `src/config.yaml`.)

## Why district.in, not bookmyshow.com

`in.bookmyshow.com` is Cloudflare-blocked for plain automated requests (confirmed: even
`/robots.txt` returns a hard 403 "Sorry, you have been blocked"). The same PVR/INOX/Cinepolis
Chennai cinema data is served by `district.in` ("District by Zomato"), which responds
normally — verified from both a non-Indian IP and, critically, from an actual GitHub-hosted
Actions runner IP (see `.github/workflows/blocking-test.yml`, run once during Phase 0: 200 OK
in <1.5s from runner IP `128.24.161.32`, correct Chennai data returned).

## How the site's data actually works (recon findings)

- Every `district.in/movies/...` page is Next.js SSR. The full API response is embedded as
  JSON in `<script id="__NEXT_DATA__">` — `src/parse.py` reads that, not the rendered HTML.
- Chennai's city code is `34` (`config.source.city_id`), and city is pinned by **URL path
  slug**, not cookie/geo-IP — verified by fetching from outside India and getting correct
  Chennai data back every time.
- Today's listing needs no query string; future dates need `?fromdate=YYYY-MM-DD` on the
  same venue URL (confirmed empirically — SSR ignores a plain `?date=` param).
- **robots.txt conflict, flagged and resolved by project decision:** district.in's
  `robots.txt` has `Disallow: /*?` under `User-Agent: *`, which literally covers the
  `?fromdate=` pattern needed for days-ahead polling. Decision: proceed anyway — this is a
  low-volume (~40 req/run), polite-cadence research poller hitting a handful of known URLs,
  not the mass-crawl behavior robots.txt exists to police. Today-only polling (no query
  string) would have been fully compliant but loses lead-time research question #4. Revisit
  if district.in's posture changes.
- A session's `statusColor` (`G`/`Y`/`D`) is the raw categorical availability code; each
  price tier under `areas[]` also exposes **real numeric seat counts** (`sAvail`/`sTotal`),
  not just a flag.
- Screen name is `audi` (e.g. `"AUDI01"`), session id is `sid`.
- A film's own `isReleased` boolean + `release_date` exist at the movie level. At a venue's
  own listing, an unreleased film is simply **absent** (no placeholder row) — this is why
  `derive.py` reconstructs "absence" from the observation log rather than expecting the API
  to say it directly.

## Repo layout

```
.github/workflows/poll.yml          # repository_dispatch + workflow_dispatch + 3h schedule
.github/workflows/derive.yml        # daily: rebuild events.jsonl + reports/summary.md
.github/workflows/blocking-test.yml # one-off Phase 0 probe, safe to delete or keep
src/config.yaml                     # venues, city code, days-ahead, watchlist, hot windows
src/client.py                       # HTTP layer: retries, backoff+jitter, budget, delay
src/parse.py                        # __NEXT_DATA__ -> Observation rows; PARSER_VERSION
src/poll.py                         # orchestration, ledger writing, CLI
src/derive.py                       # observation log -> events.jsonl (pure, deterministic)
src/report.py                       # events.jsonl + runs.jsonl -> reports/summary.md
src/notify.py                       # Telegram, disabled by default
src/models.py                       # pydantic schemas (Observation, RunLedgerRow, DerivedEvent)
tests/                              # parser, derivation, dedupe, and resilience tests
tests/fixtures/                     # a real captured district.in response, for regression
data/                               # committed output (see Data model below)
reports/summary.md                  # daily-rebuilt coverage + findings report
```

## Data model

**`data/observations/dt=YYYY-MM-DD/part-<run_id>.jsonl`** — append-only, one row per
`(venue, film, show-date, session)` observed in a poll. Never updated or deleted.

**`data/raw/dt=YYYY-MM-DD/<run_id>-<venue_id>-<date>.json.gz`** — the raw `__NEXT_DATA__`
payload behind each observation batch, gzipped. Retained for `raw_retention_days` (default
90, `src/config.yaml`), pruned automatically by `poll.py` on each run.

**`data/runs.jsonl`** — one row per run **and per venue attempt**: `status` is one of `ok`,
`http_error`, `parse_error`, `timeout`, `blocked`, `skipped_concurrent`, `skipped_by_policy`.
**Analysis must join against this** — without it, an outage looks identical to "no tickets
were on sale."

**`data/events.jsonl`** — the actual research output. Rebuilt from scratch by `src/derive.py`
on every run (pure function of `data/observations/` + `data/runs.jsonl`; running it twice
produces a byte-identical file). Each row is a `first_seen_bookable` event with
`last_absent_at_utc`, `first_present_at_utc`, `uncertainty_seconds`, and `preceded_by_gap` —
the honest uncertainty window, not a bare timestamp.

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# See what a run would do without making any requests or writes:
.venv/bin/python -m src.poll --dry-run

# Poll one venue, 2 days ahead, verbose:
.venv/bin/python -m src.poll --venues 9505 --days-ahead 2 --verbose

# Rebuild the derived events table and report from whatever's in data/:
.venv/bin/python -m src.derive
.venv/bin/python -m src.report

# Tests:
.venv/bin/python -m pytest tests/ -v
```

`poll.py` flags: `--dry-run`, `--venues phoenix,vr` (comma-separated ids or labels),
`--days-ahead N`, `--verbose`, `--trigger dispatch|schedule|manual`, `--scheduled-for
<ISO8601>`. Same code path runs locally and in CI.

## Triggering from cron-job.org

GitHub's own `schedule` trigger has 5–20 min drift and gets auto-disabled after 60 days of
repo inactivity, so the primary trigger is external: **cron-job.org, every 30 minutes**,
hitting the GitHub dispatch API. A low-frequency GitHub `schedule` (every 3 hours, see
`poll.yml`) exists purely as a dead-man's switch in case cron-job.org fails silently.

Set up a cron-job.org job with:

- **URL:** `https://api.github.com/repos/<owner>/imax-watcher/dispatches`
- **Method:** `POST`
- **Headers:**
  - `Accept: application/vnd.github+json`
  - `Authorization: Bearer <PAT>`
  - `Content-Type: application/json`
- **Body:**
  ```json
  {"event_type": "poll", "client_payload": {"source": "cron-job.org", "scheduled_for": "{{ISO8601_NOW}}"}}
  ```
  (district.in cron-job.org doesn't have a built-in `{{ISO8601_NOW}}` macro at the time of
  writing — if yours doesn't either, omit `scheduled_for` or set it via their custom
  variables feature; the workflow tolerates it being absent.)
- **Schedule:** every 30 minutes.
- Enable cron-job.org's own failure notifications (email/webhook) — that's your alerting for
  "the trigger itself died," which GitHub cannot see.

Equivalent curl, for testing the dispatch manually:

```bash
curl -X POST https://api.github.com/repos/<owner>/imax-watcher/dispatches \
  -H "Accept: application/vnd.github+json" \
  -H "Authorization: Bearer <PAT>" \
  -H "Content-Type: application/json" \
  -d '{"event_type":"poll","client_payload":{"source":"manual-test","scheduled_for":"2026-07-27T12:00:00Z"}}'
```

### PAT setup

Create a **fine-grained personal access token** scoped to this repo only:
- Repository access: this repo only.
- Permissions: **Actions: read and write**, **Contents: read**.
- Set an expiry (fine-grained PATs require one) and **write the expiry date here once
  created** — a silently expired token means silently zero data:

  > PAT expiry: `<fill in when you create it>` — rotate before this date.

Store it as the cron-job.org job's Authorization header value directly (cron-job.org's own
secret storage, not this repo). Never commit it.

### Adding a hot-window (tightened cadence) job

To poll more frequently around an expected release (e.g. every 10 min for 48h), add a
**second** cron-job.org job with the same URL/method/headers, a tighter schedule, and
`"client_payload": {"mode": "hot", ...}` in the body. No code change needed — `src/poll.py`
reads the policy state from `src/config.yaml`'s `hot_windows` and logs it in the ledger.
(Tier 2 seat-level collection, which hot windows are mainly for, is deferred — see below.)

## Adding a fifth venue

Edit `src/config.yaml`'s `venues:` list — add an entry with `id` (district.in's numeric
cinema id), `name`, `label`, and `slug` (the path segment after `/movies/`, found by
searching `district.in/movies/cinemas-in-<city>` for the venue). No code change required.

## What's deferred to v2

- **Tier 2 (seat-level polling for fill curves)** — `tiers.tier2_enabled: false` in config.
  The schema and `notify.py`/`hot_windows` plumbing exist so this is a config flip + a
  `client.py` addition, not a rewrite, when it's wanted.
- The true JSON-only XHR endpoint (vs. the ~200KB SSR HTML page) wasn't captured — no
  connected browser was available during recon. If per-request payload size becomes a
  problem, capture it once via DevTools Network tab on a `district.in` cinema page and swap
  `client.py`'s fetch target.

## Notifications

Off by default (`notify.enabled: false`). When enabled, sends a Telegram message only on
`first_seen_bookable` events for watchlist films (never every observation), via
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` repo secrets. No-ops gracefully if those secrets
are absent; a notification failure never fails the polling run.

## Secrets required in the repo

- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — optional, only if `notify.enabled: true`.
- The GitHub PAT lives in cron-job.org, **not** as a repo secret (the repo doesn't need to
  authenticate to itself).
