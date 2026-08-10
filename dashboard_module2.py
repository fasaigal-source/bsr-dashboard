"""Module 2 routes — P&L / COGS / ads / postage (/pl and everything under it).
Registered on the shared `app` from dashboard_app. INDEPENDENT of Module 1:
editing this file cannot touch Module 1's routes. It imports get_accounts from
module1_db — the P&L account selector reads Module 1's accounts table (the one
shared read; no writes to Module 1 tables happen here).
"""
import os
import io
import csv
import json
import time
from datetime import datetime, timedelta
from flask import request, redirect, render_template_string, flash, Response

from pl_db import get_accounts   # Module 2's own accounts reader (Postgres/SQLite via db.py)

import pl_db
import pl_cogs
import pl_ads
import pl_postage
import pl_price
import pl_amazon
import pl_tracker
import plotly.graph_objs as go
import plotly.offline as pyo
import plotly.io as pio

from dashboard_app import app


# ─────────────────────────────────────────────────────────────────────────────
# P&L — Module 2 (read-only reporting; no forms here write anything)
# ─────────────────────────────────────────────────────────────────────────────

PL_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>P&amp;L — BSR Repricer</title>
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c}
 /* module2_dashboard_fixes D3: dark-teal link styling, replacing default
    browser blue/underline -- more specific selectors below (.header a,
    .pill.warn a, etc.) intentionally still override this base rule. */
 a{color:#0e5c5b;text-decoration:none;font-weight:600}
 a:hover{text-decoration:underline}
 a:visited{color:#0e5c5b}
 .header{background:#0e5c5b;color:#eafcfb;padding:18px 30px;display:flex;justify-content:space-between;align-items:center}
 .header a{color:#bfe9e7;font-size:13px;text-decoration:none;margin-left:16px}
 /* module2_pl_live_fixes: spec B item 7 -- the per-product rollup table was
    cramped inside the same 1280px cap used by narrower pages on this app.
    Widened to 96vw (capped at 1800px so it doesn't stretch unreasonably on
    ultra-wide monitors) so more Columns-picker-restored columns fit without
    horizontal scrolling. */
 .wrap{max-width:min(1800px, 96vw);margin:24px auto;padding:0 20px}
 .toolbar{display:flex;gap:14px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
 .pill{background:#fff;border:1px solid #dde3e9;border-radius:20px;padding:6px 14px;font-size:13px}
 .pill b{color:#0e5c5b}
 .pill.warn{border-color:#f0d38a;background:#fbf1dd;color:#8a5906}
 .pill.warn a{color:#8a5906;font-weight:700}
 select{padding:8px 12px;border:1px solid #dde3e9;border-radius:8px;font-size:13px;background:#fff}
 .card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
 .title{font-size:15px;font-weight:700;margin-bottom:12px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:8px 10px;text-align:right;border-top:1px solid #eef1f4;white-space:nowrap}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase;letter-spacing:.3px}
 th:first-child,td:first-child{text-align:left}
 tr:hover td{background:#f7fbfb}
 .pos{color:#166b3d;font-weight:600}
 .neg{color:#9e2d3c;font-weight:600}
 .muted{color:#8a94a2}
 .badge{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:10px}
 .badge.ok{background:#e7f6ee;color:#166b3d}
 .badge.no{background:#fbe8eb;color:#9e2d3c}
 .charts{display:grid;grid-template-columns:1fr 1fr;gap:16px}
 @media (max-width:900px){.charts{grid-template-columns:1fr}}
 .hint{font-size:11.5px;color:#8a94a2;margin-top:6px}
 .table-scroll{overflow-x:auto;max-width:100%}
 .table-scroll table{width:auto;min-width:100%}
 .table-scroll th:first-child,.table-scroll td:first-child{
   position:sticky;left:0;background:#fff;z-index:2;box-shadow:2px 0 4px rgba(0,0,0,.04)}
 .table-scroll tr:hover td:first-child{background:#f7fbfb}
 .col-picker{position:relative;display:inline-block}
 .col-picker-panel{display:none;position:absolute;top:100%;right:0;margin-top:6px;background:#fff;
   border:1px solid #dde3e9;border-radius:8px;padding:12px 14px;box-shadow:0 4px 14px rgba(0,0,0,.14);
   z-index:10;min-width:200px;max-height:70vh;overflow-y:auto}
 .col-picker-panel label{display:block;font-size:12.5px;margin-bottom:7px;cursor:pointer}
 .col-picker-panel label:last-child{margin-bottom:0}
 .card-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
 .card-head .title{margin-bottom:0}
 .btn{background:#0e5c5b;color:#eafcfb;border:none;border-radius:6px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer}
 .btn-sm{padding:4px 10px;font-size:11.5px}
 .btn-ghost{background:#fff;color:#5a6472;border:1px solid #dde3e9}
 .filter-bar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px;padding-bottom:14px;border-bottom:1px solid #eef1f4}
 .filter-bar select,.filter-bar input[type=number]{padding:6px 10px;font-size:12.5px}
 .filter-bar label{font-size:11.5px;color:#8a94a2;display:flex;flex-direction:column;gap:3px}
 .search-box{position:relative;flex:1 1 260px;min-width:220px}
 .search-box input{width:100%;padding:8px 32px 8px 12px;border:1px solid #dde3e9;border-radius:8px;font-size:13px}
 .search-box .clear-x{position:absolute;right:8px;top:50%;transform:translateY(-50%);border:none;background:none;
   color:#8a94a2;font-size:15px;cursor:pointer;padding:2px 6px;display:none}
 .result-count{font-size:12px;color:#5a6472;white-space:nowrap}
 .active-filters{font-size:11.5px;color:#5a6472;margin:-4px 0 12px}
 .active-filters .chip{display:inline-block;background:#eef4f3;color:#0e5c5b;border-radius:12px;padding:2px 9px;margin:2px 4px 2px 0;font-weight:600}
 th.sortable{cursor:pointer;user-select:none}
 th.sortable:hover{color:#0e5c5b}
 th.sortable .arrow{font-size:9px;margin-left:2px;opacity:.6}
 tr.pl-hidden{display:none !important}
 .chart-note{font-size:11px;color:#8a94a2;margin:-8px 0 4px;text-align:right}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  <div class="toolbar">
    {# module2_filters_in_url: was <form method="GET" action="/pl"> whose selects
       called this.form.submit(). Now the selects navigate via plNavServer (which
       preserves the URL's filter params), so the form is vestigial -- and a stray
       GET-form submit (Enter in a field / browser quirk) was the likely source of
       the trailing-"?" that accumulated on the last URL param. Plain div now: same
       flex layout, no submit path. #}
    <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
      <select name="account" onchange="plNavServer(this)">
        <option value="all" {{ "selected" if account_filter=="all" else "" }}>All accounts</option>
        {% for a in accounts %}
        <option value="{{ a.account_id }}" {{ "selected" if account_filter==a.account_id else "" }}>{{ a.account_id }}</option>
        {% endfor %}
      </select>
      <select name="range" onchange="plNavServer(this)">
        <option value="all" {{ "selected" if range_key=="all" else "" }}>All time</option>
        <option value="7" {{ "selected" if range_key=="7" else "" }}>Last 7 days</option>
        <option value="30" {{ "selected" if range_key=="30" else "" }}>Last 30 days</option>
        <option value="90" {{ "selected" if range_key=="90" else "" }}>Last 90 days</option>
        <option value="180" {{ "selected" if range_key=="180" else "" }}>Last 6 months</option>
        <option value="365" {{ "selected" if range_key=="365" else "" }}>Last year</option>
      </select>
      <select name="period" onchange="plNavServer(this)">
        <option value="day" {{ "selected" if period=="day" else "" }}>Daily</option>
        <option value="week" {{ "selected" if period=="week" else "" }}>Weekly</option>
        <option value="month" {{ "selected" if period=="month" else "" }}>Monthly</option>
      </select>
      <select name="vat" onchange="plNavServer(this)">
        <option value="ex_vat" {{ "selected" if vat_treatment=="ex_vat" else "" }}>Ex-VAT (true profit)</option>
        <option value="cash" {{ "selected" if vat_treatment=="cash" else "" }}>Cash (inc-VAT)</option>
      </select>
      <label style="font-size:12px;color:#5a6b70;display:flex;gap:4px;align-items:center;">Chart:
        <select name="metric" onchange="plNavServer(this)">
          {% for m in ["net_profit","margin_pct","revenue","units","orders","ad_spend","tacos"] %}
          <option value="{{ m }}" {{ "selected" if metric==m else "" }}>{{ chart_metric_labels[m] }}</option>
          {% endfor %}
        </select>
      </label>
      <label style="font-size:12px;color:#5a6b70;display:flex;gap:4px;align-items:center;">Chart 2:
        <select name="metric2" onchange="plNavServer(this)">
          {% for m in ["net_profit","margin_pct","revenue","units","orders","ad_spend","tacos"] %}
          <option value="{{ m }}" {{ "selected" if metric2==m else "" }}>{{ chart_metric_labels[m] }}</option>
          {% endfor %}
        </select>
      </label>
      {% if range_start and range_end %}
      <span class="muted" style="font-size:12px;">Showing orders from <b>{{ range_start }}</b> to <b>{{ range_end }}</b></span>
      {% elif canonical_rows %}
      <span class="muted" style="font-size:12px;">No orders in this window.</span>
      {% endif %}
    </div>
    <span class="pill {{ 'warn' if pending else '' }}">Pending settlement: <b>{{ pending }}</b></span>
    {% if postage_exact_total or postage_manual_total or postage_missing_total %}
    <span class="pill{{ ' warn' if postage_missing_total else '' }}">Postage: {{ postage_exact_total }} exact{% if postage_manual_total %}, {{ postage_manual_total }} manual{% endif %}{% if postage_missing_total %}, <a href="/pl/postage">{{ postage_missing_total }} missing — fill in</a>{% endif %}</span>
    {% endif %}
    {% if postage_week_count > 15 %}
    <span class="pill warn">{{ postage_week_count }} orders from 3–10 days ago still have no label cost — past the ~1–2 day posting lag (which self-heals on the next sync), so these are unlikely to be waiting on Amazon: a real pipeline gap or genuine off-Amazon couriers. <a href="/pl/postage">Review</a></span>
    {% endif %}
    {% if unpriced_total %}
    <span class="pill warn">{{ unpriced_total }} order(s) with no COGS price yet — <a href="/pl/cogs">fill in prices</a></span>
    {% endif %}
    {% if ad_coverage_warning and not ad_coverage_warning.has_data %}
    <span class="pill warn">No ad spend data uploaded yet — <a href="/pl/ads">upload a report</a> to see AD SPEND / TACOS / net profit after ads</span>
    {% elif ad_coverage_warning %}
    <span class="pill warn">Ad data only covers {{ ad_coverage_warning.cov_min }} to {{ ad_coverage_warning.cov_max }} — the selected range extends beyond this, so after-ads figures for the uncovered period are incomplete. <a href="/pl/ads">Upload more</a></span>
    {% endif %}
    {% if flips_to_loss %}
    <span class="pill warn">{{ flips_to_loss }} product(s) flip from profitable to a LOSS once ad spend is applied — <a href="{{ flip_href }}">show these</a></span>
    {% endif %}
    <span class="pill">Read-only — no SP-API writes</span>
  </div>
  <div class="hint" style="margin:-10px 0 16px;">Note: "Daily/Weekly/Monthly" only controls how the "over time" chart
    buckets dates within the selected range above — it is a separate control from the date-range filter itself.
    Wide ranges default to a coarser bucketing automatically (daily points across years of orders becomes unreadable)
    — pick Daily yourself if you want it anyway. "Chart" picks which variable the left "over time" chart plots; it
    tracks the same account/range/VAT filters as the table below, not the table's own search/filters. "Chart 2" is a
    separate selector for the right-hand "by product" bar chart — deliberately independent, since that chart tracks
    the table's own search/filters instead (see the note under the table for why).</div>

  {# module2_filters_in_url: plNavServer defined here (always rendered, before the
     empty/has-rows branch) so the toolbar's server-param selects work even in the
     empty-state view. Merges the changed param into the current URL so any active
     table-filter params survive the reload. #}
  <script>
    function plNavServer(sel){
      var u = new URL(window.location.href);
      u.searchParams.set(sel.name, sel.value);
      window.location.href = u.toString();
    }
  </script>

  {% if not canonical_rows %}
  <div class="card">
    {% if range_key != "all" %}
    <div class="muted">No orders in the selected date range (<b>{{ range_key }}</b>{{ "" if range_key=="all" else " day(s)" }}) —
    try <a href="/pl?account={{ account_filter }}&amp;range=all">All time</a> to see whether data exists outside this window.</div>
    {% else %}
    <div class="muted">No P&amp;L data yet. Run <code>python pl_tracker.py</code> to pull settled
    financials from Amazon. First run can take a while (it walks back through history);
    later runs are quick.</div>
    {% endif %}
  </div>
  {% else %}

  <!-- module2_range_summary (File H): headline totals, above the charts, first
       thing read. Totals are computed CLIENT-SIDE from the table's currently
       VISIBLE rows (see plUpdateSummary) so they always reconcile with the
       table and track its search/filters -- same pattern as the by-product bar
       chart. Provisional stays visible; the date span is the ACTUAL settled
       window, and a sync-age indicator warns when the data is stale. -->
  <div class="card" id="plSummaryCard">
    <div class="title" style="display:flex;justify-content:space-between;align-items:baseline;gap:10px;flex-wrap:wrap;">
      <span>Range summary</span>
      <span id="plSummaryScope" style="font-size:12px;font-weight:400;color:#8a94a2;"></span>
    </div>
    <div class="hint" style="margin:-2px 0 12px;line-height:1.6;">
      {% if range_start and range_end %}Covering settled orders <b>{{ range_start }}</b> → <b>{{ range_end }}</b>{% else %}No orders in this range{% endif %}
      <span id="plSyncAge"></span>
    </div>
    <!-- Headline: Net profit (after ads) is THE answer -- biggest thing here.
         KPI strip beside it. The waterfall below is supporting detail. -->
    <div style="display:flex;gap:34px;flex-wrap:wrap;align-items:flex-start;margin-bottom:16px;">
      <div style="min-width:210px;">
        <div style="font-size:11.5px;text-transform:uppercase;letter-spacing:.4px;color:#8a94a2;font-weight:600;">
          Net profit (after ads)
          <span id="sumProvFlag" class="badge no" style="display:none;vertical-align:middle;margin-left:4px;">provisional</span>
        </div>
        <div id="sumNetAfter" style="font-size:34px;font-weight:800;line-height:1.1;margin-top:3px;">£0.00</div>
      </div>
      <div style="display:flex;flex-wrap:wrap;border:1px solid #e6eaee;border-radius:10px;overflow:hidden;">
        <div style="padding:9px 18px;border-right:1px solid #e6eaee;text-align:center;">
          <div style="font-size:10.5px;color:#8a94a2;text-transform:uppercase;letter-spacing:.3px;">Orders</div>
          <div id="sumOrders" style="font-size:18px;font-weight:700;">0</div>
        </div>
        <div style="padding:9px 18px;border-right:1px solid #e6eaee;text-align:center;">
          <div style="font-size:10.5px;color:#8a94a2;text-transform:uppercase;letter-spacing:.3px;">Units</div>
          <div id="sumUnits" style="font-size:18px;font-weight:700;">0</div>
        </div>
        <div style="padding:9px 18px;border-right:1px solid #e6eaee;text-align:center;">
          <div style="font-size:10.5px;color:#8a94a2;text-transform:uppercase;letter-spacing:.3px;">Margin (after ads)</div>
          <div id="sumMargin" style="font-size:18px;font-weight:700;">—</div>
        </div>
        <div style="padding:9px 18px;border-right:1px solid #e6eaee;text-align:center;">
          <div style="font-size:10.5px;color:#8a94a2;text-transform:uppercase;letter-spacing:.3px;">Avg rev/unit (ex-VAT)</div>
          <div id="sumAsp" style="font-size:18px;font-weight:700;">—</div>
        </div>
        <div style="padding:9px 18px;text-align:center;" title="Ad spend ÷ settled gross sales. Two clocks: ad-side figures are click-attributed and drift up on re-pull; gross counts at settlement. Deliberate, unreconciled.">
          <div style="font-size:10.5px;color:#8a94a2;text-transform:uppercase;letter-spacing:.3px;">TACOS <span style="text-transform:none;">— two clocks</span></div>
          <div id="sumTacos" style="font-size:18px;font-weight:700;">—</div>
        </div>
      </div>
    </div>

    <!-- Waterfall: supporting detail. Muted, smaller, visually separated. -->
    <details open style="border-top:1px solid #eef1f4;padding-top:8px;">
      <summary style="font-size:11px;color:#8a94a2;text-transform:uppercase;letter-spacing:.3px;font-weight:600;cursor:pointer;">How it breaks down (ex-VAT)</summary>
      <div style="display:grid;grid-template-columns:1fr auto;gap:4px 28px;font-size:12.5px;max-width:440px;margin-top:9px;color:#5a6472;">
        <span>Gross sales (ex-VAT)</span><span id="sumGross" style="text-align:right;">£0.00</span>
        <span>− Referral fees</span><span id="sumRef" style="text-align:right;">−£0.00</span>
        <span>− Other fees</span><span id="sumOther" style="text-align:right;">−£0.00</span>
        <span>− Promotions</span><span id="sumPromo" style="text-align:right;">−£0.00</span>
        <span>− COGS</span><span id="sumCogs" style="text-align:right;">−£0.00</span>
        <span>− Postage</span><span id="sumPostage" style="text-align:right;">−£0.00</span>
        <span style="font-weight:700;border-top:1px solid #eef1f4;padding-top:4px;">= Net profit (before ads)</span><span id="sumNetBefore" style="text-align:right;font-weight:700;border-top:1px solid #eef1f4;padding-top:4px;">£0.00</span>
        <span>− Ad spend</span><span id="sumAdspend" style="text-align:right;">−£0.00</span>
        <span style="font-weight:600;">= Net profit (after ads)</span><span id="sumNetAfterEcho" style="text-align:right;font-weight:600;">£0.00</span>
        <span>− Overheads (period) <a href="/pl/expenses" style="font-weight:400;">edit</a></span><span id="sumOverheads" style="text-align:right;">−£0.00</span>
        <span style="font-weight:700;border-top:1px solid #eef1f4;padding-top:4px;">= Net profit after overheads</span><span id="sumNetAfterOh" style="text-align:right;font-weight:700;border-top:1px solid #eef1f4;padding-top:4px;">£0.00</span>
      </div>
      <div class="hint" style="margin-top:8px;">Overheads are the <b>whole-business</b> total for {{ range_start }} → {{ range_end }} (monthly items pro-rated by days), from the <a href="/pl/expenses">Expenses</a> page — kept out of per-order COGS and never split per product. They don't scale with the row filters above, so <b>“after overheads” is the true bottom line only at the unfiltered view</b>.</div>
    </details>
    <script>window.PL_OVERHEADS = {{ '%.4f'|format(overheads_period or 0) }};</script>
    <div id="plSummaryProvisional" class="hint" style="margin-top:10px;color:#8a5906;display:none;"></div>
    {% if ad_coverage_warning %}
    <div class="hint" style="margin-top:6px;color:#8a5906;">⚠ Net profit (after ads) is incomplete: {% if not ad_coverage_warning.has_data %}no ad spend uploaded for this range{% else %}ad data only covers {{ ad_coverage_warning.cov_min }} to {{ ad_coverage_warning.cov_max }}, short of the selected range{% endif %} — <a href="/pl/ads">upload a report</a>.</div>
    {% endif %}
    <div class="hint" style="margin-top:6px;">Promotions/other fees are already netted into "Net profit (before ads)" via Amazon's own balance-change anchor — this itemises the SAME total, it doesn't recompute it. Totals reflect the table's current filters.</div>
  </div>

  <div class="charts">
    <div class="card">{{ chart1_html|safe }}</div>
    <div class="card">{{ chart2_html|safe }}<div class="chart-note">Tracks the table's search/filters below ↓</div></div>
  </div>

  <div class="card">
    <div class="card-head">
      <div class="title">Per-product rollup{% if account_filter != "all" %} — {{ account_filter }}{% endif %}</div>
      <div class="hint" style="margin:-4px 0 8px;">
        <b>Break-even &amp; target (inc-VAT, per unit):</b> Direct = COGS + Amazon fees + label + averaged refund cost ·
        +Ads adds this ASIN's ad-cost/unit · All-in adds a per-unit overhead share (overhead allocated by revenue, so cheap SKUs carry less) ·
        Target = All-in ÷ 0.90 (10% net margin). It's a <b>live</b> number — moves with ad spend and volume.
        Non-push ASINs priced below All-in break-even show <span class="neg">red</span>;
        <span class="badge" style="background:#fbe7c6;color:#8a5906;">push</span> ASINs below break-even are shown as info, not an alarm.
        {% if breakeven_provisional %}<br><span style="color:#8a5906;">⚠ Provisional: no refund data captured for this range yet, so the refund layer is understated — not authoritative until the refund backfill lands. This note clears once refunds arrive.</span>{% endif %}
      </div>
      <div class="col-picker">
        <button type="button" class="btn btn-sm" onclick="document.getElementById('colPickerPanel').style.display = document.getElementById('colPickerPanel').style.display==='block' ? 'none' : 'block'">⚙ Columns</button>
        <div class="col-picker-panel" id="colPickerPanel">
          <!-- module2_pl_ui_fixes Fix 4: EVERY column is now toggleable, not
               just the ones that used to be optional -- listed in the same
               order as the table itself. Family/Type are the only two
               excluded from the default-visible set (Fix 3); everything
               else here defaults to shown. -->
          <label><input type="checkbox" id="colcb-sku" onchange="plSaveVisibleCols()"> Canonical SKU</label>
          <label><input type="checkbox" id="colcb-asin" onchange="plSaveVisibleCols()"> ASIN</label>
          <label><input type="checkbox" id="colcb-family" onchange="plSaveVisibleCols()"> Family</label>
          <label><input type="checkbox" id="colcb-type" onchange="plSaveVisibleCols()"> Type</label>
          <label><input type="checkbox" id="colcb-account" onchange="plSaveVisibleCols()"> Account</label>
          <label><input type="checkbox" id="colcb-orders" onchange="plSaveVisibleCols()"> Orders</label>
          <label><input type="checkbox" id="colcb-units" onchange="plSaveVisibleCols()"> Units</label>
          <label><input type="checkbox" id="colcb-gross" onchange="plSaveVisibleCols()"> Gross sales (ex-VAT)</label>
          <label><input type="checkbox" id="colcb-asp" onchange="plSaveVisibleCols()"> Avg sell price (inc-VAT)</label>
          <label><input type="checkbox" id="colcb-referral_fees" onchange="plSaveVisibleCols()"> Referral fees</label>
          <label><input type="checkbox" id="colcb-other_fees" onchange="plSaveVisibleCols()"> Other fees</label>
          <label><input type="checkbox" id="colcb-promotions" onchange="plSaveVisibleCols()"> Promotions</label>
          <label><input type="checkbox" id="colcb-cogs" onchange="plSaveVisibleCols()"> COGS</label>
          <label><input type="checkbox" id="colcb-priced" onchange="plSaveVisibleCols()"> Priced</label>
          <label><input type="checkbox" id="colcb-postage" onchange="plSaveVisibleCols()"> Postage</label>
          <label><input type="checkbox" id="colcb-netprofit" onchange="plSaveVisibleCols()"> Net profit</label>
          <label><input type="checkbox" id="colcb-margin" onchange="plSaveVisibleCols()"> Margin</label>
          <label><input type="checkbox" id="colcb-ad_spend" onchange="plSaveVisibleCols()"> Ad spend</label>
          <label><input type="checkbox" id="colcb-ad_promoted" onchange="plSaveVisibleCols()"> Ad sales — own (promoted)</label>
          <label><input type="checkbox" id="colcb-ad_halo" onchange="plSaveVisibleCols()"> Ad sales — halo</label>
          <label><input type="checkbox" id="colcb-after_ads" onchange="plSaveVisibleCols()"> Net profit (after ads)</label>
          <label><input type="checkbox" id="colcb-tacos" onchange="plSaveVisibleCols()"> TACOS</label>
          <label><input type="checkbox" id="colcb-vat" onchange="plSaveVisibleCols()"> VAT</label>
          <label><input type="checkbox" id="colcb-pending" onchange="plSaveVisibleCols()"> Pending</label>
          <label><input type="checkbox" id="colcb-postage_source" onchange="plSaveVisibleCols()"> Postage source</label>
        </div>
      </div>
    </div>

    <div class="filter-bar">
      <div class="search-box">
        <input type="text" id="plSearchBox" placeholder="Search ASIN, SKU, or title…" autocomplete="off"
               oninput="plOnFilterChange()">
        <button type="button" class="clear-x" id="plSearchClear" onclick="plClearSearch()">×</button>
      </div>
      <label>Family
        <select id="plfFamily" onchange="plOnFilterChange()">
          <option value="all">All</option>
          {% for f in pl_families %}<option value="{{ f|lower }}">{{ f }}</option>{% endfor %}
        </select>
      </label>
      <label>Type
        <select id="plfType" onchange="plOnFilterChange()">
          <option value="all">All</option>
          {% for t in pl_types %}<option value="{{ t|lower }}">{{ t }}</option>{% endfor %}
        </select>
      </label>
      <label>Priced
        <select id="plfPriced" onchange="plOnFilterChange()">
          <option value="all">All</option>
          <option value="priced">Priced only</option>
          <option value="unpriced">No-price only</option>
        </select>
      </label>
      <label>Provisional
        <select id="plfProvisional" onchange="plOnFilterChange()">
          <option value="all">All</option>
          <option value="provisional">Provisional only</option>
          <option value="complete">Complete only</option>
        </select>
      </label>
      <label>Profit (after ads)
        <select id="plfProfit" onchange="plOnFilterChange()">
          <option value="all">All</option>
          <option value="negative">Negative only</option>
          <option value="positive">Positive only</option>
        </select>
      </label>
      <label>Postage
        <select id="plfPostage" onchange="plOnFilterChange()">
          <option value="all">All</option>
          <option value="gap">Has missing/estimated</option>
          <option value="exact">All-exact only</option>
        </select>
      </label>
      <label>Margin below % (after ads)
        <input type="number" id="plfMargin" min="0" max="100" step="1" placeholder="off" style="width:70px;"
               oninput="plOnFilterChange()">
      </label>
      <label>Flip after ads
        <select id="plfFlipped" onchange="plOnFilterChange()">
          <option value="all">All</option>
          <option value="flipped">Flipped to loss only</option>
        </select>
      </label>
      <button type="button" class="btn btn-ghost btn-sm" onclick="plClearAllFilters()">Clear all filters</button>
      <span class="result-count" id="plResultCount"></span>
    </div>
    {% if ad_coverage_warning %}
    <div class="active-filters" id="plAdFilterCaveat" style="display:none;">
      ⚠ Ad data {{ "hasn't been uploaded yet" if not ad_coverage_warning.has_data else ("only covers " ~ ad_coverage_warning.cov_min ~ " to " ~ ad_coverage_warning.cov_max) }}
      for the selected range — the Profit(after ads)/Margin filters are working correctly, but "before ads" and
      "after ads" are the same number wherever ad data is absent, not a real zero-spend result.
      <a href="/pl/ads">Upload a report</a>.
    </div>
    {% endif %}
    <div class="active-filters" id="plActiveFilters" style="display:none;"></div>

    <div class="table-scroll">
    <table id="rollupTable">
      <tr>
        <th class="col-sku sortable" data-key="sku" onclick="plSortBy('sku',this)">Canonical SKU<span class="arrow"></span></th>
        <th class="col-asin sortable" data-key="asin" onclick="plSortBy('asin',this)">ASIN<span class="arrow"></span></th>
        <th class="col-family">Family</th><th class="col-type">Type</th>
        <th class="col-account sortable" data-key="account" onclick="plSortBy('account',this)">Account<span class="arrow"></span></th>
        <th class="col-orders sortable" data-key="orders" onclick="plSortBy('orders',this)">Orders<span class="arrow"></span></th>
        <th class="col-units sortable" data-key="units" onclick="plSortBy('units',this)">Units<span class="arrow"></span></th>
        <th class="col-gross sortable" data-key="gross" onclick="plSortBy('gross',this)">Gross sales (ex-VAT)<span class="arrow"></span></th>
        <th class="col-asp sortable" data-key="asp" onclick="plSortBy('asp',this)" title="Average selling price = gross sales inc-VAT ÷ units (per pack sold), over the selected date range.">Avg sell price (inc-VAT)<span class="arrow"></span></th>
        <th class="be-col" title="Direct break-even (inc-VAT, per unit): COGS + Amazon fees + shipping label + this ASIN's averaged refund cost (its refund rate × the direct cost sunk per unit).">Direct BE</th>
        <th class="be-col" title="+ Ad spend: Direct plus this ASIN's ad-cost-per-unit (its ad spend ÷ units / TACOS applied).">+Ads BE</th>
        <th class="be-col" title="All-in break-even (inc-VAT): +Ads plus a per-unit overhead share — this ASIN's REVENUE share of period overheads ÷ its units, so cheap SKUs carry less overhead/unit than dear ones. LIVE: moves with ad spend and volume — that's correct, not a bug.">All-in BE</th>
        <th class="be-col" title="Target price = All-in break-even ÷ 0.90 → a 10% NET margin on the sale price (not markup). Inc-VAT: the price to set.">Target price</th>
        <th class="col-referral_fees">Referral fees</th>
        <th class="col-other_fees">Other fees</th>
        <th class="col-promotions">Promotions</th>
        <th class="col-cogs sortable" data-key="cogs" onclick="plSortBy('cogs',this)">COGS<span class="arrow"></span></th>
        <th class="col-priced">Priced</th>
        <th class="col-postage sortable" data-key="postage" onclick="plSortBy('postage',this)">Postage<span class="arrow"></span></th>
        <th class="col-netprofit sortable" data-key="netprofit" onclick="plSortBy('netprofit',this)">Net profit<span class="arrow"></span></th>
        <th class="col-margin sortable" data-key="margin" onclick="plSortBy('margin',this)">Margin<span class="arrow"></span></th>
        <th class="col-ad_spend sortable" data-key="adspend" onclick="plSortBy('adspend',this)">Ad spend<span class="arrow"></span></th>
        <th class="col-ad_promoted sortable" data-key="adpromoted" onclick="plSortBy('adpromoted',this)">Ad sales (own)<span class="arrow"></span></th>
        <th class="col-ad_halo sortable" data-key="adhalo" onclick="plSortBy('adhalo',this)">Ad sales (halo)<span class="arrow"></span></th>
        <th class="col-after_ads sortable" data-key="afterads" onclick="plSortBy('afterads',this)">Net profit (after ads)<span class="arrow"></span></th>
        <th class="col-tacos sortable" data-key="tacos" onclick="plSortBy('tacos',this)" title="Click-attributed ad sales ÷ settled gross sales — two clocks. Ad sales drift up on re-pull as late conversions land; gross counts at settlement. Deliberate, unreconciled — see the note under the table.">TACOS<span class="muted" style="font-weight:400;">†</span><span class="arrow"></span></th>
        <th class="col-vat">VAT</th>
        <th class="col-pending">Pending</th>
        <th class="col-postage_source">Postage source</th>
      </tr>
      {% for r in canonical_rows %}
      {% set postage_gap = (r.postage_missing_count or 0) + (r.postage_estimated_count or 0) + (r.postage_provisional_count or 0) %}
      <tr
        data-search="{{ ((r.asins|join(' ')) ~ ' ' ~ (r.member_skus|join(' ')) ~ ' ' ~ (r.title or ''))|lower }}"
        data-family="{{ (r.family or '')|lower }}"
        data-type="{{ (r.product_type or '')|lower }}"
        data-priced="{{ 'yes' if r.priced else 'no' }}"
        data-provisional="{{ 'yes' if r.provisional else 'no' }}"
        data-profit-after-ads="{{ r.net_profit_after_ads if r.net_profit_after_ads is not none else (r.net_profit or 0) }}"
        data-postage-gap="{{ 'yes' if postage_gap else 'no' }}"
        data-flipped-to-loss="{{ 'yes' if r.flipped_to_loss else 'no' }}"
        data-margin-after-ads-pct="{{ ((r.margin_pct_after_ads if r.margin_pct_after_ads is not none else (r.margin_pct or 0)) * 100)|round(2) }}"
        data-sort-sku="{{ (r.canonical_sku or '')|lower }}"
        data-sort-asin="{{ (r.asins[0] if r.asins else '')|lower }}"
        data-sort-account="{{ (r.account_id or '')|lower }}"
        data-sort-orders="{{ r.orders or 0 }}"
        data-sort-units="{{ r.units or 0 }}"
        data-sort-gross="{{ r.gross_sales_exvat or 0 }}"
        data-sort-asp="{{ ((r.gross_sales_incvat or 0) / r.units) if r.units else -1 }}"
        data-sort-cogs="{{ r.cogs or 0 }}"
        data-sort-postage="{{ r.postage or 0 }}"
        data-referral="{{ r.referral_fees or 0 }}"
        data-other="{{ r.other_fees or 0 }}"
        data-promotions="{{ r.promotions or 0 }}"
        data-sort-netprofit="{{ r.net_profit or 0 }}"
        data-sort-margin="{{ r.margin_pct or 0 }}"
        data-sort-adspend="{{ r.ad_spend or 0 }}"
        data-sort-adpromoted="{{ r.ad_sales_promoted or 0 }}"
        data-sort-adhalo="{{ r.ad_sales_halo or 0 }}"
        data-sort-afterads="{{ r.net_profit_after_ads if r.net_profit_after_ads is not none else (r.net_profit or 0) }}"
        data-sort-tacos="{{ r.tacos if r.tacos is not none else -1 }}"
      >
        <td class="col-sku">
          {% set sku_href = "/pl/sku/" ~ (r.canonical_sku|urlencode) ~ "?account=" ~ (r.account_id|urlencode) ~ "&range=" ~ range_key ~ "&vat=" ~ vat_treatment ~ "&period=" ~ period ~ "&return_qs=" ~ (current_qs|urlencode) %}
          {% if r.canonical_sku %}<a href="{{ sku_href }}">{{ r.canonical_sku }}</a>{% else %}(no SKU){% endif %}
          {# module2_pl_live_fixes item 10: product title deliberately NOT shown
             in the rollup's Canonical SKU column. Diagnosis (confirmed against
             the live DB): pl_asin_titles was never populated (0 rows, no code
             deletes it; the July-6 backup predates the table) -- the only
             titles that ever rendered came from managed_asins' 2 titled
             watchlist rows (xl-pt-BD, 6337-P4), which is what produced the
             "spotty 2-of-150" inconsistency. Per Faraz: the SKU is the
             identifier here; the product title belongs on the /pl/sku detail
             page, not this column. Removed outright (rather than left data-
             driven) so a future partial backfill can't reintroduce the spotty
             look. r.title is still attached to data-search so search-by-title
             keeps working if/when titles are populated. #}
        </td>
        <td class="col-asin">{% if r.asins and r.canonical_sku %}{% for a in r.asins %}<a href="{{ sku_href }}">{{ a }}</a>{{ ", " if not loop.last else "" }}{% endfor %}{% elif r.asins %}{{ r.asins|join(', ') }}{% else %}—{% endif %}</td>
        <td class="col-family">{{ r.family or "—" }}</td>
        <td class="col-type">{{ r.product_type or "—" }}</td>
        <td class="col-account">{{ r.account_id }}</td>
        <td class="col-orders">{{ r.orders }}</td>
        <td class="col-units">{{ r.units or 0 }}</td>
        <td class="col-gross">£{{ "%.2f"|format(r.gross_sales_exvat or 0) }}</td>
        <td class="col-asp">{% if r.units %}£{{ "%.2f"|format((r.gross_sales_incvat or 0) / r.units) }}{% else %}—{% endif %}</td>
        <td class="be-col">{% if r.be_direct is not none %}£{{ "%.2f"|format(r.be_direct) }}{% else %}—{% endif %}</td>
        <td class="be-col">{% if r.be_ads is not none %}£{{ "%.2f"|format(r.be_ads) }}{% else %}—{% endif %}</td>
        <td class="be-col {{ 'neg' if r.below_breakeven else '' }}">{% if r.be_allin is not none %}£{{ "%.2f"|format(r.be_allin) }}{% if r.is_push %} <span class="badge" style="background:#fbe7c6;color:#8a5906;" title="Push mode — deliberately below break-even (rank-buying); shown as info, not an alarm.">push</span>{% endif %}{% else %}—{% endif %}</td>
        <td class="be-col"><b>{% if r.target_price is not none %}£{{ "%.2f"|format(r.target_price) }}{% else %}—{% endif %}</b></td>
        <td class="col-referral_fees">£{{ "%.2f"|format(r.referral_fees or 0) }}</td>
        <td class="col-other_fees">£{{ "%.2f"|format(r.other_fees or 0) }}</td>
        <td class="col-promotions">£{{ "%.2f"|format(r.promotions or 0) }}</td>
        <td class="col-cogs">£{{ "%.2f"|format(r.cogs or 0) }}</td>
        <td class="col-priced">
          {% if r.priced %}<span class="badge ok">priced</span>
          {% else %}
          <form method="POST" action="/pl/inline-cogs" style="display:inline-flex;gap:4px;align-items:center;"
                onsubmit="return plConfirmCogsEdit(this)">
            <input type="hidden" name="family" value="{{ r.family }}">
            <input type="hidden" name="return_url" value="{{ return_url }}">
            <input type="number" step="0.01" name="price" placeholder="£0.00" required
                   style="width:64px;padding:4px 6px;border:1px solid #dde3e9;border-radius:6px;font-size:12px;">
            <button class="btn btn-sm" type="submit">Set</button>
          </form>
          {% endif %}
        </td>
        <td class="col-postage">£{{ "%.2f"|format(r.postage or 0) }}</td>
        <td class="col-netprofit {{ 'pos' if (r.net_profit or 0) >= 0 else 'neg' }}">
          {% if r.provisional %}<span class="badge no" title="{{ r.provisional_reasons|join(', ') }}">provisional</span>{% endif %}
          £{{ "%.2f"|format(r.net_profit or 0) }}
        </td>
        <td class="col-margin">{{ "%.1f"|format((r.margin_pct or 0)*100) }}%</td>
        <td class="col-ad_spend">£{{ "%.2f"|format(r.ad_spend or 0) }}</td>
        <td class="col-ad_promoted">£{{ "%.2f"|format(r.ad_sales_promoted or 0) }}</td>
        <td class="col-ad_halo">£{{ "%.2f"|format(r.ad_sales_halo or 0) }}</td>
        <td class="col-after_ads {{ 'pos' if (r.net_profit_after_ads or 0) >= 0 else 'neg' }}">£{{ "%.2f"|format(r.net_profit_after_ads or 0) }}</td>
        <td class="col-tacos">{% if r.tacos is not none %}{{ "%.1f"|format(r.tacos*100) }}%{% elif r.ad_spend %}∞{% else %}—{% endif %}</td>
        <td class="col-vat">{% if r.vat_rate is not none %}{{ (r.vat_rate * 100)|round|int }}%{% else %}—{% endif %}</td>
        <td class="col-pending">{% if r.pending_count %}<span class="muted">{{ r.pending_count }}</span>{% else %}—{% endif %}</td>
        <td class="col-postage_source">
          {% if r.postage_exact_count %}<span class="muted">{{ r.postage_exact_count }} exact</span>{% endif %}
          {% if r.postage_manual_count %}<span class="muted">{{ r.postage_manual_count }} manual</span>{% endif %}
          {% if postage_gap %}<span class="muted">{{ postage_gap }} missing</span>{% endif %}
        </td>
      </tr>
      {% endfor %}
    </table>
    </div>
    <div class="hint" id="plNoMatchHint" style="display:none;">No products match the current search/filters. <a href="javascript:void(0)" onclick="plClearAllFilters()">Clear all filters</a> to see everything again.</div>
    <div class="hint">Showing the <b>{{ "ex-VAT" if vat_treatment=="ex_vat" else "cash (inc-VAT)" }}</b> view
    (switch above). Grouped by <b>canonical SKU</b> (noise/duplicate/renamed SKUs resolved via the alias
    table and ASIN auto-consolidation, everything else already clean — see
    <a href="/pl/cogs">COGS &amp; pricing</a>). "Priced" — whether this product's pricing family currently
    has a COGS number entered; a "no price" row shows an inline box to set it right here (writes to the
    FAMILY price, same as <a href="/pl/cogs">COGS &amp; pricing</a> — affects every SKU in that family).
    "Postage source" — <b>exact</b> means a real Amazon shipping-label event was found; <b>manual</b> means
    the seller entered the real courier cost via the <a href="/pl/postage">missing-postage worklist</a>;
    <b>missing</b> means neither exists yet (cost £0, never guessed — see that same worklist). A
    <span class="badge no">provisional</span> tag on Net profit means this product still has orders with
    missing COGS and/or missing postage — the figure shown is real revenue/fees minus only the costs
    actually known so far, not a guess with those gaps filled in. "Ad spend" / "Net profit (after ads)" /
    "TACOS" come from whatever's been uploaded on the <a href="/pl/ads">Ad spend</a> page, joined by ASIN
    over this same date range — TACOS is ad spend ÷ TOTAL gross sales (not just ad-attributed sales), since
    that's the number that actually reflects ad cost as a share of the whole business. <b>TACOS is two
    clocks:</b> ad sales are click-date attributed (they keep rising for a past window as late conversions
    land, so re-pulling the ad report drifts them up), while gross sales count at settlement — a deliberate,
    unreconciled basis mismatch, not an error. Use <b>Columns</b>
    above to show/hide Family, Type, fee/promotion breakdowns, Postage, Ad spend/TACOS and Pending — your
    choice is remembered on this device. <b>Search &amp; filters</b> narrow this table and the "Net profit
    by product" chart below together (both track whatever's currently shown) — that pairing is deliberate
    since that chart sits directly under the table it mirrors. The "{{ chart_metric_labels[metric] }} over
    time" chart at the top of the page, the pills/warnings above it, and the account/range/period/VAT/Chart
    selectors all intentionally stay scoped to the FULL selected account + date range instead, same as each
    other — not the filtered table below — so they don't jump around while you're searching or move
    independently of one another. Click any column header to sort — sorting and filtering combine freely,
    and a <span class="badge no">provisional</span> row keeps its tag no matter how it's sorted. Your
    search/filter choices are remembered on this device, same as Columns.</div>
  </div>

  {% if ad_orphans %}
  <div class="card">
    <div class="title">Ad spend with no matching orders in this range ({{ ad_orphans|length }})</div>
    <div class="subtitle" style="font-size:12.5px;color:#5a6472;margin-bottom:10px;">These ASINs have real
      ad spend uploaded for the selected date range but zero orders in that same range — worth a look
      (paused/discontinued listing, or a real zero-conversion problem), not hidden.</div>
    <table>
      <tr><th>ASIN</th><th>Spend</th><th>Ad sales (attributed)</th><th>Clicks</th><th>Orders (Amazon-attributed)</th></tr>
      {% for o in ad_orphans %}
      <tr>
        <td>{{ o.asin }}</td>
        <td>£{{ "%.2f"|format(o.spend or 0) }}</td>
        <td>£{{ "%.2f"|format(o.ad_sales or 0) }}</td>
        <td>{{ o.clicks or 0 }}</td>
        <td>{{ o.orders or 0 }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
  {% endif %}

  <script>
    var PL_FAMILY_SKU_COUNTS = {{ family_sku_counts|tojson }};
    function plConfirmCogsEdit(form){
      var family = form.family.value;
      var n = PL_FAMILY_SKU_COUNTS[family] || 1;
      return confirm("Setting price for family " + family + " — affects " + n + " SKU" + (n === 1 ? "" : "s") + ". Continue?");
    }
    // module2_pl_ui_fixes Fix 4: EVERY column in the table is now toggleable
    // (previously only these 12 "optional" ones were -- Canonical SKU, ASIN,
    // Account, Orders, Units, Gross sales, COGS, Priced, Net profit and
    // Margin used to be permanently on with no checkbox at all).
    var PL_OPTIONAL_COLS = ["sku","asin","family","type","account","orders","units","gross","asp",
      "referral_fees","other_fees","promotions","cogs","priced","postage","netprofit","margin",
      "ad_spend","ad_promoted","ad_halo","after_ads","tacos","vat","pending","postage_source"];
    // module2_pl_ui_fixes Fix 3: default-visible = everything EXCEPT Family
    // and Type (previously ALL of PL_OPTIONAL_COLS defaulted to hidden,
    // which is a problem now that it also contains columns like Canonical
    // SKU that must stay on for a first-time visitor).
    // module2_pl_live_fixes Fix 11: also drop Priced from the default column
    // set (Faraz: "remove family, Priced") -- the Family/Type *filters* stay,
    // this only removes them as default table COLUMNS. Priced status is still
    // reachable via the Columns picker and the No-price/Provisional filters.
    // module2_ads_halo: the promoted/halo ad-sales columns also default HIDDEN
    // -- detail for the halo investigation, toggled on via the Columns picker.
    var PL_DEFAULT_VISIBLE_COLS = PL_OPTIONAL_COLS.filter(function(c){
      return c !== "family" && c !== "type" && c !== "priced"
             && c !== "ad_promoted" && c !== "ad_halo";
    });
    function plLoadVisibleCols(){
      try {
        // module2_pl_ui_fixes Fix 3/4: versioned key ("_v2") so a browser
        // that already saved a preference under the OLD scheme (just 12
        // optional columns, defaulting to all-hidden) doesn't get every
        // newly-toggleable fixed column (SKU, ASIN, Orders, etc.) hidden
        // the moment this ships -- and so a stale prior "Family/Type
        // checked" preference from earlier testing doesn't linger forever.
        var saved = JSON.parse(localStorage.getItem("pl_optional_cols_v4") || "null");
        if (saved && Array.isArray(saved)) return saved;
      } catch(e) {}
      return PL_DEFAULT_VISIBLE_COLS.slice();
    }
    function plApplyVisibleCols(cols){
      PL_OPTIONAL_COLS.forEach(function(c){
        var visible = cols.indexOf(c) !== -1;
        document.querySelectorAll(".col-" + c).forEach(function(el){
          el.style.display = visible ? "" : "none";
        });
        var cb = document.getElementById("colcb-" + c);
        if (cb) cb.checked = visible;
      });
    }
    function plSaveVisibleCols(){
      var cols = PL_OPTIONAL_COLS.filter(function(c){
        var cb = document.getElementById("colcb-" + c);
        return cb && cb.checked;
      });
      localStorage.setItem("pl_optional_cols_v4", JSON.stringify(cols));
      plApplyVisibleCols(cols);
    }
    document.addEventListener("DOMContentLoaded", function(){ plApplyVisibleCols(plLoadVisibleCols()); });
    document.addEventListener("click", function(ev){
      var panel = document.getElementById("colPickerPanel");
      if (!panel) return;
      var picker = ev.target.closest(".col-picker");
      if (!picker) panel.style.display = "none";
    });

    // ─────────────────────────────────────────────────────────────────
    // module2_search_filters — search box + filter bar. Everything below
    // operates client-side on the rows already rendered in #rollupTable
    // (server already scoped them to the selected account + date range +
    // VAT toggle) — no extra requests, instant at ~81-row scale.
    // ─────────────────────────────────────────────────────────────────
    // module2_filters_in_url: table filters now live in the URL (like account/
    // range/period/vat/metric/metric2), NOT in localStorage. A bare /pl means
    // EXACTLY what it looks like -- everything -- and the URL is the complete,
    // shareable, bookmarkable state. The flip-banner link is just another URL
    // filter (flipped=flipped), no longer a special case. (Columns stay in
    // localStorage: a display preference, low stakes -- filters change what
    // data you're looking at, which is high stakes. That's the line.)
    var PL_FILTER_KEYS = ["search","family","type","priced","provisional","profit","postage","margin","flipped"];
    var PL_FILTER_DEFAULTS = {search:"", family:"all", type:"all", priced:"all", provisional:"all", profit:"all", postage:"all", margin:"", flipped:"all"};
    var plSortState = {key: null, dir: 1};   // dir: 1 asc, -1 desc
    var plSearchDebounceTimer = null;

    function plGetFilterEls(){
      return {
        search: document.getElementById("plSearchBox"),
        family: document.getElementById("plfFamily"),
        type: document.getElementById("plfType"),
        priced: document.getElementById("plfPriced"),
        provisional: document.getElementById("plfProvisional"),
        profit: document.getElementById("plfProfit"),
        postage: document.getElementById("plfPostage"),
        margin: document.getElementById("plfMargin"),
        flipped: document.getElementById("plfFlipped"),
      };
    }

    function plReadFilterState(){
      var els = plGetFilterEls();
      return {
        search: els.search.value || "",
        family: els.family.value,
        type: els.type.value,
        priced: els.priced.value,
        provisional: els.provisional.value,
        profit: els.profit.value,
        postage: els.postage.value,
        margin: els.margin.value || "",
        flipped: els.flipped.value
      };
    }

    function plApplyFilterState(state){
      if (!state) return;
      var els = plGetFilterEls();
      els.search.value = state.search || "";
      els.family.value = state.family || "all";
      els.type.value = state.type || "all";
      els.priced.value = state.priced || "all";
      els.provisional.value = state.provisional || "all";
      els.profit.value = state.profit || "all";
      els.postage.value = state.postage || "all";
      els.margin.value = state.margin || "";
      els.flipped.value = state.flipped || "all";
    }

    function plReadUrlFilters(){
      var p = new URLSearchParams(window.location.search);
      var st = {};
      PL_FILTER_KEYS.forEach(function(k){ st[k] = p.has(k) ? p.get(k) : PL_FILTER_DEFAULTS[k]; });
      // legacy alias: the flip banner's old ?flipped_to_loss=1 == ?flipped=flipped
      if (p.get("flipped_to_loss") === "1") st.flipped = "flipped";
      return st;
    }

    // Sync the active filters INTO the URL via replaceState -- no reload, so
    // client-side filtering stays instant. Only NON-default filters appear, so
    // the URL stays clean; a cleared filter drops its param entirely.
    function plWriteUrlFilters(state){
      var u = new URL(window.location.href);
      PL_FILTER_KEYS.forEach(function(k){
        var v = state[k];
        if (v && v !== PL_FILTER_DEFAULTS[k]) u.searchParams.set(k, v);
        else u.searchParams.delete(k);
      });
      u.searchParams.delete("flipped_to_loss");   // normalised to `flipped`
      history.replaceState(null, "", u.toString());
    }

    // (plNavServer for the server-param selects is defined in an always-rendered
    // script above the empty/has-rows branch, so it works in both views.)

    function plClearSearch(){
      document.getElementById("plSearchBox").value = "";
      plOnFilterChange();
    }

    function plClearAllFilters(){
      plApplyFilterState({search:"", family:"all", type:"all", priced:"all", provisional:"all", profit:"all", postage:"all", margin:"", flipped:"all"});
      plOnFilterChange();
    }

    function plOnFilterChange(){
      // debounce the search box (~200ms) per spec; the select/number filters
      // are discrete choices so they apply immediately, but route through
      // the same debounce timer to avoid double-applying on rapid changes.
      clearTimeout(plSearchDebounceTimer);
      plSearchDebounceTimer = setTimeout(plApplyFiltersNow, 200);
    }

    function plApplyFiltersNow(){
      var state = plReadFilterState();
      // module2_filters_in_url: reflect the active filters in the URL (no
      // reload), so it's always the complete, shareable state.
      plWriteUrlFilters(state);

      var searchEl = document.getElementById("plSearchBox");
      var clearBtn = document.getElementById("plSearchClear");
      clearBtn.style.display = state.search ? "block" : "none";

      var term = state.search.trim().toLowerCase();
      var marginLimit = state.margin === "" ? null : parseFloat(state.margin);

      var rows = document.querySelectorAll("#rollupTable tr[data-search]");
      var total = rows.length, shown = 0;
      rows.forEach(function(tr){
        var ok = true;
        if (term && tr.getAttribute("data-search").indexOf(term) === -1) ok = false;
        if (ok && state.family !== "all" && tr.getAttribute("data-family") !== state.family) ok = false;
        if (ok && state.type !== "all" && tr.getAttribute("data-type") !== state.type) ok = false;
        if (ok && state.priced === "priced" && tr.getAttribute("data-priced") !== "yes") ok = false;
        if (ok && state.priced === "unpriced" && tr.getAttribute("data-priced") !== "no") ok = false;
        if (ok && state.provisional === "provisional" && tr.getAttribute("data-provisional") !== "yes") ok = false;
        if (ok && state.provisional === "complete" && tr.getAttribute("data-provisional") !== "no") ok = false;
        if (ok && state.profit === "negative" && !(parseFloat(tr.getAttribute("data-profit-after-ads")) < 0)) ok = false;
        if (ok && state.profit === "positive" && !(parseFloat(tr.getAttribute("data-profit-after-ads")) >= 0)) ok = false;
        if (ok && state.postage === "gap" && tr.getAttribute("data-postage-gap") !== "yes") ok = false;
        if (ok && state.postage === "exact" && tr.getAttribute("data-postage-gap") !== "no") ok = false;
        if (ok && marginLimit !== null && !(parseFloat(tr.getAttribute("data-margin-after-ads-pct")) < marginLimit)) ok = false;
        if (ok && state.flipped === "flipped" && tr.getAttribute("data-flipped-to-loss") !== "yes") ok = false;

        tr.classList.toggle("pl-hidden", !ok);
        if (ok) shown++;
      });

      document.getElementById("plResultCount").textContent = "Showing " + shown + " of " + total + " product" + (total === 1 ? "" : "s");

      var chips = [];
      if (term) chips.push('search: "' + state.search.trim() + '"');
      if (state.family !== "all") chips.push("family: " + state.family);
      if (state.type !== "all") chips.push("type: " + state.type);
      if (state.priced !== "all") chips.push(state.priced === "priced" ? "priced only" : "no-price only");
      if (state.provisional !== "all") chips.push(state.provisional === "provisional" ? "provisional only" : "complete only");
      if (state.profit !== "all") chips.push(state.profit === "negative" ? "negative after ads" : "positive after ads");
      if (state.postage !== "all") chips.push(state.postage === "gap" ? "has missing/estimated postage" : "all-exact postage");
      if (marginLimit !== null) chips.push("margin below " + marginLimit + "%");
      if (state.flipped === "flipped") chips.push("flipped to loss only");
      var activeEl = document.getElementById("plActiveFilters");
      if (chips.length){
        activeEl.style.display = "block";
        activeEl.innerHTML = chips.length + " filter" + (chips.length===1?"":"s") + " active: " +
          chips.map(function(c){ return '<span class="chip">' + c.replace(/</g,"&lt;") + '</span>'; }).join(" ") +
          ' <a href="javascript:void(0)" onclick="plClearAllFilters()" style="margin-left:10px;font-weight:700;color:#0e5c5b;text-decoration:underline;white-space:nowrap;">✕ Show all products</a>';
      } else {
        activeEl.style.display = "none";
        activeEl.innerHTML = "";
      }

      var caveat = document.getElementById("plAdFilterCaveat");
      if (caveat){
        caveat.style.display = (state.profit !== "all" || marginLimit !== null) ? "block" : "none";
      }

      document.getElementById("plNoMatchHint").style.display = (shown === 0 && total > 0) ? "block" : "none";

      plUpdateChart2(rows);
      plUpdateSummary(rows);
    }

    // Keeps the "by product" bar chart in sync with whatever the table is
    // currently showing (filtered/searched), per the spec's "pick one and
    // be explicit" requirement -- this chart sits directly under the table
    // it mirrors, so it tracks the table. The account-level pills/totals
    // above the chart intentionally do NOT change with table filters --
    // those reflect the full selected account + date range, same as the
    // headline "postage/unpriced/ad-coverage" warnings, which would be
    // confusing if they moved every time someone typed into the search box.
    //
    // module2_pl_ui_fixes Fix 5: this chart's plotted variable (PL_CHART2_METRIC,
    // set server-side from the "Chart 2" dropdown) is independent of chart1's
    // -- re-derives value/sort/colour per metric from the same data-sort-*
    // attributes the table's own sort feature already uses, so no extra
    // data has to be threaded through just for this chart.
    // ── File H: range summary panel ────────────────────────────────────
    // Totals recomputed from the table's VISIBLE rows so they always reconcile
    // with the table and track its filters. Component lines (referral/other/
    // promo/cogs/postage) are itemised for display; the "= Net profit" lines
    // use the authoritative balance-change-anchored net_profit / after_ads
    // values (data-sort-netprofit / -afterads), never re-derived from the
    // components -- same guarantee as the SKU page's cost breakdown.
    var PL_LAST_SYNCED_ISO = {{ last_synced_iso|tojson }};
    function plS(id, txt){ var e = document.getElementById(id); if (e) e.textContent = txt; }
    function plN(tr, attr){ var v = parseFloat(tr.getAttribute(attr)); return isNaN(v) ? 0 : v; }
    function plUpdateSummary(allRows){
      var vis = [], total = allRows.length;
      allRows.forEach(function(tr){ if (!tr.classList.contains("pl-hidden")) vis.push(tr); });
      var g=0,ref=0,oth=0,promo=0,cogs=0,post=0,netB=0,ad=0,netA=0,units=0,orders=0,prov=0,provGross=0;
      vis.forEach(function(tr){
        g   += plN(tr,"data-sort-gross");   ref += Math.abs(plN(tr,"data-referral"));
        oth += Math.abs(plN(tr,"data-other")); promo += Math.abs(plN(tr,"data-promotions"));
        cogs+= Math.abs(plN(tr,"data-sort-cogs")); post += Math.abs(plN(tr,"data-sort-postage"));
        netB+= plN(tr,"data-sort-netprofit"); ad += Math.abs(plN(tr,"data-sort-adspend"));
        netA+= plN(tr,"data-sort-afterads"); units += plN(tr,"data-sort-units"); orders += plN(tr,"data-sort-orders");
        if (tr.getAttribute("data-provisional") === "yes"){ prov++; provGross += plN(tr,"data-sort-gross"); }
      });
      // A cost line: red ONLY when it's a real non-zero cost; a £0.00 line is
      // muted grey so it doesn't shout. `val` is a positive magnitude.
      function plSetCost(id, val){
        var e = document.getElementById(id); if (!e) return;
        e.textContent = "−£" + val.toFixed(2);
        e.style.color = val > 0.005 ? "#9e2d3c" : "#c2c9d1";
      }
      // A net (= ) line: green when >= 0, red when a real loss.
      function plSetNet(id, val){
        var e = document.getElementById(id); if (!e) return;
        e.textContent = (val < 0 ? "−£" : "£") + Math.abs(val).toFixed(2);
        e.style.color = val < -0.005 ? "#cf3f52" : "#1f9d57";
      }
      plS("sumGross", "£" + g.toFixed(2));
      plSetCost("sumRef", ref); plSetCost("sumOther", oth); plSetCost("sumPromo", promo);
      plSetCost("sumCogs", cogs); plSetCost("sumPostage", post); plSetCost("sumAdspend", ad);
      plSetNet("sumNetBefore", netB); plSetNet("sumNetAfter", netA); plSetNet("sumNetAfterEcho", netA);
      // Overheads: a business-wide, period figure (not row-scaled) — subtract from
      // net-after-ads for the true bottom line.
      var oh = (window.PL_OVERHEADS || 0);
      plSetCost("sumOverheads", oh);
      plSetNet("sumNetAfterOh", netA - oh);
      plS("sumOrders", orders); plS("sumUnits", units);
      plS("sumMargin", g ? (100*netA/g).toFixed(1) + "%" : "—");
      plS("sumAsp", units ? "£" + (g/units).toFixed(2) : "—");
      // module2_tacos_two_clocks: ad spend ÷ settled gross (two clocks -- see label).
      plS("sumTacos", g ? (100*ad/g).toFixed(1) + "%" : (ad ? "∞" : "—"));
      var scope = document.getElementById("plSummaryScope");
      if (scope){
        var filtered = vis.length !== total;
        scope.textContent = filtered ? ("Filtered: " + vis.length + " of " + total + " products")
                                     : ("All " + total + " products in range");
        scope.style.color = filtered ? "#9e2d3c" : "#8a94a2";
        scope.style.fontWeight = filtered ? "700" : "400";
      }
      var flag = document.getElementById("sumProvFlag"); if (flag) flag.style.display = prov ? "" : "none";
      var pn = document.getElementById("plSummaryProvisional");
      if (pn){
        if (prov){
          pn.style.display = "block";
          pn.innerHTML = "⚠ " + prov + " of " + vis.length + " shown product(s) are provisional (missing COGS or postage) — £"
            + provGross.toFixed(2) + " of gross sits behind an incomplete net. The figures above are real revenue/fees minus only the costs known so far, not a guess with the gaps filled.";
        } else { pn.style.display = "none"; }
      }
    }
    // Sync-age indicator (durable guard: the data can silently go stale while
    // Module 2 is local/manual). Shows "synced Nh/Nd ago"; warns beyond 24h.
    function plUpdateSyncAge(){
      var el = document.getElementById("plSyncAge"); if (!el) return;
      if (!PL_LAST_SYNCED_ISO){ el.innerHTML = ' · <span style="color:#9e2d3c;font-weight:700;">last sync unknown</span>'; return; }
      var t = Date.parse(PL_LAST_SYNCED_ISO); if (isNaN(t)){ el.textContent = ""; return; }
      var hrs = (Date.now() - t) / 3.6e6;
      var ago = hrs < 1 ? Math.round(hrs*60) + "m" : hrs < 48 ? Math.round(hrs) + "h" : Math.round(hrs/24) + "d";
      var stale = hrs > 24;
      el.innerHTML = " · " + (stale
        ? '<span style="color:#9e2d3c;font-weight:700;">⚠ synced ' + ago + ' ago — data may be stale, run <code>python pl_tracker.py</code></span>'
        : '<span class="muted">synced ' + ago + ' ago</span>');
    }
    document.addEventListener("DOMContentLoaded", plUpdateSyncAge);

    var PL_CHART2_METRIC = {{ metric2|tojson }};
    var PL_CHART2_ATTR = {
      net_profit: "data-sort-netprofit", margin_pct: "data-sort-margin",
      revenue: "data-sort-gross", units: "data-sort-units", orders: "data-sort-orders",
      ad_spend: "data-sort-adspend", tacos: "data-sort-tacos"
    };
    var PL_PROFIT_LIKE_METRICS = {net_profit: true, margin_pct: true};
    function plChart2Value(tr, metric){
      var attr = PL_CHART2_ATTR[metric] || PL_CHART2_ATTR.net_profit;
      var raw = parseFloat(tr.getAttribute(attr));
      if (isNaN(raw)) raw = 0;
      if (metric === "margin_pct") return raw * 100;
      // data-sort-tacos uses -1 as a "no ad data" sentinel (see the table's
      // own sort feature) -- treat that as 0 for the chart rather than a
      // misleading negative bar.
      if (metric === "tacos") return raw < 0 ? 0 : raw * 100;
      return raw;
    }
    function plUpdateChart2(allRows){
      var gd = document.getElementById("plChart2");
      if (!gd || typeof Plotly === "undefined") return;
      var metric = PL_CHART2_METRIC;
      var visible = [];
      allRows.forEach(function(tr){
        if (tr.classList.contains("pl-hidden")) return;
        visible.push({
          sku: tr.getAttribute("data-sort-sku") || "(no sku)",
          value: plChart2Value(tr, metric),
          provisional: tr.getAttribute("data-provisional") === "yes"
        });
      });
      visible.sort(function(a,b){ return b.value - a.value; });
      var x = visible.map(function(v){ return v.sku; });
      var y = visible.map(function(v){ return v.value; });
      var colors = visible.map(function(v){
        if (v.provisional) return "#e0a11e";
        if (PL_PROFIT_LIKE_METRICS[metric]) return v.value >= 0 ? "#1f9d57" : "#cf3f52";
        return "#0e5c5b";
      });
      try {
        Plotly.restyle(gd, {x: [x], y: [y], "marker.color": [colors]}, [0]);
      } catch(e) { /* chart not yet initialised on first paint -- ignore */ }
    }

    // ── Sorting ────────────────────────────────────────────────────────
    function plSortBy(key, thEl){
      if (plSortState.key === key) { plSortState.dir = -plSortState.dir; }
      else { plSortState.key = key; plSortState.dir = 1; }
      document.querySelectorAll("#rollupTable th.sortable .arrow").forEach(function(a){ a.textContent = ""; });
      if (thEl) thEl.querySelector(".arrow").textContent = plSortState.dir === 1 ? "▲" : "▼";

      var tbody = document.getElementById("rollupTable");
      var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr[data-search]"));
      var attr = "data-sort-" + key;
      rows.sort(function(a, b){
        var av = a.getAttribute(attr), bv = b.getAttribute(attr);
        var an = parseFloat(av), bn = parseFloat(bv);
        var cmp;
        if (!isNaN(an) && !isNaN(bn)) cmp = an - bn;
        else cmp = (av || "").localeCompare(bv || "");
        return cmp * plSortState.dir;
      });
      // Re-append in sorted order -- this only reorders rows, it never
      // touches the pl-hidden class or any data-* attribute, so a row that
      // was filtered out (or flagged provisional) stays exactly as filtered/
      // marked after the re-sort; the header <tr> (no data-search attr) is
      // untouched since it's excluded from the query above.
      rows.forEach(function(tr){ tbody.appendChild(tr); });
      plUpdateChart2(rows);
    }

    document.addEventListener("DOMContentLoaded", function(){
      // module2_filters_in_url: the URL is the single source of filter state.
      // A bare /pl -> all defaults -> everything. The flip-banner link's
      // ?flipped=flipped (or legacy ?flipped_to_loss=1) is just another URL
      // filter, read here like any other -- no special case. plApplyFiltersNow
      // then normalises the URL (e.g. drops the legacy alias in favour of
      // flipped=flipped).
      plApplyFilterState(plReadUrlFilters());
      plApplyFiltersNow();
    });
  </script>

  <div class="card">
    <div class="title">Per-period rollup ({{ period }}){% if account_filter == "all" %} — combined across accounts{% endif %}</div>
    <table>
      <tr>
        <th>Period</th>{% if account_filter != "all" %}<th>Orders</th>{% endif %}<th>Units</th>
        <th>Gross sales (ex-VAT)</th><th>Net profit (contribution)</th>
        {% if period == "month" %}<th>Monthly overheads</th><th>True net</th>{% endif %}
        <th>Pending</th>
      </tr>
      {% for r in period_rows %}
      <tr>
        <td>{{ r.period }}</td>
        {% if account_filter != "all" %}<td>{{ r.orders }}</td>{% endif %}
        <td>{{ r.units or 0 }}</td>
        <td>£{{ "%.2f"|format(r.gross_sales_exvat or 0) }}</td>
        <td class="{{ 'pos' if (r.net_profit or 0) >= 0 else 'neg' }}">£{{ "%.2f"|format(r.net_profit or 0) }}</td>
        {% if period == "month" %}
        <td class="neg">-£{{ "%.2f"|format(overhead_monthly or 0) }}</td>
        <td class="{{ 'pos' if ((r.net_profit or 0) - (overhead_monthly or 0)) >= 0 else 'neg' }}">
            £{{ "%.2f"|format((r.net_profit or 0) - (overhead_monthly or 0)) }}</td>
        {% endif %}
        <td>{% if r.pending_count %}<span class="muted">{{ r.pending_count }}</span>{% else %}—{% endif %}</td>
      </tr>
      {% endfor %}
    </table>
    {% if period == "month" %}
    <div class="hint">"Monthly overheads" (warehouse + labour + packaging + utilities, ex-VAT, currently
    £{{ "%.2f"|format(overhead_monthly or 0) }}/month — <a href="/pl/cogs">edit</a>) is a FIXED cost, applied
    once per calendar month shown here — NOT allocated per order. "True net" = contribution − overheads.</div>
    {% else %}
    <div class="hint">Monthly overheads only subtract at the monthly view (switch period above) — prorating
    a fixed monthly cost into days/weeks would misrepresent it.</div>
    {% endif %}
  </div>
  {% endif %}
</div></body></html>
"""


def _default_vat_treatment():
    try:
        cfg = json.load(open("config.json"))
        return cfg.get("pl_tracker", {}).get("vat_treatment", "ex_vat")
    except Exception:
        return "ex_vat"


@app.route("/pl")
def pl_page():
    account_filter = request.args.get("account", "all")
    period_explicit = "period" in request.args
    period = request.args.get("period", "day")
    if period not in ("day", "week", "month"):
        period = "day"
    vat_treatment = request.args.get("vat", _default_vat_treatment())
    if vat_treatment not in ("cash", "ex_vat"):
        vat_treatment = "ex_vat"

    # module2_dashboard_fixes D1: which variable the "over time" chart
    # plots -- was hardcoded to net_profit only.
    metric = request.args.get("metric", "net_profit")
    if metric not in _CHART_METRICS:
        metric = "net_profit"

    # module2_pl_ui_fixes Fix 5: separate selector for the right-hand "by
    # product" bar chart -- deliberately its own param/dropdown, not shared
    # with `metric` above (see _product_metric_value's docstring for why).
    metric2 = request.args.get("metric2", "net_profit")
    if metric2 not in _CHART_METRICS:
        metric2 = "net_profit"

    # module2_debug_fix_pass FIX 1: a real date-RANGE filter, independent of
    # the day/week/month BUCKETING control above -- previously that dropdown
    # only changed how the chart grouped dates, it never filtered which
    # orders were included anywhere, so the per-product rollup always showed
    # all-time totals no matter what was selected.
    # module2_pl_ui_fixes Fix 1: default to "Last 30 days" on a bare /pl load
    # (no query params) -- was defaulting to "All time", which on a real,
    # multi-year order history loads the entire dataset every time the page
    # is opened fresh. Deliberately NOT special-cased around the ads
    # importer fix's coverage window (2026-06-15..2026-07-14) -- this is a
    # genuine default-UX fix independent of that data, not tuned to it.
    range_key = request.args.get("range", "30")
    start_date = _range_start_date(range_key)
    # module2_dashboard_fixes D1: default to a legible bucketing for wide
    # ranges (daily points across 2 years of real orders was the reported
    # "unreadable smear") -- only when the seller hasn't explicitly chosen
    # Daily/Weekly/Monthly this request.
    period = _resolve_chart_period(range_key, period, period_explicit)

    accounts = get_accounts()
    pending = pl_db.get_pending_count(account_filter)
    canonical_rows = pl_db.get_canonical_rollup(account_filter, vat_treatment=vat_treatment,
                                                 start_date=start_date)
    postage_estimated_total = sum(r.get("postage_estimated_count") or 0 for r in canonical_rows)
    # module2_postage_badge_split: 'provisional' (retired heuristic guess,
    # amount blanked) rolls into the headline "missing — fill in" count, since
    # it also needs a real per-order label fetch or manual entry.
    postage_provisional_total = sum(r.get("postage_provisional_count") or 0 for r in canonical_rows)
    postage_exact_total = sum(r.get("postage_exact_count") or 0 for r in canonical_rows)
    postage_manual_total = sum(r.get("postage_manual_count") or 0 for r in canonical_rows)
    postage_missing_total = sum(r.get("postage_missing_count") or 0 for r in canonical_rows) + postage_estimated_total + postage_provisional_total
    unpriced_total = sum(r.get("unpriced_count") or 0 for r in canonical_rows)
    range_start, range_end = pl_db.resolve_pl_date_range(account_filter, start_date=start_date)
    # Business overheads (Expenses page) pro-rated to the viewed window — a
    # business-wide bottom-line figure only, NEVER allocated to per-product rows.
    try:
        import pl_expenses
        overheads_period = pl_expenses.compute_overheads(
            account_filter, range_start, range_end).get("total", 0.0)
    except Exception:
        overheads_period = 0.0
    # module2_range_summary (File H): last SP-API sync time, so the summary can
    # state "settled orders, as of last sync ..." -- Module 2 is manual-sync, so
    # the data window ends at the newest SETTLED order, not necessarily today.
    try:
        _sc = pl_db.get_db()
        _sr = _sc.execute("SELECT MAX(latest_synced) AS m FROM pl_sync_state").fetchone()
        _sc.close()
        last_synced_iso = _sr["m"] if _sr and _sr["m"] else None
        last_synced = last_synced_iso.replace("T", " ")[:16] if last_synced_iso else None
    except Exception:
        last_synced = last_synced_iso = None

    # module2_true_profit Phase 1: join ad spend onto the rollup at the ASIN
    # level (never per-order -- see pl_ads.py). Uses the SAME date filter as
    # the rest of the page so AD SPEND/TACOS always match whatever range is
    # currently selected.
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    canonical_rows, ad_orphans = pl_ads.attach_ad_spend_to_rollup(
        canonical_rows, account_id=account_filter, start_date=start_date, end_date=None)

    # module2_breakeven: layered break-even + target price (calculated column, no writes).
    try:
        _refunded_units = pl_db.get_refunded_units_by_canonical(account_filter, start_date=start_date)
    except Exception:
        _refunded_units = {}
    try:
        _push_set = pl_db.get_push_canonicals(account_filter)
    except Exception:
        _push_set = set()
    pl_db.attach_breakeven(canonical_rows, overheads_total=(overheads_period or 0.0),
                           refunded_units=_refunded_units, push_set=_push_set)
    # Not authoritative until the refund backfill lands: if no refund units are captured
    # yet, the refund layer is understated — surface a caveat that self-clears once refunds arrive.
    breakeven_provisional = (sum(_refunded_units.values()) == 0)
    ad_coverage_warning = pl_ads.get_coverage_warning(
        account_id=account_filter, viewed_start=start_date, viewed_end=today_str)
    # module2_pl_ui_fixes Fix 6: exposed per-row (not just as a page-level
    # count) so the "N product(s) flip to a LOSS" banner can link straight
    # to a table pre-filtered to EXACTLY those rows, the same way the
    # existing Priced/Provisional/Profit/Postage filters already work off a
    # data-* attribute -- see the "flipped_to_loss=1" query-param handling
    # in the page's <script> block.
    for r in canonical_rows:
        r["flipped_to_loss"] = (r.get("net_profit") or 0) >= 0 and (r.get("net_profit_after_ads") or 0) < 0
    flips_to_loss = sum(1 for r in canonical_rows if r["flipped_to_loss"])

    # module2_true_profit Phase 2: family -> #canonical SKUs, so the inline
    # COGS-edit confirm dialog can say "affects N SKUs" without an extra
    # per-click round trip.
    family_sku_counts = {f["family"]: f["n_canonical_skus"] for f in pl_cogs.get_all_families()}
    # module2_pl_live_fixes: spec B item 6 -- Family/Type filter dropdown.
    # Distinct values present in the CURRENT rollup (already scoped to the
    # selected account/date range), sorted, so the dropdown only ever offers
    # choices that actually exist on the page (e.g. picking "Towel" here
    # always yields at least one match, and matches the "show me every
    # towel" use case exactly).
    pl_families = sorted({r["family"] for r in canonical_rows if r.get("family")})
    pl_types = sorted({r["product_type"] for r in canonical_rows if r.get("product_type")})
    current_qs = request.query_string.decode()
    return_url = "/pl" + (("?" + current_qs) if current_qs else "")

    # module2_pl_live_fixes: FIX for the flip-banner link duplicating
    # flipped_to_loss=1 on every reload -- the previous template code built
    # this by blindly appending "&flipped_to_loss=1" onto current_qs with no
    # check for whether it was ALREADY there. Since flips_to_loss itself is
    # a server-side count independent of whatever the client-side JS filter
    # is doing, landing on ?flipped_to_loss=1 still shows the same banner
    # (still >0), so any reload/re-click while already on that URL added
    # ANOTHER copy -- confirmed by rendering the real template with
    # current_qs already containing the param and observing it duplicate.
    # Same defensive stripping pattern as the SKU detail page's self_url
    # (which strips refresh_tax/refresh_price for the same reason).
    # module2_filters_in_url: the flip banner is now just another URL filter
    # (flipped=flipped), consistent with every other table filter -- no longer a
    # special ?flipped_to_loss=1 case. Strip any existing flipped/flipped_to_loss
    # so a re-click never duplicates it.
    _flip_qs_parts = [p for p in current_qs.split("&")
                      if p and not p.startswith(("flipped_to_loss=", "flipped="))]
    flip_href = "/pl?" + "&".join(_flip_qs_parts + ["flipped=flipped"])

    # module2_search_filters: attach everything the client-side search box and
    # filter bar need per row, computed here (server-side, once) rather than
    # in JS, so the template/JS just reads plain data-* attributes.
    #  - title: module2_pl_live_fixes -- corrected diagnosis of the "title
    #    line shows on some rows, not others, no obvious pattern" bug.
    #    NOT a rendering bug -- the template only ever prints whatever
    #    title_by_asin actually resolves to. The real cause: pl_asin_titles
    #    (the Group C backfill table, meant to cover every real ASIN via
    #    SP-API Catalog Items) was confirmed EMPTY in the live database --
    #    backfill_titles.py has apparently never been run successfully
    #    against it. That left the old managed_asins fallback (documented
    #    as "only ever the 3 watchlisted ASINs") doing all the work, and it
    #    only has titles for 2 of them -- which resolve to exactly the two
    #    canonical SKUs reported as affected (xl-pt-BD, 6337-P4), confirmed
    #    directly against the real DB. Every other of the ~150+ real
    #    products has no title anywhere in this app's tables, so showing
    #    "no title line" for them was already correct -- the bug was that
    #    2 rows looked inconsistent with the other 150+, not that any row
    #    displayed the wrong thing.
    #    FIX: drop the managed_asins fallback here. pl_asin_titles is the
    #    only source now -- title coverage will be uniformly ABSENT until
    #    `python backfill_titles.py` is actually run (it's already written,
    #    already safe/idempotent, and cogs_sku_asin now has 1,067 real ASIN
    #    mappings for it to work through), then uniformly PRESENT wherever
    #    Amazon has a title, instead of this spotty 2-out-of-150+ look.
    #  - member_skus: every variant/renamed SKU that resolves to this
    #    canonical (via cogs_aliases), so searching a family-member SKU that
    #    isn't the canonical one shown in the table still finds the row.
    #  - margin_pct_after_ads: mirrors the existing margin_pct field but
    #    against net_profit_after_ads, for the margin-threshold filter --
    #    equals margin_pct exactly when no ad spend has been uploaded/joined
    #    (net_profit_after_ads falls back to net_profit in that case), which
    #    is the intended "before ads == after ads until data exists" behaviour.
    all_asins_on_page = sorted({a for r in canonical_rows for a in (r.get("asins") or [])})
    title_by_asin = {k: v for k, v in pl_amazon.get_titles_map(all_asins_on_page).items() if v}
    member_skus_map = pl_cogs.get_member_skus_for_canonicals()
    for r in canonical_rows:
        title = None
        for a in (r.get("asins") or []):
            if title_by_asin.get(a):
                title = title_by_asin[a]
                break
        r["title"] = title
        members = set(member_skus_map.get(r.get("canonical_sku"), []))
        if r.get("canonical_sku"):
            members.add(r["canonical_sku"])
        r["member_skus"] = sorted(members)
        gross = r.get("gross_sales_exvat") or 0
        after_ads = r.get("net_profit_after_ads")
        r["margin_pct_after_ads"] = (after_ads / gross) if (after_ads is not None and gross) else None

    # module2_true_profit Phase 3: a long missing-postage worklist is a signal
    # the label-cost pipeline isn't fully catching orders it should -- not
    # something to just type through (see pl_postage.py).
    # module2_postage_badge_split: count only orders 3-10 days old that are STILL
    # missing a label -- past the ~1-2 day PostageBilling posting lag, which
    # self-heals on the next sync. Freshest orders (0-2 days) are excluded so the
    # banner stops false-alarming on normal lag (a "79 in last 7 days" batch
    # cleared to 0 within a day once the adjustments posted).
    postage_week_count = pl_postage.count_missing_postage_last_n_days(
        account_filter, days=10, min_age_days=3)

    # Monthly overheads: now a MONTHLY RUN-RATE derived from the Expenses page
    # (pl_expenses) — the single source of truth. The old flat pl_cogs.get_overheads
    # field is retired so overheads can't be entered in two places and double-count.
    # Run-rate = sum of currently-active recurring monthly overheads for the account
    # (+ 'shared'); the range summary shows the precise day-pro-rated figure incl. one-offs.
    try:
        import pl_expenses as _plx
        _oh_rows = _plx.list_overheads(None if account_filter == "all" else account_filter)
        overhead_monthly = _plx.monthly_run_rate(_oh_rows)   # weekly folded in (×52/12)
    except Exception:
        overhead_monthly = 0.0

    if account_filter == "all":
        period_rows = pl_db.get_combined_period_rollup(period, vat_treatment=vat_treatment,
                                                         start_date=start_date)
    else:
        period_rows = pl_db.get_period_rollup(account_filter, period, vat_treatment=vat_treatment,
                                               start_date=start_date)

    # Chart 1 — selected metric over time (module2_dashboard_fixes D1: was
    # hardcoded to net_profit only; now only over the selected date range).
    # ad_spend/tacos need a second, separately-bucketed series -- see
    # pl_ads.get_ad_spend_period_series and _metric_series's docstring.
    ad_period_map = None
    if metric in ("ad_spend", "tacos"):
        ad_period_map = pl_ads.get_ad_spend_period_series(
            account_filter, period=period, start_date=start_date, end_date=None)
    x1 = [r["period"] for r in period_rows]
    y1 = _metric_series(period_rows, metric, ad_period_map)
    metric_label = _CHART_METRIC_LABELS[metric]
    y_prefix, y_suffix = _CHART_METRIC_FMT[metric]
    fig1 = go.Figure(go.Scatter(
        x=x1, y=y1, mode="lines+markers", name=metric_label,
        line=dict(color="#0e5c5b", width=2), marker=dict(size=6),
        hovertemplate="%{x}<br>" + y_prefix + "%{y:,.2f}" + y_suffix + "<extra></extra>"))
    fig1.update_layout(title=f"{metric_label} over time ({period})", yaxis_title=metric_label,
                        margin=dict(t=40, l=50, r=20, b=40), height=340,
                        paper_bgcolor="white", plot_bgcolor="white")
    chart1_html = pyo.plot(fig1, output_type="div", include_plotlyjs="cdn", config={"displayModeBar": False})

    # Chart 2 — selected metric by canonical product (same filtered rows).
    # module2_pl_ui_fixes Fix 5: was hardcoded to net_profit only; now driven
    # by metric2 (its own dropdown, separate from chart1's). Per
    # module2_true_profit Phase 4: a PROVISIONAL row (missing COGS and/or
    # postage) is coloured amber regardless of value, instead of green/red/
    # neutral -- sorting still ranks it by its (incomplete) value, but the
    # colour makes clear at a glance that a top-ranked bar may not mean what
    # it looks like it means.
    sorted_products = sorted(canonical_rows, key=lambda r: _product_metric_value(r, metric2), reverse=True)
    x2 = [r.get("canonical_sku") or "(no SKU)" for r in sorted_products]
    y2 = [_product_metric_value(r, metric2) for r in sorted_products]
    colors = [_product_metric_color(r, metric2) for r in sorted_products]
    metric2_label = _CHART_METRIC_LABELS[metric2]
    y2_prefix, y2_suffix = _CHART_METRIC_FMT[metric2]
    fig2 = go.Figure(go.Bar(
        x=x2, y=y2, marker_color=colors,
        hovertemplate="%{x}<br>" + y2_prefix + "%{y:,.2f}" + y2_suffix + "<extra></extra>"))
    fig2.update_layout(title=f"{metric2_label} by product (amber = provisional — missing COGS/postage)",
                        margin=dict(t=40, l=50, r=20, b=80), height=340,
                        paper_bgcolor="white", plot_bgcolor="white")
    # module2_search_filters: fixed div_id so the table's search/filter JS can
    # Plotly.restyle this exact chart in place when the visible row set
    # changes, instead of a full page reload -- this chart pairs directly
    # with the rollup table below it, so (per the spec's "pick one and be
    # explicit") it tracks the table's filtered set, not the full account/
    # date-range totals shown in the pills above.
    # NOTE: plotly.offline.plot() (pyo.plot, used for chart1 above) has no
    # div_id parameter -- only plotly.io.to_html() supports pinning the div's
    # id, so chart2 uses that instead (full_html=False is the equivalent of
    # pyo.plot's output_type="div").
    chart2_html = pio.to_html(fig2, full_html=False, include_plotlyjs=False,
                               config={"displayModeBar": False}, div_id="plChart2")

    return render_template_string(
        PL_HTML, accounts=accounts, account_filter=account_filter, period=period,
        metric=metric, metric2=metric2, chart_metric_labels=_CHART_METRIC_LABELS,
        pending=pending, canonical_rows=canonical_rows, period_rows=period_rows,
        chart1_html=chart1_html, chart2_html=chart2_html,
        vat_treatment=vat_treatment, postage_estimated_total=postage_estimated_total,
        postage_exact_total=postage_exact_total,
        postage_manual_total=postage_manual_total, postage_missing_total=postage_missing_total,
        postage_week_count=postage_week_count,
        unpriced_total=unpriced_total, overhead_monthly=overhead_monthly,
        overheads_period=overheads_period, breakeven_provisional=breakeven_provisional,
        range_key=range_key, range_start=range_start, range_end=range_end,
        last_synced=last_synced, last_synced_iso=last_synced_iso,
        ad_orphans=ad_orphans, ad_coverage_warning=ad_coverage_warning,
        flips_to_loss=flips_to_loss, flip_href=flip_href, family_sku_counts=family_sku_counts,
        pl_families=pl_families, pl_types=pl_types,
        return_url=return_url, current_qs=current_qs,
    )


# ─────────────────────────────────────────────────────────────────────────────
# COGS & PRICING — Module 2 / module2_cogs_integration
#
# The only page in Module 2 that writes anything -- editing a family price,
# uploading a price CSV, or setting monthly overheads. Still entirely local
# (SQLite) and read-only against Amazon: a price change triggers
# pl_tracker.reprocess_from_stored_events(), which recomputes pl_line_items
# from the already-ingested pl_raw_events ledger, no SP-API call at all.
# ─────────────────────────────────────────────────────────────────────────────

COGS_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>COGS &amp; pricing — BSR Repricer</title>
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c}
 /* module2_dashboard_fixes D3: dark-teal link styling, replacing default
    browser blue/underline -- more specific selectors below (.header a,
    etc.) intentionally still override this base rule. */
 a{color:#0e5c5b;text-decoration:none;font-weight:600}
 a:hover{text-decoration:underline}
 a:visited{color:#0e5c5b}
 .header{background:#0e5c5b;color:#eafcfb;padding:18px 30px;display:flex;justify-content:space-between;align-items:center}
 .header a{color:#bfe9e7;font-size:13px;text-decoration:none;margin-left:16px}
 .wrap{max-width:1100px;margin:24px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
 .title{font-size:15px;font-weight:700;margin-bottom:6px}
 .subtitle{font-size:12.5px;color:#5a6472;margin-bottom:14px;line-height:1.5}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:left;border-top:1px solid #eef1f4;vertical-align:middle}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase;letter-spacing:.3px}
 tr:hover td{background:#f7fbfb}
 .muted{color:#8a94a2}
 .badge{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:10px}
 .badge.ok{background:#e7f6ee;color:#166b3d}
 .badge.no{background:#fbe8eb;color:#9e2d3c}
 .price-input{width:80px;padding:5px 7px;border:1px solid #dde3e9;border-radius:6px;font-size:13px;font-family:ui-monospace,Menlo,monospace}
 .btn{background:#0e5c5b;color:#eafcfb;border:none;border-radius:6px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer}
 .btn-sm{padding:4px 10px;font-size:11.5px}
 .btn-outline{background:#fff;color:#0e5c5b;border:1px solid #0e5c5b;}
 .flash{background:#e7f6ee;color:#166b3d;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:600}
 .flash.warn{background:#fbf1dd;color:#8a5906}
 input[type=file]{font-size:12.5px}
 .row-inline{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 label{font-size:12px;color:#5a6472}
 input[type=number]{padding:7px 9px;border:1px solid #dde3e9;border-radius:6px;font-size:13px;width:110px}
</style></head><body>
{{ nav|safe }}<div class="header" style="padding-top:10px"><b>COGS &amp; pricing</b><div><a href="/pl/cogs/skus">SKU merge &amp; fix</a></div></div>
<div class="wrap">
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}

  <div class="pill" style="display:inline-block;background:#fff;border:1px solid #dde3e9;border-radius:20px;padding:6px 14px;font-size:12.5px;margin-bottom:14px;">
    Alias table: <b>{{ alias_count.total }} mapping{{ "s" if alias_count.total != 1 else "" }} loaded</b>
    {% if alias_count.by_source %}<span class="muted" style="font-size:11.5px;">
      ({% for src, n in alias_count.by_source.items() %}{{ n }} {{ src }}{% if not loop.last %}, {% endif %}{% endfor %})
    </span>{% endif %}
    {% if alias_count.total == 0 %}
    <span style="color:#9e2d3c;font-weight:700;"> — sku_aliases.csv did not seed. Check the server console
      log for the resolved path it tried and restart the app.</span>
    {% endif %}
  </div>

  <div class="card">
    <div class="title">Monthly overheads → moved to Expenses</div>
    <div class="subtitle">Overheads now live on the dedicated <a href="/pl/expenses"><b>Expenses</b></a> page —
      one source of truth (recurring monthly + one-off, with start/end dates, per account), pro-rated to the
      period you're viewing and subtracted at the P&amp;L summary. The old single flat field here is retired so
      the same overhead can't be entered twice and double-count. <a href="/pl/expenses">Open Expenses →</a></div>
  </div>

  <datalist id="canonicalOptions">
    {% for c in all_canonicals %}<option value="{{ c.canonical_sku }}">{{ c.family }} ({{ c.product_type }})</option>{% endfor %}
  </datalist>

  <div class="card">
    <div class="title">Missing-prices worklist ({{ missing|length }} unpriced famil{{ "y" if missing|length==1 else "ies" }})</div>
    <div class="subtitle">Every family below has real orders behind it (in the selected window) but no
      price entered yet — those orders currently show COGS=£0.00 on the P&amp;L. Fill in one number per
      family (below, or via CSV), sorted by revenue so the highest-impact ones are at the top.</div>
    <form method="GET" action="/pl/cogs" class="row-inline" style="margin-bottom:10px;">
      <input type="hidden" name="account" value="{{ default_account }}">
      <label>Date range</label>
      <select name="range" onchange="this.form.submit()">
        <option value="all" {{ "selected" if range_key=="all" else "" }}>All time</option>
        <option value="7" {{ "selected" if range_key=="7" else "" }}>Last 7 days</option>
        <option value="30" {{ "selected" if range_key=="30" else "" }}>Last 30 days</option>
        <option value="90" {{ "selected" if range_key=="90" else "" }}>Last 90 days</option>
        <option value="180" {{ "selected" if range_key=="180" else "" }}>Last 6 months</option>
        <option value="365" {{ "selected" if range_key=="365" else "" }}>Last year</option>
      </select>
      {% if range_start and range_end %}
      <span class="muted" style="font-size:12px;">Showing orders from <b>{{ range_start }}</b> to <b>{{ range_end }}</b></span>
      {% elif missing %}
      <span class="muted" style="font-size:12px;">No orders in this window.</span>
      {% endif %}
    </form>
    {% if missing %}
    <div class="row-inline" style="margin-bottom:10px;">
      <a class="btn btn-sm btn-outline" href="/pl/cogs/missing.csv{{ '?range=' + range_key if range_key != 'all' else '' }}">Download worklist CSV</a>
      <span class="muted" style="font-size:12px;">(same shape as price_families.csv — fill in your_price, re-upload below)</span>
    </div>
    <table>
      <tr><th>Family</th><th>Type</th><th>Basis</th><th>SKUs / Fix &amp; Merge</th><th>Orders</th><th>Units</th><th>Revenue (ex-VAT)</th><th>Set price</th></tr>
      {% for m in missing %}
      <tr>
        <td>{{ m.family }}</td>
        <td>{{ m.product_type }}</td>
        <td>{{ m.price_basis }}</td>
        <td style="min-width:220px;">
          {% for s in m.canonical_skus %}
          <div style="margin-bottom:8px;">
            <span style="font-size:11.5px;">{{ s.sku }} (pack {{ s.pack_qty }})</span>
            {% if s.asin_merged_count %}<span class="muted" style="font-size:10px;"> — consolidated from {{ s.asin_merged_count + 1 }} SKUs via ASIN</span>{% endif %}
            <button type="button" class="btn btn-sm btn-outline" style="margin-left:6px;"
                    onclick="var p=document.getElementById('fm_{{ loop.index0 }}_{{ m.family|replace(' ','_') }}'); p.style.display = p.style.display==='block' ? 'none' : 'block';">Fix / Merge</button>
            <div id="fm_{{ loop.index0 }}_{{ m.family|replace(' ','_') }}" style="display:none;background:#f7fbfb;border:1px solid #dde3e9;border-radius:8px;padding:10px;margin-top:6px;">
              <div style="margin-bottom:8px;">
                <b style="font-size:11.5px;">A. Merge into existing canonical</b>
                <form method="POST" action="/pl/cogs/merge" class="row-inline" style="margin-top:6px;">
                  <input type="hidden" name="variant_sku" value="{{ s.sku }}">
                  <input type="hidden" name="return_range" value="{{ range_key }}">
                  <input type="text" name="target_canonical" list="canonicalOptions"
                         placeholder="Search canonical SKU or family..." required
                         style="flex:1;min-width:180px;padding:6px 8px;border:1px solid #dde3e9;border-radius:6px;font-size:12px;">
                  <button class="btn btn-sm" type="submit">Merge</button>
                </form>
              </div>
              <div>
                <b style="font-size:11.5px;">B. Define as new family</b>
                <form method="POST" action="/pl/cogs/define-family" class="row-inline" style="margin-top:6px;">
                  <input type="hidden" name="canonical_sku" value="{{ s.sku }}">
                  <input type="hidden" name="return_range" value="{{ range_key }}">
                  <select name="product_type" style="padding:6px 8px;border:1px solid #dde3e9;border-radius:6px;font-size:12px;">
                    <option value="cushion">Cushion</option>
                    <option value="pillow">Pillow</option>
                    <option value="towel">Towel</option>
                    <option value="other" selected>Other</option>
                  </select>
                  <select name="price_basis" style="padding:6px 8px;border:1px solid #dde3e9;border-radius:6px;font-size:12px;">
                    <option value="single" selected>Single</option>
                    <option value="pair">Pair</option>
                  </select>
                  <input type="number" name="pack_qty" value="{{ s.pack_qty }}" min="1" step="1"
                         style="width:60px;padding:6px 8px;border:1px solid #dde3e9;border-radius:6px;font-size:12px;">
                  <input type="number" name="price" step="0.01" placeholder="£ price (optional)"
                         style="width:110px;padding:6px 8px;border:1px solid #dde3e9;border-radius:6px;font-size:12px;">
                  <button class="btn btn-sm" type="submit">Define</button>
                </form>
              </div>
            </div>
          </div>
          {% endfor %}
        </td>
        <td>{{ m.order_count }}</td>
        <td>{{ m.units }}</td>
        <td>£{{ "%.2f"|format(m.revenue_exvat) }}</td>
        <td>
          <form method="POST" action="/pl/cogs/family/{{ m.family }}/price" class="row-inline">
            <input class="price-input" type="number" step="0.01" name="price" placeholder="£0.00" required>
            <button class="btn btn-sm" type="submit">Save</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="muted">Nothing outstanding in this window — every family with real orders behind it has a price.</div>
    {% endif %}
  </div>

  <div class="card">
    <div class="title">Bulk price upload</div>
    <div class="subtitle">Upload a CSV in the same shape as price_families.csv (family,type,price_basis,
      enter_price_for,your_price,derives_these_skus) — updates existing family prices and adds any new
      ones. Blank your_price cells are skipped (still unpriced).</div>
    <form method="POST" action="/pl/cogs/upload" enctype="multipart/form-data" class="row-inline">
      <input type="file" name="csv" accept=".csv" required>
      <button class="btn" type="submit">Upload &amp; apply</button>
    </form>
  </div>

  <div class="card">
    <div class="title">All pricing families ({{ families|length }})</div>
    <div class="subtitle">Editable — changing a price here recomputes every order under this family
      immediately (no re-pull from Amazon).</div>
    <table>
      <tr><th>Family</th><th>Type</th><th>Basis</th><th>SKUs</th><th>Price (ex-VAT)</th><th>VAT rate</th><th>Source</th><th></th></tr>
      {% for f in families %}
      <tr>
        <td>{{ f.family }}</td>
        <td>{{ f.product_type }}</td>
        <td>{{ f.price_basis }}</td>
        <td>{{ f.n_canonical_skus }}</td>
        <td>{% if f.unit_price_exvat is not none %}£{{ "%.2f"|format(f.unit_price_exvat) }}{% else %}<span class="badge no">no price</span>{% endif %}</td>
        <td>{{ (f.vat_rate * 100)|round|int }}%</td>
        <td class="muted" style="font-size:11.5px;">{{ f.source }}</td>
        <td class="row-inline">
          <form method="POST" action="/pl/cogs/family/{{ f.family }}/price" class="row-inline">
            <input class="price-input" type="number" step="0.01" name="price"
                   value="{{ '%.2f'|format(f.unit_price_exvat) if f.unit_price_exvat is not none else '' }}">
            <button class="btn btn-sm" type="submit">Save</button>
          </form>
          <form method="POST" action="/pl/cogs/family/{{ f.family }}/vat" class="row-inline">
            <select name="vat_rate" style="padding:4px 6px;font-size:12px;">
              <option value="0.20" {{ "selected" if f.vat_rate==0.20 else "" }}>20% standard</option>
              <option value="0.05" {{ "selected" if f.vat_rate==0.05 else "" }}>5% reduced</option>
              <option value="0.0" {{ "selected" if f.vat_rate==0.0 else "" }}>0% zero-rated</option>
            </select>
            <button class="btn btn-sm" type="submit">Save VAT</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
    <div class="hint" style="margin-top:8px;">VAT rate is recorded metadata for display and cross-checking
      against Amazon's own tax code (see the SKU detail page) — it never changes the ex-VAT profit maths above,
      which is already anchored on Amazon's own balance change.</div>
  </div>
</div></body></html>
"""


def _first_account_id():
    accts = get_accounts()
    return accts[0]["account_id"] if accts else None


# module2_debug_fix_pass FIX 1: shared by both /pl and /pl/cogs' date-range
# filters (previously only /pl/cogs had one at all).
_RANGE_DAYS = {"7": 7, "30": 30, "90": 90, "180": 180, "365": 365}


def _range_start_date(range_key):
    """None for 'all' (or an unrecognised key -- default to All time rather
    than silently filtering) -- else an ISO date string N days back from
    now, compared against posted_date."""
    days = _RANGE_DAYS.get(range_key)
    if not days:
        return None
    return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")


# module2_dashboard_fixes D1: shared chart-metric machinery for the two
# time-series charts on this dashboard (/pl's "chart1" and the SKU detail
# page's trend chart) -- both were hardcoded to net_profit only, and chart1
# specifically was reported as "an unreadable smear" once plotted against
# real data (33,000+ orders across 2 years at daily granularity).
_CHART_METRICS = ("net_profit", "margin_pct", "revenue", "units", "orders", "ad_spend", "tacos")
_CHART_METRIC_LABELS = {
    "net_profit": "Net profit",
    "margin_pct": "Margin %",
    "revenue": "Revenue (ex-VAT)",
    "units": "Units",
    "orders": "Orders",
    "ad_spend": "Ad spend",
    "tacos": "TACOS",
}
# (value_prefix, value_suffix) per metric -- money / percent / bare count are
# different scales and shouldn't share one axis/hover format.
_CHART_METRIC_FMT = {
    "net_profit": ("£", ""), "margin_pct": ("", "%"), "revenue": ("£", ""),
    "units": ("", ""), "orders": ("", ""), "ad_spend": ("£", ""), "tacos": ("", "%"),
}


def _resolve_chart_period(range_key, requested_period, period_explicit):
    """D1 'default to something legible, not every daily point across all
    time': daily bucketing over a wide range is exactly the 'unreadable
    smear' the spec complains about. Only auto-picks a coarser default when
    the seller hasn't explicitly touched the Daily/Weekly/Monthly control
    THIS request -- any explicit choice (they changed the dropdown
    themselves, on either chart) is always respected as-is."""
    if period_explicit:
        return requested_period
    if range_key in ("all", "365"):
        return "month"
    if range_key in ("180", "90"):
        return "week"
    return requested_period  # 7/30 days -- daily is already legible


def _metric_series(period_rows, metric, ad_period_map=None):
    """Pulls the y-values for one chart metric out of a list of period-
    rollup rows (get_period_rollup / get_combined_period_rollup /
    get_sku_period_rollup -- all share the same _finish_rollup_row shape,
    keyed by row['period']).

    ad_period_map: optional {period_key: spend}, from
    pl_ads.get_ad_spend_period_series -- required only for the ad_spend/
    tacos options. pl_line_items.ad_spend is always NULL (reserved for
    Module 3, see pl_db.py); real ad spend lives in pl_ads' own table and
    is only ever joined onto rollups by ASIN, never by period, so those two
    metrics need this second series merged in by matching period key rather
    than being present on the rollup row already."""
    if metric not in _CHART_METRICS:
        metric = "net_profit"
    if metric == "net_profit":
        return [r.get("net_profit") or 0 for r in period_rows]
    if metric == "margin_pct":
        return [(r.get("margin_pct") or 0) * 100 for r in period_rows]
    if metric == "revenue":
        return [r.get("gross_sales_exvat") or 0 for r in period_rows]
    if metric == "units":
        return [r.get("units") or 0 for r in period_rows]
    if metric == "orders":
        return [r.get("orders") or 0 for r in period_rows]
    ad_period_map = ad_period_map or {}
    if metric == "ad_spend":
        return [ad_period_map.get(r["period"], 0.0) for r in period_rows]
    if metric == "tacos":
        out = []
        for r in period_rows:
            spend = ad_period_map.get(r["period"], 0.0)
            gross = r.get("gross_sales_exvat") or 0
            out.append((spend / gross * 100) if gross else 0)
        return out
    return [r.get("net_profit") or 0 for r in period_rows]


# module2_pl_ui_fixes Fix 5: same 7-metric set as the left "over time" chart
# (_metric_series above), but for the RIGHT-hand "by product" bar chart --
# a per-canonical-row lookup instead of a per-period one. Deliberately a
# separate dropdown from the left chart's (per Faraz's call): this chart
# tracks the table's own search/filters, not the account/range toolbar, so
# coupling its metric to the left chart's would be a second, unrelated kind
# of coupling on top of that existing difference. Unlike period rows,
# canonical rollup rows already carry ad_spend/tacos directly (attached by
# pl_ads.attach_ad_spend_to_rollup, joined by ASIN) -- no second query
# needed here the way the period chart needed get_ad_spend_period_series.
_PROFIT_LIKE_METRICS = ("net_profit", "margin_pct")


def _product_metric_value(r, metric):
    if metric not in _CHART_METRICS:
        metric = "net_profit"
    if metric == "net_profit":
        return r.get("net_profit") or 0
    if metric == "margin_pct":
        return (r.get("margin_pct") or 0) * 100
    if metric == "revenue":
        return r.get("gross_sales_exvat") or 0
    if metric == "units":
        return r.get("units") or 0
    if metric == "orders":
        return r.get("orders") or 0
    if metric == "ad_spend":
        return r.get("ad_spend") or 0
    if metric == "tacos":
        return (r.get("tacos") or 0) * 100
    return r.get("net_profit") or 0


def _product_metric_color(r, metric):
    """Amber for provisional rows always wins (data-completeness caveat
    applies regardless of which variable is plotted). Green/red pos-vs-neg
    only makes sense for the two metrics that can genuinely go negative
    (net profit, margin) -- revenue/units/orders/ad spend/TACOS are always
    >=0, so a single neutral dark-teal bar is used for those instead of a
    misleading "all green" or arbitrary sign split."""
    if r.get("provisional"):
        return "#e0a11e"
    if metric in _PROFIT_LIKE_METRICS:
        return "#1f9d57" if _product_metric_value(r, metric) >= 0 else "#cf3f52"
    return "#0e5c5b"


@app.route("/pl/cogs")
def pl_cogs_page():
    accounts = get_accounts()
    default_account = request.args.get("account") or _first_account_id()
    current_overhead = pl_cogs.get_overheads(default_account) if default_account else 0.0
    range_key = request.args.get("range", "all")
    start_date = _range_start_date(range_key)
    missing = pl_cogs.get_missing_prices_worklist(start_date=start_date)
    range_start, range_end = pl_cogs.resolve_worklist_date_range(start_date=start_date)
    return render_template_string(
        COGS_HTML, accounts=accounts, default_account=default_account,
        current_overhead=current_overhead,
        missing=missing, range_key=range_key,
        range_start=range_start, range_end=range_end,
        families=pl_cogs.get_all_families(),
        all_canonicals=pl_cogs.search_canonicals("", limit=5000),
        alias_count=pl_cogs.get_alias_count(),
    )


def _reprocess_after_cogs_change(family=None):
    """A price/alias change only affects pl_line_items.cogs (and everything
    derived from it) -- recompute from the already-ingested ledger, no
    network call, so the P&L reflects the new price immediately instead of
    waiting for the next scheduled `pl_tracker.py --reprocess`.

    module2_save_scope_fix: pass `family` for a pure price edit so only that
    family's rows are recomputed (near-instant) instead of all ~34k. Callers
    that change SKU->canonical membership (merge/define) or many families at
    once (CSV upload) leave family=None for a full reprocess."""
    try:
        n = pl_tracker.reprocess_from_stored_events(family=family)
        return n
    except Exception as e:
        app.logger.warning(f"reprocess after COGS change failed: {e}")
        return None


@app.route("/pl/cogs/family/<path:family>/price", methods=["POST"])
def pl_cogs_set_price(family):
    price_raw = (request.form.get("price") or "").strip()
    try:
        price = float(price_raw)
    except ValueError:
        flash(f"Could not save {family}: '{price_raw}' is not a valid number.")
        return redirect("/pl/cogs")
    pl_cogs.upsert_family_price(family, price, source="manual")
    n = _reprocess_after_cogs_change(family=family)
    flash(f"Saved £{price:.2f} for {family}."
          + (f" Recomputed {n} line item(s)." if n is not None else ""))
    return redirect("/pl/cogs")


@app.route("/pl/cogs/family/<path:family>/vat", methods=["POST"])
def pl_cogs_set_vat(family):
    """module2_dashboard_fixes A1: records the family's VAT rate. Metadata +
    display only -- deliberately does NOT call _reprocess_after_cogs_change()
    or touch pl_line_items at all, since nothing in the profit formula reads
    this value (see pl_cogs.py's _FAMILY_MIGRATIONS comment)."""
    vat_raw = (request.form.get("vat_rate") or "").strip()
    try:
        vat_rate = float(vat_raw)
    except ValueError:
        flash(f"Could not save VAT rate for {family}: '{vat_raw}' is not a valid number.")
        return redirect("/pl/cogs")
    try:
        pl_cogs.upsert_family_vat_rate(family, vat_rate, source="manual")
    except ValueError as e:
        flash(str(e))
        return redirect("/pl/cogs")
    flash(f"Saved VAT rate {vat_rate*100:.0f}% for {family}.")
    return redirect("/pl/cogs")


@app.route("/pl/cogs/upload", methods=["POST"])
def pl_cogs_upload():
    f = request.files.get("csv")
    if not f or not f.filename:
        flash("No file selected.")
        return redirect("/pl/cogs")
    try:
        result = pl_cogs.bulk_upsert_families_csv(f.stream)
        n = _reprocess_after_cogs_change()
        flash(f"Applied CSV: {result['updated']} price(s) updated, {result['created']} new family(ies)."
              + (f" Recomputed {n} line item(s)." if n is not None else ""))
    except Exception as e:
        flash(f"Upload failed: {e}")
    return redirect("/pl/cogs")


@app.route("/pl/cogs/missing.csv")
def pl_cogs_missing_csv():
    start_date = _range_start_date(request.args.get("range", "all"))
    rows = pl_cogs.export_missing_prices_csv_rows(start_date=start_date)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                     headers={"Content-Disposition": "attachment; filename=missing_prices_worklist.csv"})


@app.route("/pl/cogs/merge", methods=["POST"])
def pl_cogs_merge():
    """module2_ux_and_merge_tool Fix/Merge option A: 'this SKU is actually
    the same product as an existing canonical.' Manual — always wins over
    any past or future ASIN auto-consolidation for this SKU."""
    variant_sku = (request.form.get("variant_sku") or "").strip()
    target_canonical = (request.form.get("target_canonical") or "").strip()
    return_range = request.form.get("return_range", "all")
    try:
        result = pl_cogs.manual_merge_sku(variant_sku, target_canonical)
        n = _reprocess_after_cogs_change()
        flash(f"Merged '{variant_sku}' into '{target_canonical}' (family {result['family']})."
              + (f" Recomputed {n} line item(s)." if n is not None else ""))
    except ValueError as e:
        flash(f"Could not merge: {e}")
    return redirect(f"/pl/cogs?range={return_range}")


@app.route("/pl/cogs/define-family", methods=["POST"])
def pl_cogs_define_family():
    """module2_ux_and_merge_tool Fix/Merge option B: 'this SKU is its own
    new product' -- bypasses the name-pattern classifier entirely with a
    seller-entered type/basis/price."""
    canonical_sku = (request.form.get("canonical_sku") or "").strip()
    product_type = (request.form.get("product_type") or "other").strip()
    price_basis = (request.form.get("price_basis") or "single").strip()
    return_range = request.form.get("return_range", "all")
    try:
        pack_qty = int(request.form.get("pack_qty") or 1)
    except ValueError:
        pack_qty = 1
    price_raw = (request.form.get("price") or "").strip()
    price = None
    if price_raw:
        try:
            price = float(price_raw)
        except ValueError:
            flash(f"'{price_raw}' is not a valid price — family created without a price.")
    try:
        pl_cogs.manual_define_family(canonical_sku, product_type, price_basis, pack_qty, price)
        n = _reprocess_after_cogs_change()
        flash(f"Defined '{canonical_sku}' as a new {product_type} family."
              + (f" Recomputed {n} line item(s)." if n is not None else ""))
    except ValueError as e:
        flash(f"Could not define family: {e}")
    return redirect(f"/pl/cogs?range={return_range}")


@app.route("/pl/cogs/overheads", methods=["POST"])
def pl_cogs_set_overheads():
    # Retired: overheads now live on the Expenses page (single source of truth).
    # Kept as a redirect so any stale bookmark/form can't write the old flat field.
    flash("Monthly overheads moved to the Expenses page — add them there instead.")
    return redirect("/pl/expenses")


@app.route("/pl/inline-cogs", methods=["POST"])
def pl_inline_cogs_set_price():
    """module2_true_profit Phase 2: the SAME family-price write path as
    /pl/cogs/family/<family>/price (pl_cogs.upsert_family_price), triggered
    from the inline box on the main /pl rollup instead of the COGS &amp;
    pricing page -- deliberately not a per-SKU override, so there is never a
    second, competing source of truth for a family's price."""
    family = (request.form.get("family") or "").strip()
    price_raw = (request.form.get("price") or "").strip()
    return_url = request.form.get("return_url") or "/pl"
    if not family:
        flash("No family specified — could not save.")
        return redirect(return_url)
    try:
        price = float(price_raw)
    except ValueError:
        flash(f"Could not save {family}: '{price_raw}' is not a valid number.")
        return redirect(return_url)
    pl_cogs.upsert_family_price(family, price, source="manual")
    n = _reprocess_after_cogs_change(family=family)
    flash(f"Saved £{price:.2f} for {family}." + (f" Recomputed {n} line item(s)." if n is not None else ""))
    return redirect(return_url)


@app.route("/pl/inline-vat", methods=["POST"])
def pl_inline_vat_set_rate():
    """module2_dashboard_fixes A1: same family-level write path as
    /pl/cogs/family/<family>/vat (pl_cogs.upsert_family_vat_rate), triggered
    from the SKU detail page's inline box instead of the COGS & pricing
    page. Metadata only -- no reprocess, nothing in the profit formula
    reads this."""
    family = (request.form.get("family") or "").strip()
    vat_raw = (request.form.get("vat_rate") or "").strip()
    return_url = request.form.get("return_url") or "/pl"
    if not family:
        flash("No family specified — could not save VAT rate.")
        return redirect(return_url)
    try:
        vat_rate = float(vat_raw)
    except ValueError:
        flash(f"Could not save VAT rate for {family}: '{vat_raw}' is not a valid number.")
        return redirect(return_url)
    try:
        pl_cogs.upsert_family_vat_rate(family, vat_rate, source="manual")
    except ValueError as e:
        flash(str(e))
        return redirect(return_url)
    flash(f"Saved VAT rate {vat_rate*100:.0f}% for {family}.")
    return redirect(return_url)


# ─────────────────────────────────────────────────────────────────────────────
# SKU MERGE WORKBENCH (File F) — the page that makes COGS entry tractable.
# Fragmentation is alias-coverage: an un-aliased SKU resolves to ITSELF and is
# classified on its own raw string, so every naming variant becomes its own
# one-SKU family. Merging writes cogs_aliases (source='manual') -- merge and
# de-fragment are the SAME durable operation. Overrides write cogs_canonical/
# cogs_families (source='manual'), which survives both a CSV re-seed (INSERT OR
# IGNORE fills gaps only) and a reprocess (ensure_canonical never reclassifies
# an existing canonical). Everything stages client-side and applies in ONE
# batch with ONE reprocess at the end -- a reprocess per click was the thing
# that made the old flow unusable.
# ─────────────────────────────────────────────────────────────────────────────

MERGE_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SKU merge &amp; COGS fix — BSR Repricer</title>
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c}
 a{color:#0e5c5b;text-decoration:none;font-weight:600}
 a:hover{text-decoration:underline}
 .header{background:#0e5c5b;color:#eafcfb;padding:18px 30px;display:flex;justify-content:space-between;align-items:center}
 .header a{color:#bfe9e7;font-size:13px;margin-left:16px}
 .wrap{max-width:min(1500px,96vw);margin:20px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
 .title{font-size:15px;font-weight:700;margin-bottom:6px}
 .subtitle{font-size:12.5px;color:#5a6472;margin-bottom:12px;line-height:1.55}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:left;border-top:1px solid #eef1f4;vertical-align:middle}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase;letter-spacing:.3px}
 .muted{color:#8a94a2}
 .btn{background:#0e5c5b;color:#eafcfb;border:none;border-radius:6px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer}
 .btn-sm{padding:4px 9px;font-size:11.5px}
 .btn-ghost{background:#fff;color:#0e5c5b;border:1px solid #0e5c5b}
 .btn-warn{background:#9e2d3c}
 .flash{background:#e7f6ee;color:#166b3d;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:600}
 .chip{display:inline-block;background:#f1f4f7;border-radius:11px;padding:1px 8px;font-size:11px;color:#5a6472;margin-right:4px}
 .badge{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:10px}
 .badge.no{background:#fbe8eb;color:#9e2d3c}
 .badge.ok{background:#e7f6ee;color:#166b3d}
 input[type=text],select{padding:5px 8px;border:1px solid #dde3e9;border-radius:6px;font-size:12.5px}
 .stagebar{position:sticky;top:0;z-index:20;background:#0e5c5b;color:#eafcfb;border-radius:10px;padding:12px 16px;
           margin-bottom:16px;display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap}
 .stagebar.empty{display:none}
 .staged-list{font-size:12px;max-height:150px;overflow:auto;flex:1;line-height:1.6}
 code{background:#f1f4f7;padding:1px 5px;border-radius:4px;font-size:12px}
</style></head><body>
{{ nav|safe }}<div class="header" style="padding-top:10px"><b>SKU merge &amp; COGS fix</b><div><a href="/pl/cogs">← COGS &amp; pricing</a></div></div>
<div class="wrap">
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}

  <form method="POST" action="/pl/cogs/skus/apply" id="applyForm">
    <input type="hidden" name="ops" id="opsField" value="[]">
    <div class="stagebar empty" id="stageBar">
      <div><b id="stageCount">0</b> staged change(s) — nothing is written until you apply.
        <div class="staged-list" id="stagedList"></div></div>
      <div style="display:flex;gap:8px;">
        <button type="button" class="btn btn-ghost" onclick="plClearStaged()">Clear</button>
        <button type="submit" class="btn">Apply all + reprocess once</button>
      </div>
    </div>
  </form>

  <div class="card">
    <div class="title">Misclassified by punctuation — {{ misclassified|length }} SKU(s)</div>
    <div class="subtitle">These fall into <b>other / single</b> only because <code>.</code>, <code>_</code> or a
      space stands in where the classifier expects <code>-P#</code> (pillow) or <code>x</code> (cushion) — so they
      lose the <b>pair</b> basis and get the wrong COGS multiplier. "Stage fix" applies the correct type/basis
      <b>and</b> folds the SKU into the existing family, so it inherits that family's single price rather than
      becoming a one-SKU family you'd have to keep in sync.</div>
    {% if not misclassified %}
    <div class="muted">None — every canonical classifies cleanly.</div>
    {% else %}
    <table>
      <tr><th>SKU</th><th>Now</th><th>Should be</th><th>Target family</th><th>Volume</th><th></th></tr>
      {% for m in misclassified %}
      <tr>
        <td><code>{{ m.canonical_sku }}</code></td>
        <td><span class="badge no">other / single</span></td>
        <td><b>{{ m.suggested_type }}</b><div class="muted" style="font-size:11.5px;">{{ m.suggested_describe }}</div></td>
        <td>{% if m.suggested_family_exists %}<code>{{ m.suggested_family }}</code>{% else %}<code>{{ m.suggested_family }}</code> <span class="badge no">new</span>{% endif %}</td>
        <td class="muted">{{ m.lines }} lines · {{ m.units }} units · £{{ "%.2f"|format(m.revenue or 0) }}</td>
        <td><button type="button" class="btn btn-sm"
              onclick="plStageOverride('{{ m.canonical_sku|e }}','{{ m.suggested_type }}','{{ m.suggested_basis }}',{{ m.suggested_pack_qty }},'{{ m.suggested_family|e }}',{{ 'true' if m.suggested_family_exists else 'false' }})">Stage fix</button></td>
      </tr>
      {% endfor %}
    </table>
    <div style="margin-top:10px;"><button type="button" class="btn btn-ghost btn-sm" onclick="plStageAllMisclassified()">Stage all {{ misclassified|length }} fixes</button></div>
    {% endif %}
  </div>

  <div class="card">
    <div class="title">SKU workbench — merge duplicates into one canonical</div>
    <div class="subtitle">Merging writes an alias (<code>variant → canonical</code>), which is exactly what
      de-fragments the family space: the merged SKU stops being its own one-SKU family and inherits the
      target's family and price. Reversible below. Chips show what you're merging so you don't collapse two
      genuinely different products.</div>
    <div style="margin-bottom:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
      <input type="text" id="wbSearch" placeholder="Filter by SKU, ASIN or family…" style="width:320px;" oninput="plFilterWb()">
      <label class="muted" style="font-size:12px;"><input type="checkbox" id="wbUnpricedOnly" onchange="plFilterWb()"> unpriced only</label>
      <span class="muted" id="wbCount" style="font-size:12px;"></span>
    </div>
    <datalist id="canonList">{% for w in workbench %}<option value="{{ w.canonical_sku }}"></option>{% endfor %}</datalist>
    <table id="wbTable">
      <tr><th>Canonical SKU</th><th>Chips</th><th>Family</th><th>Type / basis</th><th>Price</th><th>Merge into…</th></tr>
      {% for w in workbench %}
      <tr data-s="{{ (w.canonical_sku ~ ' ' ~ (w.asins|join(' ')) ~ ' ' ~ (w.family or ''))|lower }}"
          data-priced="{{ 'yes' if w.unit_price is not none else 'no' }}">
        <td><code>{{ w.canonical_sku }}</code>{% if w.source == 'manual' %} <span class="badge ok">manual</span>{% endif %}</td>
        <td>
          {% for a in w.asins[:3] %}<span class="chip">{{ a }}</span>{% endfor %}
          <span class="chip">{{ w.lines }} orders</span>
          <span class="chip">£{{ "%.2f"|format(w.revenue or 0) }}</span>
        </td>
        <td class="muted">{{ w.family }}</td>
        <td class="muted">{{ w.product_type }}<div style="font-size:11px;">{{ w.describe }}</div></td>
        <td>{% if w.unit_price is not none %}£{{ "%.2f"|format(w.unit_price) }}{% else %}<span class="badge no">no price</span>{% endif %}</td>
        <td>
          <input type="text" list="canonList" placeholder="target canonical" style="width:170px;"
                 id="mt-{{ loop.index }}">
          <button type="button" class="btn btn-sm"
                  onclick="plStageMerge('{{ w.canonical_sku|e }}', document.getElementById('mt-{{ loop.index }}').value)">Stage merge</button>
        </td>
      </tr>
      {% endfor %}
    </table>
  </div>

  <div class="card">
    <div class="title">Recent manual merges — reversible</div>
    <div class="subtitle">Undo removes the manual alias so the SKU resolves to itself again (a seeded or
      ASIN-auto mapping is never touched). Each undo reprocesses on its own.</div>
    {% if not recent %}<div class="muted">No manual merges yet.</div>{% else %}
    <table>
      <tr><th>Merged SKU</th><th>Into canonical</th><th>When</th><th></th></tr>
      {% for r in recent %}
      <tr>
        <td><code>{{ r.variant_sku }}</code></td>
        <td><code>{{ r.canonical_sku }}</code></td>
        <td class="muted">{{ (r.updated_at or '')[:19] }}</td>
        <td>
          <form method="POST" action="/pl/cogs/skus/undo" style="display:inline;"
                onsubmit="return confirm('Undo this merge? The SKU resolves to itself again and the P&amp;L reprocesses.')">
            <input type="hidden" name="variant_sku" value="{{ r.variant_sku }}">
            <button class="btn btn-sm btn-warn" type="submit">Undo</button>
          </form>
        </td>
      </tr>
      {% endfor %}
    </table>
    {% endif %}
  </div>
</div>

<script>
  // Staged batching: everything queues client-side and is applied in ONE POST
  // with ONE reprocess at the end. JS strings are single-quoted throughout --
  // this file is a Python triple-quoted string, so a backslash-escaped double
  // quote would collapse and break the whole script block.
  var PL_OPS = [];
  var PL_MIS = {{ misclassified|tojson }};

  function plRender(){
    var bar = document.getElementById('stageBar');
    var list = document.getElementById('stagedList');
    document.getElementById('stageCount').textContent = PL_OPS.length;
    document.getElementById('opsField').value = JSON.stringify(PL_OPS);
    if (!PL_OPS.length){ bar.classList.add('empty'); list.innerHTML = ''; return; }
    bar.classList.remove('empty');
    list.innerHTML = PL_OPS.map(function(o, i){
      var txt = (o.op === 'merge')
        ? ('merge ' + o.variant + ' → ' + o.target)
        : ('override ' + o.sku + ' → ' + o.type + '/' + o.basis + ' ×' + o.pack_qty + (o.family ? (' in ' + o.family) : ''));
      return '<div>' + (i+1) + '. ' + txt + ' <a href="javascript:void(0)" onclick="plUnstage(' + i + ')" style="color:#bfe9e7;">remove</a></div>';
    }).join('');
  }
  function plUnstage(i){ PL_OPS.splice(i,1); plRender(); }
  function plClearStaged(){ PL_OPS = []; plRender(); }

  function plStageOverride(sku, type, basis, packQty, family, familyExists){
    // Only join an EXISTING family (inherit its price). If the suggested family
    // doesn't exist yet, send null so the SKU keeps its own family rather than
    // failing -- manual_override_type_basis refuses to invent a family.
    PL_OPS.push({op:'override', sku:sku, type:type, basis:basis, pack_qty:packQty,
                 family: familyExists ? family : null});
    plRender();
  }
  function plStageAllMisclassified(){
    PL_MIS.forEach(function(m){
      plStageOverride(m.canonical_sku, m.suggested_type, m.suggested_basis,
                      m.suggested_pack_qty, m.suggested_family, m.suggested_family_exists);
    });
  }
  function plStageMerge(variant, target){
    target = (target || '').trim();
    if (!target){ alert('Type or pick the canonical SKU to merge into first.'); return; }
    if (target === variant){ alert('A SKU cannot be merged into itself.'); return; }
    PL_OPS.push({op:'merge', variant:variant, target:target});
    plRender();
  }

  function plFilterWb(){
    var q = (document.getElementById('wbSearch').value || '').toLowerCase().trim();
    var unpricedOnly = document.getElementById('wbUnpricedOnly').checked;
    var rows = document.querySelectorAll('#wbTable tr[data-s]');
    var shown = 0;
    rows.forEach(function(tr){
      var ok = (!q || tr.getAttribute('data-s').indexOf(q) !== -1)
               && (!unpricedOnly || tr.getAttribute('data-priced') === 'no');
      tr.style.display = ok ? '' : 'none';
      if (ok) shown++;
    });
    document.getElementById('wbCount').textContent = 'showing ' + shown + ' of ' + rows.length;
  }
  document.addEventListener('DOMContentLoaded', function(){ plRender(); plFilterWb(); });
</script>
</body></html>
"""


@app.route("/pl/cogs/skus")
def pl_cogs_skus_page():
    """File F merge workbench. Read-only render; every write goes through the
    staged batch below so there's exactly ONE reprocess per apply."""
    return render_template_string(
        MERGE_HTML,
        misclassified=pl_cogs.find_punctuation_misclassified(),
        workbench=pl_cogs.get_merge_workbench(),
        recent=pl_cogs.get_recent_manual_merges(),
    )


@app.route("/pl/cogs/skus/apply", methods=["POST"])
def pl_cogs_skus_apply():
    """Applies the whole staged batch, then reprocesses ONCE. Per-op failures
    are collected and reported rather than aborting the batch, so one bad
    target doesn't silently discard twenty good merges."""
    try:
        ops = json.loads(request.form.get("ops") or "[]")
    except Exception:
        flash("Could not read the staged changes — nothing was applied.")
        return redirect("/pl/cogs/skus")
    merged = overridden = 0
    errors = []
    for o in ops:
        try:
            if o.get("op") == "merge":
                pl_cogs.manual_merge_sku(o["variant"], o["target"])
                merged += 1
            elif o.get("op") == "override":
                pl_cogs.manual_override_type_basis(
                    o["sku"], o["type"], o["basis"],
                    int(o.get("pack_qty") or 1), o.get("family") or None)
                overridden += 1
        except Exception as e:
            errors.append(f"{o.get('sku') or o.get('variant')}: {e}")
    n = 0
    if merged or overridden:
        # ONE reprocess for the entire batch -- the point of staging.
        n = pl_tracker.reprocess_from_stored_events()
    msg = (f"Applied {merged} merge(s) and {overridden} override(s); "
           f"{n} line item(s) recomputed in a single reprocess.")
    if errors:
        msg += f" {len(errors)} failed — " + "; ".join(errors[:3])
    flash(msg)
    return redirect("/pl/cogs/skus")


@app.route("/pl/cogs/skus/undo", methods=["POST"])
def pl_cogs_skus_undo():
    variant = (request.form.get("variant_sku") or "").strip()
    n = pl_cogs.undo_merge(variant)
    if n:
        pl_tracker.reprocess_from_stored_events()
        flash(f"Un-merged {variant} — it resolves to itself again, P&L reprocessed.")
    else:
        flash(f"No manual merge found for {variant} (seeded/auto aliases are never removed by undo).")
    return redirect("/pl/cogs/skus")


# ─────────────────────────────────────────────────────────────────────────────
# AD SPEND — module2_true_profit Phase 1. CSV import today (Amazon Ads API
# application still pending); see pl_ads.py for the storage/join design and
# how the eventual API path slots in without changing anything downstream.
# ─────────────────────────────────────────────────────────────────────────────

ADS_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ad spend — BSR Repricer</title>
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c}
 /* module2_dashboard_fixes D3: dark-teal link styling, replacing default
    browser blue/underline -- more specific selectors below (.header a,
    etc.) intentionally still override this base rule. */
 a{color:#0e5c5b;text-decoration:none;font-weight:600}
 a:hover{text-decoration:underline}
 a:visited{color:#0e5c5b}
 .header{background:#0e5c5b;color:#eafcfb;padding:18px 30px;display:flex;justify-content:space-between;align-items:center}
 .header a{color:#bfe9e7;font-size:13px;text-decoration:none;margin-left:16px}
 .wrap{max-width:1100px;margin:24px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
 .title{font-size:15px;font-weight:700;margin-bottom:6px}
 .subtitle{font-size:12.5px;color:#5a6472;margin-bottom:14px;line-height:1.5}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:left;border-top:1px solid #eef1f4;vertical-align:middle}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase;letter-spacing:.3px}
 .muted{color:#8a94a2}
 .btn{background:#0e5c5b;color:#eafcfb;border:none;border-radius:6px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer}
 .flash{background:#e7f6ee;color:#166b3d;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:600}
 .flash.warn{background:#fbf1dd;color:#8a5906}
 input[type=file]{font-size:12.5px}
 .row-inline{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 label{font-size:12px;color:#5a6472}
 select{padding:8px 12px;border:1px solid #dde3e9;border-radius:8px;font-size:13px;background:#fff}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}

  <div class="card">
    <div class="title">Upload Advertised Product Report</div>
    <div class="subtitle">Sponsored Products → Advertised Product Report, exported from Seller Central /
      Amazon Ads console (per advertised ASIN/SKU, per day: spend, ad sales, clicks, orders). Re-uploading a
      report that overlaps dates you've already loaded is safe — it updates those days' totals, it never
      duplicates them. When the Amazon Ads API application is approved, this same table will populate
      automatically instead; nothing else about this page or the P&amp;L join will need to change.</div>
    <form method="POST" action="/pl/ads/upload" enctype="multipart/form-data" class="row-inline">
      <label>Account</label>
      <select name="account_id">{% for a in accounts %}
        <option value="{{ a.account_id }}" {{ "selected" if a.account_id==default_account else "" }}>{{ a.account_id }}</option>
      {% endfor %}</select>
      <input type="file" name="csv" accept=".csv" required>
      <button class="btn" type="submit">Upload &amp; apply</button>
    </form>
  </div>

  <div class="card">
    <div class="title">What's loaded so far</div>
    {% if not coverage %}
    <div class="muted">No ad spend data uploaded yet.</div>
    {% else %}
    <table>
      <tr><th>Account</th><th>Source</th><th>Date range covered</th><th>Distinct ASINs</th><th>Rows</th><th>Total spend</th></tr>
      {% for c in coverage %}
      <tr>
        <td>{{ c.account_id }}</td>
        <td>{{ c.source }}</td>
        <td>{{ c.date_min }} to {{ c.date_max }}</td>
        <td>{{ c.distinct_asins }}</td>
        <td>{{ c.n_rows }}</td>
        <td>£{{ "%.2f"|format(c.total_spend or 0) }}</td>
      </tr>
      {% endfor %}
    </table>
    {% endif %}
    <div class="subtitle" style="margin-top:12px;margin-bottom:0;">If the <a href="/pl">P&amp;L</a> page's
      selected date range extends beyond what's covered here, it shows a plain warning rather than silently
      treating the uncovered days as zero ad spend.</div>
  </div>

  <div class="card">
    <div class="title">Ad performance by Amazon variation parent</div>
    <div class="subtitle" style="line-height:1.55;">
      <b style="color:#9e2d3c;">This is Amazon's variation grouping — NOT your COGS pricing family.</b>
      An Amazon <b>parent ASIN</b> (e.g. <code>B0H2NP5547</code>) links every size <b>and</b> colour Amazon sells
      as one listing family — dozens of SKUs spanning 80140 / 5080 / 100200 sizes. Your <b>COGS family</b>
      (e.g. <code>TOWEL-80140</code>) is size-specific and much narrower. Two different groupings — never conflate
      them. Judge <b>ad spend</b> here: <b>halo</b> sales land on siblings within the Amazon parent, so a family
      can be healthy (good ROAS) even when many of its individual ASINs read as ad-losses on their own sales.
    </div>
    <form method="GET" action="/pl/ads" class="row-inline" style="margin-bottom:12px;">
      <label>Window</label>
      <select name="range" onchange="this.form.submit()">
        <option value="all" {{ "selected" if range_key=="all" else "" }}>All loaded</option>
        <option value="7" {{ "selected" if range_key=="7" else "" }}>Last 7 days</option>
        <option value="30" {{ "selected" if range_key=="30" else "" }}>Last 30 days</option>
        <option value="90" {{ "selected" if range_key=="90" else "" }}>Last 90 days</option>
      </select>
    </form>
    {% if not parent_rollup %}
    <div class="muted">No ad data with a parent ASIN in this window. (Standalone products with no Amazon
      variation parent aren't shown here — that's expected, not a gap.)</div>
    {% else %}
    <table>
      <tr><th>Amazon parent ASIN</th><th>Variations</th><th>Spend</th><th>Ad sales (own+halo)</th><th>ROAS</th><th>ACOS</th><th>ASINs that look like a loss</th></tr>
      {% for p in parent_rollup %}
      <tr>
        <td><code>{{ p.parent_asin }}</code></td>
        <td>{{ p.n_asins }}</td>
        <td>£{{ "%.2f"|format(p.spend) }}</td>
        <td>£{{ "%.2f"|format(p.ad_sales) }} <span class="muted" style="font-size:11px;">(own £{{ "%.0f"|format(p.promoted) }} · halo £{{ "%.0f"|format(p.halo) }})</span></td>
        <td>{% if p.roas is not none %}<b>{{ "%.2f"|format(p.roas) }}×</b>{% else %}—{% endif %}</td>
        <td>{% if p.acos is not none %}{{ "%.1f"|format(p.acos*100) }}%{% else %}—{% endif %}</td>
        <td>{% if p.advertised_asins %}{{ p.own_loss_asins }} of {{ p.advertised_asins }}{% if p.own_loss_asins and p.roas and p.roas >= 1 %} <span class="muted">— family still {{ "%.2f"|format(p.roas) }}× (halo)</span>{% endif %}{% else %}—{% endif %}</td>
      </tr>
      {% endfor %}
    </table>
    <div class="subtitle" style="margin-top:12px;margin-bottom:0;">"ASINs that look like a loss" = advertised
      ASINs whose own (promoted) sales don't cover their own spend. When the family ROAS is healthy but many
      ASINs read as losses, the campaigns <b>are</b> working — the return lands on sibling colours/sizes, not the
      advertised ASIN. This is the number that decides whether a family's campaigns need cutting, which the
      per-ASIN P&amp;L view can't show.<br><br>
      <b>ROAS / ACOS here are one clock</b> — ad sales ÷ ad spend, both click-date attributed from the ad
      report. That's different from <b>TACOS</b> on the P&amp;L (ad spend ÷ <i>settled</i> gross sales), which
      straddles two clocks. Both ad-sales figures here drift up slightly when you re-pull the report as late
      conversions land — expected, not an error.</div>
    {% endif %}
  </div>
</div></body></html>
"""


@app.route("/pl/ads")
def pl_ads_page():
    accounts = get_accounts()
    default_account = request.args.get("account") or _first_account_id()
    # module2_ads_parent_rollup (D2): ad economics grouped by Amazon variation
    # parent, range-aware so the "is this family's ad spend working over the
    # last N days?" question can be answered on the same window as the P&L.
    range_key = request.args.get("range", "all")
    start_date = _range_start_date(range_key)
    parent_rollup = pl_ads.get_parent_asin_rollup(
        account_id=request.args.get("account"), start_date=start_date)
    return render_template_string(
        ADS_HTML, accounts=accounts, default_account=default_account,
        coverage=pl_ads.get_upload_history(),
        parent_rollup=parent_rollup, range_key=range_key,
    )


@app.route("/pl/ads/upload", methods=["POST"])
def pl_ads_upload():
    account_id = request.form.get("account_id") or _first_account_id()
    f = request.files.get("csv")
    if not f or not f.filename:
        flash("No file selected.")
        return redirect("/pl/ads")
    if not account_id:
        flash("No account configured — add one on the Accounts page first.")
        return redirect("/pl/ads")
    try:
        report = pl_ads.import_advertised_product_csv(account_id, f.stream)
        msg = (f"Imported {report['rows_read']} row(s) for {account_id} "
               f"({report['rows_skipped']} skipped) — {report['distinct_asins']} distinct ASIN(s), "
               f"{report['date_min']} to {report['date_max']}, total spend £{report['total_spend']:.2f}.")
        if report["rows_skipped"]:
            msg += " Skipped rows had no usable date/ASIN — check the export if this seems high."
        flash(msg)
    except ValueError as e:
        flash(f"Upload failed: {e}")
    return redirect("/pl/ads")


# ─────────────────────────────────────────────────────────────────────────────
# MISSING POSTAGE — module2_true_profit Phase 3. Order-level worklist for
# genuinely off-Amazon orders (no real Amazon label found AND nothing
# entered yet). See pl_postage.py for why the old flat-default guess was
# retired in favour of this.
# ─────────────────────────────────────────────────────────────────────────────

POSTAGE_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Missing postage — BSR Repricer</title>
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c}
 /* module2_dashboard_fixes D3: dark-teal link styling, replacing default
    browser blue/underline -- more specific selectors below (.header a,
    etc.) intentionally still override this base rule. */
 a{color:#0e5c5b;text-decoration:none;font-weight:600}
 a:hover{text-decoration:underline}
 a:visited{color:#0e5c5b}
 .header{background:#0e5c5b;color:#eafcfb;padding:18px 30px;display:flex;justify-content:space-between;align-items:center}
 .header a{color:#bfe9e7;font-size:13px;text-decoration:none;margin-left:16px}
 .wrap{max-width:1200px;margin:24px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
 .title{font-size:15px;font-weight:700;margin-bottom:6px}
 .subtitle{font-size:12.5px;color:#5a6472;margin-bottom:14px;line-height:1.5}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:left;border-top:1px solid #eef1f4;vertical-align:middle}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase;letter-spacing:.3px}
 tr:hover td{background:#f7fbfb}
 .muted{color:#8a94a2}
 .btn{background:#0e5c5b;color:#eafcfb;border:none;border-radius:6px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer}
 .flash{background:#e7f6ee;color:#166b3d;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:600}
 .flash.warn{background:#fbf1dd;color:#8a5906}
 .pill{display:inline-block;background:#fbf1dd;color:#8a5906;border-radius:20px;padding:6px 14px;font-size:12.5px;margin-bottom:14px;}
 .row-inline{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 label{font-size:12px;color:#5a6472}
 select{padding:8px 12px;border:1px solid #dde3e9;border-radius:8px;font-size:13px;background:#fff}
 input[type=number]{padding:7px 9px;border:1px solid #dde3e9;border-radius:6px;font-size:13px;width:110px}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}

  <form method="GET" action="/pl/postage" class="row-inline" style="margin-bottom:10px;">
    <label>Account</label>
    <select name="account" onchange="this.form.submit()">
      {% for a in accounts %}<option value="{{ a.account_id }}" {{ "selected" if a.account_id==default_account else "" }}>{{ a.account_id }}</option>{% endfor %}
    </select>
  </form>

  {% if week_count > 15 %}
  <div class="pill">{{ week_count }} orders missing postage in the last 7 days — this is high enough to be
    a sign the Amazon label-cost pipeline isn't fully catching orders it should, not just genuine
    off-Amazon couriers. Worth checking a few of these against Seller Central before assuming they're all
    real off-Amazon shipments.</div>
  {% endif %}

  <div class="card">
    <div class="title">Orders with no postage cost ({{ worklist|length }})</div>
    <div class="subtitle">Only orders with no real Amazon label AND nothing entered yet appear here — an
      order with a real Amazon label or an already-confirmed manual amount never shows up. Never guess: a
      blank stays £0 in the P&amp;L (visibly "provisional") until it's filled in here.</div>
    {% if worklist %}
    <form method="POST" action="/pl/postage/save">
      <input type="hidden" name="account_id" value="{{ default_account }}">
      <input type="hidden" name="return_qs" value="account={{ default_account }}">
      <div class="row-inline" style="margin-bottom:10px;">
        <label>Actual postage paid (£, ex-VAT) for every selected row</label>
        <input type="number" step="0.01" name="amount" placeholder="£0.00"
               {% if last_value is not none %}value="{{ '%.2f'|format(last_value) }}"{% endif %} required>
        <button class="btn" type="submit">Apply to selected</button>
        {% if total_missing %}<button class="btn" type="submit" formaction="/pl/postage/apply-all"
          style="background:#8a5906;" onclick="return confirm('Set this amount for ALL {{ total_missing }} remaining missing-postage orders (in batches of 1000)?')">Apply to all remaining ({{ total_missing }})</button>{% endif %}
        <span class="muted" style="font-size:11.5px;">(prefilled with your last-entered value — courier rates rarely change week to week, just confirm or adjust)</span>
      </div>
      <table>
        <tr>
          <th><input type="checkbox" onclick="document.querySelectorAll('.pmRow').forEach(function(cb){cb.checked=this.checked;}, this)"></th>
          <th>Order ID</th><th>Date</th><th>SKU(s)</th><th>Units</th><th>Buyer-paid shipping</th>
        </tr>
        {% for w in worklist %}
        <tr>
          <td><input class="pmRow" type="checkbox" name="order_ids" value="{{ w.order_id }}"></td>
          <td>{{ w.order_id }}</td>
          <td>{{ w.posted_date[:10] if w.posted_date else "—" }}</td>
          <td>{{ w.skus or "—" }}</td>
          <td>{{ w.units or 0 }}</td>
          <td>£{{ "%.2f"|format(w.buyer_paid_shipping or 0) }}</td>
        </tr>
        {% endfor %}
      </table>
    </form>
    {% else %}
    <div class="muted">Nothing outstanding for {{ default_account }} — every off-Amazon order either has a
      real Amazon label or a manual postage amount entered.</div>
    {% endif %}
  </div>
</div></body></html>
"""


@app.route("/pl/postage")
def pl_postage_page():
    accounts = get_accounts()
    default_account = request.args.get("account") or _first_account_id()
    worklist = pl_postage.get_missing_postage_worklist(default_account) if default_account else []
    last_value = pl_postage.get_last_manual_postage_value(default_account) if default_account else None
    week_count = pl_postage.count_missing_postage_last_n_days(default_account, days=7) if default_account else 0
    total_missing = pl_postage.count_missing_orders(default_account) if default_account else 0
    return render_template_string(
        POSTAGE_HTML, accounts=accounts, default_account=default_account,
        worklist=worklist, last_value=last_value, week_count=week_count,
        total_missing=total_missing,
    )


@app.route("/pl/postage/save", methods=["POST"])
def pl_postage_save():
    account_id = request.form.get("account_id")
    order_ids = request.form.getlist("order_ids")
    amount_raw = (request.form.get("amount") or "").strip()
    return_qs = request.form.get("return_qs", "")
    return_url = "/pl/postage" + (("?" + return_qs) if return_qs else "")
    if not account_id:
        flash("No account specified.")
        return redirect(return_url)
    try:
        amount = float(amount_raw)
    except ValueError:
        flash(f"'{amount_raw}' is not a valid number.")
        return redirect(return_url)
    if not order_ids:
        flash("No order(s) selected — tick at least one row first.")
        return redirect(return_url)
    n = pl_postage.bulk_set_manual_postage(account_id, order_ids, amount)
    # Recompute ONLY the edited orders (not all ~34k rows) so the manual postage
    # is applied — postage_source flips to 'manual' and they drop off this worklist.
    try:
        reprocessed = pl_tracker.reprocess_orders(account_id, order_ids)
    except Exception as e:
        app.logger.warning(f"postage reprocess failed: {e}")
        reprocessed = None
    flash(f"Set £{amount:.2f} postage for {n} order(s), now postage_source='manual'."
          + (f" Recomputed {reprocessed} line item(s)." if reprocessed is not None else ""))
    return redirect(return_url)


@app.route("/pl/postage/apply-all", methods=["POST"])
def pl_postage_apply_all():
    """Bulk-fill EVERY remaining missing-postage order for the account with one
    value — clears the historical backlog without ticking 500 rows at a time.
    Processed in batches so the reprocess can't time out on the remote DB; the
    flash tells you how many remain so you can click again for the rest."""
    account_id = request.form.get("account_id")
    amount_raw = (request.form.get("amount") or "").strip()
    return_url = "/pl/postage" + (f"?account={account_id}" if account_id else "")
    if not account_id:
        flash("No account specified.")
        return redirect(return_url)
    try:
        amount = float(amount_raw)
    except ValueError:
        flash(f"'{amount_raw}' is not a valid number.")
        return redirect(return_url)
    # Process 500-order batches in a loop until either nothing's left or we hit a
    # soft time budget (kept well under gunicorn --timeout). With idx_pl_raw_key
    # each batch is fast, so one click clears thousands; if a big backlog can't
    # finish in one request, the flash says how many remain — just click again.
    deadline = time.time() + 150
    total_set, total_reproc = 0, 0
    while time.time() < deadline:
        order_ids = pl_postage.get_missing_order_ids(account_id, limit=500)
        if not order_ids:
            break
        pl_postage.bulk_set_manual_postage(account_id, order_ids, amount)
        try:
            total_reproc += (pl_tracker.reprocess_orders(account_id, order_ids) or 0)
        except Exception as e:
            app.logger.warning(f"apply-all reprocess failed: {e}")
            break
        total_set += len(order_ids)
    remaining = pl_postage.count_missing_orders(account_id)
    if not total_set:
        flash("Nothing to fill — no orders are missing postage for this account.")
    else:
        flash(f"Set £{amount:.2f} for {total_set} order(s), recomputed {total_reproc} line item(s). "
              + (f"{remaining} still missing — click 'Apply to all remaining' again to continue."
                 if remaining else "All caught up — none left missing."))
    return redirect(return_url)


# ─────────────────────────────────────────────────────────────────────────────
# PER-SKU DETAIL PAGE — module2_sku_detail. "Everything about this one
# product" -- profit/loss, editable COGS/price, ad spend, cost breakdown,
# order list, identity graph, and a BSR placeholder. Read-only toward
# Amazon everywhere on this page; COGS writes go through the same
# family-price path used everywhere else; price edits are local-only
# (pl_price.py) and never call Amazon.
# ─────────────────────────────────────────────────────────────────────────────

SKU_DETAIL_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ canonical_sku }} — Product detail — BSR Repricer</title>
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c}
 /* module2_dashboard_fixes D3: dark-teal link styling, replacing default
    browser blue/underline -- more specific selectors below (.header a,
    etc.) intentionally still override this base rule. */
 a{color:#0e5c5b;text-decoration:none;font-weight:600}
 a:hover{text-decoration:underline}
 a:visited{color:#0e5c5b}
 .header{background:#0e5c5b;color:#eafcfb;padding:18px 30px;display:flex;justify-content:space-between;align-items:center}
 .header a{color:#bfe9e7;font-size:13px;text-decoration:none;margin-left:16px}
 .wrap{max-width:1200px;margin:24px auto;padding:0 20px}
 .toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
 select,input[type=number],input[type=text]{padding:8px 12px;border:1px solid #dde3e9;border-radius:8px;font-size:13px;background:#fff}
 .card{background:#fff;border-radius:12px;padding:20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
 .title{font-size:15px;font-weight:700;margin-bottom:12px}
 .subtitle{font-size:12.5px;color:#5a6472;margin-bottom:14px;line-height:1.5}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:8px 10px;text-align:left;border-top:1px solid #eef1f4}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase;letter-spacing:.3px}
 tr:hover td{background:#f7fbfb}
 .pos{color:#166b3d;font-weight:600}
 .neg{color:#9e2d3c;font-weight:600}
 .muted{color:#8a94a2}
 .pill{display:inline-block;background:#fff;border:1px solid #dde3e9;border-radius:20px;padding:6px 14px;font-size:13px}
 .pill.warn{border-color:#f0d38a;background:#fbf1dd;color:#8a5906}
 .badge{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:10px}
 .badge.ok{background:#e7f6ee;color:#166b3d}
 .badge.no{background:#fbe8eb;color:#9e2d3c}
 .badge.soon{background:#eef4f3;color:#0e5c5b}
 .btn{background:#0e5c5b;color:#eafcfb;border:none;border-radius:6px;padding:6px 12px;font-size:12px;font-weight:600;cursor:pointer}
 .btn-sm{padding:4px 10px;font-size:11.5px}
 .btn[disabled]{background:#c7cdd3;cursor:not-allowed}
 .flash{background:#e7f6ee;color:#166b3d;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:600}
 .hint{font-size:11.5px;color:#8a94a2;margin-top:6px;line-height:1.5}
 .headline{display:flex;gap:36px;align-items:flex-end;flex-wrap:wrap}
 .headline .big{font-size:34px;font-weight:800;line-height:1}
 .headline .label{font-size:11.5px;color:#8a94a2;text-transform:uppercase;letter-spacing:.4px;margin-bottom:4px}
 .stat-row{display:flex;gap:28px;flex-wrap:wrap;margin-top:16px}
 .stat{min-width:100px}
 .stat .n{font-size:18px;font-weight:700}
 .stat .l{font-size:11px;color:#8a94a2;text-transform:uppercase;letter-spacing:.3px}
 .identity-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:18px}
 .id-block .k{font-size:11px;color:#8a94a2;text-transform:uppercase;letter-spacing:.3px;margin-bottom:5px}
 .id-block .v{font-size:13.5px}
 .link-chip{display:inline-block;background:#f3f6f6;border-radius:6px;padding:3px 8px;margin:2px 4px 2px 0;font-size:12px}
 .link-chip .src{color:#8a94a2;font-size:10px;margin-left:4px}
 .cost-line{display:flex;justify-content:space-between;padding:7px 0;border-top:1px solid #eef1f4;font-size:13.5px}
 .cost-line.total{border-top:2px solid #12161c;font-weight:700;margin-top:4px}
 .cost-line .neg-amt{color:#9e2d3c}
 .row-inline{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 .bsr-slot{border:2px dashed #dde3e9;border-radius:10px;padding:24px;text-align:center;color:#8a94a2;font-size:13px}
</style></head><body>
{{ nav|safe }}<div class="header" style="padding-top:10px"><b>{{ canonical_sku }}</b>
  <div><a href="{{ back_url }}">← Back to rollup</a></div>
</div>
<div class="wrap">
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}

  <div class="toolbar">
    <form method="GET" action="/pl/sku/{{ canonical_sku|urlencode }}" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
      <input type="hidden" name="return_qs" value="{{ return_qs }}">
      <select name="account" onchange="this.form.submit()">
        <option value="all" {{ "selected" if account_filter=="all" else "" }}>All accounts</option>
        {% for a in accounts %}
        <option value="{{ a.account_id }}" {{ "selected" if account_filter==a.account_id else "" }}>{{ a.account_id }}</option>
        {% endfor %}
      </select>
      <select name="range" onchange="this.form.submit()">
        <option value="all" {{ "selected" if range_key=="all" else "" }}>All time</option>
        <option value="7" {{ "selected" if range_key=="7" else "" }}>Last 7 days</option>
        <option value="30" {{ "selected" if range_key=="30" else "" }}>Last 30 days</option>
        <option value="90" {{ "selected" if range_key=="90" else "" }}>Last 90 days</option>
        <option value="180" {{ "selected" if range_key=="180" else "" }}>Last 6 months</option>
        <option value="365" {{ "selected" if range_key=="365" else "" }}>Last year</option>
      </select>
      <select name="period" onchange="this.form.submit()">
        <option value="day" {{ "selected" if period=="day" else "" }}>Daily</option>
        <option value="week" {{ "selected" if period=="week" else "" }}>Weekly</option>
        <option value="month" {{ "selected" if period=="month" else "" }}>Monthly</option>
      </select>
      <select name="vat" onchange="this.form.submit()">
        <option value="ex_vat" {{ "selected" if vat_treatment=="ex_vat" else "" }}>Ex-VAT (true profit)</option>
        <option value="cash" {{ "selected" if vat_treatment=="cash" else "" }}>Cash (inc-VAT)</option>
      </select>
      <label style="font-size:12px;color:#5a6b70;display:flex;gap:4px;align-items:center;">Chart:
        <select name="metric" onchange="this.form.submit()">
          {% for m in ["net_profit","margin_pct","revenue","units","orders","ad_spend","tacos"] %}
          <option value="{{ m }}" {{ "selected" if metric==m else "" }}>{{ chart_metric_labels[m] }}</option>
          {% endfor %}
        </select>
      </label>
      {% if sku_range_start and sku_range_end %}
      <span class="muted" style="font-size:12px;">Showing orders from <b>{{ sku_range_start }}</b> to <b>{{ sku_range_end }}</b></span>
      {% elif row.orders == 0 %}
      <span class="muted" style="font-size:12px;">No orders in this window.</span>
      {% endif %}
    </form>
    <span class="pill">Read-only — no SP-API writes</span>
  </div>
  <div class="hint" style="margin:-10px 0 16px;">Note: "Daily/Weekly/Monthly" only controls how the trend chart below
    buckets dates within the selected range above — it is a separate control from the date-range filter itself
    (range narrows which orders are included everywhere on this page; period only changes chart granularity).</div>

  {% if multi_account_note %}<div class="pill warn" style="display:block;margin-bottom:14px;">{{ multi_account_note }}</div>{% endif %}

  <!-- 1. HEADLINE -->
  <div class="card">
    <div class="headline">
      <div>
        <div class="label">Net profit (after ads){% if row.provisional %} <span class="badge no" title="{{ row.provisional_reasons|join(', ') }}">provisional</span>{% endif %}</div>
        <div class="big {{ 'pos' if (row.net_profit_after_ads or 0) >= 0 else 'neg' }}">£{{ "%.2f"|format(row.net_profit_after_ads or 0) }}</div>
      </div>
      <div>
        <div class="label">Margin (after ads)</div>
        <div class="big">{% if margin_pct_after_ads is not none %}{{ "%.1f"|format(margin_pct_after_ads*100) }}%{% else %}—{% endif %}</div>
      </div>
    </div>
    {% if row.provisional %}
    <div class="hint" style="margin-top:10px;color:#8a5906;">⚠ {{ row.provisional_reasons|join(' · ') }} — this figure is real revenue/fees minus only the costs known so far, not a guess with the gaps filled in.</div>
    {% endif %}
    {% if ad_coverage_warning %}
    <div class="hint" style="margin-top:6px;color:#8a5906;">⚠ {% if not ad_coverage_warning.has_data %}No ad spend data uploaded yet for this range{% else %}Ad data only covers {{ ad_coverage_warning.cov_min }} to {{ ad_coverage_warning.cov_max }}, less than the selected range{% endif %} — "after ads" may equal "before ads" here simply because ad cost is absent, not because it's genuinely zero. <a href="/pl/ads">Upload a report</a>.</div>
    {% endif %}
    <div class="stat-row">
      <div class="stat"><div class="n">{{ row.orders or 0 }}</div><div class="l">Orders</div></div>
      <div class="stat"><div class="n">{{ row.units or 0 }}</div><div class="l">Units</div></div>
      <div class="stat"><div class="n">£{{ "%.2f"|format(row.gross_sales_exvat or 0) }}</div><div class="l">Gross sales (ex-VAT)</div></div>
      <div class="stat"><div class="n">£{{ "%.2f"|format(row.net_profit or 0) }}</div><div class="l">Net profit (before ads)</div></div>
    </div>
  </div>

  <!-- 2. TREND CHART — moved up per module2_dashboard_fixes B4: sits
       directly under the headline, ahead of identity/relationships. -->
  <div class="card">
    <div class="title">{{ chart_metric_labels[metric] }} trend</div>
    {{ trend_chart_html|safe }}
  </div>

  <!-- 3. IDENTITY & RELATIONSHIPS -->
  <div class="card">
    <div class="title">Identity &amp; relationships</div>
    <div class="identity-grid">
      <div class="id-block">
        <div class="k">Canonical SKU</div><div class="v">{{ identity.canonical_sku }}</div>
      </div>
      <div class="id-block">
        <div class="k">Family / type / basis</div>
        <div class="v">{{ identity.family or "—" }} · {{ identity.product_type or "—" }} · {{ identity.price_basis or "—" }}{% if identity.pack_qty %} · pack of {{ identity.pack_qty }}{% endif %}</div>
      </div>
      <div class="id-block">
        <div class="k">VAT</div>
        <div class="v">{% if identity.vat_rate is not none %}{{ (identity.vat_rate * 100)|round|int }}%{% else %}<span class="muted">not set</span>{% endif %}
          <span class="muted" style="font-weight:400;">(figures on this page remain ex-VAT — this reports status, not the maths)</span></div>
      </div>
      <div class="id-block">
        <div class="k">Account</div><div class="v">{{ row.account_id or "—" }}</div>
      </div>
      <div class="id-block">
        <div class="k">Title(s)</div>
        <div class="v">{% if titles %}{{ titles|join(' / ') }}{% else %}<span class="muted">(not held in Module 1)</span>{% endif %}</div>
      </div>
      <div class="id-block" style="grid-column:1/-1;">
        <div class="k">Linked / family-member SKUs ({{ identity.member_skus|length }})</div>
        <div class="v">
          {% for m in identity.member_skus %}
          <span class="link-chip">{{ m.sku }}<span class="src">{{ m.source }}</span></span>
          {% endfor %}
        </div>
      </div>
      <div class="id-block" style="grid-column:1/-1;">
        <div class="k">Linked ASINs ({{ identity.asins|length }})</div>
        <div class="v">
          {% if identity.asins %}
          {% for a in identity.asins %}
          <span class="link-chip">{{ a.asin }}<span class="src">via {{ a.via_sku }}, {{ a.source }}</span></span>
          {% endfor %}
          {% else %}<span class="muted">No ASIN observed for this SKU yet.</span>{% endif %}
        </div>
      </div>
      {% if identity.variation_siblings %}
      <div class="id-block" style="grid-column:1/-1;">
        <div class="k">Variation siblings in this family ({{ identity.variation_siblings|length }})</div>
        <div class="v">
          {% for s in identity.variation_siblings %}
          <a class="link-chip" href="/pl/sku/{{ s.canonical_sku|urlencode }}?account={{ account_filter }}&amp;range={{ range_key }}&amp;vat={{ vat_treatment }}&amp;period={{ period }}&amp;return_qs={{ return_qs|urlencode if return_qs else '' }}">{{ s.canonical_sku }} (pack {{ s.pack_qty }}, {{ s.price_basis }})</a>
          {% endfor %}
        </div>
      </div>
      {% endif %}
    </div>
    <div class="hint" style="margin-top:12px;">"Source" on each chip shows HOW that SKU/ASIN got linked here — <b>native</b> (this is the canonical itself), <b>seed_csv/manual/csv_upload</b> (alias table), or <b>asin_auto</b> (grouped automatically because it shares an ASIN with this canonical) — so a wrong merge is easy to spot and fix on <a href="/pl/cogs">COGS &amp; pricing</a>.</div>
  </div>

  <!-- 4. EDITABLE STATUS -->
  <div class="card">
    <div class="title">Editable status</div>
    <div class="identity-grid">
      <div class="id-block">
        <div class="k">COGS / pricing family status</div>
        <div class="v">
          {% if row.priced %}
          <span class="badge ok">priced</span>
          £{{ "%.2f"|format(family_pack_cogs or 0) }} / pack
          {% if identity.pack_qty and identity.pack_qty > 1 %}<span class="muted" style="font-weight:400;">(£{{ "%.2f"|format(identity.family_price or 0) }} / {{ price_basis_unit }} × {% if price_basis_unit == 'pair' %}{{ pack_basis_units }} pair{{ 's' if pack_basis_units > 1 else '' }} in a pack of {{ identity.pack_qty }}{% else %}pack of {{ identity.pack_qty }}{% endif %})</span>{% endif %}
          {% else %}<span class="badge no">no price</span>{% endif %}
        </div>
        <form method="POST" action="/pl/inline-cogs" style="margin-top:8px;display:inline-flex;gap:4px;align-items:center;" onsubmit="return plConfirmCogsEdit(this)">
          <input type="hidden" name="family" value="{{ identity.family }}">
          <input type="hidden" name="return_url" value="{{ self_url }}">
          <input type="number" step="0.01" name="price" placeholder="£0.00 / {{ price_basis_unit }}"
                 value="{{ '%.2f'|format(identity.family_price) if identity.family_price is not none else '' }}"
                 style="width:110px;" required>
          <button class="btn btn-sm" type="submit">Save family price</button>
        </form>
        <div class="hint">Writes to the FAMILY price (same as <a href="/pl/cogs">COGS &amp; pricing</a>) — affects every SKU in <b>{{ identity.family }}</b>. The box takes the PER-{{ price_basis_unit|upper }} rate{% if price_basis_unit == 'pair' %} (this family is priced per pair — a pack of {{ identity.pack_qty }} is {{ pack_basis_units }} pair{{ 's' if pack_basis_units > 1 else '' }}){% endif %}; the pack/ASIN price above is the real cost of the box you buy.</div>

        <div class="k" style="margin-top:14px;">VAT rate</div>
        <div class="v">
          {% if identity.vat_rate is not none %}{{ (identity.vat_rate * 100)|round|int }}%{% else %}<span class="muted">not set</span>{% endif %}
          {% if tax_code_info %}
            {% if tax_code_info.available and tax_code_info.product_tax_code %}
            <span class="muted" style="font-weight:400;">— Amazon reports <b>{{ tax_code_info.product_tax_code }}</b></span>
            {% else %}
            <span class="muted" style="font-weight:400;">— Amazon tax code: not available</span>
            {% endif %}
          {% endif %}
        </div>
        {% if vat_mismatch %}
        <div class="pill warn" style="display:block;margin-top:6px;">⚠ Mismatch: you have this recorded at {{ (vat_mismatch.seller_rate*100)|round|int }}%,
          but Amazon's product_tax_code ({{ vat_mismatch.amazon_code }}) implies {{ (vat_mismatch.amazon_implied_rate*100)|round|int }}%.
          Your recorded rate has NOT been changed — review and update it yourself if Amazon is right.</div>
        {% endif %}
        <form method="POST" action="/pl/inline-vat" style="margin-top:8px;display:inline-flex;gap:4px;align-items:center;">
          <input type="hidden" name="family" value="{{ identity.family }}">
          <input type="hidden" name="return_url" value="{{ self_url }}">
          <select name="vat_rate" style="padding:6px 8px;font-size:12.5px;">
            <option value="0.20" {{ "selected" if identity.vat_rate==0.20 else "" }}>20% standard</option>
            <option value="0.05" {{ "selected" if identity.vat_rate==0.05 else "" }}>5% reduced</option>
            <option value="0.0" {{ "selected" if identity.vat_rate==0.0 else "" }}>0% zero-rated</option>
          </select>
          <button class="btn btn-sm" type="submit">Save VAT rate</button>
        </form>
        <div class="hint">Metadata + cross-check only — the ex-VAT profit figures on this page are already
          anchored on Amazon's own balance change and never divide by a VAT factor; this setting does not
          change them. <a href="{{ self_url }}{{ '&' if '?' in self_url else '?' }}refresh_tax=1">Refresh Amazon's tax code</a></div>
      </div>
      <div class="id-block">
        <div class="k">Recorded selling price — P&amp;L record only</div>
        <div class="v">{% if recorded_price is not none %}£{{ "%.2f"|format(recorded_price) }}{% else %}<span class="muted">not recorded</span>{% endif %}</div>
        <form method="POST" action="/pl/sku/{{ canonical_sku|urlencode }}/price" style="margin-top:8px;display:inline-flex;gap:4px;align-items:center;"
              onsubmit="return confirm('Record this as the selling price for P&amp;L purposes? This does NOT change your live Amazon listing.')">
          <input type="hidden" name="account_id" value="{{ price_account or '' }}">
          <input type="hidden" name="return_url" value="{{ self_url }}">
          <input type="number" step="0.01" name="price" placeholder="£0.00"
                 value="{{ '%.2f'|format(recorded_price) if recorded_price is not none else '' }}"
                 style="width:90px;" required {{ 'disabled' if not price_account else '' }}>
          <button class="btn btn-sm" type="submit" {{ 'disabled' if not price_account else '' }}>Save</button>
          <button class="btn btn-sm" type="button" disabled title="Not built yet — Option B, deferred. Would add SP-API write scope, a price floor/ceiling, a 5%/day change cap, and an idempotency lock. Seller-click-only, never automatic.">Push to Amazon (not yet)</button>
        </form>
        <div class="hint"><b>Records price for P&amp;L — does not change your Amazon listing.</b> Live price changes still happen in Seller Central. Every edit is logged below.</div>
        {% if projected_profit_per_unit is not none %}
        <div class="hint" style="margin-top:6px;">Projected at this price: <b class="{{ 'pos' if projected_profit_per_unit >= 0 else 'neg' }}">£{{ "%.2f"|format(projected_profit_per_unit) }}/unit{% if projected_margin_pct is not none %} ({{ "%.1f"|format(projected_margin_pct*100) }}% margin){% endif %}</b> — using this range's average referral/other fees, COGS, postage and ad spend per unit. This is a what-if projection; it does not rewrite any historical order's real sale price.</div>
        {% endif %}

        <div class="k" style="margin-top:14px;">Live Amazon listing price</div>
        <div class="v">
          {% if live_price_info and live_price_info.available and live_price_info.price is not none %}
          £{{ "%.2f"|format(live_price_info.price) }}
          {% if recorded_price is not none %}
            {% set drift = live_price_info.price - recorded_price %}
            {% if drift|abs > 0.01 %}<span class="pill warn" style="padding:2px 8px;font-size:11px;">{{ "+" if drift > 0 else "" }}£{{ "%.2f"|format(drift) }} vs recorded</span>{% endif %}
          {% endif %}
          {% else %}<span class="muted">not available</span>{% endif %}
        </div>
        <div class="hint">Read-only, fetched from SP-API Product Pricing (same call Module 1 uses) — never written anywhere.
          <a href="{{ self_url }}{{ '&' if '?' in self_url else '?' }}refresh_price=1">Refresh live price</a></div>

        <div class="k" style="margin-top:14px;">Average selling price — realised revenue per unit (this range)</div>
        <div class="v">
          {% if asp_exvat is not none %}
            £{{ "%.2f"|format(asp_exvat) }} <span class="muted" style="font-weight:400;">ex-VAT</span>
            {% if asp_incvat is not none %}
              &nbsp;·&nbsp; £{{ "%.2f"|format(asp_incvat) }} <span class="muted" style="font-weight:400;">inc-VAT @ {{ "%.0f"|format(asp_vat_rate*100) }}%</span>
            {% else %}
              &nbsp;·&nbsp; <span class="muted">inc-VAT unavailable — no VAT rate set for this product</span>
            {% endif %}
          {% else %}<span class="muted">no units sold in this range</span>{% endif %}
        </div>

        <div class="k" style="margin-top:14px;">Break-even &amp; target price — layered (inc-VAT, per unit)</div>
        <div class="v">
          {% if row.be_allin is not none %}
          <table style="font-size:13px;border-collapse:collapse;">
            <tr><td style="padding:2px 16px 2px 0;">Direct break-even</td><td>£{{ "%.2f"|format(row.be_direct) }} <span class="muted" style="font-weight:400;">COGS + Amazon fees + label + refund</span></td></tr>
            <tr><td style="padding:2px 16px 2px 0;">+ Ad spend</td><td>£{{ "%.2f"|format(row.be_ads) }} <span class="muted" style="font-weight:400;">+ ad-cost per unit</span></td></tr>
            <tr><td style="padding:2px 16px 2px 0;">All-in break-even</td><td class="{{ 'neg' if row.below_breakeven else '' }}">£{{ "%.2f"|format(row.be_allin) }} <span class="muted" style="font-weight:400;">+ overhead £{{ "%.2f"|format(row.overhead_pu or 0) }}/unit (allocated by revenue)</span></td></tr>
            <tr><td style="padding:2px 16px 2px 0;"><b>Target price</b></td><td><b>£{{ "%.2f"|format(row.target_price) }}</b> <span class="muted" style="font-weight:400;">= all-in ÷ 0.90 (10% net margin)</span></td></tr>
          </table>
          {% else %}<span class="muted">no units sold in this range</span>{% endif %}
        </div>
        <div class="hint">
          {% if row.is_push %}<span class="badge" style="background:#fbe7c6;color:#8a5906;">push mode</span> below break-even is expected (rank-buying) — information, not an alarm.{% elif row.below_breakeven %}<span class="neg"><b>Below all-in break-even</b></span> — selling under true cost.{% endif %}
          A <b>live</b> number — moves with ad spend and volume (that's correct). Target is a 10% <b>net</b> margin on the sale price, not a markup.
          {% if breakeven_provisional %}<br><span style="color:#8a5906;">⚠ Provisional — no refund data captured for this range yet (refund backfill pending), so the refund layer is understated. Clears once refunds arrive.</span>{% endif %}
          <form method="POST" action="/pl/sku/{{ canonical_sku|urlencode }}/push" style="margin-top:8px;">
            <input type="hidden" name="account_id" value="{{ row.account_id or account_filter }}">
            <input type="hidden" name="return_url" value="{{ self_url }}">
            <input type="hidden" name="push" value="{{ '0' if row.is_push else '1' }}">
            <button class="btn btn-sm" type="submit">{{ 'Unset push mode' if row.is_push else 'Mark as push (rank-buying)' }}</button>
          </form>
        </div>
        <div class="hint">Average <b>revenue</b> per unit actually realised over the selected range (gross sales ex-VAT ÷ units).
          This <b>includes buyer-paid shipping</b>, so it is <b>not</b> a listing price — expect it to differ from both
          the "Live Amazon listing price" and "Recorded selling price" above. inc-VAT multiplies this product's stored
          VAT rate (going ex→inc is legitimate; it does not divide). Shown even when margin is provisional, since it is
          revenue-side only.</div>

        {% if price_changelog %}
        <table style="margin-top:8px;">
          <tr><th>When</th><th>Old</th><th>New</th></tr>
          {% for c in price_changelog %}
          <tr>
            <td class="muted">{{ c.changed_at[:19] }}</td>
            <td>{% if c.old_price_exvat is not none %}£{{ "%.2f"|format(c.old_price_exvat) }}{% else %}—{% endif %}</td>
            <td>£{{ "%.2f"|format(c.new_price_exvat) }}</td>
          </tr>
          {% endfor %}
        </table>
        {% endif %}
      </div>
    </div>
  </div>

  <!-- 5. COST BREAKDOWN -->
  <div class="card">
    <div class="title">Cost breakdown ({{ "ex-VAT" if vat_treatment=="ex_vat" else "cash (inc-VAT)" }}, this range)</div>
    <div class="cost-line"><span>Gross sales (ex-VAT)</span><span>£{{ "%.2f"|format(row.gross_sales_exvat or 0) }}</span></div>
    <div class="cost-line"><span>− Referral fees</span><span class="neg-amt">−£{{ "%.2f"|format(row.referral_fees or 0) }}</span></div>
    <div class="cost-line"><span>− Other fees</span><span class="neg-amt">−£{{ "%.2f"|format(row.other_fees or 0) }}</span></div>
    <div class="cost-line"><span>− Promotions</span><span class="neg-amt">−£{{ "%.2f"|format(row.promotions or 0) }}</span></div>
    <div class="cost-line"><span>− COGS</span><span class="neg-amt">−£{{ "%.2f"|format(row.cogs or 0) }}</span></div>
    <div class="cost-line"><span>− Postage</span><span class="neg-amt">−£{{ "%.2f"|format(row.postage or 0) }}</span></div>
    <div class="cost-line total"><span>= Net profit (before ads)</span><span>£{{ "%.2f"|format(row.net_profit or 0) }}</span></div>
    <div class="cost-line"><span>− Ad spend</span><span class="neg-amt">−£{{ "%.2f"|format(row.ad_spend or 0) }}</span></div>
    <div class="cost-line total"><span>= Net profit (after ads)</span><span>£{{ "%.2f"|format(row.net_profit_after_ads or 0) }}</span></div>
    <div class="hint" style="margin-top:8px;">Promotions/other fees are already netted into "Net profit (before ads)" via Amazon's own balance-change anchor — this breakdown itemises the SAME total, it doesn't recompute it separately, so the lines above always sum to the totals shown.</div>
  </div>

  <!-- 6. ORDER LIST -->
  <div class="card">
    <div class="title">Orders in range ({{ orders|length }})</div>
    {% if orders %}
    <table>
      <tr><th>Date</th><th>Order</th><th>Qty</th><th>Sale (ex-VAT)</th><th>Postage</th><th>Net profit</th><th></th></tr>
      {% for o in orders %}
      <tr>
        <td class="muted">{{ (o.posted_date or "")[:10] }}</td>
        <td>{{ o.order_id }}</td>
        <td>{{ o.quantity }}</td>
        <td>£{{ "%.2f"|format(o.sale_price_exvat or 0) }}</td>
        <td>
          {% if o.postage_source == 'exact' %}<span class="badge ok">exact</span> £{{ "%.2f"|format(o.postage or 0) }}
          {% elif o.postage_source == 'manual' %}<span class="badge ok">manual</span> £{{ "%.2f"|format(o.postage or 0) }}
          {% else %}<span class="badge no">{{ o.postage_source or 'missing' }}</span>
          <form method="POST" action="/pl/sku/{{ canonical_sku|urlencode }}/postage" style="display:inline-flex;gap:4px;align-items:center;margin-left:4px;">
            <input type="hidden" name="account_id" value="{{ o.account_id }}">
            <input type="hidden" name="order_id" value="{{ o.order_id }}">
            <input type="hidden" name="return_url" value="{{ self_url }}">
            <input type="number" step="0.01" name="amount" placeholder="£0.00" required style="width:70px;">
            <button class="btn btn-sm" type="submit">Save</button>
          </form>
          {% endif %}
        </td>
        <td class="{{ 'pos' if (o.net_profit_view or 0) >= 0 else 'neg' }}">£{{ "%.2f"|format(o.net_profit_view or 0) }}{% if not o.cogs_priced %} <span class="badge no" title="Missing COGS">prov.</span>{% endif %}</td>
        <td class="muted" style="font-size:11px;">{{ o.settlement_status }}</td>
      </tr>
      {% endfor %}
    </table>
    {% else %}
    <div class="muted">No orders for this SKU in the selected range.</div>
    {% endif %}
  </div>

  <!-- 7. AD SPEND -->
  <div class="card">
    <div class="title">Ad spend (this range)</div>
    <div class="stat-row">
      <div class="stat"><div class="n">£{{ "%.2f"|format(row.ad_spend or 0) }}</div><div class="l">Ad spend</div></div>
      <div class="stat"><div class="n">£{{ "%.2f"|format(row.ad_sales_promoted or 0) }}</div><div class="l">Ad sales — own (promoted)</div></div>
      <div class="stat"><div class="n">£{{ "%.2f"|format(row.ad_sales_halo or 0) }}</div><div class="l">Ad sales — halo (other ASINs)</div></div>
      <div class="stat"><div class="n">£{{ "%.2f"|format(row.ad_sales or 0) }}</div><div class="l">Ad sales — total</div></div>
      <div class="stat"><div class="n">{{ row.ad_clicks or 0 }}</div><div class="l">Clicks</div></div>
      <div class="stat" title="Click-attributed ad sales ÷ settled gross sales — two clocks. Expect small drift when you re-pull the ad report."><div class="n">{% if row.tacos is not none %}{{ "%.1f"|format(row.tacos*100) }}%{% elif row.ad_spend %}∞{% else %}—{% endif %}</div><div class="l">TACOS<span class="muted" style="font-weight:400;"> — two clocks</span></div></div>
    </div>
    <div class="hint" style="margin-top:8px;"><b>own (promoted)</b> = sales of THIS ASIN driven by its ad; <b>halo</b> = sales of OTHER ASINs (e.g. sibling colours) driven by the same ad. A click on one colour that buys another shows here as halo — so an ad that looks like a loss on its own sales can still be paying off across the family (see the parent-ASIN view). Joined by this SKU's ASIN(s) from <a href="/pl/ads">Ad spend</a>.</div>
    <div class="hint" style="margin-top:6px;"><b>TACOS — two clocks.</b> It's ad spend ÷ <b>total</b> gross sales (not just attributed sales), but the two sides are measured differently: <b>ad sales are click-date attributed</b> — a click today that converts in 7–14 days is credited back to today, so the same window reports higher ad sales when you re-pull the report as late conversions land; <b>gross sales are settlement-anchored</b> — an order counts when Amazon settles it, not when the click happened. Neither is wrong; they answer different questions. Expect a little drift on re-pull — that's the two clocks, not an error, and it is deliberately not "reconciled".</div>
  </div>

  <!-- 8. BSR PANEL — SLOT NOW, WIRE LATER -->
  <div class="card">
    <div class="title">BSR trend <span class="badge soon">connects when Module 2 goes online</span></div>
    <div class="bsr-slot">
      Best-Seller-Rank trend (daily, info only) will render here once Module 2's cross-system read from
      Module 1 (<code>bsr-collector</code>, hosted separately on Railway/Postgres) is wired up — deferred
      until Module 2 goes online. No connection is attempted from this page yet. This panel is designed to
      accept a daily BSR series keyed by ASIN once that's ready, so it drops in without restructuring.
    </div>
  </div>

  <script>
    var PL_FAMILY_SKU_COUNTS = {{ family_sku_counts|tojson }};
    function plConfirmCogsEdit(form){
      var family = form.family.value;
      var n = PL_FAMILY_SKU_COUNTS[family] || 1;
      return confirm("Setting price for family " + family + " — affects " + n + " SKU" + (n === 1 ? "" : "s") + ". Continue?");
    }
  </script>
</div></body></html>
"""


@app.route("/pl/sku/<path:canonical_sku>")
def pl_sku_detail(canonical_sku):
    accounts = get_accounts()
    account_filter = request.args.get("account", "all")
    vat_treatment = request.args.get("vat", _default_vat_treatment())
    if vat_treatment not in ("cash", "ex_vat"):
        vat_treatment = "ex_vat"
    range_key = request.args.get("range", "all")
    start_date = _range_start_date(range_key)
    period_explicit = "period" in request.args
    period = request.args.get("period", "day")
    if period not in ("day", "week", "month"):
        period = "day"
    # module2_dashboard_fixes D1: same legible-default bucketing as /pl.
    period = _resolve_chart_period(range_key, period, period_explicit)
    # module2_dashboard_fixes D1: which variable the trend chart plots.
    metric = request.args.get("metric", "net_profit")
    if metric not in _CHART_METRICS:
        metric = "net_profit"
    return_qs = request.args.get("return_qs", "")
    back_url = "/pl" + (("?" + return_qs) if return_qs else "")
    current_qs = request.query_string.decode()
    # self_url is used as the return target for every write form on this
    # page (COGS/price/postage edits) -- deliberately strips the one-shot
    # refresh_tax/refresh_price flags so clicking "Refresh from Amazon"
    # doesn't get baked into every subsequent save-and-return-here redirect,
    # which would otherwise force a fresh Amazon call on every future visit.
    _self_qs_parts = [p for p in current_qs.split("&") if p and not p.startswith(("refresh_tax=", "refresh_price="))]
    self_url = f"/pl/sku/{canonical_sku}" + (("?" + "&".join(_self_qs_parts)) if _self_qs_parts else "")
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    # Identity & relationships -- works even with zero orders in range, since
    # it's derived from cogs_canonical/cogs_aliases/cogs_sku_asin, not from
    # pl_line_items.
    identity = pl_cogs.get_sku_identity(canonical_sku)

    # module2_cogs_display_fix: the "pack" (= the box you actually buy) cost is
    # the family price times the SAME multiplier the stored-COGS pipeline uses
    # (pl_cogs.cogs_multiplier) -- NOT family_price x pack_qty. For a
    # pair-priced pillow the family price is PER PAIR, so a pack of 4 = 2 pairs
    # (x pack_qty/2); the old template did family_price x pack_qty, which
    # showed pair COGS at 2x on this page (a pack-of-4 at a £2.02 pair price
    # rendered £8.08 instead of the correct £4.04). Stored line-item COGS was
    # always correct -- this was display-only -- so nothing about the real P&L
    # or the loss-flip counts changes; only this box did. Computed once here
    # via the one canonical multiplier so the display can never drift from the
    # stored maths again. price_basis drives per-unit vs per-pair labelling.
    _fp = identity.get("family_price")
    _mult = pl_cogs.cogs_multiplier(identity.get("product_type"),
                                    identity.get("price_basis"),
                                    identity.get("pack_qty") or 1)
    family_pack_cogs = (_fp * _mult) if _fp is not None else None
    price_basis_unit = "pair" if identity.get("price_basis") == "pair" else "unit"
    # number of price-basis units in one pack (pairs for pair-priced pillows,
    # else the pack quantity) -- used only for the "(£x / unit x pack of n)"
    # explanatory label so it reads truthfully for pairs.
    pack_basis_units = int(_mult) if identity.get("price_basis") == "pair" else (identity.get("pack_qty") or 1)

    # Headline: reuse the EXACT same rollup code path /pl uses (get_canonical_
    # rollup + attach_ad_spend_to_rollup), scoped to this one product, so the
    # numbers here can never drift from what /pl shows for the same row.
    rows = pl_db.get_canonical_rollup(account_filter, vat_treatment=vat_treatment,
                                       start_date=start_date, canonical_sku=canonical_sku)
    multi_account_note = None
    if rows:
        rows, _orphans = pl_ads.attach_ad_spend_to_rollup(
            rows, account_id=account_filter, start_date=start_date, end_date=None)
        if len(rows) > 1:
            # Same canonical SKU sold under >1 account in this window (only
            # possible when account_filter=="all", since /pl's own rows are
            # already split by account) -- don't silently merge two accounts'
            # numbers into one misleading figure; show the first and say so.
            multi_account_note = (
                f"This SKU has real orders under {len(rows)} different accounts in this range — "
                f"showing {rows[0]['account_id']}. Use the account selector above to see the others."
            )
        row = rows[0]
    else:
        row = {
            "canonical_sku": canonical_sku,
            "account_id": account_filter if account_filter != "all" else (accounts[0]["account_id"] if accounts else None),
            "orders": 0, "units": 0, "gross_sales_exvat": 0, "gross_sales_incvat": 0,
            "referral_fees": 0, "other_fees": 0, "promotions": 0, "cogs": 0, "postage": 0,
            "net_profit": 0, "margin_pct": None, "true_profit": None,
            "ad_spend": 0, "ad_sales": 0, "ad_sales_promoted": 0, "ad_sales_halo": 0,
            "ad_clicks": 0, "ad_orders": 0,
            "net_profit_after_ads": 0, "tacos": None,
            "pending_count": 0, "postage_exact_count": 0, "postage_manual_count": 0,
            "postage_missing_count": 0, "postage_estimated_count": 0, "unpriced_count": 0,
            "provisional": not identity["family_priced"],
            "provisional_reasons": [] if identity["family_priced"] else ["no COGS price on this family yet"],
        }
    row["priced"] = identity["family_priced"]
    row["family"] = identity["family"]
    row["product_type"] = identity["product_type"]

    # module2_breakeven: the same layered break-even /pl shows, for this one SKU.
    # Overhead is allocated by ACCOUNT-WIDE revenue (not just this SKU) so it matches /pl.
    _acct_be = row.get("account_id") or account_filter
    try:
        import pl_expenses as _plx
        _oh_range = pl_db.resolve_pl_date_range(_acct_be, start_date=start_date)
        _oh_total = _plx.compute_overheads(_acct_be, _oh_range[0], _oh_range[1]).get("total", 0.0) if _oh_range[0] else 0.0
        pl_db.attach_breakeven(
            [row], overheads_total=_oh_total,
            refunded_units=pl_db.get_refunded_units_by_canonical(_acct_be, start_date=start_date),
            push_set=pl_db.get_push_canonicals(_acct_be),
            total_revenue=pl_db.get_total_revenue(_acct_be, start_date))
        breakeven_provisional = (pl_db.get_total_units(_acct_be, start_date) or 0) and \
            not sum(pl_db.get_refunded_units_by_canonical(_acct_be, start_date=start_date).values())
    except Exception:
        breakeven_provisional = True

    gross = row.get("gross_sales_exvat") or 0
    after_ads = row.get("net_profit_after_ads")
    margin_pct_after_ads = (after_ads / gross) if (after_ads is not None and gross) else None

    ad_coverage_warning = pl_ads.get_coverage_warning(
        account_id=account_filter, viewed_start=start_date, viewed_end=today_str)

    # Title(s) -- module2_pl_live_fixes: dropped the managed_asins fallback
    # (see the matching fix in pl_page() for the full diagnosis -- it only
    # ever covers 2-3 ASINs and was the cause of the "title shows on some
    # rows, not others" bug on /pl). pl_asin_titles is now the only source
    # here too, for the same consistency reason. Never fabricated -- blank
    # if it has no title for this ASIN yet.
    asin_list = [a["asin"] for a in identity["asins"]]
    title_by_asin = {k: v for k, v in pl_amazon.get_titles_map(asin_list).items() if v}
    titles = sorted({title_by_asin[a["asin"]] for a in identity["asins"] if title_by_asin.get(a["asin"])})

    # module2_dashboard_fixes A2/B3: Amazon's own product_tax_code (cross-
    # check only, never overwrites the seller's recorded VAT rate) and the
    # live SP-API selling price, for this SKU's primary ASIN -- lazy,
    # cached (see pl_amazon.py freshness windows), read-only. Best-effort:
    # a fetch failure just shows "not available", never blocks the page.
    primary_asin = asin_list[0] if asin_list else None
    tax_code_info = None
    live_price_info = None
    if primary_asin:
        sku_hint = identity["asins"][0]["via_sku"] if identity["asins"] else canonical_sku
        fetch_account = None
        for a in accounts:
            if a["account_id"] == (row.get("account_id") or account_filter):
                fetch_account = a
                break
        if fetch_account is None and accounts:
            fetch_account = accounts[0]
        force_tax = request.args.get("refresh_tax") == "1"
        force_price = request.args.get("refresh_price") == "1"
        try:
            tax_code_info = pl_amazon.fetch_tax_code(primary_asin, sku_hint, fetch_account, force=force_tax)
        except Exception as e:
            app.logger.warning(f"tax code fetch failed for {primary_asin}: {e}")
            tax_code_info = {"product_tax_code": None, "available": 0}
        try:
            live_price_info = pl_amazon.fetch_live_price_cached(primary_asin, fetch_account, force=force_price)
        except Exception as e:
            app.logger.warning(f"live price fetch failed for {primary_asin}: {e}")
            live_price_info = {"price": None, "available": 0}

    # Profit/margin trend -- per-SKU period rollup, same day/week/month
    # bucketing control as /pl, same account+range filter as the rest of
    # this page. module2_dashboard_fixes D1: selectable metric, same
    # machinery as /pl's chart1.
    trend_rows = pl_db.get_sku_period_rollup(canonical_sku, account_id=account_filter, period=period,
                                              vat_treatment=vat_treatment, start_date=start_date)
    trend_ad_period_map = None
    if metric in ("ad_spend", "tacos"):
        trend_ad_period_map = pl_ads.get_ad_spend_period_series(
            account_filter, period=period, start_date=start_date, end_date=None, asins=asin_list or None)
    tx = [r["period"] for r in trend_rows]
    ty = _metric_series(trend_rows, metric, trend_ad_period_map)
    trend_metric_label = _CHART_METRIC_LABELS[metric]
    trend_y_prefix, trend_y_suffix = _CHART_METRIC_FMT[metric]
    fig = go.Figure(go.Scatter(
        x=tx, y=ty, mode="lines+markers", name=trend_metric_label,
        line=dict(color="#0e5c5b", width=2), marker=dict(size=6),
        hovertemplate="%{x}<br>" + trend_y_prefix + "%{y:,.2f}" + trend_y_suffix + "<extra></extra>"))
    fig.update_layout(title=f"{trend_metric_label} over time ({period})", yaxis_title=trend_metric_label,
                       margin=dict(t=40, l=50, r=20, b=40),
                       height=300, paper_bgcolor="white", plot_bgcolor="white")
    trend_chart_html = pyo.plot(fig, output_type="div", include_plotlyjs="cdn", config={"displayModeBar": False})

    # Order list -- every real order line for this SKU in range, newest
    # first; missing/estimated postage editable in context (same write as
    # /pl/postage).
    orders = pl_db.get_sku_order_list(canonical_sku, account_id=account_filter, start_date=start_date)
    for o in orders:
        o["net_profit_view"] = o["net_profit_exvat"] if vat_treatment != "cash" else o["net_profit_cash"]

    # module2_dashboard_fixes B1: make the applied window VISIBLE (same
    # pattern /pl already uses via resolve_pl_date_range) -- derived from
    # this SKU's own order list rather than a shared account-wide helper,
    # since resolve_pl_date_range has no canonical_sku filter. This is the
    # concrete, on-page proof that changing "range" actually changed what's
    # included, for a bug report that the range control "does nothing."
    if orders:
        sku_range_start = min(o["posted_date"] for o in orders if o.get("posted_date"))[:10]
        sku_range_end = max(o["posted_date"] for o in orders if o.get("posted_date"))[:10]
    else:
        sku_range_start = sku_range_end = None

    # Recorded selling price (Option A -- local P&L record only, never
    # pushed to Amazon; see pl_price.py). Scoped to whichever single account
    # this row is actually showing.
    price_account = account_filter if account_filter != "all" else row.get("account_id")
    recorded_price = pl_price.get_recorded_price(price_account, canonical_sku) if price_account else None
    price_changelog = pl_price.get_price_changelog(price_account, canonical_sku) if price_account else []

    # "It updates what the P&L uses to calculate profit" (per spec) -- WITHOUT
    # rewriting real historical order data (that would corrupt actual sale
    # prices with a guess). Instead: project per-unit profit/margin AT the
    # recorded price, using this range's actual average per-unit cost rates
    # (referral/other fees, COGS, postage, ad spend) -- "if I sell at this
    # price going forward, here's what the P&L would look like", a live
    # what-if, not an edit to history.
    projected_profit_per_unit = None
    projected_margin_pct = None
    units_n = row.get("units") or 0
    if recorded_price is not None and units_n:
        per_unit_costs = (
            ((row.get("referral_fees") or 0) + (row.get("other_fees") or 0) +
             (row.get("cogs") or 0) + (row.get("postage") or 0) + (row.get("ad_spend") or 0)) / units_n
        )
        projected_profit_per_unit = recorded_price - per_unit_costs
        projected_margin_pct = (projected_profit_per_unit / recorded_price) if recorded_price else None

    family_sku_counts = {f["family"]: f["n_canonical_skus"] for f in pl_cogs.get_all_families()}

    # module2_asp: average selling price = average REVENUE per unit over the
    # selected range (gross ex-VAT / units). Notes, all deliberate:
    #  - Denominator is `units` (packs/ASINs, same basis COGS uses -- e.g.
    #    BD-6372-P4's 210 units are packs, not individual pillows), so this is
    #    per-pack, consistent with the rest of the page.
    #  - inc-VAT MULTIPLIES the stored per-product VAT rate. Going ex->inc is
    #    the legitimate direction; the never-divide-by-1.2 rule only bans
    #    ex-stripping Amazon's already-ex-VAT Principal. Never a hardcoded 1.2
    #    -- if the rate is unset, inc-VAT stays blank/flagged rather than
    #    assuming 20% (same principle as never fabricating postage). This is
    #    the first place the VAT-rate metadata field drives a displayed number.
    #  - Revenue-side only, so it displays even where COGS/postage is
    #    provisional. gross includes buyer-paid shipping, so it is average
    #    REVENUE per unit, NOT the listing price -- labelled as such, and it
    #    will not match the live-listing or recorded-selling-price fields.
    #  - Zero units in range -> None (blank), never a divide-by-zero.
    #  - gross/units are already range-scoped (start_date), so ASP honours the
    #    range=... URL param automatically.
    asp_exvat = (gross / units_n) if units_n else None
    asp_vat_rate = identity.get("vat_rate")
    asp_incvat = (asp_exvat * (1 + asp_vat_rate)) if (asp_exvat is not None and asp_vat_rate is not None) else None

    # module2_dashboard_fixes A2: flag a mismatch between the seller's
    # recorded VAT rate and Amazon's product_tax_code -- display only, the
    # seller's setting is never touched by this comparison.
    _TAX_CODE_ZERO = {"A_GEN_NOTAX"}
    _TAX_CODE_STANDARD = {"A_GEN_STANDARD"}
    _TAX_CODE_REDUCED = {"A_GEN_REDUCED"}
    vat_mismatch = None
    if tax_code_info and tax_code_info.get("available") and tax_code_info.get("product_tax_code") and identity.get("vat_rate") is not None:
        code = tax_code_info["product_tax_code"]
        seller_rate = identity["vat_rate"]
        implied_rate = None
        if code in _TAX_CODE_ZERO:
            implied_rate = 0.0
        elif code in _TAX_CODE_REDUCED:
            implied_rate = 0.05
        elif code in _TAX_CODE_STANDARD:
            implied_rate = 0.20
        if implied_rate is not None and abs(implied_rate - seller_rate) > 0.001:
            vat_mismatch = {"seller_rate": seller_rate, "amazon_code": code, "amazon_implied_rate": implied_rate}

    return render_template_string(
        SKU_DETAIL_HTML,
        accounts=accounts, account_filter=account_filter, vat_treatment=vat_treatment,
        range_key=range_key, period=period, canonical_sku=canonical_sku,
        metric=metric, chart_metric_labels=_CHART_METRIC_LABELS,
        row=row, identity=identity, titles=titles, margin_pct_after_ads=margin_pct_after_ads,
        family_pack_cogs=family_pack_cogs, price_basis_unit=price_basis_unit,
        pack_basis_units=pack_basis_units,
        ad_coverage_warning=ad_coverage_warning, multi_account_note=multi_account_note,
        trend_chart_html=trend_chart_html, orders=orders,
        recorded_price=recorded_price, price_changelog=price_changelog, price_account=price_account,
        back_url=back_url, current_qs=current_qs, self_url=self_url, return_qs=return_qs,
        family_sku_counts=family_sku_counts,
        projected_profit_per_unit=projected_profit_per_unit, projected_margin_pct=projected_margin_pct,
        asp_exvat=asp_exvat, asp_incvat=asp_incvat, asp_vat_rate=asp_vat_rate,
        breakeven_provisional=breakeven_provisional,
        sku_range_start=sku_range_start, sku_range_end=sku_range_end,
        tax_code_info=tax_code_info, live_price_info=live_price_info, vat_mismatch=vat_mismatch,
    )


@app.route("/pl/sku/<path:canonical_sku>/push", methods=["POST"])
def pl_sku_push_toggle(canonical_sku):
    """Mark/unmark this SKU as PUSH mode (deliberate below-break-even rank-buying).
    Below-break-even is shown as info (not red) for push ASINs. No Amazon write."""
    account_id = (request.form.get("account_id") or "").strip()
    on = request.form.get("push") == "1"
    return_url = request.form.get("return_url") or f"/pl/sku/{canonical_sku}"
    if account_id and account_id != "all":
        pl_db.set_push_canonical(account_id, canonical_sku, on)
        flash(f"{canonical_sku}: push mode {'ON' if on else 'off'}.")
    else:
        flash("Pick a specific account (not 'all') to set push mode.")
    return redirect(return_url)


@app.route("/pl/sku/<path:canonical_sku>/price", methods=["POST"])
def pl_sku_price_save(canonical_sku):
    """Option A -- records a selling price for P&L purposes only. Does NOT
    call Amazon in any way; see pl_price.py module docstring for the full
    Option A/B split. Every write here also appends to pl_price_changelog
    (old -> new, timestamped) from the very first edit."""
    account_id = (request.form.get("account_id") or "").strip()
    price_raw = (request.form.get("price") or "").strip()
    return_url = request.form.get("return_url") or f"/pl/sku/{canonical_sku}"
    if not account_id:
        flash("No account specified — could not save price.")
        return redirect(return_url)
    try:
        price = float(price_raw)
    except ValueError:
        flash(f"'{price_raw}' is not a valid number.")
        return redirect(return_url)
    result = pl_price.set_recorded_price(account_id, canonical_sku, price)
    if result["old_price"] is not None:
        flash(f"Recorded price updated: £{result['old_price']:.2f} → £{price:.2f} "
              f"(P&L record only — this does NOT change your Amazon listing).")
    else:
        flash(f"Recorded price set to £{price:.2f} (P&L record only — this does NOT change your Amazon listing).")
    return redirect(return_url)


@app.route("/pl/sku/<path:canonical_sku>/postage", methods=["POST"])
def pl_sku_postage_save(canonical_sku):
    """Same write as /pl/postage/save (pl_postage.bulk_set_manual_postage),
    but for a single order entered in-context on the detail page's order
    list, so the seller doesn't have to leave this page for a one-off fix."""
    account_id = request.form.get("account_id")
    order_id = request.form.get("order_id")
    amount_raw = (request.form.get("amount") or "").strip()
    return_url = request.form.get("return_url") or f"/pl/sku/{canonical_sku}"
    if not account_id or not order_id:
        flash("Missing account or order — could not save.")
        return redirect(return_url)
    try:
        amount = float(amount_raw)
    except ValueError:
        flash(f"'{amount_raw}' is not a valid number.")
        return redirect(return_url)
    pl_postage.bulk_set_manual_postage(account_id, [order_id], amount)
    n = _reprocess_after_cogs_change()
    flash(f"Set £{amount:.2f} postage for order {order_id}, now postage_source='manual'."
          + (f" Recomputed {n} line item(s)." if n is not None else ""))
    return redirect(return_url)


