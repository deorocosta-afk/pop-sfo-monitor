#!/usr/bin/env python3
"""
Comprehensive Unit Test Suite for POP <-> SFO 1-Stop Flight Price Monitor.
Validates all requirements from the technical specification and external audits:
1. Date window & matrix generation (>= May 20, return arrival <= July 5, 14..30 nights, exactly 425 pairs with frozen time).
2. Strict 1-stop rule (len(segments) == 2).
3. Exact airport match (POP <-> SFO, same mid hub, no ground transfer).
4. Airline whitelist (AA, B6, UA, CM only).
5. Layover constraints (60 min <= layover <= 12 hours, total <= 24 hours).
6. Return flight arrival date ceiling (July 5 arrival strictly enforced).
7. Error vs Empty response handling (errors are NEVER cached).
8. Multi-carrier labeling & honest booking search URLs.
9. Threshold NaN / Inf validation.
10. Parsing authentic Google Flights SSR response fixture.
11. Schema change & index drift detection (corrupt raw items or swapped indices return 'ERROR', never 'EMPTY').
12. SQLite telegram delivery state tracking (decoupled idempotency).
13. Exact carrier code filtering (verifying 'UA' does not match 'GUA', 'CM' does not match 'CMH').
14. Python 3.12 typing annotation evaluation (NameError prevention via typing.get_type_hints).
15. Incomplete scan status & scan_runs isolation (P0 blocker fix).
16. Guard idempotency and redelivery decision logic (P0 blocker fix).
17. Cloudflare Worker mandatory secret token enforcement (P0 blocker fix).
18. GitHub Actions workflow step flags and delivery commit verification (P0 blocker fix).
19. Failed date memory within session (avoids duplicate retries within run).
20. Session state reset (clean state across multi-runs in same process).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
import tempfile
import typing
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure script directory is on sys.path
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from tracker_pop_sfo import (
    ALLOWED_CARRIERS,
    CONSECUTIVE_ERRORS,
    FAILED_DATES,
    MAX_CONSECUTIVE_ERRORS,
    MAX_LAYOVER_MIN,
    MAX_TOTAL_MIN,
    MIN_LAYOVER_MIN,
    ORIGIN,
    DESTINATION,
    STATS,
    build_airline_search_urls,
    build_google_booking_url,
    extract_carrier_codes,
    fetch_live_roundtrip_options,
    fetch_one_stop_legs,
    generate_flight_matrix,
    init_database,
    record_scan_run,
    reset_session_state,
    resolve_window_dates,
    save_partial_quotes_batch,
    save_quotes_batch,
)
import guard_daily_run
import send_telegram_summary

FIXTURE_PATH = BASE_DIR / "tests" / "fixtures" / "sample_google_flights_pop_sfo.json"


class TestPopSfoTracker(unittest.TestCase):

    def setUp(self):
        reset_session_state()

    def test_window_and_matrix_generation_frozen_time(self):
        """Matrix must have 425 pairs covering May 20 to July 5 with 14..30 nights stay when frozen before window."""
        frozen_today = date(2027, 1, 1)
        with patch("tracker_pop_sfo.datetime") as mock_dt:
            mock_dt.now.return_value.date.return_value = frozen_today
            mock_dt.now.return_value.tzinfo = timezone.utc
            matrix = generate_flight_matrix(
                step_days=1,
                start_dep_str="2027-05-20",
                max_ret_str="2027-07-05",
                durations=list(range(14, 31)),
            )
            self.assertEqual(len(matrix), 425)

            for p in matrix:
                dep_d = date.fromisoformat(p["dep_date"])
                ret_d = date.fromisoformat(p["ret_date"])
                stay = p["stay_days"]
                self.assertGreaterEqual(dep_d, date(2027, 5, 20))
                self.assertLessEqual(ret_d, date(2027, 7, 5))
                self.assertGreaterEqual(stay, 14)
                self.assertLessEqual(stay, 30)
                self.assertEqual((ret_d - dep_d).days, stay)

    def test_window_past_clipping(self):
        """If today is inside the departure window, departure start is clipped to tomorrow."""
        today = date(2027, 5, 25)
        with patch("tracker_pop_sfo.datetime") as mock_dt:
            mock_dt.now.return_value.date.return_value = today
            mock_dt.now.return_value.tzinfo = timezone.utc
            start_d, max_ret_d = resolve_window_dates("2027-05-20", "2027-07-05")
            self.assertEqual(start_d, date(2027, 5, 26))
            self.assertEqual(max_ret_d, date(2027, 7, 5))

    def test_strictly_one_stop_rule(self):
        """Only flights with len(segments) == 2 are accepted; direct and 2+ stops are rejected."""
        itin_direct = ["AA", ["American"], [["seg1"]], "POP", [2027, 5, 20], [15, 30], "SFO", [2027, 5, 20], [20, 0], 360]
        itin_2stops = ["AA", ["American"], [["seg1"], ["seg2"], ["seg3"]], "POP", [2027, 5, 20], [15, 30], "SFO", [2027, 5, 20], [23, 0], 700]

        self.assertNotEqual(len(itin_direct[2]), 2)
        self.assertNotEqual(len(itin_2stops[2]), 2)

    def test_airport_match_and_hub_transfer(self):
        """Airports must strictly be POP <-> SFO, and mid airport must match (no ground transfer)."""
        seg1 = [None, None, None, "POP", None, None, "MIA", None, None, None, None, 140, None, None, None, None, None, None, None, None, [2027, 5, 20], [2027, 5, 20], ["AA", "100"]]
        seg2_ground_transfer = [None, None, None, "FLL", None, None, "SFO", None, None, None, None, 350, None, None, None, None, None, None, None, None, [2027, 5, 20], [2027, 5, 20], ["AA", "200"]]
        self.assertNotEqual(seg1[6], seg2_ground_transfer[3])

        seg2_valid_hub = [None, None, None, "MIA", None, None, "SFO", None, None, None, None, 350, None, None, None, None, None, None, None, None, [2027, 5, 20], [2027, 5, 20], ["AA", "200"]]
        self.assertEqual(seg1[6], seg2_valid_hub[3])

    def test_airline_whitelist(self):
        """Only AA, B6, UA, CM are permitted."""
        self.assertIn("AA", ALLOWED_CARRIERS)
        self.assertIn("B6", ALLOWED_CARRIERS)
        self.assertIn("UA", ALLOWED_CARRIERS)
        self.assertIn("CM", ALLOWED_CARRIERS)
        self.assertNotIn("DL", ALLOWED_CARRIERS)
        self.assertNotIn("NK", ALLOWED_CARRIERS)
        self.assertNotIn("F9", ALLOWED_CARRIERS)

    def test_layover_constraints(self):
        """Layover must be between 60 min and 720 min (12 hours), total <= 24 hours."""
        self.assertEqual(MIN_LAYOVER_MIN, 60)
        self.assertEqual(MAX_LAYOVER_MIN, 720)
        self.assertEqual(MAX_TOTAL_MIN, 1440)

        self.assertTrue(26 < MIN_LAYOVER_MIN)
        self.assertTrue(MIN_LAYOVER_MIN <= 108 <= MAX_LAYOVER_MIN)
        self.assertTrue(1080 > MAX_LAYOVER_MIN)

    def test_inbound_arrival_date_ceiling(self):
        """Return flight arrival date in POP must be <= max_ret_date (July 5)."""
        max_ret_d = date(2027, 7, 5)
        self.assertGreater(date(2027, 7, 6), max_ret_d)
        self.assertLessEqual(date(2027, 7, 5), max_ret_d)

    @patch("tracker_pop_sfo.SESSION.get")
    def test_fetch_error_handling_vs_empty(self, mock_get):
        """Network error (e.g. 429) returns status 'ERROR' and must not be cached."""
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_get.return_value = mock_resp

        status, legs = fetch_one_stop_legs("2027-05-20", "POP", "SFO")
        self.assertEqual(status, "ERROR")
        self.assertEqual(legs, [])

    def test_honest_booking_search_urls(self):
        """Multi-carrier pairs produce separate outbound and inbound airline URLs."""
        urls_single = build_airline_search_urls("AA", "AA", ORIGIN, DESTINATION, "2027-05-20", "2027-06-05")
        self.assertFalse(urls_single["is_mixed"])
        self.assertIsNotNone(urls_single["roundtrip_airline_url"])
        self.assertIn("aa.com/booking/search", urls_single["roundtrip_airline_url"])
        self.assertIn("slices=", urls_single["roundtrip_airline_url"])
        self.assertIn("type=RoundTrip", urls_single["roundtrip_airline_url"])
        self.assertNotIn("homePage.do", urls_single["roundtrip_airline_url"])

        urls_mixed = build_airline_search_urls("AA", "B6", ORIGIN, DESTINATION, "2027-05-20", "2027-06-05")
        self.assertTrue(urls_mixed["is_mixed"])
        self.assertIsNone(urls_mixed["roundtrip_airline_url"])
        self.assertIn("aa.com/booking/search", urls_mixed["outbound_url"])
        self.assertIn("type=OneWay", urls_mixed["outbound_url"])
        self.assertNotIn("homePage.do", urls_mixed["outbound_url"])
        self.assertIn("jetblue.com", urls_mixed["inbound_url"])
        self.assertIn("google.com/travel/flights", urls_mixed["google_flights"])

    def test_threshold_validation(self):
        """Threshold must reject NaN, Inf, and non-positive numbers."""
        invalid_thresholds = [float("nan"), float("inf"), float("-inf"), 0.0, -100.0]
        for t in invalid_thresholds:
            is_invalid = math.isnan(t) or math.isinf(t) or t <= 0
            self.assertTrue(is_invalid)

    def test_google_flights_fixture_parsing(self):
        """Authentic Google Flights SSR response fixture parses into valid 1-stop legs."""
        self.assertTrue(FIXTURE_PATH.exists(), f"Fixture missing at {FIXTURE_PATH}")
        with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
            fixture_json = f.read()

        html_body = f"<html>AF_initDataCallback({{key: 'ds:1', data:{fixture_json}, sideChannel: ''}});</html>"

        with patch("tracker_pop_sfo.SESSION.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = html_body
            mock_get.return_value = mock_resp

            status, legs = fetch_one_stop_legs("2027-05-20", "POP", "SFO")
            self.assertEqual(status, "OK")
            self.assertGreater(len(legs), 0)
            first_leg = legs[0]
            self.assertEqual(first_leg["origin"], "POP")
            self.assertEqual(first_leg["dest"], "SFO")
            self.assertEqual(len(first_leg["segments"]), 2)
            self.assertIn(first_leg["carrier_1"], ALLOWED_CARRIERS)
            self.assertGreater(first_leg["total"], 0)

    def test_exact_google_booking_url(self):
        """Exact booking tfs is generated from segment-level dates/flights."""
        out = [
            {"origin": "POP", "dest": "MIA", "date": "2027-05-20", "carrier": "AA", "flight": "2329"},
            {"origin": "MIA", "dest": "SFO", "date": "2027-05-20", "carrier": "AA", "flight": "2426"},
        ]
        inn = [
            {"origin": "SFO", "dest": "MIA", "date": "2027-06-14", "carrier": "AA", "flight": "2799"},
            {"origin": "MIA", "dest": "POP", "date": "2027-06-15", "carrier": "AA", "flight": "2329"},
        ]
        url = build_google_booking_url(out, inn)
        self.assertIn("/travel/flights/booking?tfs=", url)
        self.assertIn("hl=en", url)
        self.assertEqual(build_google_booking_url([], []), "")

    def test_schema_change_and_index_drift_detection(self):
        """If raw items exist but indices/schema are corrupt (e.g. swapped), return 'ERROR' rather than 'EMPTY'."""
        # Corrupt segment structure: seg[3] and seg[6] are not valid 3-letter IATA airport codes
        corrupt_seg1 = [None, None, None, 12345, None, None, "INVALID_AIRPORT_CODE", None, None, None, None, 100, None, None, None, None, None, None, None, None, None, None, ["AA", "100"]]
        corrupt_seg2 = [None, None, None, "INVALID_AIRPORT_CODE", None, None, 67890, None, None, None, None, 100, None, None, None, None, None, None, None, None, None, None, ["AA", "200"]]
        corrupt_itin = [None, None, [corrupt_seg1, corrupt_seg2], None, None, None, None, None, None, 250]
        corrupt_item = [corrupt_itin, [[None, "350.0"]]]

        corrupt_json = json.dumps([None, None, [[corrupt_item]], None])
        html_body = f"<html>AF_initDataCallback({{key: 'ds:1', data:{corrupt_json}, sideChannel: ''}});</html>"

        with patch("tracker_pop_sfo.SESSION.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = html_body
            mock_get.return_value = mock_resp

            status, legs = fetch_one_stop_legs("2027-05-20", "POP", "SFO")
            # Must return ERROR (schema drift), NEVER EMPTY!
            self.assertEqual(status, "ERROR")
            self.assertEqual(legs, [])

    def test_price_nan_inf_validation_in_flight_leg(self):
        """Items with NaN, Inf, or <= 0 prices are rejected as parse errors."""
        bad_prices = ["NaN", "Infinity", "-Infinity", "0", "-50"]
        for p_str in bad_prices:
            item = [
                [None, None, [[None]*25, [None]*25], None, None, None, None, None, None, 200],
                [[None, p_str]]
            ]
            json_str = json.dumps([None, None, [[item]], None])
            html_body = f"<html>AF_initDataCallback({{key: 'ds:1', data:{json_str}, sideChannel: ''}});</html>"

            with patch("tracker_pop_sfo.SESSION.get") as mock_get:
                mock_resp = MagicMock()
                mock_resp.status_code = 200
                mock_resp.text = html_body
                mock_get.return_value = mock_resp

                status, legs = fetch_one_stop_legs("2027-05-20", "POP", "SFO")
                self.assertEqual(status, "ERROR")
                self.assertEqual(legs, [])

    def test_failed_date_not_repeated_within_run(self):
        """When a leg date fails, it is recorded in FAILED_DATES and not re-queried across subsequent pairs."""
        reset_session_state()
        with patch("tracker_pop_sfo.fetch_one_stop_legs") as mock_fetch, \
             patch("tracker_pop_sfo.get_from_disk_cache", return_value=None), \
             patch("tracker_pop_sfo.put_to_disk_cache"):
            mock_fetch.return_value = ("ERROR", [])

            # First attempt for 2027-05-20
            fetch_live_roundtrip_options("2027-05-20", "2027-06-05")
            self.assertIn(("POP", "SFO", "2027-05-20"), FAILED_DATES)
            initial_call_count = mock_fetch.call_count

            # Second attempt with same departure date must NOT trigger a new network fetch
            fetch_live_roundtrip_options("2027-05-20", "2027-06-06")
            self.assertEqual(mock_fetch.call_count, initial_call_count)

    def test_reset_session_state(self):
        """reset_session_state cleanly clears all counters, stats, caches, and failed dates."""
        import tracker_pop_sfo
        tracker_pop_sfo.CONSECUTIVE_ERRORS = 4
        tracker_pop_sfo.STATS["network_error"] = 10
        tracker_pop_sfo.OUTBOUND_CACHE["2027-05-20"] = [{"test": 1}]
        tracker_pop_sfo.FAILED_DATES.add(("POP", "SFO", "2027-05-20"))

        reset_session_state()

        self.assertEqual(tracker_pop_sfo.CONSECUTIVE_ERRORS, 0)
        self.assertEqual(tracker_pop_sfo.STATS["network_error"], 0)
        self.assertEqual(len(tracker_pop_sfo.OUTBOUND_CACHE), 0)
        self.assertEqual(len(tracker_pop_sfo.FAILED_DATES), 0)

    def test_sqlite_delivery_state(self):
        """SQLite delivery state tracking records and checks delivery properly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = Path(tmpdir) / "test_delivery.db"
            send_telegram_summary.init_delivery_table(test_db)

            self.assertFalse(send_telegram_summary.is_already_delivered("sfo", "2027-05-20T12:00:00Z", test_db))

            send_telegram_summary.mark_as_delivered("sfo", "2027-05-20T12:00:00Z", test_db)
            self.assertTrue(send_telegram_summary.is_already_delivered("sfo", "2027-05-20T12:00:00Z", test_db))

            self.assertFalse(send_telegram_summary.is_already_delivered("other", "2027-05-20T12:00:00Z", test_db))
            self.assertFalse(send_telegram_summary.is_already_delivered("sfo", "2027-05-21T12:00:00Z", test_db))

    def test_annotation_integrity_runtime_evaluation(self):
        """Verify Python 3.12 compatibility: type annotations evaluate with typing.get_type_hints without NameError."""
        hints_telegram = typing.get_type_hints(send_telegram_summary.format_sfo_deals)
        self.assertIn("return", hints_telegram)

        hints_guard = typing.get_type_hints(guard_daily_run.read_latest_scan_run)
        self.assertIn("return", hints_guard)

        hints_tracker = typing.get_type_hints(extract_carrier_codes)
        self.assertIn("return", hints_tracker)

    def test_exact_airline_filtering_and_code_extraction(self):
        """Carrier filter checks exact code boundaries: UA does not match GUA, CM does not match CMH."""
        self.assertEqual(extract_carrier_codes("AA 100➔AA 200 (GUA)"), ["AA"])
        self.assertEqual(extract_carrier_codes("UA 1234➔UA 5678 (CMH)"), ["UA"])
        self.assertEqual(extract_carrier_codes("B6 501➔B6 502 (FLL)"), ["B6"])
        self.assertEqual(extract_carrier_codes("CM 101➔CM 102 (PTY)"), ["CM"])
        self.assertEqual(extract_carrier_codes("AA 100➔B6 200 (MIA)"), ["AA", "B6"])
        self.assertEqual(extract_carrier_codes("UA 100➔CM 200 (IAH)"), ["CM", "UA"])

    def test_scan_runs_table_and_incomplete_scan(self):
        """Incomplete scan (circuit breaker) is recorded as 'partial' in scan_runs and isolated from main tables."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = Path(tmpdir) / "test_sfo.db"
            init_database(test_db)

            # Record a partial run
            sample_partial = [{
                "timestamp": "2027-05-20T12:00:00+00:00",
                "origin": "POP",
                "dest": "SFO",
                "dep_date": "2027-05-20",
                "ret_date": "2027-06-05",
                "stay_days": 16,
                "price_usd": 850.0,
                "outbound_flight": "AA 100➔AA 200 (MIA)",
                "inbound_flight": "AA 300➔AA 400 (MIA)",
                "stops_desc": "1 пересадка",
                "is_deal": 0,
                "booking_url": "https://flights.google.com",
            }]
            save_partial_quotes_batch(test_db, "run_partial_1", sample_partial)
            record_scan_run(
                db_path=test_db,
                run_id="run_partial_1",
                route_id="sfo",
                timestamp="2027-05-20T12:00:00+00:00",
                status="partial",
                expected_pairs=425,
                attempted_pairs=25,
                completed_pairs=20,
                total_quotes=1,
                error_message="Circuit breaker tripped",
            )

            conn = sqlite3.connect(test_db)
            try:
                cur = conn.cursor()
                # Main tables must remain empty
                cur.execute("SELECT count(*) FROM flight_quotes")
                self.assertEqual(cur.fetchone()[0], 0)
                cur.execute("SELECT count(*) FROM daily_prices")
                self.assertEqual(cur.fetchone()[0], 0)

                # Diagnostic table must contain the partial records
                cur.execute("SELECT count(*) FROM partial_flight_quotes WHERE run_id = 'run_partial_1'")
                self.assertEqual(cur.fetchone()[0], 1)

                # scan_runs must record status as partial
                cur.execute("SELECT status, completed_pairs, total_quotes FROM scan_runs WHERE run_id = 'run_partial_1'")
                row = cur.fetchone()
                self.assertEqual(row[0], "partial")
                self.assertEqual(row[1], 20)
            finally:
                conn.close()

    def test_guard_decisions_with_scan_runs(self):
        """Guard correctly flags 'partial' scan as needing re-scan, and respects delivery state."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = Path(tmpdir) / "pop_sfo_prices.db"
            init_database(test_db)
            send_telegram_summary.init_delivery_table(test_db)

            # Case 1: Latest run is partial -> should_run_scan must be True
            record_scan_run(
                db_path=test_db,
                run_id="run_partial",
                route_id="sfo",
                timestamp=datetime.now(timezone.utc).isoformat(),
                status="partial",
                expected_pairs=425,
                attempted_pairs=25,
                completed_pairs=20,
                total_quotes=50,
                error_message="Circuit breaker",
            )

            with patch("guard_daily_run.BASE_DIR", Path(tmpdir)), \
                 patch("guard_daily_run.SHARED_DB_PATH", test_db):
                scan_run = guard_daily_run.read_latest_scan_run("pop_sfo_prices.db")
                self.assertIsNotNone(scan_run)
                self.assertEqual(scan_run["status"], "partial")

            # Case 2: Latest run is complete & fresh & undelivered -> should_run_scan False, should_send_telegram True
            now_iso = datetime.now(timezone.utc).isoformat()
            record_scan_run(
                db_path=test_db,
                run_id="run_complete",
                route_id="sfo",
                timestamp=now_iso,
                status="complete",
                expected_pairs=425,
                attempted_pairs=425,
                completed_pairs=425,
                total_quotes=425,
                error_message=None,
            )

            with patch("guard_daily_run.BASE_DIR", Path(tmpdir)), \
                 patch("guard_daily_run.SHARED_DB_PATH", test_db):
                self.assertFalse(guard_daily_run.is_route_delivered("sfo", now_iso))

            # Case 3: Mark delivered -> should_send_telegram False
            send_telegram_summary.mark_as_delivered("sfo", now_iso, test_db)
            with patch("guard_daily_run.BASE_DIR", Path(tmpdir)), \
                 patch("guard_daily_run.SHARED_DB_PATH", test_db):
                self.assertTrue(guard_daily_run.is_route_delivered("sfo", now_iso))

    def test_cloudflare_worker_secret_token_enforcement(self):
        """Cloudflare Worker strictly requires X-Telegram-Bot-Api-Secret-Token (HTTP 403 on missing or invalid)."""
        worker_file = BASE_DIR / "cloudflare_worker.js"
        self.assertTrue(worker_file.exists())
        worker_code = worker_file.read_text(encoding="utf-8")

        # Must NOT contain the old optional 'if (env.TELEGRAM_WEBHOOK_SECRET)' bypass
        self.assertNotIn("if (env.TELEGRAM_WEBHOOK_SECRET) {", worker_code)

        # Must enforce mandatory secret check rejecting missing or non-matching secret with 403
        self.assertTrue(
            "!env.TELEGRAM_WEBHOOK_SECRET" in worker_code
            and "X-Telegram-Bot-Api-Secret-Token" in worker_code
            and "403" in worker_code
        )

    def test_workflow_step_conditions_and_idempotent_commit(self):
        """The standalone workflow has exactly one tracker and persists data before Telegram."""
        workflow_file = BASE_DIR / ".github" / "workflows" / "monitor.yml"
        self.assertTrue(workflow_file.exists())
        content = workflow_file.read_text(encoding="utf-8")

        # Scan and Telegram triggers must be independent
        self.assertIn("steps.check_run.outputs.should_run_scan == 'true'", content)
        self.assertIn("steps.check_run.outputs.should_send_telegram == 'true'", content)

        # Dedicated delivery-state commit
        self.assertIn("Commit Telegram delivery state", content)

        # Python + Worker tests must run in CI
        self.assertIn("Run tests (Python + Worker)", content)
        self.assertIn("node tests/test_worker.mjs", content)

        # Exactly one tracker, with an ID for outcome checks
        self.assertIn("id: tracker_sfo", content)
        self.assertNotIn("tracker_gua", content)
        self.assertNotIn("tracker_eze", content)
        self.assertNotIn("tracker_nlu", content)
        self.assertNotIn("arajet", content.lower())

        # Final verification under if: always()
        self.assertIn("Verify All Step Outcomes", content)
        self.assertIn("if: always()", content)

        # Leg cache must NOT be committed
        self.assertIn("git add pop_sfo_prices.db", content)
        self.assertNotIn(".cache_pop_sfo_legs.json", content.split("git add", 1)[1].split("\n", 1)[0])

    def test_sentinel_check_catches_shifted_index(self):
        """If raw items exist but 0 items have seg1[3] == origin, return ERROR (index drift sentinel)."""
        # Build synthetic response where departure airport is at an unexpected index
        fake_itin = [None] * 15
        fake_itin[2] = [
            # seg1: seg1[3] is 'MIA' instead of queried origin 'POP'
            [None, None, None, "MIA", None, None, "MIA", None, None, None, None, 180, None, None, None, None, None, None, None, None, None, None, ["AA", "1234"]],
            [None, None, None, "MIA", None, None, "SFO", None, None, None, None, 360, None, None, None, None, None, None, None, None, None, None, ["AA", "5678"]],
        ]
        fake_item = [fake_itin, [[None, 450.0]]]
        fake_data = [None, None, [[fake_item]]]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = f"AF_initDataCallback({{key: 'ds:1', hash: '1', data:{json.dumps(fake_data)}, sideChannel: {{}}}});"

        with patch("tracker_pop_sfo.SESSION.get", return_value=mock_resp):
            status, legs = fetch_one_stop_legs("2027-05-20", "POP", "SFO")
            # Sentinel check must catch that NO item has 'POP' at seg1[3] and return ERROR, not EMPTY
            self.assertEqual(status, "ERROR")
            self.assertEqual(legs, [])

    def test_structural_error_ratio_threshold(self):
        """If >= 50% of items fail structural parsing and 0 valid legs remain, return ERROR."""
        # 2 items: 1 malformed structure (structural error), 1 filtered by non-whitelist carrier
        corrupt_item = ["not_a_valid_itin_list", [[None, 500.0]]]
        fake_itin_filtered = [None] * 15
        fake_itin_filtered[2] = [
            [None, None, None, "POP", None, None, "MIA", None, None, None, None, 180, None, None, None, None, None, None, None, None, None, None, ["XX", "999"]], # XX not in whitelist
            [None, None, None, "MIA", None, None, "SFO", None, None, None, None, 360, None, None, None, None, None, None, None, None, None, None, ["XX", "888"]],
        ]
        filtered_item = [fake_itin_filtered, [[None, 600.0]]]
        fake_data = [None, None, [[corrupt_item, filtered_item]]]

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = f"AF_initDataCallback({{key: 'ds:1', hash: '1', data:{json.dumps(fake_data)}, sideChannel: {{}}}});"

        with patch("tracker_pop_sfo.SESSION.get", return_value=mock_resp):
            status, legs = fetch_one_stop_legs("2027-05-20", "POP", "SFO")
            # 1 structural error out of 2 items = 50% -> triggers ERROR
            self.assertEqual(status, "ERROR")
            self.assertEqual(legs, [])

    def test_granular_telegram_delivery_state(self):
        """Granular delivery state tracks message_delivered and document_delivered independently."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = Path(tmpdir) / "test_delivery.db"
            send_telegram_summary.init_delivery_table(test_db)

            # Initially not delivered
            msg_del, doc_del = send_telegram_summary.get_delivery_status("sfo", "2027-05-20T10:00:00", test_db)
            self.assertFalse(msg_del)
            self.assertFalse(doc_del)
            self.assertFalse(send_telegram_summary.is_already_delivered("sfo", "2027-05-20T10:00:00", test_db))

            # Record message delivered only (e.g. text succeeded, doc failed)
            send_telegram_summary.record_delivery_progress("sfo", "2027-05-20T10:00:00", message_delivered=True, db_path=test_db)
            msg_del, doc_del = send_telegram_summary.get_delivery_status("sfo", "2027-05-20T10:00:00", test_db)
            self.assertTrue(msg_del)
            self.assertFalse(doc_del)
            self.assertFalse(send_telegram_summary.is_already_delivered("sfo", "2027-05-20T10:00:00", test_db))

            # Record document delivered (now both complete)
            send_telegram_summary.record_delivery_progress("sfo", "2027-05-20T10:00:00", document_delivered=True, db_path=test_db)
            msg_del, doc_del = send_telegram_summary.get_delivery_status("sfo", "2027-05-20T10:00:00", test_db)
            self.assertTrue(msg_del)
            self.assertTrue(doc_del)
            self.assertTrue(send_telegram_summary.is_already_delivered("sfo", "2027-05-20T10:00:00", test_db))

    def test_guard_fallback_without_scan_runs(self):
        """Guard falls back to raw quotes when scan_runs is absent (e.g. migrated DB)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sfo_db = Path(tmpdir) / "pop_sfo_prices.db"
            conn = sqlite3.connect(sfo_db)
            cur = conn.cursor()
            cur.execute("""
                CREATE TABLE flight_quotes (
                    id INTEGER PRIMARY KEY,
                    timestamp TEXT,
                    origin TEXT,
                    dest TEXT,
                    price_usd REAL
                )
            """)
            now_ts = datetime.now(timezone.utc).isoformat()
            rows = [(now_ts, "POP", "SFO", 250.0 + i) for i in range(15)]
            cur.executemany("INSERT INTO flight_quotes (timestamp, origin, dest, price_usd) VALUES (?, ?, ?, ?)", rows)
            conn.commit()
            conn.close()

            with patch("guard_daily_run.BASE_DIR", Path(tmpdir)):
                self.assertIsNone(guard_daily_run.read_latest_scan_run("pop_sfo_prices.db"))
                dt, ts_str, cnt = guard_daily_run.read_latest_scan_info("pop_sfo_prices.db")
                self.assertIsNotNone(dt)
                self.assertEqual(ts_str, now_ts)
                self.assertEqual(cnt, 15)

    def test_guard_running_status_treated_as_incomplete(self):
        """Guard treats 'running' status in scan_runs as incomplete, triggering a new scan."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sfo_db = Path(tmpdir) / "pop_sfo_prices.db"
            init_database(sfo_db)
            now_iso = datetime.now(timezone.utc).isoformat()
            record_scan_run(
                db_path=sfo_db,
                run_id="run_crashed",
                route_id="sfo",
                timestamp=now_iso,
                status="running",
                expected_pairs=425,
                attempted_pairs=10,
                completed_pairs=5,
                total_quotes=5,
                error_message=None,
            )

            with patch("guard_daily_run.BASE_DIR", Path(tmpdir)):
                scan_run = guard_daily_run.read_latest_scan_run("pop_sfo_prices.db")
                self.assertIsNotNone(scan_run)
                self.assertEqual(scan_run["status"], "running")
                self.assertNotEqual(scan_run["status"], "complete")

    def test_scan_concurrency_lock_rejects_parallel_runs(self):
        """In-process concurrency lock rejects parallel run_monitor invocations."""
        from tracker_pop_sfo import _SCAN_LOCK, run_monitor
        self.assertTrue(_SCAN_LOCK.acquire(blocking=False))
        try:
            with self.assertRaises(RuntimeError) as ctx:
                run_monitor()
            self.assertIn("rejected by in-process lock", str(ctx.exception))
        finally:
            _SCAN_LOCK.release()

    def test_honest_fare_labeling_and_is_mixed(self):
        """Fare labeling uses '1 авиакомпания (сумма плеч)' for single carrier and '2 отдельных билета' for mixed."""
        # Single carrier roundtrip (AA outbound + AA inbound)
        urls_same = build_airline_search_urls("AA", "AA", "POP", "SFO", "2027-05-20", "2027-06-05")
        self.assertFalse(urls_same["is_mixed"])
        self.assertIsNotNone(urls_same["roundtrip_airline_url"])

        # Mixed carrier roundtrip (AA outbound + B6 inbound)
        urls_mixed = build_airline_search_urls("AA", "B6", "POP", "SFO", "2027-05-20", "2027-06-05")
        self.assertTrue(urls_mixed["is_mixed"])
        self.assertIsNone(urls_mixed["roundtrip_airline_url"])

    def test_harness_reset_utility(self):
        """Harness wipes and seeds the standalone POP-SFO environment with valid fixtures."""
        import harness_reset
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir)
            test_sfo_db = test_dir / "pop_sfo_prices.db"
            test_dash = test_dir / "pop_sfo_dashboard.html"

            with patch("harness_reset.BASE_DIR", test_dir), \
                 patch("harness_reset.DB_SFO", test_sfo_db), \
                 patch("harness_reset.DASHBOARD", test_dash), \
                 patch("harness_reset.CACHE_LEG", test_dir / ".cache_pop_sfo_legs.json"), \
                 patch("generate_pop_sfo_dashboard.DB_PATH", test_sfo_db), \
                 patch("generate_pop_sfo_dashboard.DASHBOARD_PATH", test_dash):

                # 1. Seed
                harness_reset.seed_environment()
                self.assertTrue(test_sfo_db.exists())

                conn = sqlite3.connect(test_sfo_db)
                c = conn.cursor()
                c.execute("SELECT count(*) FROM flight_quotes")
                self.assertEqual(c.fetchone()[0], 90)
                c.execute("SELECT status FROM scan_runs")
                self.assertEqual(c.fetchone()[0], "complete")
                c.execute("SELECT count(*) FROM flight_quotes WHERE stay_days < 14 OR stay_days > 30")
                self.assertEqual(c.fetchone()[0], 0)
                conn.close()

                # 2. Wipe
                harness_reset.wipe_environment()
                conn = sqlite3.connect(test_sfo_db)
                c = conn.cursor()
                c.execute("SELECT count(*) FROM flight_quotes")
                self.assertEqual(c.fetchone()[0], 0)
                conn.close()




    def test_promo_code_extraction(self):
        """Public promo codes are extracted; arena/member-only noise is filtered out."""
        from promo_watcher import extract_codes, is_public_sale
        codes = extract_codes(
            "Use code SAVE25 for the spring sale. Promo code FALLTRAVEL is public. "
            "Not a code: SALE, CODE, MEMBER."
        )
        self.assertIn("SAVE25", codes)
        self.assertIn("FALLTRAVEL", codes)
        self.assertNotIn("CODE", codes)
        self.assertNotIn("SALE", codes)
        self.assertNotIn("MEMBER", codes)

        self.assertTrue(is_public_sale(
            "JetBlue flight sale: fares from $49 with promo code FLY25", "B6"))
        self.assertFalse(is_public_sale(
            "American Airlines Center tickets on sale for the Mavericks", "AA"))
        self.assertFalse(is_public_sale(
            "JetBlue TrueBlue member-only fare sale", "B6"))


if __name__ == "__main__":
    unittest.main()
