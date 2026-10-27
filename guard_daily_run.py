#!/usr/bin/env python3
"""
guard_daily_run.py
Idempotency guard for the standalone POP <-> SFO flight monitor.

Checks:
1. Whether the latest POP-SFO scan is complete, fresh (< 18 hours) and has
   at least 80 quotes.
2. Whether the completed scan has an undelivered Telegram digest
   (telegram_delivery_state).

Outputs (GITHUB_OUTPUT):
- should_run_scan: "true" if the scan is missing, incomplete, stale or too small.
- should_send_telegram: "true" if a scan is about to run OR a completed scan is undelivered.
- should_run: "true" if either action is needed.
"""

from __future__ import annotations

import datetime
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "pop_sfo_prices.db"
SHARED_DB_PATH = DB_PATH  # backwards-compatible alias for tests
MIN_REQUIRED_QUOTES = 80
FRESH_HOURS = 18.0


def parse_utc(value: Optional[str]) -> Optional[datetime.datetime]:
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def read_latest_scan_run(db_file: str = DB_PATH.name) -> Optional[Dict[str, Any]]:
    """Returns the latest scan run from scan_runs table if it exists."""
    path = BASE_DIR / db_file
    if not path.exists():
        return None
    conn = None
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='scan_runs'")
        if not c.fetchone():
            return None
        c.execute("SELECT * FROM scan_runs ORDER BY timestamp DESC LIMIT 1")
        row = c.fetchone()
        return dict(row) if row else None
    except Exception as e:
        print(f"[CHECK] Could not query scan_runs in {db_file}: {e}")
        return None
    finally:
        if conn:
            conn.close()


def read_latest_scan_info(db_file: str = DB_PATH.name) -> Tuple[Optional[datetime.datetime], Optional[str], int]:
    """Fallback for databases without scan_runs: returns (dt, timestamp, quote_count)."""
    path = BASE_DIR / db_file
    if not path.exists():
        return None, None, 0

    conn = None
    try:
        conn = sqlite3.connect(path)
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='flight_quotes'")
        if c.fetchone():
            c.execute("SELECT max(timestamp) FROM flight_quotes")
            row = c.fetchone()
            if row and row[0]:
                ts_str = row[0]
                c.execute("SELECT count(*) FROM flight_quotes WHERE timestamp = ?", (ts_str,))
                cnt_row = c.fetchone()
                return parse_utc(ts_str), ts_str, (cnt_row[0] if cnt_row else 0)

        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_prices'")
        if c.fetchone():
            c.execute("SELECT max(timestamp) FROM daily_prices")
            row = c.fetchone()
            if row and row[0]:
                ts_str = row[0]
                c.execute("SELECT count(*) FROM daily_prices WHERE timestamp = ?", (ts_str,))
                cnt_row = c.fetchone()
                return parse_utc(ts_str), ts_str, (cnt_row[0] if cnt_row else 0)
    except Exception as e:
        print(f"[CHECK] Could not query {db_file}: {e}")
    finally:
        if conn:
            conn.close()

    return None, None, 0


def is_route_delivered(route_id: str, scan_ts: str) -> bool:
    """Checks the granular delivery flags in pop_sfo_prices.db."""
    if not SHARED_DB_PATH.exists() or not scan_ts:
        return False
    conn = None
    try:
        conn = sqlite3.connect(SHARED_DB_PATH)
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='telegram_delivery_state'")
        if not c.fetchone():
            return False
        c.execute("PRAGMA table_info(telegram_delivery_state)")
        cols = [r[1] for r in c.fetchall()]
        if "message_delivered" in cols and "document_delivered" in cols:
            c.execute(
                "SELECT message_delivered, document_delivered FROM telegram_delivery_state "
                "WHERE route_id = ? AND scan_timestamp = ?",
                (route_id, scan_ts),
            )
            row = c.fetchone()
            if row:
                return bool(row[0]) and bool(row[1])
            return False
        c.execute(
            "SELECT 1 FROM telegram_delivery_state WHERE route_id = ? AND scan_timestamp = ?",
            (route_id, scan_ts),
        )
        return c.fetchone() is not None
    except Exception as e:
        print(f"[CHECK] Could not query delivery state: {e}")
        return False
    finally:
        if conn:
            conn.close()


def set_github_output(name: str, value: str) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a", encoding="utf-8") as f:
            f.write(f"{name}={value}\n")
    print(f"[OUTPUT] {name}={value}")


def check_and_decide() -> None:
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    force_run = os.environ.get("INPUT_FORCE", "").strip().lower() in ("true", "1", "yes")

    if event_name == "workflow_dispatch" and force_run:
        print("[CHECK] Triggered manually with force=true -> FORCING SCAN.")
        set_github_output("should_run_scan", "true")
        set_github_output("should_send_telegram", "true")
        set_github_output("should_run", "true")
        return

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    scans_fresh = True
    undelivered = False

    scan_run = read_latest_scan_run()
    if scan_run:
        status = scan_run.get("status", "")
        ts_str = scan_run.get("timestamp")
        dt = parse_utc(ts_str)
        total_quotes = scan_run.get("total_quotes", 0)
        completed_pairs = scan_run.get("completed_pairs", 0)
        expected_pairs = scan_run.get("expected_pairs", 0)

        if status != "complete":
            print(
                f"[CHECK] Latest scan is NOT complete (status='{status}', "
                f"{completed_pairs}/{expected_pairs} pairs). Scan will run."
            )
            scans_fresh = False
        elif not dt or not ts_str:
            print("[CHECK] Invalid timestamp in scan_runs.")
            scans_fresh = False
        else:
            hours_ago = (now_utc - dt).total_seconds() / 3600.0
            if hours_ago >= FRESH_HOURS:
                print(f"[CHECK] Scan is stale ({hours_ago:.1f}h ago >= {FRESH_HOURS:.0f}h).")
                scans_fresh = False
            elif total_quotes < MIN_REQUIRED_QUOTES:
                print(
                    f"[CHECK] Scan has too few quotes "
                    f"({total_quotes} < {MIN_REQUIRED_QUOTES} required)."
                )
                scans_fresh = False
            else:
                print(
                    f"[CHECK] Fresh & complete ({total_quotes} quotes, "
                    f"{completed_pairs}/{expected_pairs} pairs, {hours_ago:.1f}h ago)."
                )
            if ts_str and not is_route_delivered("sfo", ts_str):
                undelivered = True
    else:
        dt, ts_str, count = read_latest_scan_info()
        if not dt or not ts_str:
            print("[CHECK] No scan found.")
            scans_fresh = False
        else:
            hours_ago = (now_utc - dt).total_seconds() / 3600.0
            if hours_ago >= FRESH_HOURS:
                print(f"[CHECK] Scan is stale ({hours_ago:.1f}h ago >= {FRESH_HOURS:.0f}h).")
                scans_fresh = False
            elif count < MIN_REQUIRED_QUOTES:
                print(f"[CHECK] Scan is incomplete ({count} quotes < {MIN_REQUIRED_QUOTES} required).")
                scans_fresh = False
            else:
                print(f"[CHECK] Fresh & complete ({count} quotes, {hours_ago:.1f}h ago).")
            if ts_str and not is_route_delivered("sfo", ts_str):
                undelivered = True

    should_run_scan = not scans_fresh
    should_send_telegram = should_run_scan or undelivered
    should_run = should_run_scan or should_send_telegram

    if undelivered:
        print("[CHECK] Undelivered digest found for POP-SFO. Telegram will run.")

    set_github_output("should_run_scan", "true" if should_run_scan else "false")
    set_github_output("should_send_telegram", "true" if should_send_telegram else "false")
    set_github_output("should_run", "true" if should_run else "false")


if __name__ == "__main__":
    check_and_decide()
