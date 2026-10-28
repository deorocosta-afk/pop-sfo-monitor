#!/usr/bin/env python3
"""
Top-N round-trip price validation for Google Flights.

The main scanner sums two one-way legs (a discovery estimate). This module
opens Google Flights' round-trip search for the cheapest date pairs and reads
the actual "from $X" round-trip price shown by Google. Results are stored in
`roundtrip_checks` and used by the digest/dashboard for those pairs.

Usage:
    python roundtrip_validator.py --top 10
    python roundtrip_validator.py --top 2 --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from playwright.sync_api import sync_playwright

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "pop_sfo_prices.db"


def parse_roundtrip_price(text: str) -> Optional[float]:
    """Extract the cheapest round-trip price from Google Flights page text."""
    match = re.search(r"from\s*\$([\d,]+)", text, re.I)
    if match:
        return float(match.group(1).replace(",", ""))
    match = re.search(r"Cheapest.*?\$([\d,]+)", text, re.I | re.S)
    if match:
        return float(match.group(1).replace(",", ""))
    # Fallback: prices after the results heading; take the minimum.
    tail = text.split("Search results", 1)[1] if "Search results" in text else text
    prices = [float(p.replace(",", "")) for p in re.findall(r"\$([\d,]+)", tail)]
    return min(prices) if prices else None


def top_pairs(db_path: Path, limit: int) -> List[Tuple[str, str, float]]:
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT date, return_date, MIN(total_price) AS p,
                   CAST(julianday(return_date) - julianday(date) AS INTEGER) AS s
            FROM daily_prices
            WHERE timestamp = (SELECT MAX(timestamp) FROM daily_prices)
              AND return_date IS NOT NULL
            GROUP BY date, return_date
            ORDER BY p ASC, s ASC, date ASC
            LIMIT ?
        """, (limit,))
        return [(str(r[0]), str(r[1]), float(r[2])) for r in cur.fetchall()]
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS roundtrip_checks (
                timestamp TEXT NOT NULL,
                dep_date TEXT NOT NULL,
                ret_date TEXT NOT NULL,
                sum_price REAL,
                bookable_price REAL,
                url TEXT,
                note TEXT,
                PRIMARY KEY (timestamp, dep_date, ret_date)
            )
        """)
        conn.commit()
    finally:
        conn.close()


def save_checks(db_path: Path, timestamp: str, checks: List[dict]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        init_db(db_path)
        for c in checks:
            conn.execute("""
                INSERT OR REPLACE INTO roundtrip_checks
                    (timestamp, dep_date, ret_date, sum_price, bookable_price, url, note)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (timestamp, c["dep_date"], c["ret_date"], c["sum_price"],
                  c.get("bookable_price"), c.get("url"), c.get("note")))
        conn.commit()
    finally:
        conn.close()


def search_url(dep: str, ret: str) -> str:
    q = urllib.parse.quote(
        f"Flights from POP to SFO on {dep} through {ret} with 1 stop"
    )
    return f"https://www.google.com/travel/flights?q={q}&curr=USD&hl=en"


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate Google round-trip prices for top pairs")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db)
    pairs = top_pairs(db_path, max(1, args.top))
    if not pairs:
        print("[RT] No scan data")
        return

    timestamp = datetime.now(timezone.utc).isoformat()
    checks: List[dict] = []
    print(f"[RT] Checking {len(pairs)} date pairs on Google Flights")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
        )
        context = browser.new_context(locale="en-US", viewport={"width": 1366, "height": 900})
        page = context.new_page()
        for dep, ret, sum_price in pairs:
            url = search_url(dep, ret)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(12000)
                text = page.inner_text("body")
                price = parse_roundtrip_price(text)
                status = "OK" if price is not None else "EMPTY"
                print(f"[RT] {dep} -> {ret}: sum ${sum_price:.0f}, roundtrip "
                      + (f"${price:.0f}" if price is not None else "not found"))
                checks.append({
                    "dep_date": dep,
                    "ret_date": ret,
                    "sum_price": sum_price,
                    "bookable_price": price,
                    "url": url,
                    "note": status,
                })
            except Exception as e:
                print(f"[RT] {dep} -> {ret}: ERROR {type(e).__name__}: {e}")
                checks.append({
                    "dep_date": dep,
                    "ret_date": ret,
                    "sum_price": sum_price,
                    "bookable_price": None,
                    "url": url,
                    "note": f"ERROR {type(e).__name__}",
                })
        browser.close()

    if not args.dry_run:
        save_checks(db_path, timestamp, checks)
        print(f"[RT] Saved {len(checks)} checks to {db_path.name}")


if __name__ == "__main__":
    main()
