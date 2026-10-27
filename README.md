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

## Project layout

| File | Purpose |
|---|---|
| `tracker_pop_sfo.py` | Scan, parse, validate, persist; CLI entry point |
| `guard_daily_run.py` | 18 h idempotency, `scan_runs`, delivery trigger |
| `send_telegram_summary.py` | Top-5 digest + dashboard upload |
| `generate_pop_sfo_dashboard.py` | Interactive HTML dashboard |
| `cloudflare_worker.js` | Hosts the dashboard + secured Telegram webhook |
| `harness_reset.py` | `--wipe` / `--seed` / `--status` |
| `.github/workflows/monitor.yml` | Scheduled scan, persistence before delivery |
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
```

Production scan (writes real data and may send Telegram):

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
python tracker_pop_sfo.py --step-days 1 --threshold 700
python send_telegram_summary.py
```

`harness_reset.py --wipe` before handing the environment over to production.

## CI

`.github/workflows/monitor.yml` runs five off-peak crons per day and one
`workflow_dispatch` (optional `force`). Only the first run of the day scans;
the rest are skipped by the guard unless a digest is undelivered.

Required repository secrets:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Optional repository variable:

- `WORKER_URL` — used in the digest dashboard link.

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
