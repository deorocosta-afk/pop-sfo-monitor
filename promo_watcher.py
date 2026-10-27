#!/usr/bin/env python3
"""
Public promo watcher for the POP <-> SFO project.

Fare scanning is Google Flights only. This module watches *public* airline
sales/promo announcements (not subscriber-only / member-only fares) and sends a
Telegram alert when a new public sale or promo code appears.

Reliable cloud source: Google News RSS for AA/JetBlue sale/promo coverage.
Official aa.com / jetblue.com deal pages are also probed, but they are often
blocked by Akamai/406 from datacenter IPs; the status is recorded and the
watcher continues with the news feed.

Usage:
    python promo_watcher.py --dry-run
    python promo_watcher.py
"""

from __future__ import annotations

import argparse
import hashlib
import html
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "pop_sfo_prices.db"
REPORT_PATH = BASE_DIR / "promo_watcher_report.md"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

NEWS_SOURCES = [
    {
        "airline": "B6",
        "name": "Google News: JetBlue sale/promo",
        "url": "https://news.google.com/rss/search",
        "query": 'JetBlue ("promo code" OR "flight sale" OR "fare sale") '
                 '-Amex -Grubhub -DraftKings when:30d',
    },
    {
        "airline": "AA",
        "name": "Google News: American Airlines sale/promo",
        "url": "https://news.google.com/rss/search",
        "query": '"American Airlines" ("promo code" OR "flight sale" OR "fare sale") '
                 '-Alaska -Southwest -United -Delta -JetSMART when:30d',
    },
]

NEWSROOM_SOURCES = [
    {"airline": "AA", "name": "AA newsroom", "url": "https://news.aa.com/overview/default.aspx"},
    {"airline": "B6", "name": "JetBlue newsroom", "url": "https://news.jetblue.com/overview/default.aspx"},
]

OFFICIAL_SOURCES = [
    {"airline": "AA", "name": "AA flight deals", "url": "https://www.aa.com/en-us/flight-deals"},
    {"airline": "B6", "name": "JetBlue deals", "url": "https://www.jetblue.com/deals"},
]

BLOCK_CODES = {
    "CODE", "CODES", "PROMO", "PROMOS", "SALE", "DEAL", "DEALS", "FLIGHT", "FLIGHTS",
    "TRAVEL", "GOOGLE", "MEMBER", "MEMBERS", "ELITE", "SAVE", "DISCOUNT", "OFFER",
    "OFFERS", "COUPON", "COUPONS", "AIRLINE", "AIRLINES", "AMERICAN", "JETBLUE",
    "GOALS", "ABOUT", "MORE", "WITH", "FROM", "YOUR", "THIS", "THAT",
}


def strip_html(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def extract_codes(text: str) -> List[str]:
    """Extract public promo codes mentioned next to 'code'/'promo code'."""
    clean = strip_html(text)
    found: set[str] = set()

    for m in re.finditer(
        r"(?:promo(?:\s*code)?|coupon\s*code|use\s*code|discount\s*code|code)"
        r"\s*[:\-]?\s*[\"']?([A-Z0-9]{4,12})[\"']?",
        clean,
        re.I,
    ):
        code = m.group(1).upper()
        if code not in BLOCK_CODES and not code.isdigit():
            found.add(code)

    for m in re.finditer(r"[\"']([A-Z]{3,}[0-9]{2,}[A-Z0-9]*)[\"']", clean):
        code = m.group(1).upper()
        if code not in BLOCK_CODES:
            found.add(code)

    return sorted(found)


def parse_pubdate(value: str) -> Optional[datetime]:
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def is_public_sale(text: str, airline: str) -> bool:
    low = text.lower()
    positive = re.search(
        r"(promo\s*code|discount\s*code|coupon\s*code|flight\s*sale|fare\s*sale|"
        r"airfare\s*sale|flight\s*deal|fare\s*deal|airfare\s*deal|save\s+\$?\d+|"
        r"\d+%\s*off)",
        low,
    )
    if not positive:
        return False
    negative = re.search(
        r"(american airlines center|tickets?\s+on\s+sale|concert|nba|nhl|nfl|"
        r"mavericks|celtics|knicks|lakers|spurs|first class|bluefirst|launch|"
        r"new flights|data sale|airline changes|points and miles|credit card|"
        r"amex|grubhub|draftkings|savings account)",
        low,
    )
    if negative:
        return False
    if airline == "AA" and "american airlines" not in low:
        return False
    if airline == "B6" and "jetblue" not in low:
        return False
    if re.search(r"(member|subscriber|subscription|loyalty only|for members)", low):
        return False
    return True


def init_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_seen (
                key TEXT PRIMARY KEY,
                airline TEXT NOT NULL,
                code TEXT,
                title TEXT,
                url TEXT,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_sources (
                name TEXT PRIMARY KEY,
                airline TEXT,
                url TEXT,
                status TEXT,
                checked_at TEXT,
                note TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


def record_source(db_path: Path, source: Dict[str, Any], status: str, note: str = "") -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            INSERT INTO promo_sources (name, airline, url, status, checked_at, note)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                airline = excluded.airline,
                url = excluded.url,
                status = excluded.status,
                checked_at = excluded.checked_at,
                note = excluded.note
        """, (source["name"], source["airline"], source["url"], status,
              datetime.now(timezone.utc).isoformat(), note))
        conn.commit()
    finally:
        conn.close()


def save_new_items(db_path: Path, items: List[Dict[str, Any]], dry_run: bool) -> List[Dict[str, Any]]:
    """Insert unseen items and return only the new ones."""
    if dry_run:
        return items
    now_iso = datetime.now(timezone.utc).isoformat()
    new_items: List[Dict[str, Any]] = []
    conn = sqlite3.connect(db_path)
    try:
        for item in items:
            row = conn.execute("SELECT key FROM promo_seen WHERE key = ?", (item["key"],)).fetchone()
            if row:
                conn.execute("UPDATE promo_seen SET last_seen = ? WHERE key = ?", (now_iso, item["key"]))
                continue
            conn.execute("""
                INSERT INTO promo_seen (key, airline, code, title, url, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (item["key"], item["airline"], item.get("code"), item["title"],
                  item["url"], now_iso, now_iso))
            new_items.append(item)
        conn.commit()
    finally:
        conn.close()
    return new_items


def fetch_news(source: Dict[str, Any], max_age_days: int) -> Tuple[str, List[Dict[str, Any]], str]:
    params = {"q": source["query"], "hl": "en-US", "gl": "US", "ceid": "US:en"}
    resp = requests.get(source["url"], params=params, headers=HEADERS, timeout=30)
    if resp.status_code != 200:
        return "BLOCKED", [], f"HTTP {resp.status_code}"

    try:
        root = ET.fromstring(resp.content)
    except Exception as e:
        return "ERROR", [], f"RSS parse: {e}"

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    items: List[Dict[str, Any]] = []
    for item in root.iter("item"):
        title = strip_html(item.findtext("title") or "")
        link = (item.findtext("link") or "").strip()
        description = strip_html(item.findtext("description") or "")
        pub_raw = item.findtext("pubDate") or ""
        published = parse_pubdate(pub_raw)
        if published and published < cutoff:
            continue
        text = f"{title} {description}"
        if not is_public_sale(text, source["airline"]):
            continue

        codes = extract_codes(text)
        if codes:
            for code in codes:
                items.append({
                    "airline": source["airline"],
                    "key": f"{source['airline']}|{code}",
                    "code": code,
                    "title": title or description[:160],
                    "url": link,
                    "published": published.isoformat() if published else "",
                })
        else:
            norm = re.sub(r"\W+", " ", title.lower()).strip()
            key = f"{source['airline']}|title:{hashlib.sha1(norm.encode()).hexdigest()[:16]}"
            items.append({
                "airline": source["airline"],
                "key": key,
                "code": None,
                "title": title or description[:160],
                "url": link,
                "published": published.isoformat() if published else "",
            })
    return "OK", items, ""


def fetch_newsroom(source: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], str]:
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=30, allow_redirects=True)
    except Exception as e:
        return "ERROR", [], f"{type(e).__name__}: {e}"
    if resp.status_code in (403, 406, 429):
        return "BLOCKED", [], f"HTTP {resp.status_code}"
    if resp.status_code != 200:
        return "ERROR", [], f"HTTP {resp.status_code}"

    text = resp.text
    items: List[Dict[str, Any]] = []
    seen = set()
    for href, label in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.S | re.I):
        title = strip_html(label)
        if len(title) < 12:
            continue
        if not is_public_sale(title, source["airline"]):
            continue
        if title in seen:
            continue
        seen.add(title)
        url = href if href.startswith("http") else urllib.parse.urljoin(source["url"], href)
        codes = extract_codes(title)
        if codes:
            for code in codes:
                items.append({
                    "airline": source["airline"],
                    "key": f"{source['airline']}|{code}",
                    "code": code,
                    "title": title,
                    "url": url,
                    "published": "",
                })
        else:
            norm = re.sub(r"\W+", " ", title.lower()).strip()
            key = f"{source['airline']}|title:{hashlib.sha1(norm.encode()).hexdigest()[:16]}"
            items.append({
                "airline": source["airline"],
                "key": key,
                "code": None,
                "title": title,
                "url": url,
                "published": "",
            })
        if len(items) >= 10:
            break
    return "OK", items, ""


def fetch_official(source: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]], str]:
    try:
        resp = requests.get(source["url"], headers=HEADERS, timeout=30, allow_redirects=True)
    except Exception as e:
        return "ERROR", [], f"{type(e).__name__}: {e}"

    if resp.status_code in (403, 406, 429):
        return "BLOCKED", [], f"HTTP {resp.status_code}"
    if resp.status_code != 200:
        return "ERROR", [], f"HTTP {resp.status_code}"

    text = strip_html(resp.text)
    codes = extract_codes(text)
    items = [{
        "airline": source["airline"],
        "key": f"{source['airline']}|{code}",
        "code": code,
        "title": f"{source['name']}: public promo code",
        "url": source["url"],
        "published": datetime.now(timezone.utc).isoformat(),
    } for code in codes]
    return "OK", items, ""


def send_telegram(items: List[Dict[str, Any]]) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat or not items:
        return False

    lines = ["🎟 <b>Promo watcher: публичные акции</b>", ""]
    for item in items[:5]:
        airline = html.escape(item["airline"])
        title = html.escape(item["title"][:140])
        if item.get("code"):
            lines.append(f"• <b>{airline}</b> · код <code>{html.escape(item['code'])}</code> — "
                         f'<a href="{html.escape(item["url"], quote=True)}">{title}</a>')
        else:
            lines.append(f'• <b>{airline}</b> — <a href="{html.escape(item["url"], quote=True)}">{title}</a>')
    if len(items) > 5:
        lines.append(f"…и ещё {len(items) - 5}")
    lines += ["", "<i>Публичные акции из новостей. Проверьте код и условия на сайте авиакомпании; "
                  "member-only и персональные предложения сюда не входят.</i>"]

    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": "\n".join(lines), "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20,
        )
        return resp.status_code == 200
    except Exception:
        return False


def write_report(source_rows: List[Tuple[str, str, str]], new_items: List[Dict[str, Any]]) -> None:
    lines = [
        "# Promo watcher — public AA/JetBlue sales",
        "",
        f"Checked: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "Fare scanning is Google Flights only. This report covers public sale/promo",
        "announcements (not member-only fares).",
        "",
        "## Sources",
        "",
        "| Source | Airline | Status | Note |",
        "|---|---|---|---|",
    ]
    for name, airline, status in source_rows:
        lines.append(f"| {name} | {airline} | {status} | |")
    lines += ["", "## New public promos", ""]
    if new_items:
        for item in new_items:
            code = f" · code `{item['code']}`" if item.get("code") else ""
            lines.append(f"- **{item['airline']}** — {item['title']}{code} — {item['url']}")
    else:
        lines.append("No new public promos this run.")
    lines += [
        "",
        "Official aa.com/jetblue.com pages are often blocked from datacenter IPs;",
        "Google News RSS remains the reliable public source.",
    ]
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Public airline promo watcher")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print, no DB writes/Telegram")
    parser.add_argument("--max-age-days", type=int, default=14, help="Ignore news older than N days")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite database path")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not args.dry_run:
        init_db(db_path)

    source_rows: List[Tuple[str, str, str]] = []
    all_items: List[Dict[str, Any]] = []

    for source in NEWS_SOURCES:
        try:
            status, items, note = fetch_news(source, max(1, args.max_age_days))
        except Exception as e:
            status, items, note = "ERROR", [], f"{type(e).__name__}: {e}"
        source_rows.append((source["name"], source["airline"], status))
        print(f"[PROMO] {source['name']}: {status} ({len(items)} items) {note}")
        all_items.extend(items)
        if not args.dry_run:
            record_source(db_path, source, status, note)

    for source in NEWSROOM_SOURCES:
        try:
            status, items, note = fetch_newsroom(source)
        except Exception as e:
            status, items, note = "ERROR", [], f"{type(e).__name__}: {e}"
        source_rows.append((source["name"], source["airline"], status))
        print(f"[PROMO] {source['name']}: {status} ({len(items)} items) {note}")
        all_items.extend(items)
        if not args.dry_run:
            record_source(db_path, source, status, note)

    for source in OFFICIAL_SOURCES:
        try:
            status, items, note = fetch_official(source)
        except Exception as e:
            status, items, note = "ERROR", [], f"{type(e).__name__}: {e}"
        source_rows.append((source["name"], source["airline"], status))
        print(f"[PROMO] {source['name']}: {status} ({len(items)} items) {note}")
        all_items.extend(items)
        if not args.dry_run:
            record_source(db_path, source, status, note)

    # De-duplicate within this run.
    unique: Dict[str, Dict[str, Any]] = {}
    for item in all_items:
        unique.setdefault(item["key"], item)
    all_items = list(unique.values())

    new_items = save_new_items(db_path, all_items, args.dry_run) if not args.dry_run else all_items

    for item in new_items:
        code = f" code={item['code']}" if item.get("code") else ""
        print(f"[PROMO] NEW {item['airline']}{code}: {item['title'][:110]}")

    if not args.dry_run:
        write_report(source_rows, new_items)
        if new_items:
            print(f"[PROMO] Telegram sent: {send_telegram(new_items)}")
        else:
            print("[PROMO] No new public promos")

    print(f"[PROMO] Done: {len(new_items)} new item(s)")


if __name__ == "__main__":
    main()
