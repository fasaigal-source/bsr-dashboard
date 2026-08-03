"""Module 3 routes — PPC Ad Optimiser (Phase A: ingest + view the Search Term
report at hour grain). Registered on the shared `app` from dashboard_app.
Independent of Modules 1 & 2. The rule engine (worklist) is deferred; this ships
the data pipeline and the day/hour views so ads can be read by day and time.
"""
import os
import json
import tempfile

from flask import request, redirect, render_template_string, flash

from dashboard_app import app
import pl_ppc
import ppc_ads_api
from pl_db import get_accounts   # Module 2's own accounts reader (Postgres/SQLite)


PPC_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PPC — Ad Optimiser — BSR Repricer</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1500px;margin:22px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:20px 22px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
 h2{margin:0 0 4px;font-size:17px}
 .muted{color:#8a94a2;font-size:13px}
 .flash{background:#e7f6ee;color:#166b3d;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:600}
 .toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:16px}
 select,input[type=file]{padding:8px 10px;border:1px solid #dde3e9;border-radius:8px;font-size:14px;background:#fff}
 .btn{background:#0e5c5b;color:#eafcfb;border:none;border-radius:8px;padding:9px 16px;font-weight:600;cursor:pointer;font-size:14px}
 .kpis{display:flex;gap:14px;flex-wrap:wrap}
 .kpi{flex:1;min-width:130px;background:#f7fafa;border:1px solid #e4edec;border-radius:10px;padding:12px 14px}
 .kpi .lab{font-size:11px;color:#8a94a2;text-transform:uppercase;letter-spacing:.4px}
 .kpi .val{font-size:20px;font-weight:800;margin-top:3px;font-family:ui-monospace,Menlo,monospace}
 .charts{display:grid;grid-template-columns:1fr 1fr;gap:18px}
 @media(max-width:1000px){.charts{grid-template-columns:1fr}}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:8px 9px;text-align:right;border-top:1px solid #eef1f4;white-space:nowrap}
 th:first-child,td:first-child{text-align:left}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase;letter-spacing:.3px;cursor:pointer}
 tbody tr:hover{background:#f7fafa}
 .pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;background:#eef4f4;color:#0e5c5b}
 .pill.product{background:#f3ecfb;color:#6b3fa0}
 .zero{color:#9e2d3c;font-weight:700}
 .tabs{display:flex;gap:8px;margin-bottom:10px}
 .tab{padding:6px 12px;border-radius:8px;background:#eef1f4;cursor:pointer;font-size:13px;border:1px solid transparent}
 .tab.active{background:#0e5c5b;color:#fff}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}

  <div class="card">
    <h2>PPC — Ad Optimiser <span class="muted">· Phase A: data</span></h2>
    <div class="muted">Upload the Sponsored Products → <b>Search Term</b> report (CSV). Hourly rows are
      kept, so you can read spend/sales by day <b>and</b> time of day. Re-uploading a wider range is safe (upsert).</div>
    <form class="toolbar" method="POST" action="/ppc/upload" enctype="multipart/form-data" style="margin-top:14px;">
      <label>Account
        <select name="account_id">
          {% for a in accounts %}<option value="{{ a.account_id }}" {{ 'selected' if a.account_id==account_filter else '' }}>{{ a.account_id }}</option>{% endfor %}
        </select>
      </label>
      <input type="file" name="csv" accept=".csv" required>
      <button class="btn" type="submit">Import Search Term report</button>
    </form>
    <form class="toolbar" method="GET" action="/ppc" style="margin-top:0;">
      <label>View account
        <select name="account" onchange="this.form.submit()">
          <option value="all" {{ 'selected' if account_filter=='all' else '' }}>All accounts</option>
          {% for a in accounts %}<option value="{{ a.account_id }}" {{ 'selected' if a.account_id==account_filter else '' }}>{{ a.account_id }}</option>{% endfor %}
        </select>
      </label>
      <span class="muted">{% if totals.rows %}{{ totals.rows }} rows · {{ totals.date_min }} → {{ totals.date_max }}{% else %}No data yet — import a report above.{% endif %}</span>
    </form>
    <div style="margin-top:6px;"><a class="btn" href="/ppc/schedule?account={{ account_filter }}">⏱ Day-parting schedule (auto on/off)</a></div>
  </div>

  {% if totals.rows %}
  <div class="card">
    <div class="kpis">
      <div class="kpi"><div class="lab">Spend</div><div class="val">£{{ "%.2f"|format(totals.spend or 0) }}</div></div>
      <div class="kpi"><div class="lab">Ad sales</div><div class="val">£{{ "%.2f"|format(totals.sales or 0) }}</div></div>
      <div class="kpi"><div class="lab">ACOS</div><div class="val">{{ "%.0f%%"|format(100*(totals.spend or 0)/(totals.sales or 1)) if totals.sales else "—" }}</div></div>
      <div class="kpi"><div class="lab">Clicks</div><div class="val">{{ totals.clicks or 0 }}</div></div>
      <div class="kpi"><div class="lab">Orders</div><div class="val">{{ totals.orders or 0 }}</div></div>
      <div class="kpi"><div class="lab">Search terms</div><div class="val">{{ totals.terms or 0 }}</div></div>
      <div class="kpi"><div class="lab">Campaigns</div><div class="val">{{ totals.campaigns or 0 }}</div></div>
    </div>
  </div>

  {% if tacos and tacos.has_ad %}
  <div class="card">
    <h2>TACOS <span class="muted">— ad spend ÷ total settled revenue · {{ tacos.cov_min }} → {{ tacos.cov_max }}</span></h2>
    {% if tacos.stale %}
    <div style="background:#fbf1dd;color:#8a5906;padding:10px 14px;border-radius:10px;margin:10px 0;font-size:13px;">
      ⚠ Revenue is behind the ad data (P&amp;L synced to <b>{{ tacos.last_synced[:10] if tacos.last_synced else '—' }}</b>, ads to <b>{{ tacos.cov_max }}</b>) — run the P&amp;L sync so TACOS isn't divided into frozen revenue. The figure below is understated until it catches up.</div>
    {% endif %}
    <div class="kpis" style="margin-top:8px;">
      <div class="kpi"><div class="lab">Account TACOS</div><div class="val">{{ "%.1f%%"|format(100*tacos.account_tacos) if tacos.account_tacos is not none else "—" }}</div></div>
      <div class="kpi"><div class="lab">Ad spend</div><div class="val">£{{ "%.2f"|format(tacos.total_spend) }}</div></div>
      <div class="kpi"><div class="lab">Total revenue (settled)</div><div class="val">£{{ "%.2f"|format(tacos.total_rev) }}</div></div>
    </div>
    <div class="muted" style="margin-top:8px;"><b>Two clocks:</b> ad spend is <b>click-date</b>, revenue is <b>settlement-date</b> — they won't reconcile to the penny (Amazon backdates ad conversions). TACOS is per-ASIN here; ACOS in the search-term table is per term.</div>
    <table style="margin-top:10px;">
      <thead><tr><th>ASIN</th><th>Ad spend</th><th>Revenue (settled)</th><th>TACOS</th></tr></thead>
      <tbody>
      {% for r in tacos.per_asin[:100] %}
        <tr>
          <td>{{ r.asin }}</td>
          <td>£{{ "%.2f"|format(r.spend) }}</td>
          <td>£{{ "%.2f"|format(r.revenue) }}</td>
          <td>{% if r.tacos is not none %}{{ "%.0f%%"|format(100*r.tacos) }}{% elif r.spend %}<span class="zero">∞ (spend, no matched revenue)</span>{% else %}—{% endif %}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    <div class="muted" style="margin-top:6px;">Per-ASIN revenue is matched via your SKU→ASIN map; an ASIN with spend but no matched revenue shows ∞ (add its SKU on COGS &amp; pricing to resolve).</div>
  </div>
  {% endif %}

  <div class="charts">
    <div class="card"><h2>By day</h2><div class="muted">Spend vs ad sales per day.</div><canvas id="dayChart" height="150"></canvas></div>
    <div class="card"><h2>By hour of day</h2><div class="muted">When your spend and sales happen — aggregated across the range.</div><canvas id="hourChart" height="150"></canvas></div>
  </div>

  <div class="card">
    <div class="tabs">
      <div class="tab active" onclick="showTab('terms',this)">Search terms</div>
      <div class="tab" onclick="showTab('camps',this)">Campaigns</div>
    </div>
    <div id="tab-terms">
      <table id="termsTable">
        <thead><tr><th>Search term</th><th>Type</th><th>Camps</th><th>Impr</th><th>Clicks</th><th>Spend</th><th>Orders</th><th>Sales</th><th>ACOS</th></tr></thead>
        <tbody>
        {% for r in terms %}
          <tr>
            <td><a href="/ppc/term/{{ r.search_term|urlencode }}?account={{ account_filter }}">{{ r.search_term }}</a></td>
            <td><span class="pill {{ 'product' if r.targeting_type=='product' else '' }}">{{ r.targeting_type }}</span></td>
            <td>{{ r.campaigns }}</td>
            <td>{{ r.impressions or 0 }}</td>
            <td>{{ r.clicks or 0 }}</td>
            <td>£{{ "%.2f"|format(r.spend or 0) }}</td>
            <td class="{{ 'zero' if not r.orders else '' }}">{{ r.orders or 0 }}</td>
            <td>£{{ "%.2f"|format(r.sales or 0) }}</td>
            <td>{{ "%.0f%%"|format(100*(r.spend or 0)/(r.sales or 1)) if r.sales else '—' }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
      <div class="muted" style="margin-top:8px;">Top {{ terms|length }} terms by spend. Zero-order terms (red) are dead-term candidates — the rule engine will surface these once you set thresholds.</div>
    </div>
    <div id="tab-camps" style="display:none;">
      <table>
        <thead><tr><th>Campaign</th><th>Terms</th><th>Clicks</th><th>Spend</th><th>Orders</th><th>Sales</th><th>ACOS</th></tr></thead>
        <tbody>
        {% for r in campaigns %}
          <tr>
            <td><a href="/ppc/campaign/{{ r.campaign_id }}?account={{ account_filter }}">{{ r.campaign_name or r.campaign_id }}</a></td>
            <td>{{ r.terms }}</td>
            <td>{{ r.clicks or 0 }}</td>
            <td>£{{ "%.2f"|format(r.spend or 0) }}</td>
            <td>{{ r.orders or 0 }}</td>
            <td>£{{ "%.2f"|format(r.sales or 0) }}</td>
            <td>{{ "%.0f%%"|format(100*(r.spend or 0)/(r.sales or 1)) if r.sales else '—' }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </div>
  {% endif %}
</div>

<script>
  var DAY = {{ day_json|safe }};
  var HOUR = {{ hour_json|safe }};
  function mkBars(id, labels, spend, sales, spendLabel){
    if(!document.getElementById(id)) return;
    new Chart(document.getElementById(id), {
      type:'bar',
      data:{labels:labels, datasets:[
        {label:'Spend (£)', data:spend, backgroundColor:'#0e5c5b'},
        {label:'Ad sales (£)', data:sales, backgroundColor:'#8fcf9a'}
      ]},
      options:{responsive:true, plugins:{legend:{position:'top'}}, scales:{y:{beginAtZero:true}}}
    });
  }
  if (DAY && DAY.length) mkBars('dayChart', DAY.map(d=>d.date), DAY.map(d=>d.spend), DAY.map(d=>d.sales));
  if (HOUR && HOUR.length) mkBars('hourChart', HOUR.map(d=>d.hour+':00'), HOUR.map(d=>d.spend), HOUR.map(d=>d.sales));
  function showTab(which, el){
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    el.classList.add('active');
    document.getElementById('tab-terms').style.display = which==='terms' ? '' : 'none';
    document.getElementById('tab-camps').style.display = which==='camps' ? '' : 'none';
  }
</script>
</body></html>
"""


@app.route("/ppc")
def ppc_page():
    account_filter = request.args.get("account", "all")
    try:
        accounts = get_accounts()
    except Exception:
        accounts = []
    totals = pl_ppc.get_totals(account_filter)
    day = pl_ppc.by_day(account_filter) if totals.get("rows") else []
    hour = pl_ppc.by_hour(account_filter) if totals.get("rows") else []
    terms = pl_ppc.by_search_term(account_filter, limit=500) if totals.get("rows") else []
    campaigns = pl_ppc.by_campaign(account_filter) if totals.get("rows") else []
    try:
        tacos = pl_ppc.get_tacos(account_filter)
    except Exception:
        tacos = {"has_ad": False}
    return render_template_string(
        PPC_HTML, accounts=accounts, account_filter=account_filter,
        totals=totals, terms=terms, campaigns=campaigns, tacos=tacos,
        day_json=json.dumps(day), hour_json=json.dumps(hour))


@app.route("/ppc/upload", methods=["POST"])
def ppc_upload():
    account_id = request.form.get("account_id")
    f = request.files.get("csv")
    if not account_id:
        flash("Pick an account first.")
        return redirect("/ppc")
    if not f or not f.filename:
        flash("No file selected.")
        return redirect("/ppc")
    fd, tmp = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    f.save(tmp)
    try:
        s = pl_ppc.import_search_term_csv(account_id, tmp)
        flash(f"Imported {s['rows']} row(s) for {account_id} "
              f"({s['date_min']} → {s['date_max']}, {len(s['hours'])} hours)."
              + (f" {s['skipped']} row(s) skipped." if s.get("skipped") else ""))
    except Exception as e:
        flash(f"Import failed: {e}")
    finally:
        os.remove(tmp)
    return redirect(f"/ppc?account={account_id}")


# ── drill-down: one campaign or one search term, by hour of day ──────────────

DETAIL_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }} — PPC — BSR Repricer</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1400px;margin:22px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:20px 22px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
 h2{margin:0 0 4px;font-size:17px} .muted{color:#8a94a2;font-size:13px}
 a{color:#0e5c5b}
 .kpis{display:flex;gap:14px;flex-wrap:wrap;margin-top:12px}
 .kpi{flex:1;min-width:120px;background:#f7fafa;border:1px solid #e4edec;border-radius:10px;padding:10px 12px}
 .kpi .lab{font-size:11px;color:#8a94a2;text-transform:uppercase;letter-spacing:.4px}
 .kpi .val{font-size:19px;font-weight:800;margin-top:3px;font-family:ui-monospace,Menlo,monospace}
 .charts{display:grid;grid-template-columns:1fr 1fr;gap:18px}
 @media(max-width:1000px){.charts{grid-template-columns:1fr}}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:right;border-top:1px solid #eef1f4;white-space:nowrap}
 th:first-child,td:first-child{text-align:left}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase}
 tr.waste{background:#fdecee}
 tr.win{background:#eefaf0}
 .tag{font-size:11px;font-weight:700;padding:2px 7px;border-radius:9px}
 .tag.waste{background:#f9d6da;color:#9e2d3c}
 .tag.win{background:#cdefd6;color:#166b3d}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  <div class="card">
    <div class="muted"><a href="/ppc?account={{ account_filter }}">← PPC overview</a></div>
    <h2 style="margin-top:8px;">{{ title }}</h2>
    <div class="muted">{{ subtitle }} · {{ totals.date_min }} → {{ totals.date_max }}</div>
    <div class="kpis">
      <div class="kpi"><div class="lab">Spend</div><div class="val">£{{ "%.2f"|format(totals.spend or 0) }}</div></div>
      <div class="kpi"><div class="lab">Ad sales</div><div class="val">£{{ "%.2f"|format(totals.sales or 0) }}</div></div>
      <div class="kpi"><div class="lab">ACOS</div><div class="val">{{ "%.0f%%"|format(100*(totals.spend or 0)/(totals.sales or 1)) if totals.sales else "—" }}</div></div>
      <div class="kpi"><div class="lab">Clicks</div><div class="val">{{ totals.clicks or 0 }}</div></div>
      <div class="kpi"><div class="lab">Orders</div><div class="val">{{ totals.orders or 0 }}</div></div>
    </div>
  </div>

  <div class="charts">
    <div class="card"><h2>By hour of day</h2><div class="muted">When this {{ kind }} spends vs converts — decide run/pause hours.</div><canvas id="hourChart" height="150"></canvas></div>
    <div class="card"><h2>By day</h2><canvas id="dayChart" height="150"></canvas></div>
  </div>

  <div class="card">
    <h2>Hour-of-day breakdown</h2>
    <div class="muted">Every hour 0–23. <span class="tag waste">WASTING</span> = spend but zero sales (pause candidate);
      <span class="tag win">CONVERTS</span> = has sales. Sums across all days in the report.</div>
    <table style="margin-top:10px;">
      <thead><tr><th>Hour</th><th>Impr</th><th>Clicks</th><th>Spend</th><th>Orders</th><th>Sales</th><th>ROAS</th><th>ACOS</th><th></th></tr></thead>
      <tbody>
      {% for h in hours %}
        <tr class="{{ 'waste' if (h.spend and not h.sales) else ('win' if h.sales else '') }}">
          <td>{{ '%02d:00'|format(h.hour) }}</td>
          <td>{{ h.impressions or 0 }}</td>
          <td>{{ h.clicks or 0 }}</td>
          <td>£{{ "%.2f"|format(h.spend or 0) }}</td>
          <td>{{ h.orders or 0 }}</td>
          <td>£{{ "%.2f"|format(h.sales or 0) }}</td>
          <td>{{ "%.2f×"|format((h.sales or 0)/(h.spend or 1)) if h.spend else "—" }}</td>
          <td>{{ "%.0f%%"|format(100*(h.spend or 0)/(h.sales or 1)) if h.sales else "—" }}</td>
          <td>{% if h.spend and not h.sales %}<span class="tag waste">WASTING</span>{% elif h.sales %}<span class="tag win">CONVERTS</span>{% endif %}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2>{{ related_title }}</h2>
    <table>
      <thead><tr><th>{{ related_col }}</th><th>Clicks</th><th>Spend</th><th>Orders</th><th>Sales</th><th>ACOS</th></tr></thead>
      <tbody>
      {% for r in related %}
        <tr>
          <td>{{ related_link(r)|safe }}</td>
          <td>{{ r.clicks or 0 }}</td>
          <td>£{{ "%.2f"|format(r.spend or 0) }}</td>
          <td>{{ r.orders or 0 }}</td>
          <td>£{{ "%.2f"|format(r.sales or 0) }}</td>
          <td>{{ "%.0f%%"|format(100*(r.spend or 0)/(r.sales or 1)) if r.sales else "—" }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
</div>
<script>
  var HOUR = {{ hour_json|safe }};
  var DAY = {{ day_json|safe }};
  function mk(id, labels, spend, sales){
    if(!document.getElementById(id)) return;
    new Chart(document.getElementById(id), {type:'bar',
      data:{labels:labels, datasets:[
        {label:'Spend (£)', data:spend, backgroundColor:'#0e5c5b'},
        {label:'Ad sales (£)', data:sales, backgroundColor:'#8fcf9a'}]},
      options:{responsive:true, plugins:{legend:{position:'top'}}, scales:{y:{beginAtZero:true}}}});
  }
  mk('hourChart', HOUR.map(d=>d.hour+':00'), HOUR.map(d=>d.spend), HOUR.map(d=>d.sales));
  mk('dayChart', DAY.map(d=>d.date), DAY.map(d=>d.spend), DAY.map(d=>d.sales));
</script>
</body></html>
"""


def _hours_full(rows):
    """Fill 0..23 so every hour shows (a blank hour = ad not spending then)."""
    by = {r["hour"]: r for r in rows}
    out = []
    for h in range(24):
        r = by.get(h) or {}
        out.append({"hour": h, "impressions": r.get("impressions") or 0,
                    "clicks": r.get("clicks") or 0, "spend": r.get("spend") or 0,
                    "orders": r.get("orders") or 0, "sales": r.get("sales") or 0})
    return out


@app.route("/ppc/campaign/<campaign_id>")
def ppc_campaign(campaign_id):
    account_filter = request.args.get("account", "all")
    totals = pl_ppc.entity_totals(account_filter, campaign_id=campaign_id)
    hours = _hours_full(pl_ppc.by_hour(account_filter, campaign_id=campaign_id))
    day = pl_ppc.by_day(account_filter, campaign_id=campaign_id)
    related = pl_ppc.terms_for_campaign(account_filter, campaign_id)

    def related_link(r):
        return (f'<a href="/ppc/term/{r["search_term"]}?account={account_filter}">{r["search_term"]}</a>'
                f' <span class="muted">· {r.get("targeting_type","")}</span>')
    return render_template_string(
        DETAIL_HTML, title=(totals.get("campaign_name") or campaign_id),
        subtitle=f"Campaign · {totals.get('terms',0)} search terms", kind="campaign",
        account_filter=account_filter, totals=totals, hours=hours,
        related=related, related_title="Search terms in this campaign",
        related_col="Search term", related_link=related_link,
        hour_json=json.dumps([{"hour": h["hour"], "spend": h["spend"], "sales": h["sales"]} for h in hours]),
        day_json=json.dumps(day))


# ── Phase C: day-parting schedule (auto on/off via Ads API) ──────────────────

SCHEDULE_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Day-parting schedule — PPC — BSR Repricer</title>
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1300px;margin:22px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:20px 22px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
 h2{margin:0 0 4px;font-size:17px} .muted{color:#8a94a2;font-size:13px} a{color:#0e5c5b}
 .flash{background:#e7f6ee;color:#166b3d;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:600}
 .banner{padding:10px 14px;border-radius:10px;font-size:13px;margin-bottom:14px}
 .banner.live{background:#e7f6ee;color:#166b3d} .banner.dry{background:#fbf1dd;color:#8a5906}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:8px 9px;border-top:1px solid #eef1f4;text-align:left;vertical-align:middle}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase}
 input[type=number]{width:64px;padding:6px;border:1px solid #dde3e9;border-radius:6px}
 .btn{background:#0e5c5b;color:#eafcfb;border:none;border-radius:7px;padding:7px 13px;font-weight:600;cursor:pointer;font-size:13px;text-decoration:none;display:inline-block}
 .btn.sm{padding:4px 9px;font-size:12px}
 .btn.grey{background:#6b7684} .btn.amber{background:#8a5906}
 .state{font-size:11px;font-weight:800;padding:2px 8px;border-radius:9px}
 .state.enabled{background:#cdefd6;color:#166b3d} .state.paused{background:#f3d0d5;color:#9e2d3c}
 .res-ok{color:#166b3d}.res-error{color:#9e2d3c}.res-dry_run{color:#8a5906}.res-noop{color:#8a94a2}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}
  <div class="card">
    <div class="muted"><a href="/ppc?account={{ account_filter }}">← PPC overview</a></div>
    <h2 style="margin-top:8px;">Day-parting schedule <span class="muted">· auto on/off</span></h2>
    <div class="muted">Tick <b>Active</b> and set the ON window (24-hour clock, {{ tz }}). The scheduler runs hourly and
      enables the campaign inside the window, pauses it outside. Times are your local timezone. Use <b>Test</b> to fire a
      single enable/pause now and confirm the API works before scheduling.</div>
    <form method="GET" action="/ppc/schedule" style="margin-top:10px;">
      <label>Account
        <select name="account" onchange="this.form.submit()">
          {% for a in accounts %}<option value="{{ a.account_id }}" {{ 'selected' if a.account_id==account_filter else '' }}>{{ a.account_id }}</option>{% endfor %}
        </select>
      </label>
    </form>
    {% if configured %}
      <div class="banner live" style="margin-top:12px;">✓ Ads API configured for <b>{{ account_filter }}</b> — active campaigns are toggled <b>live</b> on Amazon.</div>
    {% else %}
      <div class="banner dry" style="margin-top:12px;">⚠ Ads API not configured for <b>{{ account_filter }}</b> (or PPC_DRY_RUN=1) — actions are <b>dry-run</b> (logged, nothing sent to Amazon). Set the ADS_* env vars to go live.</div>
    {% endif %}
  </div>

  <div class="card">
    <form method="POST" action="/ppc/schedule/save">
      <input type="hidden" name="account_id" value="{{ account_filter }}">
      <table>
        <thead><tr><th>Campaign</th><th>Spend</th><th>Active</th><th>ON from</th><th>ON to</th><th>Now wants</th><th>Test</th></tr></thead>
        <tbody>
        {% for c in campaigns %}
          {% set s = sched_by_id.get(c.campaign_id, {}) %}
          <tr>
            <td>{{ c.campaign_name or c.campaign_id }}<input type="hidden" name="cid" value="{{ c.campaign_id }}"><input type="hidden" name="cname_{{ c.campaign_id }}" value="{{ c.campaign_name or '' }}"></td>
            <td>£{{ "%.2f"|format(c.spend or 0) }}</td>
            <td><input type="checkbox" name="active_{{ c.campaign_id }}" {{ 'checked' if s.get('active') else '' }}></td>
            <td><input type="number" min="0" max="24" name="start_{{ c.campaign_id }}" value="{{ s.get('on_start_hour', 0) }}"></td>
            <td><input type="number" min="0" max="24" name="end_{{ c.campaign_id }}" value="{{ s.get('on_end_hour', 24) }}"></td>
            <td>{% if s.get('active') %}<span class="state {{ desired.get(c.campaign_id,'') }}">{{ desired.get(c.campaign_id,'') }}</span>{% else %}<span class="muted">—</span>{% endif %}</td>
            <td>
              <button class="btn sm" formaction="/ppc/schedule/test" name="test" value="{{ c.campaign_id }}:enabled">Enable now</button>
              <button class="btn sm amber" formaction="/ppc/schedule/test" name="test" value="{{ c.campaign_id }}:paused">Pause now</button>
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
      <div style="margin-top:14px;"><button class="btn" type="submit">Save schedule</button>
        <span class="muted" style="margin-left:8px;">ON window wraps midnight if "from" &gt; "to" (e.g. 22 → 6). Set from=0,to=24 for always-on.</span></div>
    </form>
  </div>

  <div class="card">
    <h2>Recent actions</h2>
    <div class="muted">Every enable/pause the scheduler or a Test fired. <b>dry_run</b> = logged only, nothing sent to Amazon.</div>
    <table style="margin-top:10px;">
      <thead><tr><th>When (UTC)</th><th>Account</th><th>Campaign</th><th>Action</th><th>Result</th><th>Detail</th></tr></thead>
      <tbody>
      {% for a in actions %}
        <tr>
          <td>{{ a.at[:16] if a.at else '' }}</td>
          <td>{{ a.account_id }}</td>
          <td>{{ a.campaign_name or a.campaign_id }}</td>
          <td>{{ a.action }} → {{ a.desired_state }}</td>
          <td class="res-{{ a.result }}">{{ a.result }}</td>
          <td class="muted">{{ (a.detail or '')[:80] }}</td>
        </tr>
      {% endfor %}
      {% if not actions %}<tr><td colspan="6" class="muted">No actions yet.</td></tr>{% endif %}
      </tbody>
    </table>
  </div>
</div></body></html>
"""


def _sched_context(account_filter):
    campaigns = pl_ppc.campaign_names(account_filter) if account_filter != "all" else pl_ppc.campaign_names()
    scheds = pl_ppc.get_schedules(account_filter if account_filter != "all" else None)
    sched_by_id = {s["campaign_id"]: s for s in scheds}
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        tz = os.environ.get("ADS_TZ", "Europe/London")
        now_hour = _dt.datetime.now(ZoneInfo(tz)).hour
    except Exception:
        tz = "UTC"
        now_hour = _dt.datetime.now(_dt.timezone.utc).hour
    desired = {cid: pl_ppc.desired_state_for_hour(s, now_hour) for cid, s in sched_by_id.items()}
    return campaigns, sched_by_id, desired, tz


@app.route("/ppc/schedule")
def ppc_schedule():
    account_filter = request.args.get("account", "all")
    try:
        accounts = get_accounts()
    except Exception:
        accounts = []
    if account_filter == "all" and accounts:
        account_filter = accounts[0]["account_id"]
    campaigns, sched_by_id, desired, tz = _sched_context(account_filter)
    return render_template_string(
        SCHEDULE_HTML, accounts=accounts, account_filter=account_filter,
        campaigns=campaigns, sched_by_id=sched_by_id, desired=desired, tz=tz,
        configured=ppc_ads_api.is_configured(account_filter),
        actions=pl_ppc.get_action_log(account_filter, limit=100))


@app.route("/ppc/schedule/save", methods=["POST"])
def ppc_schedule_save():
    account_id = request.form.get("account_id")
    cids = request.form.getlist("cid")
    n = 0
    for cid in cids:
        active = 1 if request.form.get(f"active_{cid}") else 0
        try:
            start = int(request.form.get(f"start_{cid}", 0))
            end = int(request.form.get(f"end_{cid}", 24))
        except ValueError:
            start, end = 0, 24
        cname = request.form.get(f"cname_{cid}") or None
        pl_ppc.upsert_schedule(account_id, cid, campaign_name=cname, active=active,
                               on_start_hour=start, on_end_hour=end)
        n += 1
    flash(f"Saved schedule for {n} campaign(s). The hourly scheduler will apply active ones.")
    return redirect(f"/ppc/schedule?account={account_id}")


@app.route("/ppc/schedule/test", methods=["POST"])
def ppc_schedule_test():
    account_id = request.form.get("account_id")
    test = request.form.get("test", "")   # "<campaign_id>:<state>"
    cid, _, state = test.partition(":")
    if state not in ("enabled", "paused"):
        flash("Bad test action.")
        return redirect(f"/ppc/schedule?account={account_id}")
    cname = request.form.get(f"cname_{cid}") or None
    res = ppc_ads_api.set_campaign_state(account_id, cid, state, cname)
    flash(f"Test {state} on campaign {cid}: {res.get('result')}"
          + (f" — {res.get('detail')}" if res.get('detail') else ""))
    return redirect(f"/ppc/schedule?account={account_id}")


@app.route("/ppc/term/<path:search_term>")
def ppc_term(search_term):
    account_filter = request.args.get("account", "all")
    totals = pl_ppc.entity_totals(account_filter, search_term=search_term)
    hours = _hours_full(pl_ppc.by_hour(account_filter, search_term=search_term))
    day = pl_ppc.by_day(account_filter, search_term=search_term)
    related = pl_ppc.campaigns_for_term(account_filter, search_term)

    def related_link(r):
        return (f'<a href="/ppc/campaign/{r["campaign_id"]}?account={account_filter}">'
                f'{r.get("campaign_name") or r["campaign_id"]}</a>')
    return render_template_string(
        DETAIL_HTML, title=search_term,
        subtitle=f"Search term · in {totals.get('campaigns',0)} campaign(s)", kind="term",
        account_filter=account_filter, totals=totals, hours=hours,
        related=related, related_title="Campaigns running this term",
        related_col="Campaign", related_link=related_link,
        hour_json=json.dumps([{"hour": h["hour"], "spend": h["spend"], "sales": h["sales"]} for h in hours]),
        day_json=json.dumps(day))
