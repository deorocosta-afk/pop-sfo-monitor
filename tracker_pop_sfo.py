#!/usr/bin/env python3
"""
POP <-> SFO 1-Stop Flight Price Monitor (Puerto Plata -> San Francisco)
Monitors official fares for American Airlines (AA), JetBlue (B6), United Airlines (UA), and Copa Airlines (CM).

Strict Constraints & Architecture (v3.3.0):
- Airports: strictly POP <-> SFO (no nearby airports, no ground/airport transfers)
- Stops: strictly 1 stop (len(segments) == 2 in each direction)
- Departure window: dep_date >= May 20, return arrival in POP <= July 5
- Step: DEP_STEP = 1 day (all 7 days without gaps)
- Stay durations: 14 to 30 nights (17 variations)
- Layover filter: 60 min <= layover <= 720 min (12h), total duration <= 1440 min (24h)
- Zero ban risk: Persistent disk cache (TTL 12h), circuit breaker (5 consecutive errors), 1.2-2.0s jitter
- Robust error handling: Distinguishes ERROR vs EMPTY, element-level parsing, never caches failed dates
- Honest pricing & multi-carrier support:
  * No fabricated 0.82 tax splits.
  * Mixed-carrier pairs explicitly labeled as "2 separate one-way tickets".
  * Deep search links for all 4 carriers (AA, B6, UA, CM) + Google Flights aggregate link.
- Decoupled persistence & scan status:
  * Records scan runs in `scan_runs` table ('complete', 'partial', 'failed').
  * Isolates partial scans into diagnostic table `partial_flight_quotes`, keeping main tables clean.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import logging
import math
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Force UTF-8 and live line-buffering for Windows and CI
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "pop_sfo_prices.db"
DISK_CACHE_PATH = BASE_DIR / ".cache_pop_sfo_legs.json"

# Route & Threshold
ORIGIN = "POP"
DESTINATION = "SFO"
ALERT_THRESHOLD_USD = 700.0

# Strict Airline Whitelist
ALLOWED_CARRIERS: Dict[str, str] = {
    "AA": "American Airlines",
    "B6": "JetBlue",
    "UA": "United Airlines",
    "CM": "Copa Airlines",
}


def sanitize_log(msg: str) -> str:
    """Mask Telegram bot tokens and credentials in logs and error strings."""
    if not msg:
        return ""
    return re.sub(r"bot[0-9]+:[a-zA-Z0-9_-]+", "bot<REDACTED>", str(msg))


def extract_carrier_codes(flight_no: str) -> List[str]:
    """Extracts valid carrier codes (AA, B6, UA, CM) using word boundaries to avoid false positives (e.g. GUA -> UA)."""
    if not flight_no:
        return []
    carriers = set()
    for code in ALLOWED_CARRIERS:
        if re.search(rf"\b{code}\b", flight_no):
            carriers.add(code)
    return sorted(list(carriers))

# Date Window Configuration
DEFAULT_START_DEP = "2027-05-20"
DEFAULT_MAX_RET = "2027-07-05"
DEP_STEP = 1

# Stay durations: 14 to 30 nights
MIN_STAY_DAYS = 14
MAX_STAY_DAYS = 30
STAY_DURATIONS = list(range(MIN_STAY_DAYS, MAX_STAY_DAYS + 1))

# Layover constraints (minutes)
MIN_LAYOVER_MIN = 60    # Min 1 hour for safe connection / CBP
MAX_LAYOVER_MIN = 720   # Max 12 hours (avoids 20+ hr overnight stays)
MAX_TOTAL_MIN = 1440    # Max 24 hours total travel time

DISK_CACHE_TTL_HOURS = 12
RETENTION_DAYS = 120

SESSION = requests.Session()
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
]
SESSION.headers.update({
    "User-Agent": random.choice(USER_AGENTS),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
})
SESSION.cookies.update({
    "CONSENT": "PENDING+987",
    "SOCS": "CAESHAgBEhJnd3NfMjAyNDA1MTQtMF9SQzIaAmVuIAEaBgiAo_CmBg",
})

_retry = Retry(
    total=3, connect=3, read=3, status=3,
    backoff_factor=1.2,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST"]),
    respect_retry_after_header=True,
    raise_on_status=False,
)
_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=10, max_retries=_retry)
SESSION.mount("https://", _adapter)
SESSION.mount("http://", _adapter)

# Statistics & Circuit Breaker
STATS = {
    "network_ok": 0,
    "network_empty": 0,
    "network_error": 0,
    "mem_cache_hits": 0,
    "disk_cache_hits": 0,
}
CONSECUTIVE_ERRORS = 0
MAX_CONSECUTIVE_ERRORS = 5

OUTBOUND_CACHE: Dict[str, List[Dict[str, Any]]] = {}
INBOUND_CACHE: Dict[str, List[Dict[str, Any]]] = {}
FAILED_DATES: Set[Tuple[str, str, str]] = set()
MIN_COVERAGE_RATIO = 0.85
MIN_REQUIRED_COMPLETE_QUOTES = 80
_SCAN_LOCK = threading.Lock()


def reset_session_state() -> None:
    """Reset transient scan counters, failed dates, and in-memory caches between runs."""
    global CONSECUTIVE_ERRORS, STATS, OUTBOUND_CACHE, INBOUND_CACHE, FAILED_DATES
    CONSECUTIVE_ERRORS = 0
    STATS = {
        "network_ok": 0,
        "network_empty": 0,
        "network_error": 0,
        "mem_cache_hits": 0,
        "disk_cache_hits": 0,
    }
    OUTBOUND_CACHE.clear()
    INBOUND_CACHE.clear()
    FAILED_DATES.clear()


# --- Disk Cache Support ---

def load_disk_cache() -> Dict[str, Any]:
    if not DISK_CACHE_PATH.exists():
        return {}
    try:
        with open(DISK_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        # Prune expired entries to prevent disk cache bloat
        now_dt = datetime.now(timezone.utc)
        valid_cache = {}
        for k, v in cache.items():
            cached_at = v.get("cached_at")
            if cached_at:
                try:
                    dt = datetime.fromisoformat(cached_at)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if (now_dt - dt).total_seconds() / 3600.0 <= DISK_CACHE_TTL_HOURS:
                        valid_cache[k] = v
                except Exception:
                    pass
        return valid_cache
    except Exception as e:
        logging.warning(f"Could not load disk cache: {e}")
        return {}


def save_disk_cache(cache: Dict[str, Any]) -> None:
    try:
        with open(DISK_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Could not save disk cache: {e}")


def get_from_disk_cache(date_str: str, origin: str, dest: str) -> Optional[List[Dict[str, Any]]]:
    cache = load_disk_cache()
    key = f"{origin}_{dest}_{date_str}"
    entry = cache.get(key)
    if not entry:
        return None
    cached_at = entry.get("cached_at")
    if not cached_at:
        return None
    try:
        dt = datetime.fromisoformat(cached_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0
        if age_hours <= DISK_CACHE_TTL_HOURS:
            return entry.get("legs", [])
    except Exception:
        pass
    return None


def put_to_disk_cache(date_str: str, origin: str, dest: str, legs: List[Dict[str, Any]]) -> None:
    cache = load_disk_cache()
    key = f"{origin}_{dest}_{date_str}"
    cache[key] = {
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "origin": origin,
        "dest": dest,
        "date": date_str,
        "legs": legs,
    }
    save_disk_cache(cache)


# --- Date & Window Helpers ---

def resolve_window_dates(start_dep_str: str = DEFAULT_START_DEP, max_ret_str: str = DEFAULT_MAX_RET) -> Tuple[date, date]:
    """Parse departure start and return max dates, rolling year forward if already past and clipping past dates."""
    start_d = date.fromisoformat(start_dep_str)
    max_ret_d = date.fromisoformat(max_ret_str)
    today = datetime.now(timezone.utc).date()

    while max_ret_d < today:
        start_d = date(start_d.year + 1, start_d.month, start_d.day)
        max_ret_d = date(max_ret_d.year + 1, max_ret_d.month, max_ret_d.day)

    # Edge case: If today is already inside the window, never query past departure dates
    start_d = max(start_d, today + timedelta(days=1))
    return start_d, max_ret_d


def generate_flight_matrix(
    step_days: int = DEP_STEP,
    start_dep_str: str = DEFAULT_START_DEP,
    max_ret_str: str = DEFAULT_MAX_RET,
    durations: Optional[List[int]] = None,
) -> List[Dict[str, Any]]:
    """Generates all valid combinations with departure >= May 20, stay in 14..30 nights, return departure <= max_ret."""
    durations = durations or STAY_DURATIONS
    start_dep, max_ret = resolve_window_dates(start_dep_str, max_ret_str)
    pairs: List[Dict[str, Any]] = []

    curr_dep = start_dep
    while curr_dep <= max_ret:
        for stay in durations:
            ret_date = curr_dep + timedelta(days=stay)
            if ret_date <= max_ret:
                pairs.append({
                    "dep_date": curr_dep.strftime("%Y-%m-%d"),
                    "ret_date": ret_date.strftime("%Y-%m-%d"),
                    "stay_days": stay,
                })
        curr_dep += timedelta(days=step_days)

    return pairs


def order_pairs_for_leg_cache(pairs: List[Tuple[str, str, int]]) -> List[Tuple[str, str, int]]:
    """Orders candidate pairs so that pairs covering unqueried departure & return dates run first."""
    seen_d, seen_r = set(), set()
    remaining = list(pairs)
    covering = []
    while remaining:
        best_idx = -1
        best_score = -1
        for i, p in enumerate(remaining):
            score = (1 if p[0] not in seen_d else 0) + (1 if p[1] not in seen_r else 0)
            if score > best_score:
                best_score = score
                best_idx = i
                if score == 2:
                    break
        if best_score <= 0:
            break
        chosen = remaining.pop(best_idx)
        seen_d.add(chosen[0])
        seen_r.add(chosen[1])
        covering.append(chosen)
    return covering + remaining


# --- Protobuf Encoder for Google Flights SSR (tfs) ---

def _varint(val: int) -> bytes:
    out = bytearray()
    while val > 0x7F:
        out.append((val & 0x7F) | 0x80)
        val >>= 7
    out.append(val & 0x7F)
    return bytes(out)


def _field_varint(field_no: int, val: int) -> bytes:
    return _varint((field_no << 3) | 0) + _varint(val)


def _field_bytes(field_no: int, data: bytes) -> bytes:
    return _varint((field_no << 3) | 2) + _varint(len(data)) + data


def _airport(code: str) -> bytes:
    return _field_varint(1, 1) + _field_bytes(2, code.encode("utf-8"))


def _flight_leg(date_str: str, origin: str, dest: str, max_stops_enum: int = 2, airlines: Optional[List[str]] = None) -> bytes:
    b = _field_bytes(2, date_str.encode("utf-8"))
    if max_stops_enum is not None:
        b += _field_varint(5, max_stops_enum)
    if airlines:
        for al in airlines:
            b += _field_bytes(6, al.encode("utf-8"))
    b += _field_bytes(13, _airport(origin))
    b += _field_bytes(14, _airport(dest))
    return b


def build_tfs(dep_date: str, origin: str, dest: str, airlines: Optional[List[str]] = None) -> str:
    msg = _field_varint(1, 28) + _field_varint(2, 2)
    msg += _field_bytes(3, _flight_leg(dep_date, origin, dest, max_stops_enum=2, airlines=airlines))
    msg += _field_varint(8, 1) + _field_varint(9, 1) + _field_varint(14, 1) + _field_varint(19, 2)
    return base64.urlsafe_b64encode(msg).decode("ascii").rstrip("=")


def format_duration_minutes(total_min: Optional[int]) -> str:
    if not total_min or total_min <= 0:
        return "Н/Д"
    h = int(total_min) // 60
    m = int(total_min) % 60
    return f"{h}ч {m:02d}м"


# --- Honest Search & Booking URLs for all 4 Airlines ---

def build_aa_search_url(
    origin: str,
    dest: str,
    dep_date: str,
    ret_date: Optional[str] = None,
) -> str:
    """
    AA's current booking deep link (the one its own React form constructs):
    /booking/search?...&slices=[{"orig":...,"dest":...,"date":...}].
    The legacy /homePage.do?searchType=flight... link is ignored by aa.com and
    redirects to the localized home page, so it must not be used.
    """
    slices = [{
        "orig": origin,
        "origNearby": False,
        "dest": dest,
        "destNearby": False,
        "date": dep_date,
    }]
    if ret_date:
        slices.append({
            "orig": dest,
            "origNearby": False,
            "dest": origin,
            "destNearby": False,
            "date": ret_date,
        })
    params = {
        "locale": "en_US",
        "fareType": "Lowest",
        "pax": "1",
        "adult": "1",
        "type": "RoundTrip" if ret_date else "OneWay",
        "searchType": "Revenue",
        "cabin": "",
        "carriers": "ALL",
        "travelType": "personal",
        "slices": json.dumps(slices, separators=(",", ":")),
    }
    return "https://www.aa.com/booking/search?" + urllib.parse.urlencode(params)


def build_airline_search_urls(
    carrier_out: str,
    carrier_in: str,
    origin: str,
    dest: str,
    dep_date: str,
    ret_date: str,
) -> Dict[str, str]:
    """
    Builds authentic, tested search URLs for Google Flights and each airline.
    Distinguishes single-carrier roundtrips vs separate one-way tickets for mixed carriers.
    """
    q = urllib.parse.quote(f"Flights from {origin} to {dest} on {dep_date} through {ret_date} with 1 stop")
    gf_url = f"https://www.google.com/travel/flights?q={q}&curr=USD&hl=en"

    urls = {
        "google_flights": gf_url,
        "is_mixed": (carrier_out != carrier_in),
        "carrier_out": carrier_out,
        "carrier_in": carrier_in,
    }

    # Outbound airline search URL
    if carrier_out == "AA":
        urls["outbound_url"] = build_aa_search_url(origin, dest, dep_date)
    elif carrier_out == "B6":
        urls["outbound_url"] = (
            f"https://www.jetblue.com/booking/flights?from={origin}&to={dest}&depart={dep_date}&isMultiCity=false&noOfRoute=1&lang=en"
        )
    elif carrier_out == "UA":
        urls["outbound_url"] = (
            f"https://www.united.com/en/us/fsr/choose-flights?f={origin}&t={dest}&d={dep_date}&tt=1&px=1&taxng=1&idx=1"
        )
    elif carrier_out == "CM":
        urls["outbound_url"] = (
            f"https://www.copaair.com/en-gs/?origin={origin}&destination={dest}&departureDate={dep_date}&tripType=oneWay"
        )
    else:
        urls["outbound_url"] = gf_url

    # Inbound airline search URL
    if carrier_in == "AA":
        urls["inbound_url"] = build_aa_search_url(dest, origin, ret_date)
    elif carrier_in == "B6":
        urls["inbound_url"] = (
            f"https://www.jetblue.com/booking/flights?from={dest}&to={origin}&depart={ret_date}&isMultiCity=false&noOfRoute=1&lang=en"
        )
    elif carrier_in == "UA":
        urls["inbound_url"] = (
            f"https://www.united.com/en/us/fsr/choose-flights?f={dest}&t={origin}&d={ret_date}&tt=1&px=1&taxng=1&idx=1"
        )
    elif carrier_in == "CM":
        urls["inbound_url"] = (
            f"https://www.copaair.com/en-gs/?origin={dest}&destination={origin}&departureDate={ret_date}&tripType=oneWay"
        )
    else:
        urls["inbound_url"] = gf_url

    # Single-carrier round-trip URL (if same carrier)
    if carrier_out == carrier_in:
        c = carrier_out
        if c == "AA":
            urls["roundtrip_airline_url"] = build_aa_search_url(origin, dest, dep_date, ret_date)
        elif c == "B6":
            urls["roundtrip_airline_url"] = (
                f"https://www.jetblue.com/booking/flights?from={origin}&to={dest}&depart={dep_date}&return={ret_date}&isMultiCity=false&noOfRoute=1&lang=en"
            )
        elif c == "UA":
            urls["roundtrip_airline_url"] = (
                f"https://www.united.com/en/us/fsr/choose-flights?f={origin}&t={dest}&d={dep_date}&r={ret_date}&px=1&taxng=1&idx=1"
            )
        elif c == "CM":
            urls["roundtrip_airline_url"] = (
                f"https://www.copaair.com/en-gs/?origin={origin}&destination={dest}&departureDate={dep_date}&returnDate={ret_date}&tripType=roundTrip"
            )
    else:
        urls["roundtrip_airline_url"] = None

    return urls


# --- Core Leg Fetcher with Error vs Empty distinction ---

def fetch_one_stop_legs(
    date_str: str,
    origin: str,
    dest: str,
    max_ret_date: Optional[date] = None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Fetches strictly 1-stop flights between origin and dest on date_str.
    Returns:
      (status, legs)
      status: "OK" (data found), "EMPTY" (valid 200 response but 0 flights), "ERROR" (network/parse/HTTP fail)
    """
    global CONSECUTIVE_ERRORS

    # Check circuit breaker
    if CONSECUTIVE_ERRORS >= MAX_CONSECUTIVE_ERRORS:
        logging.error(f"[CIRCUIT BREAKER] Aborting fetch: {CONSECUTIVE_ERRORS} consecutive network errors.")
        return "ERROR", []

    tfs = build_tfs(date_str, origin, dest, airlines=list(ALLOWED_CARRIERS.keys()))
    try:
        resp = SESSION.get(
            "https://www.google.com/travel/flights/search",
            params={"tfs": tfs, "hl": "en", "gl": "US", "curr": "USD"},
            timeout=22,
        )
        if resp.status_code != 200:
            CONSECUTIVE_ERRORS += 1
            STATS["network_error"] += 1
            logging.warning(f"HTTP {resp.status_code} on {origin}->{dest} on {date_str}")
            return "ERROR", []

        if "AF_initDataCallback" not in resp.text:
            CONSECUTIVE_ERRORS += 1
            STATS["network_error"] += 1
            logging.warning(f"No callback script in response for {origin}->{dest} on {date_str} (possible captcha/block)")
            return "ERROR", []

        matches = re.findall(
            r"AF_initDataCallback\(\{key:\s*\'ds:1\'.*?data:(.*?), sideChannel:",
            resp.text,
            re.DOTALL,
        )
        if not matches:
            CONSECUTIVE_ERRORS += 1
            STATS["network_error"] += 1
            return "ERROR", []

        try:
            parsed = json.loads(matches[0])
        except Exception as e:
            CONSECUTIVE_ERRORS += 1
            STATS["network_error"] += 1
            logging.warning(f"JSON decode error for {date_str}: {e}")
            return "ERROR", []

        raw_items = []
        if len(parsed) > 2 and isinstance(parsed[2], list) and parsed[2]:
            raw_items.extend(parsed[2][0] if isinstance(parsed[2][0], list) else [])
        if len(parsed) > 3 and isinstance(parsed[3], list) and parsed[3]:
            raw_items.extend(parsed[3][0] if isinstance(parsed[3][0], list) else [])

        # Reset consecutive errors on successful valid response
        CONSECUTIVE_ERRORS = 0

        if not raw_items:
            STATS["network_empty"] += 1
            return "EMPTY", []

        # SENTINEL CHECK (Проверка-сторож):
        # In Google Flights SSR format, the departure airport code must be at seg1[3] of itinerary segment 0.
        # If raw_items is not empty, but 0 items across the entire response contain `origin` at seg1[3],
        # this is a guaranteed index shift / schema change from Google Flights SSR.
        has_matching_origin = False
        for item in raw_items:
            if (
                isinstance(item, list)
                and len(item) > 0
                and isinstance(item[0], list)
                and len(item[0]) > 2
                and isinstance(item[0][2], list)
                and len(item[0][2]) > 0
                and isinstance(item[0][2][0], list)
                and len(item[0][2][0]) > 3
                and item[0][2][0][3] == origin
            ):
                has_matching_origin = True
                break
        if not has_matching_origin:
            CONSECUTIVE_ERRORS += 1
            STATS["network_error"] += 1
            logging.error(
                f"[SCHEMA DRIFT] Sentinel check failed for {origin}->{dest} on {date_str}: "
                f"0/{len(raw_items)} items matched origin '{origin}' at seg1[3]. Returning ERROR."
            )
            return "ERROR", []

        valid_legs: List[Dict[str, Any]] = []
        parse_errors = 0
        structural_errors = 0
        legit_filters = 0
        iata_pattern = re.compile(r"^[A-Z]{3}$")
        carrier_code_pattern = re.compile(r"^[A-Z0-9]{2}$")

        # Parse each flight item individually with element-level error handling
        for item in raw_items:
            try:
                if not isinstance(item, list) or len(item) < 2:
                    parse_errors += 1
                    structural_errors += 1
                    continue
                itin = item[0]
                price_block = item[1]
                if not isinstance(itin, list) or len(itin) < 10:
                    parse_errors += 1
                    structural_errors += 1
                    continue

                price = None
                if isinstance(price_block, list) and price_block and isinstance(price_block[0], list) and len(price_block[0]) > 1:
                    price = price_block[0][1]

                try:
                    price_val = float(price)
                    if math.isnan(price_val) or math.isinf(price_val) or price_val <= 0 or price_val > 50000.0:
                        parse_errors += 1
                        structural_errors += 1
                        continue
                except (ValueError, TypeError):
                    parse_errors += 1
                    structural_errors += 1
                    continue

                segments = itin[2] if len(itin) > 2 else None
                if not isinstance(segments, list):
                    parse_errors += 1
                    structural_errors += 1
                    continue

                if len(segments) != 2:
                    # STRICT RULE 1: Exactly 1 stop (2 segments) - valid filter rejection
                    legit_filters += 1
                    continue

                seg1, seg2 = segments[0], segments[1]
                if not isinstance(seg1, list) or not isinstance(seg2, list):
                    parse_errors += 1
                    structural_errors += 1
                    continue

                dep_airport = seg1[3] if len(seg1) > 3 and isinstance(seg1[3], str) else None
                mid_arr = seg1[6] if len(seg1) > 6 and isinstance(seg1[6], str) else None
                mid_dep = seg2[3] if len(seg2) > 3 and isinstance(seg2[3], str) else None
                arr_airport = seg2[6] if len(seg2) > 6 and isinstance(seg2[6], str) else None

                # Detect index/schema drift: airport fields must match 3-letter IATA format
                if (
                    not dep_airport or not mid_arr or not mid_dep or not arr_airport
                    or not iata_pattern.match(dep_airport)
                    or not iata_pattern.match(mid_arr)
                    or not iata_pattern.match(mid_dep)
                    or not iata_pattern.match(arr_airport)
                ):
                    parse_errors += 1
                    structural_errors += 1
                    continue

                # STRICT RULE 2: Exact airport match and same connecting hub
                if dep_airport != origin or arr_airport != dest:
                    legit_filters += 1
                    continue
                if mid_arr != mid_dep:
                    legit_filters += 1
                    continue

                # STRICT RULE 3: Carrier verification (AA, B6, UA, CM only)
                al1_info = seg1[22] if len(seg1) > 22 and isinstance(seg1[22], list) else []
                al2_info = seg2[22] if len(seg2) > 22 and isinstance(seg2[22], list) else []
                if not al1_info or not al2_info:
                    parse_errors += 1
                    structural_errors += 1
                    continue

                c1 = str(al1_info[0]).upper() if len(al1_info) > 0 and al1_info[0] else ""
                fn1 = str(al1_info[1]) if len(al1_info) > 1 and al1_info[1] is not None else ""
                c2 = str(al2_info[0]).upper() if len(al2_info) > 0 and al2_info[0] else ""
                fn2 = str(al2_info[1]) if len(al2_info) > 1 and al2_info[1] is not None else ""

                if not c1 or not c2 or not carrier_code_pattern.match(c1) or not carrier_code_pattern.match(c2):
                    parse_errors += 1
                    structural_errors += 1
                    continue

                if c1 not in ALLOWED_CARRIERS or c2 not in ALLOWED_CARRIERS:
                    legit_filters += 1
                    continue

                # STRICT RULE 4: Layover duration & total travel time limits
                total_min = itin[9] if len(itin) > 9 and isinstance(itin[9], (int, float)) else 0
                seg1_dur = seg1[11] if len(seg1) > 11 and isinstance(seg1[11], (int, float)) else 0
                seg2_dur = seg2[11] if len(seg2) > 11 and isinstance(seg2[11], (int, float)) else 0
                layover_min = total_min - (seg1_dur + seg2_dur)

                # Direct layover from itin[13] if available
                if len(itin) > 13 and isinstance(itin[13], list) and itin[13]:
                    first_layover = itin[13][0]
                    if isinstance(first_layover, list) and len(first_layover) > 0 and isinstance(first_layover[0], (int, float)):
                        layover_min = int(first_layover[0])

                if layover_min < MIN_LAYOVER_MIN or layover_min > MAX_LAYOVER_MIN:
                    legit_filters += 1
                    continue
                if total_min > MAX_TOTAL_MIN:
                    legit_filters += 1
                    continue

                # STRICT RULE 5: Return flight arrival date check (must arrive <= max_ret_date)
                if max_ret_date and len(itin) > 7 and isinstance(itin[7], list) and len(itin[7]) >= 3:
                    try:
                        arr_date = date(int(itin[7][0]), int(itin[7][1]), int(itin[7][2]))
                        if arr_date > max_ret_date:
                            legit_filters += 1
                            continue
                    except Exception:
                        pass

                primary_carrier = c1 if c1 == c2 else f"{c1}+{c2}"
                flight_code = f"{c1} {fn1}➔{c2} {fn2} ({mid_arr})"

                valid_legs.append({
                    "date": date_str,
                    "origin": origin,
                    "dest": dest,
                    "carrier": primary_carrier,
                    "carrier_1": c1,
                    "carrier_2": c2,
                    "carrier_name": ALLOWED_CARRIERS.get(c1, c1),
                    "via": mid_arr,
                    "flight_no": flight_code,
                    "total": round(price_val, 2),
                    "layover_min": int(layover_min),
                    "duration_min": int(total_min),
                    "duration_str": format_duration_minutes(total_min),
                })
            except Exception:
                # Never let one corrupt item drop the whole date
                parse_errors += 1
                structural_errors += 1
                continue

        # Format drift and schema error detection:
        # If Google returned raw items, but 0 valid legs were found:
        # If structural_errors >= len(raw_items) * 0.5 (or parse_errors >= len(raw_items) * 0.5),
        # fail as ERROR instead of falsely returning EMPTY.
        if not valid_legs and raw_items:
            if structural_errors >= (len(raw_items) * 0.5) or parse_errors >= (len(raw_items) * 0.5):
                CONSECUTIVE_ERRORS += 1
                STATS["network_error"] += 1
                logging.error(f"[SCHEMA ERROR] {structural_errors}/{len(raw_items)} items had structural/schema errors for {origin}->{dest} on {date_str}. Probable Google Flights SSR schema change!")
                return "ERROR", []

        valid_legs.sort(key=lambda x: (x["total"], x["duration_min"]))
        if valid_legs:
            STATS["network_ok"] += 1
            return "OK", valid_legs
        else:
            STATS["network_empty"] += 1
            return "EMPTY", []

    except Exception as e:
        CONSECUTIVE_ERRORS += 1
        STATS["network_error"] += 1
        logging.warning(f"Exception during request {origin}->{dest} on {date_str}: {e}")
        return "ERROR", []


def fetch_live_roundtrip_options(
    dep_date: str,
    ret_date: str,
    max_ret_date: Optional[date] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """
    Combines cached 1-stop outbound (POP->SFO) and inbound (SFO->POP) legs.
    Uses memory cache -> disk cache -> network in order.
    NEVER caches network/parse errors! Remembers failed dates within current session.
    """
    used_network = False

    # 1. Outbound leg resolution
    if (ORIGIN, DESTINATION, dep_date) in FAILED_DATES:
        out_legs = []
    elif dep_date in OUTBOUND_CACHE:
        out_legs = OUTBOUND_CACHE[dep_date]
        STATS["mem_cache_hits"] += 1
    else:
        disk_legs = get_from_disk_cache(dep_date, ORIGIN, DESTINATION)
        if disk_legs is not None:
            OUTBOUND_CACHE[dep_date] = disk_legs
            out_legs = disk_legs
            STATS["disk_cache_hits"] += 1
        else:
            status, net_legs = fetch_one_stop_legs(dep_date, ORIGIN, DESTINATION)
            used_network = True
            if status in ("OK", "EMPTY"):
                OUTBOUND_CACHE[dep_date] = net_legs
                put_to_disk_cache(dep_date, ORIGIN, DESTINATION, net_legs)
                out_legs = net_legs
            else:
                # Do NOT cache errors to disk/mem, but remember failure within run
                FAILED_DATES.add((ORIGIN, DESTINATION, dep_date))
                out_legs = []

    if not out_legs:
        return [], used_network

    # 2. Inbound leg resolution
    if (DESTINATION, ORIGIN, ret_date) in FAILED_DATES:
        in_legs = []
    elif ret_date in INBOUND_CACHE:
        in_legs = INBOUND_CACHE[ret_date]
        STATS["mem_cache_hits"] += 1
    else:
        disk_legs = get_from_disk_cache(ret_date, DESTINATION, ORIGIN)
        if disk_legs is not None:
            INBOUND_CACHE[ret_date] = disk_legs
            in_legs = disk_legs
            STATS["disk_cache_hits"] += 1
        else:
            if used_network:
                time.sleep(random.uniform(1.2, 2.0))
            status, net_legs = fetch_one_stop_legs(ret_date, DESTINATION, ORIGIN, max_ret_date=max_ret_date)
            used_network = True
            if status in ("OK", "EMPTY"):
                INBOUND_CACHE[ret_date] = net_legs
                put_to_disk_cache(ret_date, DESTINATION, ORIGIN, net_legs)
                in_legs = net_legs
            else:
                # Do NOT cache errors to disk/mem, but remember failure within run
                FAILED_DATES.add((DESTINATION, ORIGIN, ret_date))
                in_legs = []

    if not out_legs or not in_legs:
        return [], used_network

    # Index legs by primary carrier
    out_by_carrier: Dict[str, Dict[str, Any]] = {}
    for leg in out_legs:
        c = leg["carrier_1"]
        if c not in out_by_carrier:
            out_by_carrier[c] = leg

    in_by_carrier: Dict[str, Dict[str, Any]] = {}
    for leg in in_legs:
        c = leg["carrier_1"]
        if c not in in_by_carrier:
            in_by_carrier[c] = leg

    options: List[Dict[str, Any]] = []
    seen_pairs = set()

    def _make_option(out_l: Dict[str, Any], in_l: Dict[str, Any]) -> Dict[str, Any]:
        c_out = out_l["carrier_1"]
        c_in = in_l["carrier_1"]
        is_mixed = (c_out != c_in)
        total_rt = round(out_l["total"] + in_l["total"], 2)

        booking_urls = build_airline_search_urls(
            carrier_out=c_out,
            carrier_in=c_in,
            origin=ORIGIN,
            dest=DESTINATION,
            dep_date=dep_date,
            ret_date=ret_date,
        )

        fare_type_desc = "1 авиакомпания (сумма плеч)" if not is_mixed else "2 отдельных билета"

        return {
            "dep_date": dep_date,
            "ret_date": ret_date,
            "outbound": out_l,
            "inbound": in_l,
            "total_roundtrip": total_rt,
            "base_fare": None,      # Honest: no fabricated 0.82 tax splits
            "taxes": None,          # Taxes determined at airline checkout
            "is_mixed_carrier": is_mixed,
            "carrier_out": c_out,
            "carrier_in": c_in,
            "primary_carrier": c_out if not is_mixed else f"{c_out}+{c_in}",
            "fare_type_desc": fare_type_desc,
            "stops_desc": f"1 пересадка ({out_l['via']} / {in_l['via']}) • {fare_type_desc}",
            "duration_str": f"{out_l['duration_str']} / {in_l['duration_str']}",
            "booking_urls": booking_urls,
            "booking_url": booking_urls["google_flights"],
        }

    # 1. Overall cheapest combination
    best_out = out_legs[0]
    best_in = in_legs[0]
    options.append(_make_option(best_out, best_in))
    seen_pairs.add((best_out["flight_no"], best_in["flight_no"]))

    # 2. Same-carrier Round-Trips for all available carriers
    for carrier in ALLOWED_CARRIERS:
        if carrier in out_by_carrier and carrier in in_by_carrier:
            o_leg = out_by_carrier[carrier]
            i_leg = in_by_carrier[carrier]
            key = (o_leg["flight_no"], i_leg["flight_no"])
            if key not in seen_pairs:
                options.append(_make_option(o_leg, i_leg))
                seen_pairs.add(key)
        elif carrier in in_by_carrier:
            # Carrier operates return leg (e.g. JetBlue SFO->JFK->POP), pair with best outbound
            o_leg = best_out
            i_leg = in_by_carrier[carrier]
            key = (o_leg["flight_no"], i_leg["flight_no"])
            if key not in seen_pairs:
                options.append(_make_option(o_leg, i_leg))
                seen_pairs.add(key)
        elif carrier in out_by_carrier:
            o_leg = out_by_carrier[carrier]
            i_leg = best_in
            key = (o_leg["flight_no"], i_leg["flight_no"])
            if key not in seen_pairs:
                options.append(_make_option(o_leg, i_leg))
                seen_pairs.add(key)

    options.sort(key=lambda x: x["total_roundtrip"])
    return options, used_network


# --- SQLite Database Management ---

def init_database(db_path: Path = DB_PATH):
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS flight_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                origin TEXT NOT NULL,
                dest TEXT NOT NULL,
                dep_date TEXT NOT NULL,
                ret_date TEXT,
                stay_days INTEGER,
                price_usd REAL NOT NULL,
                base_fare REAL,
                taxes REAL,
                outbound_flight TEXT,
                inbound_flight TEXT,
                stops_desc TEXT,
                is_deal INTEGER DEFAULT 0,
                booking_url TEXT
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_sfo_dep_ret
            ON flight_quotes(origin, dest, dep_date, ret_date)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS daily_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                date TEXT NOT NULL,
                return_date TEXT,
                trip_type TEXT NOT NULL,
                base_fare REAL,
                taxes REAL,
                total_price REAL NOT NULL,
                is_promo INTEGER DEFAULT 0,
                duration_str TEXT,
                flight_no TEXT,
                booking_url TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS scan_runs (
                run_id TEXT PRIMARY KEY,
                route_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL,
                expected_pairs INTEGER NOT NULL,
                attempted_pairs INTEGER NOT NULL,
                completed_pairs INTEGER NOT NULL,
                total_quotes INTEGER NOT NULL,
                error_message TEXT
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_scan_runs_route_ts
            ON scan_runs(route_id, timestamp)
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS partial_flight_quotes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                origin TEXT NOT NULL,
                dest TEXT NOT NULL,
                dep_date TEXT NOT NULL,
                ret_date TEXT,
                stay_days INTEGER,
                price_usd REAL NOT NULL,
                outbound_flight TEXT,
                inbound_flight TEXT,
                stops_desc TEXT,
                is_deal INTEGER DEFAULT 0,
                booking_url TEXT
            )
        """)
        conn.commit()
    finally:
        if conn:
            conn.close()


def save_scan_data_and_status(
    db_path: Path,
    run_id: str,
    route_id: str,
    timestamp: str,
    status: str,
    expected_pairs: int,
    attempted_pairs: int,
    completed_pairs: int,
    total_quotes: int,
    error_message: Optional[str] = None,
    records: Optional[List[Dict[str, Any]]] = None,
    is_partial: bool = False,
) -> None:
    """
    Saves quotes/prices AND scan_runs metadata in a SINGLE atomic SQLite transaction.
    Prevents half-written states and guarantees metadata consistency.
    """
    records = records or []
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")

        # 1. Insert quotes
        if is_partial and records:
            partial_rows = [
                (
                    run_id,
                    r["timestamp"],
                    r["origin"],
                    r["dest"],
                    r["dep_date"],
                    r.get("ret_date"),
                    r.get("stay_days"),
                    r["price_usd"],
                    r.get("outbound_flight"),
                    r.get("inbound_flight"),
                    r.get("stops_desc", "1 пересадка"),
                    1 if r.get("is_deal") else 0,
                    r["booking_url"],
                )
                for r in records
            ]
            cur.executemany("""
                INSERT INTO partial_flight_quotes (
                    run_id, timestamp, origin, dest, dep_date, ret_date, stay_days,
                    price_usd, outbound_flight, inbound_flight, stops_desc, is_deal, booking_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, partial_rows)
        elif not is_partial and records:
            quotes_rows = [
                (
                    r["timestamp"],
                    r["origin"],
                    r["dest"],
                    r["dep_date"],
                    r.get("ret_date"),
                    r.get("stay_days"),
                    r["price_usd"],
                    r.get("base_fare"),
                    r.get("taxes"),
                    r.get("outbound_flight"),
                    r.get("inbound_flight"),
                    r.get("stops_desc", "1 пересадка"),
                    1 if r.get("is_deal") else 0,
                    r["booking_url"],
                )
                for r in records
            ]
            cur.executemany("""
                INSERT INTO flight_quotes (
                    timestamp, origin, dest, dep_date, ret_date, stay_days,
                    price_usd, base_fare, taxes, outbound_flight, inbound_flight, stops_desc, is_deal, booking_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, quotes_rows)

            daily_rows = [
                (
                    r["timestamp"],
                    r["origin"],
                    r["dest"],
                    r["dep_date"],
                    r.get("ret_date"),
                    "round-trip" if r.get("ret_date") else "one-way",
                    r.get("base_fare"),
                    r.get("taxes"),
                    r["price_usd"],
                    1 if r.get("is_deal") else 0,
                    r.get("duration_str", ""),
                    f"{r.get('outbound_flight', '')} / {r.get('inbound_flight', '')}",
                    r["booking_url"],
                )
                for r in records
            ]
            cur.executemany("""
                INSERT INTO daily_prices (
                    timestamp, origin, destination, date, return_date, trip_type,
                    base_fare, taxes, total_price, is_promo, duration_str, flight_no, booking_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, daily_rows)

        # 2. Insert or replace scan_runs metadata
        cur.execute("""
            INSERT OR REPLACE INTO scan_runs (
                run_id, route_id, timestamp, status,
                expected_pairs, attempted_pairs, completed_pairs, total_quotes, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run_id, route_id, timestamp, status,
            expected_pairs, attempted_pairs, completed_pairs, total_quotes, error_message
        ))

        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        raise e
    finally:
        if conn:
            conn.close()


def record_scan_run(
    db_path: Path,
    run_id: str,
    route_id: str,
    timestamp: str,
    status: str,
    expected_pairs: int,
    attempted_pairs: int,
    completed_pairs: int,
    total_quotes: int,
    error_message: Optional[str] = None,
) -> None:
    """Records run execution metadata in the scan_runs table."""
    save_scan_data_and_status(
        db_path=db_path,
        run_id=run_id,
        route_id=route_id,
        timestamp=timestamp,
        status=status,
        expected_pairs=expected_pairs,
        attempted_pairs=attempted_pairs,
        completed_pairs=completed_pairs,
        total_quotes=total_quotes,
        error_message=error_message,
        records=None,
        is_partial=False,
    )


def save_partial_quotes_batch(db_path: Path, run_id: str, records: List[Dict[str, Any]]):
    """Saves partial scan results into diagnostic table partial_flight_quotes."""
    if not records:
        return
    save_scan_data_and_status(
        db_path=db_path,
        run_id=run_id,
        route_id="sfo",
        timestamp=records[0]["timestamp"] if records else datetime.now(timezone.utc).isoformat(),
        status="partial",
        expected_pairs=len(records),
        attempted_pairs=len(records),
        completed_pairs=len(records),
        total_quotes=len(records),
        error_message="Partial quotes batch save",
        records=records,
        is_partial=True,
    )


def save_quotes_batch(db_path: Path, records: List[Dict[str, Any]]):
    """Saves complete flight quotes batch."""
    if not records:
        return
    save_scan_data_and_status(
        db_path=db_path,
        run_id=f"batch_{int(time.time())}",
        route_id="sfo",
        timestamp=records[0]["timestamp"] if records else datetime.now(timezone.utc).isoformat(),
        status="complete",
        expected_pairs=len(records),
        attempted_pairs=len(records),
        completed_pairs=len(records),
        total_quotes=len(records),
        error_message=None,
        records=records,
        is_partial=False,
    )


def maintain_database(db_path: Path = DB_PATH, retention_days: int = RETENTION_DAYS) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA busy_timeout=5000")
        cur = conn.cursor()
        cur.execute("DELETE FROM flight_quotes WHERE timestamp < ?", (cutoff,))
        cur.execute("DELETE FROM daily_prices WHERE timestamp < ?", (cutoff,))
        conn.commit()
        cur.execute("PRAGMA page_count")
        pages = cur.fetchone()[0]
        cur.execute("PRAGMA freelist_count")
        free = cur.fetchone()[0]
        if free > max(8, pages * 0.2):
            conn.execute("VACUUM")
    finally:
        if conn:
            conn.close()


# --- Telegram Notification Helper ---

def send_telegram_alert(record: Dict[str, Any]) -> bool:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        env_path = BASE_DIR / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    bot_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                elif line.startswith("TELEGRAM_CHAT_ID="):
                    chat_id = line.split("=", 1)[1].strip().strip('"').strip("'")

    if not bot_token or not chat_id:
        return False

    fare_note = "2 отдельных билета" if record.get("is_mixed_carrier") else "1 авиакомпания (сумма плеч)"
    safe_url = record["booking_url"].replace("&", "&amp;")

    msg = (
        f"🇺🇸 <b>СУПЕРЦЕНА: ПУЭРТО-ПЛАТА ➔ САН-ФРАНЦИСКО (1 ПЕРЕСАДКА)</b>\n\n"
        f"📍 <b>Маршрут:</b> {record['origin']} ⇄ {record['dest']} ({record.get('stops_desc')})\n"
        f"🛫 <b>Туда:</b> {record['dep_date']} ({record.get('outbound_flight')})\n"
        f"🛬 <b>Обратно:</b> {record['ret_date']} ({record.get('inbound_flight')}) — {record['stay_days']} ночей\n"
        f"💵 <b>Итого: ${record['price_usd']:.2f} USD</b> ({fare_note})\n\n"
        f"🔗 <a href=\"{safe_url}\">Выбрать рейс на Google Flights ↗</a>"
    )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            return resp.status == 200
    except Exception:
        return False


# --- Main Scanner Runner ---

def run_monitor(
    start_dep_str: str = DEFAULT_START_DEP,
    max_ret_str: str = DEFAULT_MAX_RET,
    step_days: int = DEP_STEP,
    durations: Optional[List[int]] = None,
    threshold: float = ALERT_THRESHOLD_USD,
    open_browser: bool = False,
    notify_deals: bool = False,
) -> List[Dict[str, Any]]:
    """
    Execute POP <-> SFO 1-stop flight monitor for AA, B6, UA, CM with zero ban risk,
    health metrics, circuit breaker, honest multi-carrier pricing, and atomic persistence.
    """
    # In-process concurrency lock: reject parallel execution in the same process
    if not _SCAN_LOCK.acquire(blocking=False):
        logging.warning("Scan is already running in another thread! Rejecting concurrent execution.")
        raise RuntimeError("Concurrent scan execution rejected by in-process lock.")

    try:
        # Reset transient session counters, caches, and failed dates
        reset_session_state()

        # Threshold validation: reject NaN, Inf, non-positive
        if math.isnan(threshold) or math.isinf(threshold) or threshold <= 0:
            threshold = ALERT_THRESHOLD_USD

        init_database(DB_PATH)
        durations = durations or STAY_DURATIONS
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        run_id = f"sfo_{now.strftime('%Y%m%d_%H%M%S')}"

        start_dep, max_ret = resolve_window_dates(start_dep_str, max_ret_str)

        print("\n" + "=" * 80)
        print("🇺🇸 ROBUST FLIGHT MONITOR: PUERTO PLATA (POP) ⇄ SAN FRANCISCO (SFO)")
        print(f"🕒 Время запуска (UTC): {now.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"📍 Маршрут: строго {ORIGIN} (Puerto Plata) ⇄ {DESTINATION} (San Francisco)")
        print(f"📅 Окно: вылет не ранее {start_dep.strftime('%Y-%m-%d')}, прилет не позднее {max_ret.strftime('%Y-%m-%d')}")
        print(f"⏱ Шаг перебора дат вылета: {step_days} день (без пропусков)")
        print(f"🧳 Длительности пребывания (Stay): {min(durations)}..{max(durations)} ночей ({len(durations)} вариантов)")
        print(f"✈️ Авиакомпании: {', '.join(f'{v} ({k})' for k, v in ALLOWED_CARRIERS.items())}")
        print("🛑 Пересадки: СТРОГО 1 ПЕРЕСАДКА (60 мин <= стыковка <= 12 ч)")
        print(f"💵 Порог суперцены: <= ${threshold:.0f} USD")
        print("=" * 80 + "\n")

        all_pairs = generate_flight_matrix(
            step_days=step_days,
            start_dep_str=start_dep.strftime("%Y-%m-%d"),
            max_ret_str=max_ret.strftime("%Y-%m-%d"),
            durations=durations,
        )
        print(f"📐 Матрица дат: сгенерировано {len(all_pairs)} комбинаций")

        raw_pairs = [(p["dep_date"], p["ret_date"], p["stay_days"]) for p in all_pairs]
        candidate_pairs = order_pairs_for_leg_cache(raw_pairs)

        # Record initial status = 'running' so aborted/crashed processes are identified as incomplete by Guard
        record_scan_run(
            db_path=DB_PATH,
            run_id=run_id,
            route_id="sfo",
            timestamp=now_iso,
            status="running",
            expected_pairs=len(candidate_pairs),
            attempted_pairs=0,
            completed_pairs=0,
            total_quotes=0,
            error_message=None,
        )

        print(f"🔍 Опрос связок с кэшированием плеч (Leg Cache & Disk Cache TTL 12h)...\n")

        results: List[Dict[str, Any]] = []
        circuit_broken = False
        last_attempted_idx = 0
        error_msg = None
        successful_pairs: Set[Tuple[str, str]] = set()

        for idx, (dep_str, ret_str, stay) in enumerate(candidate_pairs, 1):
            last_attempted_idx = idx
            if CONSECUTIVE_ERRORS >= MAX_CONSECUTIVE_ERRORS:
                print(f"⚠️ [CIRCUIT BREAKER] Скан прерван: {CONSECUTIVE_ERRORS} сетевых ошибок подряд.")
                circuit_broken = True
                error_msg = f"Circuit Breaker tripped after {CONSECUTIVE_ERRORS} consecutive network errors"
                break

            options, used_net = fetch_live_roundtrip_options(dep_str, ret_str, max_ret_date=max_ret)

            # Check if neither departure nor return had an unhandled network/schema error
            dep_failed = (ORIGIN, DESTINATION, dep_str) in FAILED_DATES
            ret_failed = (DESTINATION, ORIGIN, ret_str) in FAILED_DATES
            if not dep_failed and not ret_failed:
                successful_pairs.add((dep_str, ret_str))

            if not options:
                if used_net:
                    time.sleep(random.uniform(1.2, 2.0))
                continue

            quote = options[0]
            total_price = quote["total_roundtrip"]

            for opt_idx, opt in enumerate(options):
                opt_price = opt["total_roundtrip"]
                opt_deal = (opt_price <= threshold)

                record = {
                    "timestamp": now_iso,
                    "origin": ORIGIN,
                    "dest": DESTINATION,
                    "dep_date": dep_str,
                    "ret_date": ret_str,
                    "stay_days": stay,
                    "price_usd": opt_price,
                    "base_fare": None,
                    "taxes": None,
                    "is_mixed_carrier": opt["is_mixed_carrier"],
                    "outbound_flight": opt["outbound"]["flight_no"],
                    "inbound_flight": opt["inbound"]["flight_no"],
                    "stops_desc": opt["stops_desc"],
                    "is_deal": opt_deal,
                    "duration_str": opt["duration_str"],
                    "booking_url": opt["booking_url"],
                }
                results.append(record)
                if opt_deal and notify_deals:
                    send_telegram_alert(record)

            deal_marker = "🔥 [СУПЕРЦЕНА]" if total_price <= threshold else "⚪"
            fl_out = quote["outbound"]["flight_no"]
            fl_in = quote["inbound"]["flight_no"]
            fare_type = "2 билета" if quote["is_mixed_carrier"] else "1 авиакомпания"
            print(
                f"[{idx:>3}/{len(candidate_pairs)}] {dep_str} ➔ {ret_str} ({stay:2d} н.) | "
                f"{fl_out} + {fl_in} = ${total_price:>6.2f} USD ({fare_type}) {deal_marker}"
            )

            if used_net:
                time.sleep(random.uniform(1.2, 2.0))

        # Health check & metrics
        total_dates_queried = STATS["network_ok"] + STATS["network_empty"] + STATS["network_error"]
        success_rate = (
            (STATS["network_ok"] + STATS["network_empty"] + STATS["disk_cache_hits"])
            / max(1, total_dates_queried + STATS["disk_cache_hits"])
        )

        print("\n" + "=" * 80)
        print(
            f"📊 Статистика опроса POP⇄SFO: Запросов OK={STATS['network_ok']}, Пусто={STATS['network_empty']}, "
            f"Ошибок={STATS['network_error']}, Диск-кэш={STATS['disk_cache_hits']}, Память-кэш={STATS['mem_cache_hits']}"
        )
        print(f"📈 Метрика здоровья скана: {success_rate * 100:.1f}% успешно. Найдено связок: {len(results)}")
        print("=" * 80)

        completed_pairs_count = len(successful_pairs)
        coverage_ratio = completed_pairs_count / max(1, len(candidate_pairs))

        if circuit_broken:
            logging.error(f"[CIRCUIT BREAKER] Aborted scan: {CONSECUTIVE_ERRORS} consecutive network errors. Scan is INCOMPLETE.")
            save_scan_data_and_status(
                db_path=DB_PATH,
                run_id=run_id,
                route_id="sfo",
                timestamp=now_iso,
                status="partial",
                expected_pairs=len(candidate_pairs),
                attempted_pairs=last_attempted_idx,
                completed_pairs=completed_pairs_count,
                total_quotes=len(results),
                error_message=error_msg,
                records=results,
                is_partial=True,
            )
            raise RuntimeError(f"Scan aborted by Circuit Breaker after {CONSECUTIVE_ERRORS} consecutive errors.")

        # Determine complete status strictly: coverage >= 85% AND >= 80 quotes
        is_complete = (
            not circuit_broken
            and coverage_ratio >= MIN_COVERAGE_RATIO
            and len(results) >= MIN_REQUIRED_COMPLETE_QUOTES
        )

        if is_complete:
            final_status = "complete"
            save_scan_data_and_status(
                db_path=DB_PATH,
                run_id=run_id,
                route_id="sfo",
                timestamp=now_iso,
                status=final_status,
                expected_pairs=len(candidate_pairs),
                attempted_pairs=len(candidate_pairs),
                completed_pairs=completed_pairs_count,
                total_quotes=len(results),
                error_message=None,
                records=results,
                is_partial=False,
            )
            maintain_database(DB_PATH)
        elif results:
            final_status = "partial"
            logging.warning(
                f"Scan produced {len(results)} quotes with {coverage_ratio*100:.1f}% coverage "
                f"(requirements: >={MIN_COVERAGE_RATIO*100:.0f}%, >={MIN_REQUIRED_COMPLETE_QUOTES} quotes). Marking as PARTIAL."
            )
            save_scan_data_and_status(
                db_path=DB_PATH,
                run_id=run_id,
                route_id="sfo",
                timestamp=now_iso,
                status=final_status,
                expected_pairs=len(candidate_pairs),
                attempted_pairs=len(candidate_pairs),
                completed_pairs=completed_pairs_count,
                total_quotes=len(results),
                error_message=f"Partial scan: {len(results)} quotes, coverage={coverage_ratio*100:.1f}%",
                records=results,
                is_partial=True,
            )
        else:
            final_status = "empty" if coverage_ratio >= MIN_COVERAGE_RATIO else "partial"
            save_scan_data_and_status(
                db_path=DB_PATH,
                run_id=run_id,
                route_id="sfo",
                timestamp=now_iso,
                status=final_status,
                expected_pairs=len(candidate_pairs),
                attempted_pairs=len(candidate_pairs),
                completed_pairs=completed_pairs_count,
                total_quotes=0,
                error_message="No flight options found for candidate date pairs",
                records=[],
                is_partial=False,
            )

        try:
            from generate_pop_sfo_dashboard import export_dashboard
            export_dashboard(open_in_browser=open_browser)
        except Exception as e:
            print(f"Ошибка экспорта дашборда POP-SFO: {e}")

        return results
    finally:
        _SCAN_LOCK.release()


def main():
    parser = argparse.ArgumentParser(description="Robust POP <-> SFO 1-Stop Flight Price Monitor (AA, B6, UA, CM)")
    parser.add_argument("--step-days", type=int, default=DEP_STEP, help="Date step in days (default: 1 day)")
    parser.add_argument("--start-date", type=str, default=DEFAULT_START_DEP, help=f"Earliest departure date (default: {DEFAULT_START_DEP})")
    parser.add_argument("--end-date", type=str, default=DEFAULT_MAX_RET, help=f"Latest return date (default: {DEFAULT_MAX_RET})")
    parser.add_argument("--durations", nargs="+", type=int, default=STAY_DURATIONS, help="Stay durations in nights (default: 14..30)")
    parser.add_argument("--threshold", type=float, default=ALERT_THRESHOLD_USD, help=f"Alert threshold in USD (default: ${ALERT_THRESHOLD_USD})")
    parser.add_argument("--browser", action="store_true", help="Automatically open results dashboard in Chrome")
    parser.add_argument("--notify-deals", action="store_true", help="Send individual deal alerts during scan")
    args = parser.parse_args()

    try:
        run_monitor(
            start_dep_str=args.start_date,
            max_ret_str=args.end_date,
            step_days=args.step_days,
            durations=args.durations,
            threshold=args.threshold,
            open_browser=args.browser,
            notify_deals=args.notify_deals,
        )
    except RuntimeError as e:
        logging.critical(f"FATAL: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
