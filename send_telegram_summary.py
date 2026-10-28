#!/usr/bin/env python3
"""
Send the POP <-> SFO digest and the HTML dashboard to Telegram.

Standalone POP <-> SFO project:
- Top-5 distinct date pairs from the latest POP-SFO scan.
- Granular delivery tracking in SQLite:
  message_delivered / document_delivered.
- Token masking in all logs and exception messages.
- Honest multi-carrier labeling (no fabricated tax breakdowns).
- 429 rate-limit backoff.
"""

from __future__ import annotations

import html
import logging
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).resolve().parent
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CID = os.environ.get("TELEGRAM_CHAT_ID")
BASE_URL = os.environ.get("WORKER_URL", "https://your-worker.workers.dev")
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "")
SHARED_DB_PATH = BASE_DIR / "pop_sfo_prices.db"
DASHBOARD_PATH = BASE_DIR / "pop_sfo_dashboard.html"
ROUTE_ID = "sfo"
THRESHOLD_USD = 700.0


def sanitize_log(msg: str) -> str:
    """Mask Telegram bot tokens in logs and error strings."""
    if not msg:
        return ""
    return re.sub(r"bot[0-9]+:[a-zA-Z0-9_-]+", "bot<REDACTED>", str(msg))


def escape_html_attr(url: str) -> str:
    """Escape a URL for HTML attributes in Telegram parse_mode='HTML'."""
    if not url:
        return ""
    return url.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def init_delivery_table(db_path: Path = SHARED_DB_PATH) -> None:
    """Ensures telegram_delivery_state exists with granular status tracking."""
    conn = None
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_delivery_state (
                route_id TEXT NOT NULL,
                scan_timestamp TEXT NOT NULL,
                message_delivered INTEGER DEFAULT 0,
                document_delivered INTEGER DEFAULT 0,
                delivered_at TEXT NOT NULL,
                PRIMARY KEY(route_id, scan_timestamp)
            )
        """)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(telegram_delivery_state)")
        cols = [r[1] for r in cur.fetchall()]
        if "message_delivered" not in cols:
            cur.execute("ALTER TABLE telegram_delivery_state ADD COLUMN message_delivered INTEGER DEFAULT 1")
        if "document_delivered" not in cols:
            cur.execute("ALTER TABLE telegram_delivery_state ADD COLUMN document_delivered INTEGER DEFAULT 1")
        conn.commit()
    except Exception as e:
        logging.warning(f"Could not init delivery table in {db_path}: {sanitize_log(str(e))}")
    finally:
        if conn:
            conn.close()


def get_delivery_status(route_id: str, scan_ts: str, db_path: Path = SHARED_DB_PATH) -> Tuple[bool, bool]:
    """Returns (message_delivered, document_delivered)."""
    if not db_path.exists() or not scan_ts:
        return False, False
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='telegram_delivery_state'")
        if not cur.fetchone():
            return False, False
        cur.execute("PRAGMA table_info(telegram_delivery_state)")
        cols = [r[1] for r in cur.fetchall()]
        if "message_delivered" in cols and "document_delivered" in cols:
            cur.execute(
                "SELECT message_delivered, document_delivered FROM telegram_delivery_state "
                "WHERE route_id = ? AND scan_timestamp = ?",
                (route_id, scan_ts),
            )
            row = cur.fetchone()
            if row:
                return bool(row[0]), bool(row[1])
            return False, False
        cur.execute(
            "SELECT 1 FROM telegram_delivery_state WHERE route_id = ? AND scan_timestamp = ?",
            (route_id, scan_ts),
        )
        exists = cur.fetchone() is not None
        return exists, exists
    except Exception as e:
        logging.warning(f"Error checking delivery status: {sanitize_log(str(e))}")
        return False, False
    finally:
        if conn:
            conn.close()


def record_delivery_progress(
    route_id: str,
    scan_ts: str,
    message_delivered: Optional[bool] = None,
    document_delivered: Optional[bool] = None,
    db_path: Path = SHARED_DB_PATH,
) -> None:
    """Updates delivery progress flags atomically."""
    if not scan_ts:
        return
    conn = None
    try:
        init_delivery_table(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.cursor()
        cur.execute(
            "SELECT message_delivered, document_delivered FROM telegram_delivery_state "
            "WHERE route_id = ? AND scan_timestamp = ?",
            (route_id, scan_ts),
        )
        row = cur.fetchone()
        cur_msg = row[0] if row else 0
        cur_doc = row[1] if row else 0

        new_msg = int(message_delivered) if message_delivered is not None else cur_msg
        new_doc = int(document_delivered) if document_delivered is not None else cur_doc

        cur.execute("""
            INSERT INTO telegram_delivery_state
                (route_id, scan_timestamp, message_delivered, document_delivered, delivered_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(route_id, scan_timestamp) DO UPDATE SET
                message_delivered = excluded.message_delivered,
                document_delivered = excluded.document_delivered,
                delivered_at = excluded.delivered_at
        """, (route_id, scan_ts, new_msg, new_doc))
        conn.commit()
    except Exception as e:
        logging.warning(f"Could not record delivery progress: {sanitize_log(str(e))}")
    finally:
        if conn:
            conn.close()


def is_already_delivered(route_id: str, scan_ts: str, db_path: Path = SHARED_DB_PATH) -> bool:
    msg_del, doc_del = get_delivery_status(route_id, scan_ts, db_path)
    return msg_del and doc_del


def mark_as_delivered(route_id: str, scan_ts: str, db_path: Path = SHARED_DB_PATH) -> None:
    record_delivery_progress(
        route_id, scan_ts, message_delivered=True, document_delivered=True, db_path=db_path
    )


def send_msg(text: str) -> bool:
    if not TOKEN or not CID:
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=20)
            if r.status_code == 200:
                return True
            if r.status_code == 429:
                retry_after = int(r.json().get("parameters", {}).get("retry_after", 3))
                logging.warning(f"Telegram 429 rate limit hit. Sleeping {retry_after}s...")
                time.sleep(retry_after)
            else:
                logging.error(f"Error sending Telegram message: {r.status_code} {sanitize_log(r.text)}")
                time.sleep(2)
        except Exception as e:
            logging.error(f"Failed to send Telegram message (attempt {attempt + 1}): {sanitize_log(str(e))}")
            time.sleep(2)
    return False


def send_doc(filepath: Path, caption: str) -> bool:
    if not TOKEN or not CID or not filepath.exists():
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendDocument"
    for attempt in range(3):
        try:
            with open(filepath, "rb") as f:
                r = requests.post(
                    url,
                    data={"chat_id": CID, "caption": caption, "parse_mode": "HTML"},
                    files={"document": (filepath.name, f, "text/html")},
                    timeout=35,
                )
                if r.status_code == 200:
                    return True
                if r.status_code == 429:
                    retry_after = int(r.json().get("parameters", {}).get("retry_after", 3))
                    logging.warning(f"Telegram 429 rate limit hit on doc upload. Sleeping {retry_after}s...")
                    time.sleep(retry_after)
                else:
                    logging.error(f"Error sending document: {r.status_code} {sanitize_log(r.text)}")
                    time.sleep(2)
        except Exception as e:
            logging.error(f"Failed to send document (attempt {attempt + 1}): {sanitize_log(str(e))}")
            time.sleep(2)
    return False


def format_nights(n: int) -> str:
    """Russian pluralization for nights."""
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} ночь"
    if 2 <= n % 10 <= 4 and (n % 100 < 10 or n % 100 >= 20):
        return f"{n} ночи"
    return f"{n} ночей"


def format_sfo_deals(
    db_file: str = SHARED_DB_PATH.name,
    threshold: float = THRESHOLD_USD,
) -> Tuple[str, Optional[str]]:
    """
    Reads the latest POP-SFO scan and formats the Telegram digest.
    Returns (message, latest_timestamp) or ("", None/ts) when there is no data.
    """
    db_path = BASE_DIR / db_file
    if not db_path.exists():
        return "", None

    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='flight_quotes'")
        if not cur.fetchone():
            return "", None

        cur.execute(
            "SELECT max(timestamp) FROM flight_quotes WHERE origin = ? AND dest = ?",
            ("POP", "SFO"),
        )
        row = cur.fetchone()
        latest_ts = row[0] if row else None
        if not latest_ts:
            return "", None

        cur.execute("""
            SELECT dep_date, ret_date, stay_days, price_usd,
                   outbound_flight, inbound_flight, stops_desc, booking_url
            FROM flight_quotes
            WHERE origin = ? AND dest = ? AND ret_date IS NOT NULL AND timestamp = ?
            ORDER BY price_usd ASC, stay_days ASC, dep_date ASC
        """, ("POP", "SFO", latest_ts))
        raw_rows = cur.fetchall()
    finally:
        if conn:
            conn.close()

    if not raw_rows:
        return "", latest_ts

    seen_pairs = set()
    distinct_rows = []
    for r in raw_rows:
        pair_key = (r[0], r[1])
        if pair_key not in seen_pairs:
            seen_pairs.add(pair_key)
            distinct_rows.append(r)
            if len(distinct_rows) >= 5:
                break

    msg = "🇺🇸 <b>ТОП РЕЙСОВ: POP ⇄ SFO (1 пересадка)</b>\n\n"
    msg += "ℹ️ Итог — сумма one-way плеч; точная цена и fare class — по ссылке Google Flights.\n\n"
    for dep, ret, stay, price, fl_out, fl_in, stops_desc, url in distinct_rows:
        marker = "🔥" if (price is not None and price <= threshold) else "✈️"
        price_val = f"${price:.2f}" if price is not None else "Н/Д"
        flight_str = f"{fl_out} + {fl_in}" if (fl_out and fl_in) else "AA / B6 / UA / CM"
        nights_str = format_nights(stay if stay is not None else 0)

        is_mixed = "отдельных билета" in str(stops_desc) or "2 билета" in str(stops_desc)
        if not is_mixed and fl_out and fl_in:
            c_out = re.findall(r"\b(AA|B6|UA|CM)\b", str(fl_out))
            c_in = re.findall(r"\b(AA|B6|UA|CM)\b", str(fl_in))
            if c_out and c_in and c_out[0] != c_in[0]:
                is_mixed = True
        fare_note = " (2 отдельных билета)" if is_mixed else " (1 авиакомпания, сумма плеч)"

        msg += f"{marker} <b>{html.escape(str(dep))} ➔ {html.escape(str(ret))}</b> ({nights_str})\n"
        msg += f"   • Цена: <b>{price_val}</b>{fare_note}\n"
        msg += f"   • Рейсы: {html.escape(str(flight_str))} ({html.escape(str(stops_desc or '1 пересадка'))})\n"
        if url:
            msg += f'   • <a href="{escape_html_attr(str(url))}">🔗 Поиск рейсов на Google Flights</a>\n'
        msg += "\n"

    dashboard_link = DASHBOARD_URL or f"{BASE_URL}/sfo"
    msg += f'📊 <a href="{escape_html_attr(dashboard_link)}">Открыть интерактивный дашборд</a>\n'
    return msg, latest_ts


def main() -> None:
    if not TOKEN or not CID:
        logging.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not provided.")
        sys.exit(1)

    init_delivery_table(SHARED_DB_PATH)

    msg, ts = format_sfo_deals()
    if not msg or not ts:
        logging.info("No POP-SFO scan data to deliver.")
        return

    msg_delivered, doc_delivered = get_delivery_status(ROUTE_ID, ts, SHARED_DB_PATH)
    doc_exists = DASHBOARD_PATH.exists()

    if msg_delivered and (doc_delivered or not doc_exists):
        logging.info(f"Digest for scan {ts} is already delivered. Skipping.")
        if not doc_exists and not doc_delivered:
            # A missing dashboard must not leave the delivery state permanently open.
            record_delivery_progress(ROUTE_ID, ts, document_delivered=True)
        return

    failures = 0

    msg_ok = msg_delivered
    if not msg_delivered:
        logging.info(f"Sending POP-SFO text digest (scan {ts})...")
        msg_ok = send_msg(msg)
        if msg_ok:
            record_delivery_progress(ROUTE_ID, ts, message_delivered=True)
            logging.info("Text digest recorded in telegram_delivery_state.")
        else:
            logging.error("Failed to send the text digest.")
            failures += 1

    doc_ok = doc_delivered
    if doc_exists and not doc_delivered and msg_ok:
        time.sleep(1.0)
        doc_ok = send_doc(DASHBOARD_PATH, "🇺🇸 Дашборд POP ⇄ SFO в файле")
        if doc_ok:
            record_delivery_progress(ROUTE_ID, ts, document_delivered=True)
            logging.info("HTML dashboard delivery recorded.")
        else:
            logging.error("Failed to upload the HTML dashboard.")
            failures += 1
    elif not doc_exists and not doc_delivered:
        # No file to send: mark the document step satisfied so Guard can close the scan.
        record_delivery_progress(ROUTE_ID, ts, document_delivered=True)
        logging.info("Dashboard file is absent; document delivery marked as satisfied.")
        doc_ok = True

    if failures > 0:
        logging.error(f"Failed to deliver {failures} Telegram item(s).")
        sys.exit(1)

    logging.info("POP-SFO digest and dashboard processed successfully.")


if __name__ == "__main__":
    main()
