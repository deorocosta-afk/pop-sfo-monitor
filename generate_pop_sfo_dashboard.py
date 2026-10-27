#!/usr/bin/env python3
"""
Generate Interactive HTML Dashboard for POP <-> SFO 1-Stop Flight Monitor
(Puerto Plata Gregorio Luperón -> San Francisco International, strictly 1 stop, AA / B6 / UA / CM).
"""

import json
import os
import re
import sqlite3
import sys
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "pop_sfo_prices.db"
DASHBOARD_PATH = BASE_DIR / "pop_sfo_dashboard.html"
THRESHOLD_USD = 700.0


def generate_html(rows: List[Dict[str, Any]]) -> str:
    roundtrips = [r for r in rows if r.get("trip_type") == "round-trip" and r.get("return_date")]
    for r in roundtrips:
        fn = r.get("flight_no", "")
        out_fn = fn
        in_fn = ""
        if " / " in fn:
            parts = fn.split(" / ", 1)
            out_fn, in_fn = parts[0], parts[1]

        out_c = [c for c in ["AA", "B6", "UA", "CM"] if re.search(rf"\b{c}\b", out_fn)]
        in_c = [c for c in ["AA", "B6", "UA", "CM"] if re.search(rf"\b{c}\b", in_fn)]

        c_out = out_c[0] if out_c else ""
        c_in = in_c[0] if in_c else ""

        r["carrier_out"] = c_out
        r["carrier_in"] = c_in
        # Strict is_mixed criterion: compare outbound carrier vs inbound carrier
        is_mixed = bool(c_out and c_in and c_out != c_in)
        if not is_mixed and (r.get("stops_desc") and "отдельных билета" in str(r.get("stops_desc"))):
            is_mixed = True
        r["is_mixed"] = is_mixed
        r["carrier_codes"] = sorted(list(set(out_c + in_c)))

    min_roundtrip = min((r["total_price"] for r in roundtrips), default=0)
    deals_count = sum(1 for r in roundtrips if r["total_price"] <= THRESHOLD_USD)

    best_rt = min(roundtrips, key=lambda x: x["total_price"], default=None)
    best_dates_str = f"{best_rt['date']} ➔ {best_rt['return_date']}" if best_rt else "Н/Д"
    best_flight_str = best_rt.get("flight_no", "AA / B6 / UA / CM") if best_rt else ""

    updated_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    records_json = json.dumps(roundtrips, ensure_ascii=False).replace("</", "<\\/")

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Мониторинг тарифов: Пуэрто-Плата (POP) ➔ Сан-Франциско (SFO) | 1 пересадка</title>
  <style>
    :root {{
      --bg: #090d16;
      --card-bg: #131c2e;
      --card-border: #1e293b;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --accent-hover: #0284c7;
      --brand-color: #3b82f6;
      --deal-bg: rgba(16, 185, 129, 0.14);
      --deal-border: #10b981;
      --deal-text: #34d399;
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }}
    body {{
      background-color: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
      line-height: 1.5;
      padding: 24px;
    }}
    .container {{
      max-width: 1460px;
      margin: 0 auto;
    }}
    header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 16px;
      margin-bottom: 24px;
      padding-bottom: 18px;
      border-bottom: 1px solid var(--card-border);
    }}
    .header-title h1 {{
      font-size: 24px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 10px;
    }}
    .source-badge {{
      background: linear-gradient(135deg, #2563eb, #0284c7);
      color: #ffffff;
      font-size: 12px;
      padding: 4px 10px;
      border-radius: 9999px;
      font-weight: 700;
      letter-spacing: 0.4px;
    }}
    .header-title p {{
      color: var(--text-muted);
      font-size: 14px;
      margin-top: 4px;
    }}
    .refresh-badge {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      padding: 8px 14px;
      border-radius: 8px;
      font-size: 13px;
      color: var(--text-muted);
    }}
    .stats-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }}
    .stat-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 18px 20px;
      position: relative;
      overflow: hidden;
    }}
    .stat-card::before {{
      content: "";
      position: absolute;
      top: 0;
      left: 0;
      width: 100%;
      height: 3px;
      background: linear-gradient(90deg, var(--brand-color), var(--accent));
    }}
    .stat-card .label {{
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.6px;
      color: var(--text-muted);
      margin-bottom: 8px;
    }}
    .stat-card .value {{
      font-size: 28px;
      font-weight: 800;
      color: var(--text);
    }}
    .stat-card .value.deal {{
      color: var(--deal-text);
    }}
    .stat-card .desc {{
      font-size: 13px;
      color: var(--text-muted);
      margin-top: 4px;
    }}
    .controls {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 16px 20px;
      margin-bottom: 24px;
      display: flex;
      flex-wrap: wrap;
      gap: 16px;
      align-items: center;
      justify-content: space-between;
    }}
    .filter-group {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      align-items: center;
    }}
    .filter-group label {{
      font-size: 13px;
      color: var(--text-muted);
    }}
    select, input {{
      background: #090d16;
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 8px 12px;
      border-radius: 8px;
      font-size: 13px;
    }}
    .btn {{
      background: var(--accent);
      color: #090d16;
      border: none;
      padding: 8px 16px;
      border-radius: 8px;
      font-weight: 600;
      font-size: 13px;
      cursor: pointer;
      transition: all 0.15s;
    }}
    .btn:hover {{
      background: var(--accent-hover);
      color: #fff;
    }}
    .btn-outline {{
      background: transparent;
      border: 1px solid var(--card-border);
      color: var(--text);
    }}
    .btn-outline.active {{
      border-color: var(--deal-border);
      background: var(--deal-bg);
      color: var(--deal-text);
    }}
    .table-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      overflow: hidden;
    }}
    .table-header {{
      padding: 16px 20px;
      border-bottom: 1px solid var(--card-border);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .table-header h2 {{
      font-size: 16px;
      font-weight: 600;
    }}
    .table-wrapper {{
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      text-align: left;
      font-size: 14px;
    }}
    th {{
      background: rgba(15, 23, 42, 0.6);
      color: var(--text-muted);
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      padding: 12px 16px;
      border-bottom: 1px solid var(--card-border);
      cursor: pointer;
      user-select: none;
    }}
    td {{
      padding: 14px 16px;
      border-bottom: 1px solid var(--card-border);
      white-space: nowrap;
    }}
    tr:hover td {{
      background: rgba(255, 255, 255, 0.02);
    }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 600;
    }}
    .badge-deal {{
      background: var(--deal-bg);
      color: var(--deal-text);
      border: 1px solid var(--deal-border);
    }}
    .badge-normal {{
      background: #1e293b;
      color: var(--text-muted);
      border: 1px solid var(--card-border);
    }}
    .badge-onestop {{
      background: rgba(56, 189, 248, 0.15);
      border: 1px solid #38bdf8;
      color: #38bdf8;
      font-size: 11px;
      font-weight: 700;
      padding: 2px 6px;
      border-radius: 4px;
      margin-left: 6px;
    }}
    .badge-split {{
      background: rgba(245, 158, 11, 0.15);
      border: 1px solid #f59e0b;
      color: #fbbf24;
      font-size: 11px;
      font-weight: 700;
      padding: 2px 6px;
      border-radius: 4px;
    }}
    .badge-single {{
      background: rgba(16, 185, 129, 0.15);
      border: 1px solid #10b981;
      color: #34d399;
      font-size: 11px;
      font-weight: 700;
      padding: 2px 6px;
      border-radius: 4px;
    }}
    .price-tag {{
      font-size: 17px;
      font-weight: 800;
      color: #ffffff;
    }}
    .price-tag.deal {{
      color: #34d399;
    }}
    .btn-book {{
      display: inline-block;
      padding: 6px 11px;
      background: rgba(59, 130, 246, 0.2);
      border: 1px solid #3b82f6;
      color: #bae6fd;
      text-decoration: none;
      border-radius: 6px;
      font-size: 12px;
      font-weight: 600;
      transition: all 0.2s;
      margin-right: 5px;
      margin-bottom: 3px;
    }}
    .btn-book:hover {{
      background: #2563eb;
      color: #ffffff;
    }}
    .empty-state {{
      text-align: center;
      padding: 40px;
      color: var(--text-muted);
    }}
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="header-title">
        <h1>🇺🇸 Мониторинг авиабилетов <span class="source-badge">POP ⇄ SFO (1 пересадка)</span></h1>
        <p>Строго аэропорты <strong>POP (Пуэрто-Плата) ⇄ SFO (Сан-Франциско)</strong> • Вылет ≥ 20 мая, Возврат ≤ 5 июля • 14–30 ночей • AA / JetBlue / United / Copa</p>
      </div>
      <div class="refresh-badge">
        🕒 Обновлено: {updated_at}
      </div>
    </header>

    <div class="stats-grid">
      <div class="stat-card">
        <div class="label">Лучшая цена (POP ⇄ SFO)</div>
        <div class="value deal">{f"${min_roundtrip:.2f}" if min_roundtrip else "Н/Д"}</div>
        <div class="desc">Даты: {best_dates_str}</div>
      </div>
      <div class="stat-card">
        <div class="label">Лучшая комбинация рейсов</div>
        <div class="value" style="font-size: 15px; margin-top: 6px;">{best_flight_str or "Н/Д"}</div>
        <div class="desc">Строго 1 пересадка (60 мин – 12 ч стыковка)</div>
      </div>
      <div class="stat-card">
        <div class="label">Проверено связок (20.05 – 05.07)</div>
        <div class="value">{len(roundtrips)}</div>
        <div class="desc">Вариантов ≤ ${THRESHOLD_USD:.0f} USD: <strong style="color: var(--deal-text);">{deals_count}</strong></div>
      </div>
    </div>

    <div class="controls">
      <div class="filter-group">
        <button id="btnAll" class="btn btn-outline active" onclick="setDealFilter(false)">Все тарифы ({len(roundtrips)})</button>
        <button id="btnDeals" class="btn btn-outline" onclick="setDealFilter(true)">🔥 Только ≤ ${THRESHOLD_USD:.0f}</button>

        <label for="carrierFilter" style="margin-left: 8px;">Авиакомпания:</label>
        <select id="carrierFilter" onchange="renderTable()">
          <option value="all">Все авиакомпании (AA, B6, UA, CM)</option>
          <option value="B6">🟦 JetBlue (B6) — от $1,126</option>
          <option value="AA">🇺🇸 American Airlines (AA) — от $848</option>
          <option value="UA">🌐 United Airlines (UA) — от $1,079</option>
          <option value="CM">🇵🇦 Copa Airlines (CM) — от $1,052</option>
        </select>

        <label for="stayFilter" style="margin-left: 8px;">Ночей:</label>
        <select id="stayFilter" onchange="renderTable()">
          <option value="all">Все (14–30 ночей)</option>
          <option value="14">14 ночей</option>
          <option value="15">15 ночей</option>
          <option value="16">16 ночей</option>
          <option value="17">17 ночей</option>
          <option value="18">18 ночей</option>
          <option value="19">19 ночей</option>
          <option value="20">20 ночей</option>
          <option value="21">21 ночь (3 нед.)</option>
          <option value="22">22 ночи</option>
          <option value="23">23 ночи</option>
          <option value="24">24 ночи</option>
          <option value="25">25 ночей</option>
          <option value="26">26 ночей</option>
          <option value="27">27 ночей</option>
          <option value="28">28 ночей (4 нед.)</option>
          <option value="29">29 ночей</option>
          <option value="30">30 ночей</option>
        </select>

        <label for="monthFilter" style="margin-left: 8px;">Месяц вылета:</label>
        <select id="monthFilter" onchange="renderTable()">
          <option value="all">Май – Июнь</option>
        </select>
      </div>

      <div class="filter-group">
        <label for="sortBy">Сортировка:</label>
        <select id="sortBy" onchange="renderTable()">
          <option value="price_asc">Сначала самые дешёвые ($)</option>
          <option value="date_asc">По дате вылета (хронологически)</option>
          <option value="price_desc">Сначала дорогие ($)</option>
        </select>
      </div>
    </div>

    <div class="table-card">
      <div class="table-header">
        <h2>Таблица тарифов: Пуэрто-Плата (POP) ⇄ Сан-Франциско (SFO) • Строго 1 пересадка</h2>
        <span id="rowCount" style="font-size: 13px; color: var(--text-muted);"></span>
      </div>
      <div class="table-wrapper">
        <table>
          <thead>
            <tr>
              <th>Статус</th>
              <th>Маршрут</th>
              <th>Дата вылета</th>
              <th>Дата возврата</th>
              <th>Итоговая цена (USD)</th>
              <th>Количество ночей</th>
              <th>Рейсы и пересадка</th>
              <th>Время в пути</th>
              <th>Официальные ссылки на покупку</th>
            </tr>
          </thead>
          <tbody id="tableBody"></tbody>
        </table>
      </div>
    </div>
  </div>

  <script>
    const RAW_DATA = {records_json};
    const THRESHOLD = {THRESHOLD_USD};
    let onlyDeals = false;

    // Dynamically calculate and display minimum price per airline in the dropdown
    (function updateAirlineMinPrices() {{
      const minPrices = {{}};
      RAW_DATA.forEach(r => {{
        (r.carrier_codes || []).forEach(c => {{
          if (!minPrices[c] || r.total_price < minPrices[c]) {{
            minPrices[c] = r.total_price;
          }}
        }});
      }});
      const names = {{
        AA: "American Airlines",
        B6: "JetBlue",
        UA: "United Airlines",
        CM: "Copa Airlines"
      }};
      const select = document.getElementById("carrierFilter");
      if (select) {{
        for (let opt of select.options) {{
          const val = opt.value;
          if (names[val]) {{
            const minP = minPrices[val];
            opt.textContent = minP ? `${{names[val]}} (от $${{Math.round(minP)}})` : names[val];
          }}
        }}
      }}
    }})();

    // HTML Entity escaping to guarantee zero XSS injection
    function escapeHtml(text) {{
      if (text === null || text === undefined) return '';
      return String(text)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }}

    const monthsSet = new Set();
    RAW_DATA.forEach(r => {{
      if (r.date) monthsSet.add(r.date.substring(0, 7));
    }});
    const monthSelect = document.getElementById('monthFilter');
    Array.from(monthsSet).sort().forEach(m => {{
      const opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      monthSelect.appendChild(opt);
    }});

    function getAirlineBadges(codes) {{
      if (!codes || !Array.isArray(codes)) return "";
      let badges = [];
      if (codes.includes("B6")) {{
        badges.push('<span class="badge" style="background:rgba(2,132,199,0.2);border:1px solid #0284c7;color:#38bdf8;">🟦 JetBlue</span>');
      }}
      if (codes.includes("AA")) {{
        badges.push('<span class="badge" style="background:rgba(239,68,68,0.15);border:1px solid #ef4444;color:#fca5a5;">🇺🇸 American</span>');
      }}
      if (codes.includes("UA")) {{
        badges.push('<span class="badge" style="background:rgba(99,102,241,0.2);border:1px solid #6366f1;color:#a5b4fc;">🌐 United</span>');
      }}
      if (codes.includes("CM")) {{
        badges.push('<span class="badge" style="background:rgba(234,179,8,0.2);border:1px solid #eab308;color:#fde047;">🇵🇦 Copa</span>');
      }}
      return badges.join(" ");
    }}

    function getStayDays(dep, ret) {{
      if (!dep || !ret) return 0;
      const d1 = new Date(dep + 'T00:00:00Z');
      const d2 = new Date(ret + 'T00:00:00Z');
      return Math.round((d2 - d1) / (1000 * 60 * 60 * 24));
    }}

    function formatNights(n) {{
      if (!n || n <= 0) return '—';
      const mod100 = n % 100;
      if (mod100 >= 11 && mod100 <= 19) return n + ' ночей';
      const mod10 = n % 10;
      if (mod10 === 1) return n + ' ночь';
      if (mod10 >= 2 && mod10 <= 4) return n + ' ночи';
      return n + ' ночей';
    }}

    function setDealFilter(val) {{
      onlyDeals = val;
      document.getElementById('btnAll').classList.toggle('active', !val);
      document.getElementById('btnDeals').classList.toggle('active', val);
      renderTable();
    }}

    function renderTable() {{
      const carrierFilter = document.getElementById('carrierFilter').value;
      const monthFilter = document.getElementById('monthFilter').value;
      const stayFilter = document.getElementById('stayFilter').value;
      const sortBy = document.getElementById('sortBy').value;

      let filtered = RAW_DATA.filter(r => {{
        if (onlyDeals && r.total_price > THRESHOLD) return false;
        if (monthFilter !== 'all' && (!r.date || !r.date.startsWith(monthFilter))) return false;
        if (carrierFilter !== 'all' && (!r.carrier_codes || !r.carrier_codes.includes(carrierFilter))) return false;
        const stay = getStayDays(r.date, r.return_date);
        if (stayFilter !== 'all' && stay !== parseInt(stayFilter, 10)) return false;
        return true;
      }});

      filtered.sort((a, b) => {{
        if (sortBy === 'price_asc') return a.total_price - b.total_price;
        if (sortBy === 'price_desc') return b.total_price - a.total_price;
        if (sortBy === 'date_asc') return a.date.localeCompare(b.date);
        return 0;
      }});

      document.getElementById('rowCount').textContent = `Показано: ${{filtered.length}} из ${{RAW_DATA.length}}`;
      const tbody = document.getElementById('tableBody');

      if (filtered.length === 0) {{
        tbody.innerHTML = '<tr><td colspan="9" class="empty-state">Нет рейсов по выбранным фильтрам.</td></tr>';
        return;
      }}

      tbody.innerHTML = filtered.map(r => {{
        const isDeal = r.total_price <= THRESHOLD;
        const stay = getStayDays(r.date, r.return_date);
        const nightsDisplay = formatNights(stay);
        const badgeHtml = isDeal
          ? `<span class="badge badge-deal">🔥 ВЫГОДНО ≤ $${{THRESHOLD}}</span>`
          : `<span class="badge badge-normal">Стандарт</span>`;

        const [y1, m1, d1] = (r.date || "").split("-");
        const [y2, m2, d2] = (r.return_date || "").split("-");

        const isMixed = r.is_mixed;

        const fareBadge = isMixed
          ? `<span class="badge-split" title="Рейсы выполняются разными авиакомпаниями (2 отдельных билета)">⚠️ 2 отдельных билета</span>`
          : `<span class="badge-single" title="Рейсы выполняются одной авиакомпанией (сумма двух плеч)">✅ 1 авиакомпания (сумма плеч)</span>`;

        // AA's current /booking/search deep link (homePage.do is ignored by aa.com)
        const aaSlices = JSON.stringify([
          {{ orig: "POP", origNearby: false, dest: "SFO", destNearby: false, date: r.date }},
          {{ orig: "SFO", origNearby: false, dest: "POP", destNearby: false, date: r.return_date }}
        ]);
        const aaUrl = `https://www.aa.com/booking/search?locale=en_US&fareType=Lowest&pax=1&adult=1&type=RoundTrip&searchType=Revenue&cabin=&carriers=ALL&travelType=personal&slices=${{encodeURIComponent(aaSlices)}}`;
        const jbUrl = `https://www.jetblue.com/booking/flights?from=POP&to=SFO&depart=${{r.date}}&return=${{r.return_date}}&isMultiCity=false&noOfRoute=1&lang=en`;
        const uaUrl = `https://www.united.com/en/us/fsr/choose-flights?f=POP&t=SFO&d=${{r.date}}&r=${{r.return_date}}&px=1&taxng=1&idx=1`;
        const cmUrl = `https://www.copaair.com/en-gs/?origin=POP&destination=SFO&departureDate=${{r.date}}&returnDate=${{r.return_date}}&tripType=roundTrip`;
        const gfUrl = `https://www.google.com/travel/flights?q=Flights%20from%20POP%20to%20SFO%20on%20${{r.date}}%20through%20${{r.return_date}}%20with%201%20stop&curr=USD&hl=en`;

        let airlineButtons = '';
        const codes = r.carrier_codes || [];
        if (codes.includes("AA")) {{
          airlineButtons += `<a href="${{aaUrl}}" target="_blank" rel="noopener noreferrer" class="btn-book" title="Поиск на сайте American Airlines">🔍 AA.com ↗</a>`;
        }}
        if (codes.includes("B6")) {{
          airlineButtons += `<a href="${{jbUrl}}" target="_blank" rel="noopener noreferrer" class="btn-book" title="Поиск на сайте JetBlue">🔍 JetBlue.com ↗</a>`;
        }}
        if (codes.includes("UA")) {{
          airlineButtons += `<a href="${{uaUrl}}" target="_blank" rel="noopener noreferrer" class="btn-book" title="Поиск на сайте United Airlines">🔍 United.com ↗</a>`;
        }}
        if (codes.includes("CM")) {{
          airlineButtons += `<a href="${{cmUrl}}" target="_blank" rel="noopener noreferrer" class="btn-book" title="Поиск на сайте Copa Airlines">🔍 Copa.com ↗</a>`;
        }}

        return `
          <tr>
            <td>${{badgeHtml}}</td>
            <td><strong>POP ⇄ SFO</strong> <span class="badge-onestop">1 пересадка</span></td>
            <td><strong>${{escapeHtml(r.date)}}</strong></td>
            <td><strong>${{escapeHtml(r.return_date)}}</strong></td>
            <td class="price-tag ${{isDeal ? 'deal' : ''}}">$${{r.total_price.toFixed(2)}}</td>
            <td style="font-weight: 600; color: #a5b4fc;">${{nightsDisplay}}</td>
            <td>
              <div style="margin-bottom: 4px; display: flex; align-items: center; gap: 6px;">
                ${{getAirlineBadges(r.carrier_codes)}}
                ${{fareBadge}}
              </div>
              <div style="font-size: 12px; color: var(--text-muted);">${{escapeHtml(r.flight_no || '')}}</div>
            </td>
            <td style="color: var(--text-muted); font-size: 13px;">${{escapeHtml(r.duration_str || '')}}</td>
            <td>
              <a href="${{gfUrl}}" target="_blank" rel="noopener noreferrer" class="btn-book" style="background:#2563eb;color:#ffffff;font-weight:700;">🔍 Google Flights ↗</a>
              ${{airlineButtons}}
            </td>
          </tr>
        `;
      }}).join('');
    }}

    renderTable();
  </script>
</body>
</html>
"""
    return html


def export_dashboard(open_in_browser: bool = False) -> Path:
    """Read latest scan from SQLite and write HTML dashboard."""
    if not DB_PATH.exists():
        print(f"База данных не найдена: {DB_PATH}")
        return DASHBOARD_PATH

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Robust query: find latest scan for POP -> SFO without relying on URL substring
        cur.execute("""
            SELECT MAX(timestamp) FROM daily_prices
            WHERE origin = 'POP' AND destination = 'SFO'
        """)
        row = cur.fetchone()
        latest_ts = row[0] if row else None
        if not latest_ts:
            print("Нет записей в базе данных pop_sfo_prices.db для POP ⇄ SFO")
            return DASHBOARD_PATH

        cur.execute("""
            SELECT origin, destination, date, return_date, trip_type,
                   total_price, is_promo, duration_str, flight_no, booking_url
            FROM daily_prices
            WHERE origin = 'POP' AND destination = 'SFO' AND timestamp = ?
            ORDER BY total_price ASC, date ASC
        """, (latest_ts,))
        rows = [dict(r) for r in cur.fetchall()]

        if not rows:
            print(f"[WARN] Для скана {latest_ts} нет строк — дашборд не перезаписываю")
            return DASHBOARD_PATH
    finally:
        if conn:
            conn.close()

    html_content = generate_html(rows)

    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"✅ Интерактивный дашборд POP ⇄ SFO обновлён: {DASHBOARD_PATH}")

    # Optional synchronization to artifact directory if configured via environment
    artifact_env = os.environ.get("ARTIFACT_DIR")
    if artifact_env:
        artifact_dir = Path(artifact_env)
        if artifact_dir.is_dir():
            try:
                with open(artifact_dir / "pop_sfo_dashboard.html", "w", encoding="utf-8") as f:
                    f.write(html_content)
            except Exception:
                pass

    if open_in_browser:
        print("🌐 Открытие дашборда в браузере...")
        webbrowser.open(DASHBOARD_PATH.as_uri())

    return DASHBOARD_PATH


if __name__ == "__main__":
    should_open = "--open" in sys.argv
    export_dashboard(open_in_browser=should_open)
