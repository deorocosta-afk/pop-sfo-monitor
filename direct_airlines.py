#!/usr/bin/env python3
"""
Direct airline check for the standalone POP <-> SFO monitor.

Google Flights remains the broad discovery engine. This module verifies the
cheapest date pairs directly on the airlines' own booking sites:

- American Airlines: /booking/search deep link (normal Chrome + CDP; Akamai may
  challenge datacenter IPs and then the result is recorded as BLOCKED).
- JetBlue: /booking/flights form + "Search flights" click (normal Chrome + CDP).

It is intentionally limited to the top N date pairs: the airline sites are
browser-only and rate-limited, so querying all 425 pairs there is not viable.

Chrome is driven through the DevTools Protocol with a normal (non-automation)
browser launch. On Linux run it under xvfb-run.

Usage:
    python direct_airlines.py --top 5
    python direct_airlines.py --top 3 --airlines AA
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

import websocket

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "pop_sfo_prices.db"
REPORT_PATH = BASE_DIR / "direct_airline_report.md"

CHROME_CANDIDATES = [
    os.environ.get("CHROME_PATH", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

STATUS_OK = "OK"
STATUS_EMPTY = "EMPTY"
STATUS_BLOCKED = "BLOCKED"
STATUS_ERROR = "ERROR"


def find_chrome() -> Optional[str]:
    for candidate in CHROME_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def launch_chrome(chrome: str, url: str, port: int, profile: str) -> subprocess.Popen:
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-sync",
        "--new-window",
        url,
    ]
    if os.name != "nt":
        args += ["--no-sandbox", "--disable-dev-shm-usage"]
    return subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=(os.name != "nt"),
    )


def wait_for_tab(port: int, domain: str, timeout: float = 25.0) -> Optional[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as resp:
                tabs = json.load(resp)
            for tab in tabs:
                if domain in (tab.get("url") or ""):
                    return tab
        except Exception:
            pass
        time.sleep(0.7)
    return None


def close_chrome(proc: subprocess.Popen, profile: str, remove: bool = True) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            proc.wait(timeout=10)
    except Exception:
        pass
    if remove:
        shutil.rmtree(profile, ignore_errors=True)


class Cdp:
    def __init__(self, ws_url: str):
        self.ws = websocket.create_connection(ws_url, timeout=30, origin="http://localhost")
        self.msg_id = 0

    def eval(self, expression: str):
        self.msg_id += 1
        current = self.msg_id
        self.ws.send(json.dumps({
            "id": current,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True},
        }))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == current:
                return (msg.get("result", {}).get("result", {}) or {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def click_script() -> str:
    return r"""
    (() => {
      const buttons = [...document.querySelectorAll('button')];
      const accept = buttons.find(b => /^accept$/i.test((b.innerText || '').trim()));
      if (accept) accept.click();
      const search = buttons.find(b => /search flights/i.test(b.innerText || ''));
      if (search) { search.click(); return 'clicked'; }
      const submit = document.querySelector('button[type=submit]');
      if (submit) { submit.click(); return 'submit'; }
      return 'not-found';
    })()
    """


def parse_prices(text: str) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    prices = []
    for raw in re.findall(r"\$\s?([0-9][0-9,]*(?:\.[0-9]{2})?)", text):
        try:
            prices.append(float(raw.replace(",", "")))
        except ValueError:
            pass
    price = min(prices) if prices else None

    flight = None
    m = re.search(r"\b(AA|B6)\s?([0-9]{1,4})\b", text)
    if m:
        flight = f"{m.group(1)} {m.group(2)}"

    stops = None
    low = text.lower()
    if "nonstop" in low:
        stops = "Nonstop"
    elif "1 stop" in low:
        stops = "1 stop"
    elif "2 stop" in low:
        stops = "2 stops"
    return price, flight, stops


def check_american(chrome: str, dep: str, ret: str) -> dict:
    from tracker_pop_sfo import build_aa_search_url

    url = build_aa_search_url("POP", "SFO", dep, ret)
    port = free_port()
    persistent_profile = os.environ.get("DIRECT_CHROME_PROFILE")
    profile = persistent_profile or tempfile.mkdtemp(prefix="pop_sfo_aa_")
    proc = launch_chrome(chrome, url, port, profile)
    cdp = None
    try:
        tab = wait_for_tab(port, "aa.com")
        if not tab:
            return {"status": STATUS_ERROR, "note": "AA tab did not open", "url": url}
        cdp = Cdp(tab["webSocketDebuggerUrl"])
        body = ""
        title = ""
        for _ in range(16):
            time.sleep(3)
            title = cdp.eval("document.title") or ""
            body = cdp.eval("document.body ? document.body.innerText : ''") or ""
            if "Access Denied" in title:
                return {"status": STATUS_BLOCKED, "note": "Akamai Access Denied", "url": url}
            if "Choose flights" in title or ("results" in body and "POP" in body):
                # Wait for the React cards to render prices after the challenge.
                for _ in range(10):
                    if re.search(r"\$\s?[0-9]", body):
                        break
                    time.sleep(2)
                    body = cdp.eval("document.body ? document.body.innerText : ''") or ""
                break
        if "Choose flights" not in title and "Choose flights" not in body:
            return {"status": STATUS_BLOCKED, "note": f"Akamai challenge (title={title!r})", "url": url}
        if "No flights" in body or "no flights" in body:
            return {"status": STATUS_EMPTY, "note": "AA reports no flights", "url": url}
        after = body.split("results", 1)[1] if "results" in body else body
        if not re.search(r"\$\s?[0-9]", after):
            after = body
        price, flight, stops = parse_prices(after)
        if price is None:
            return {"status": STATUS_EMPTY, "note": "AA results without a price", "url": url}
        return {"status": STATUS_OK, "price_usd": price, "flight_no": flight, "stops": stops,
                "note": "AA choose-flights page", "url": url}
    except Exception as e:
        return {"status": STATUS_ERROR, "note": f"{type(e).__name__}: {e}", "url": url}
    finally:
        if cdp:
            cdp.close()
        close_chrome(proc, profile, remove=not persistent_profile)


def check_jetblue(chrome: str, dep: str, ret: str) -> dict:
    url = (f"https://www.jetblue.com/booking/flights?from=POP&to=SFO&depart={dep}&return={ret}"
           "&isMultiCity=false&noOfRoute=1&lang=en")
    port = free_port()
    profile = tempfile.mkdtemp(prefix="pop_sfo_b6_")
    proc = launch_chrome(chrome, url, port, profile)
    cdp = None
    try:
        tab = wait_for_tab(port, "jetblue.com")
        if not tab:
            return {"status": STATUS_ERROR, "note": "JetBlue tab did not open", "url": url}
        cdp = Cdp(tab["webSocketDebuggerUrl"])
        time.sleep(6)
        clicked = cdp.eval(click_script())
        time.sleep(14)
        body = ""
        for _ in range(6):
            body = cdp.eval("document.body ? document.body.innerText : ''") or ""
            if "Nonstop" in body or "1 Stop" in body or "2 Stops" in body:
                break
            time.sleep(3)
        if "No flights have been found" in body and not re.search(r"(Nonstop|1 Stop|2 Stops)", body):
            return {"status": STATUS_EMPTY, "note": f"JetBlue no flights (click={clicked})", "url": url}
        marker = re.search(r"(Nonstop|1 Stop|2 Stops)", body)
        after = body[marker.start():] if marker else body
        price, flight, stops = parse_prices(after)
        if price is None:
            return {"status": STATUS_EMPTY, "note": f"No JetBlue price (click={clicked})", "url": url}
        return {"status": STATUS_OK, "price_usd": price, "flight_no": flight, "stops": stops,
                "note": "JetBlue select-flights page", "url": url}
    except Exception as e:
        return {"status": STATUS_ERROR, "note": f"{type(e).__name__}: {e}", "url": url}
    finally:
        if cdp:
            cdp.close()
        close_chrome(proc, profile)


def top_pairs(db_path: Path, limit: int) -> list[tuple[str, str, int]]:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT dep_date, ret_date, MIN(price_usd) AS p
            FROM flight_quotes
            WHERE origin = 'POP' AND dest = 'SFO'
              AND timestamp = (SELECT MAX(timestamp) FROM flight_quotes WHERE origin = 'POP' AND dest = 'SFO')
            GROUP BY dep_date, ret_date
            ORDER BY p ASC
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
    finally:
        conn.close()
    return [(str(r[0]), str(r[1]), int(round(r[2]))) for r in rows]


def save_checks(db_path: Path, timestamp: str, checks: list[dict]) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS direct_airline_checks (
                timestamp TEXT NOT NULL,
                airline TEXT NOT NULL,
                dep_date TEXT NOT NULL,
                ret_date TEXT NOT NULL,
                status TEXT NOT NULL,
                price_usd REAL,
                flight_no TEXT,
                stops TEXT,
                url TEXT,
                note TEXT,
                PRIMARY KEY (timestamp, airline, dep_date, ret_date)
            )
        """)
        for c in checks:
            conn.execute("""
                INSERT OR REPLACE INTO direct_airline_checks
                    (timestamp, airline, dep_date, ret_date, status, price_usd,
                     flight_no, stops, url, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (timestamp, c["airline"], c["dep_date"], c["ret_date"], c["status"],
                  c.get("price_usd"), c.get("flight_no"), c.get("stops"), c.get("url"), c.get("note")))
        conn.commit()
    finally:
        conn.close()


def write_report(timestamp: str, checks: list[dict]) -> None:
    lines = [
        "# Direct airline check — POP ⇄ SFO",
        "",
        f"Generated: `{timestamp}`",
        "",
        "Top date pairs were checked on the airlines' own booking sites.",
        "Google Flights remains the source for the full 425-pair scan.",
        "",
        "| Airline | Depart | Return | Status | Price | Flight | Stops | Note |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in checks:
        price = f"${c['price_usd']:.2f}" if c.get("price_usd") is not None else "—"
        lines.append(
            f"| {c['airline']} | {c['dep_date']} | {c['ret_date']} | {c['status']} | "
            f"{price} | {c.get('flight_no') or '—'} | {c.get('stops') or '—'} | {c.get('note') or ''} |"
        )
    lines += [
        "",
        "Statuses: `OK` — price found; `EMPTY` — airline reports no flights; "
        "`BLOCKED` — Akamai/bot challenge; `ERROR` — browser/CDP failure.",
        "",
        "AA.com is Akamai-protected: datacenter IPs (GitHub runners) are often "
        "challenged, while a residential browser can pass. JetBlue is queried via "
        "its normal booking form.",
    ]
    if any(c.get("airline") == "B6" and c.get("status") == STATUS_EMPTY for c in checks):
        lines += [
            "",
            "JetBlue currently reports no POP ⇄ SFO itinerary. A B6 check can still "
            "be run manually with `--airlines B6`, but it is not part of the daily "
            "cloud pipeline because it produces no through fare. Google Flights "
            "remains the source for B6 rows.",
        ]
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Direct AA/JetBlue check for top POP-SFO pairs")
    parser.add_argument("--top", type=int, default=5, help="Number of cheapest date pairs to check")
    parser.add_argument("--airlines", default="AA,B6", help="Comma-separated airlines (AA,B6)")
    parser.add_argument("--db", default=str(DB_PATH), help="SQLite database path")
    args = parser.parse_args()

    chrome = find_chrome()
    if not chrome:
        print("[DIRECT] Chrome not found; cannot run direct airline checks.")
        return

    db_path = Path(args.db)
    pairs = top_pairs(db_path, max(1, args.top))
    if not pairs:
        print("[DIRECT] No scan data; nothing to check.")
        return

    airlines = [a.strip().upper() for a in args.airlines.split(",") if a.strip()]
    timestamp = datetime.now(timezone.utc).isoformat()
    checks: list[dict] = []
    blocked: set[str] = set()

    for dep, ret, google_price in pairs:
        print(f"[DIRECT] {dep} -> {ret} (Google min ${google_price})")
        for airline in airlines:
            if airline in blocked:
                checks.append({
                    "airline": airline, "dep_date": dep, "ret_date": ret,
                    "status": STATUS_BLOCKED, "note": "skipped after first block",
                })
                continue
            if airline == "AA":
                result = check_american(chrome, dep, ret)
            elif airline == "B6":
                result = check_jetblue(chrome, dep, ret)
            else:
                continue
            result.update({"airline": airline, "dep_date": dep, "ret_date": ret})
            checks.append(result)
            if result["status"] == STATUS_BLOCKED:
                blocked.add(airline)
            price = result.get("price_usd")
            print(f"  {airline}: {result['status']}"
                  + (f" ${price:.2f}" if price is not None else "")
                  + (f" — {result.get('note')}" if result.get("note") else ""))
            time.sleep(2)

    save_checks(db_path, timestamp, checks)
    write_report(timestamp, checks)
    print(f"[DIRECT] Saved {len(checks)} checks to {db_path.name} and {REPORT_PATH.name}")


if __name__ == "__main__":
    main()
