# POP ⇄ SFO Flight Price Monitor

Standalone production monitor for **Puerto Plata (POP) ⇄ San Francisco (SFO)**,
strictly **1 stop**, on **American Airlines (AA), JetBlue (B6), United (UA), Copa (CM)**.

Nothing in this repository depends on the former Arajet SDQ–GUA/EZE/NLU project:
one tracker, one SQLite DB, one Telegram digest, one dashboard, one Worker, one CI pipeline.

## What it does

- Queries Google Flights SSR (protobuf `tfs`) for one-way legs and combines them
  into round trips with exactly **2 segments per direction** (`len(segments) == 2`).
- Enforces the exact airports (POP ⇄ SFO), the same connecting airport inside each
  leg (no ground transfer), a 60–720 min layover and ≤ 1440 min total travel time.
- Enforces the date window: departure **≥ 2027-05-20**, return arrival in POP
  **≤ 2027-07-05**, stays **14–30 nights**, step **1 day** → **425 valid pairs**.
- Distinguishes a valid empty result (`EMPTY`) from network/schema failures
  (`ERROR`); failures are never cached across runs.
- Uses a circuit breaker (5 consecutive errors), a 12 h disk leg cache and
  1.2–2.0 s jitter between network calls.
- Writes quotes and `scan_runs` metadata in **one SQLite transaction**
  (`BEGIN IMMEDIATE`); `complete` requires **coverage ≥ 85%** and **≥ 80 quotes**.
- Sends a Top-5 Telegram digest and the HTML dashboard with granular delivery
  flags (`message_delivered`, `document_delivered`).

## Price basis and booking links

The displayed total is the **sum of two one-way legs** (outbound + inbound). It
is a discovery estimate, not a guaranteed round-trip fare: Google's round-trip
booking page can be lower or higher and has separate fare classes.

Example (27.10.2026, POP 20.05 → SFO, return 14–15.06):

- scanner sum: **$848** (AA 2329/2426 + AA 2799/1153);
- exact booking page for those flights: **$869** Basic Economy;
- a different return (AA 2799/2329) showed **$829** Basic / $982 Main /
  $1,213 Main Plus / $1,581 Business.

For every same-carrier 1-stop itinerary the scanner now generates an **exact
Google Flights booking deep link** (`/travel/flights/booking?tfs=...`) from the
segment dates, airports, carrier and flight numbers. The dashboard/digest link
opens that page and shows the real fare classes. Mixed-carrier pairs keep a
Google Flights search link because they are separate tickets. The table/digest
labels the total as “сумма one-way плеч”.

After each scan, `roundtrip_validator.py` opens Google Flights' round-trip
search for the **top 10 date pairs** (Playwright + Chrome under xvfb in CI) and
stores the actual “from $X” round-trip price in `roundtrip_checks`. The digest
and dashboard show it as **“Google roundtrip”** next to the sum. This keeps the
headline deals bookable-checked without turning the 425-pair scanner into a
slow browser crawl.

## Fare source: Google Flights only

Google Flights is the **only fare scanner**. Direct airline fare checks were
removed: the trial showed only a small, inconsistent difference (AA was $7 lower
on 2 of 5 pairs but higher on the other 3; JetBlue has no through POP ⇄ SFO
fare), and aa.com is Akamai-blocked on GitHub runners. The local AA scheduled
task was unregistered.

## Promo watcher (public airline sales)

`promo_watcher.py` watches **public** AA/JetBlue sale and promo announcements —
not member-only or subscriber offers — and sends a Telegram alert when a new
public promo or promo code appears.

- **Reliable cloud source:** Google News RSS for AA/JetBlue sale/promo coverage.
- **Official pages:** aa.com and jetblue.com deal pages are also probed, but
  they often return HTTP 403/406 from datacenter IPs; the watcher records them
  as `BLOCKED` and continues with the news feed.
- **Codes:** extracted from headlines/snippets; the alert explicitly says to
  verify the code and conditions on the airline site. Member/subscriber-only
  items are filtered out.
- **Dedupe:** `promo_seen` / `promo_sources` in `pop_sfo_prices.db`; the same
  code or title is not sent twice.
- **No fare scanning:** Google Flights remains the fare source.

```bash
python promo_watcher.py --dry-run   # fetch and print, no DB writes/Telegram
python promo_watcher.py             # write state and alert on new promos
```

## Project layout

| File | Purpose |
|---|---|
| `tracker_pop_sfo.py` | Scan, parse, validate, persist; CLI entry point |
| `guard_daily_run.py` | 18 h / local-day idempotency, `scan_runs`, delivery trigger |
| `send_telegram_summary.py` | Top-5 digest + dashboard upload |
| `generate_pop_sfo_dashboard.py` | Interactive HTML dashboard |
| `cloudflare_worker.js` | Hosts the dashboard + secured Telegram webhook |
| `harness_reset.py` | `--wipe` / `--seed` / `--status` |
| `promo_watcher.py` | Public AA/JetBlue sale/promo watcher (Google News RSS) |
| `roundtrip_validator.py` | Google round-trip price validation for the top 10 pairs |
| `.github/workflows/monitor.yml` | Scheduled scan, round-trip validation, promo watch, persistence before delivery |
| `test_tracker_pop_sfo.py` | Python test suite |
| `tests/test_worker.mjs` | Cloudflare Worker security tests |

## Quick start

```bash
python harness_reset.py --wipe          # clean DB + delivery table
python harness_reset.py --seed          # 90 synthetic quotes for offline checks
python harness_reset.py --status

python guard_daily_run.py               # idempotency decision
# Expected after --seed:
#   should_run_scan=false
#   should_send_telegram=true
#   should_run=true

python -m unittest discover -s . -v     # Python tests
node tests/test_worker.mjs              # Worker tests
python promo_watcher.py --dry-run       # public AA/JetBlue promo preview
python roundtrip_validator.py --top 10  # actual Google round-trip prices (Chrome)
```

Production scan (writes real data and may send Telegram):

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python tracker_pop_sfo.py --step-days 1 --threshold 700
python send_telegram_summary.py
```

`harness_reset.py --wipe` before handing the environment over to production.

## Live dashboard

Public dashboard (GitHub Pages, regenerated by every scan):

https://deorocosta-afk.github.io/pop-sfo-monitor/pop_sfo_dashboard.html

## CI

`.github/workflows/monitor.yml` runs five morning crons (07:17–09:17
America/La_Paz) and one
`workflow_dispatch` (optional `force`). Only the first run of the day scans;
the rest are skipped by the guard unless a digest is undelivered. After the
scan, `promo_watcher.py` checks public AA/JetBlue sale news and sends a Telegram
alert only when a new public promo/code appears (`continue-on-error`).
`roundtrip_validator.py` then checks the actual Google round-trip price for the
top 10 date pairs (Playwright + Chrome under xvfb, `continue-on-error`).

Required repository secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional repository variables:

- `DASHBOARD_URL` — full public dashboard URL used in the Telegram digest
  (GitHub Pages: `https://deorocosta-afk.github.io/pop-sfo-monitor/pop_sfo_dashboard.html`);
- `WORKER_URL` — legacy Cloudflare Worker base URL (optional).

Database (`pop_sfo_prices.db`) and dashboard (`pop_sfo_dashboard.html`) are
committed by the workflow **before** Telegram, so a Telegram failure cannot
lose scan results. Delivery state is committed **after** Telegram.

## Cloudflare Worker

Environment/secrets:

| Name | Required | Meaning |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token (also used for sendMessage) |
| `GITHUB_TOKEN` | yes | PAT with Actions write access |
| `TELEGRAM_WEBHOOK_SECRET` | yes | Must match `setWebhook(secret_token=...)`; 403 otherwise |
| `ALLOWED_CHAT_IDS` | yes | Comma-separated chat IDs; **deny-by-default** |
| `GITHUB_REPO` | no | `owner/pop-sfo-monitor` |
| `WORKER_URL` | no | Public Worker URL |

The webhook validates the secret **before** parsing the update, checks both
`chat.id` and `from.id`, and returns HTTP 200 for refusals so Telegram does not
retry. Only `/` (portal) and `/sfo` (dashboard) are served.

## Invariants

Do not change without a new acceptance round:

- exactly 1 stop: `len(segments) == 2`;
- carriers: AA, B6, UA, CM only, word-boundary matching;
- date window 2027-05-20 … 2027-07-05, stays 14–30;
- layover 60–720 min, total ≤ 1440 min;
- atomic `save_scan_data_and_status` (`BEGIN IMMEDIATE`);
- `complete` only with coverage ≥ 85% and ≥ 80 quotes;
- mandatory webhook secret (403 by default);
- `_SCAN_LOCK.acquire(blocking=False)`;
- honest fare labels, no fabricated tax splits.
