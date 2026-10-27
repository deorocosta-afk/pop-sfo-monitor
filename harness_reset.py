#!/usr/bin/env python3
"""
harness_reset.py
Operational harness for the standalone POP <-> SFO Flight Monitor.

1. --wipe:   deletes the runtime DB, leg cache and dashboard, then creates a
             pristine SQLite schema.
2. --seed:   wipes and injects 90 synthetic POP-SFO quotes derived from the
             authoritative 425-pair date matrix, a 'complete' scan_runs row and
             the HTML dashboard — no network access required.
3. --status: prints DB integrity, table row counts, the latest scan_runs row and
             Telegram delivery flags.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
DB_SFO = BASE_DIR / "pop_sfo_prices.db"
CACHE_LEG = BASE_DIR / ".cache_pop_sfo_legs.json"
DASHBOARD = BASE_DIR / "pop_sfo_dashboard.html"

MIN_STAY = 14
MAX_STAY = 30
WINDOW_START = date(2027, 5, 20)
WINDOW_END = date(2027, 7, 5)


def build_authoritative_pairs() -> list[tuple[date, date, int]]:
    """The same 425-pair matrix as the production tracker (May 20 - July 5, 14..30 nights)."""
    pairs: list[tuple[date, date, int]] = []
    dep = WINDOW_START
    while dep <= WINDOW_END:
        for stay in range(MIN_STAY, MAX_STAY + 1):
            ret = dep + timedelta(days=stay)
            if ret <= WINDOW_END:
                pairs.append((dep, ret, stay))
        dep += timedelta(days=1)
    return pairs


def wipe_environment() -> None:
    """Delete runtime DB/cache/dashboard and recreate a pristine schema."""
    print("🧹 [HARNESS WIPE] Cleaning runtime database, cache and dashboard...")

    for p in (DB_SFO, CACHE_LEG, DASHBOARD):
        if p.exists():
            try:
                p.unlink()
                print(f"  ✓ Deleted: {p.name}")
            except Exception as e:
                print(f"  ⚠️ Error deleting {p.name}: {e}")

    from tracker_pop_sfo import init_database
    init_database(DB_SFO)
    print(f"  ✓ Initialized pristine schema in {DB_SFO.name}")

    from send_telegram_summary import init_delivery_table
    init_delivery_table(DB_SFO)
    print(f"  ✓ Initialized telegram_delivery_state in {DB_SFO.name}")
    print("✅ Environment is clean and ready for deployment/benchmarking.")


def seed_environment() -> None:
    """Inject 90 valid synthetic quotes and a complete scan_runs row, then export the dashboard."""
    print("🌱 [HARNESS SEED] Seeding synthetic POP-SFO baseline...")
    wipe_environment()

    all_pairs = build_authoritative_pairs()
    if len(all_pairs) != 425:
        raise RuntimeError(f"Expected 425 authoritative pairs, got {len(all_pairs)}")

    now_iso = datetime.now(timezone.utc).isoformat()
    carriers = ["AA", "B6", "UA", "CM"]

    quotes_rows = []
    daily_rows = []
    for i in range(90):
        dep_d, ret_d, stay = all_pairs[int(i * len(all_pairs) / 90)]
        dep_date = dep_d.isoformat()
        ret_date = ret_d.isoformat()

        c1 = carriers[i % len(carriers)]
        c2 = carriers[(i + 1) % len(carriers)] if i % 3 == 0 else c1
        price = round(450.0 + i * 2.5, 2)
        is_deal = 1 if price <= 700.0 else 0
        is_mixed = c1 != c2
        fare_desc = "2 отдельных билета" if is_mixed else "1 авиакомпания (сумма плеч)"
        stops_desc = f"1 пересадка (MIA / MIA) • {fare_desc}"
        fl_out = f"{c1} 10{i}➔{c1} 20{i} (MIA)"
        fl_in = f"{c2} 30{i}➔{c2} 40{i} (MIA)"
        url = f"https://www.google.com/travel/flights?q=Flights+POP+to+SFO+{dep_date}"

        quotes_rows.append((
            now_iso, "POP", "SFO", dep_date, ret_date, stay,
            price, None, None, fl_out, fl_in, stops_desc, is_deal, url,
        ))
        daily_rows.append((
            now_iso, "POP", "SFO", dep_date, ret_date, "round-trip",
            None, None, price, is_deal, "8ч 45м / 9ч 15м",
            f"{fl_out} / {fl_in}", url,
        ))

    conn = None
    try:
        conn = sqlite3.connect(DB_SFO)
        cur = conn.cursor()
        cur.executemany("""
            INSERT INTO flight_quotes (
                timestamp, origin, dest, dep_date, ret_date, stay_days,
                price_usd, base_fare, taxes, outbound_flight, inbound_flight,
                stops_desc, is_deal, booking_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, quotes_rows)
        cur.executemany("""
            INSERT INTO daily_prices (
                timestamp, origin, destination, date, return_date, trip_type,
                base_fare, taxes, total_price, is_promo, duration_str, flight_no, booking_url
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, daily_rows)
        cur.execute("""
            INSERT INTO scan_runs (
                run_id, route_id, timestamp, status, expected_pairs,
                attempted_pairs, completed_pairs, total_quotes, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            f"seed_{int(datetime.now().timestamp())}", "sfo", now_iso, "complete",
            425, 425, 425, 90, None,
        ))
        conn.commit()
        print(f"  ✓ Injected 90 POP-SFO quotes and a 'complete' scan_run into {DB_SFO.name}")
    finally:
        if conn:
            conn.close()

    try:
        from generate_pop_sfo_dashboard import export_dashboard
        dash_path = export_dashboard(open_in_browser=False)
        print(f"  ✓ Exported HTML dashboard: {dash_path.name}")
    except Exception as e:
        print(f"  ⚠️ Error exporting dashboard: {e}")

    print("✅ Environment seeded with offline benchmark fixtures.")


def print_status() -> None:
    """Print DB tables, row counts, latest scan_runs row and delivery state."""
    print("📊 [HARNESS STATUS]")
    print(f"\n📂 Main DB: {DB_SFO.name} ({'EXISTS' if DB_SFO.exists() else 'MISSING'})")
    if DB_SFO.exists():
        conn = None
        try:
            conn = sqlite3.connect(DB_SFO)
            c = conn.cursor()
            c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            tables = [r[0] for r in c.fetchall()]
            print(f"   Tables: {', '.join(tables) if tables else 'None'}")
            for t in tables:
                c.execute(f"SELECT count(*) FROM {t}")
                print(f"     • {t}: {c.fetchone()[0]} rows")

            if "scan_runs" in tables:
                c.execute(
                    "SELECT run_id, timestamp, status, completed_pairs, total_quotes "
                    "FROM scan_runs ORDER BY timestamp DESC LIMIT 3"
                )
                print("   Latest scan_runs:")
                for r in c.fetchall():
                    print(f"     -> {r[0]} | {r[1]} | status={r[2]} | completed={r[3]} | quotes={r[4]}")

            if "telegram_delivery_state" in tables:
                c.execute(
                    "SELECT route_id, scan_timestamp, message_delivered, document_delivered "
                    "FROM telegram_delivery_state"
                )
                print("   Delivery state:")
                for r in c.fetchall():
                    print(f"     -> route={r[0]} | ts={r[1]} | msg={r[2]} | doc={r[3]}")
        finally:
            if conn:
                conn.close()

    print(f"\n📁 Leg cache: {CACHE_LEG.name} ({'EXISTS' if CACHE_LEG.exists() else 'MISSING'})")
    print(f"📄 Dashboard: {DASHBOARD.name} ({'EXISTS' if DASHBOARD.exists() else 'MISSING'})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Harness for the standalone POP-SFO Flight Monitor")
    parser.add_argument("--wipe", action="store_true", help="Delete DB/cache/dashboard and create a clean schema")
    parser.add_argument("--seed", action="store_true", help="Wipe and inject 90 synthetic quotes for offline tests")
    parser.add_argument("--status", action="store_true", help="Show table counts, scan_runs and delivery state")
    args = parser.parse_args()

    if args.wipe:
        wipe_environment()
    elif args.seed:
        seed_environment()
    elif args.status:
        print_status()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
