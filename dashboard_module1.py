"""Module 1 routes — BSR tracking / repricing UI (/, /product, /recommendations,
/baseline, /products, /accounts). Registered on the shared `app` from
dashboard_app. INDEPENDENT of Module 2: editing this file cannot touch /pl.
"""
import os
import tempfile
from datetime import datetime, timedelta
from flask import request, redirect, render_template_string, flash, Response

from module1_db import (
    init_schema, get_db, get_pending_recommendations, get_recommendation,
    decide_recommendation, import_baseline_csv, get_managed_asins,
    get_daily_units, get_current_price, upsert_managed_asin, get_asin,
    set_asin_active, upsert_account, get_accounts,
    get_price_changes, get_recommendation_history,
    import_bsr_history_csv, get_bsr_history_import,
)

from dashboard_app import app, COLLECTOR_STATUS_URL

# Detail-page data source. If COLLECTOR_RO_URL is set, Module 1's product page
# reads the LIVE collector Postgres READ-ONLY (collector_ro maps its schema:
# bsr_price_snapshots -> ranks, velocity_daily -> units, watchlist -> meta,
# daily_recommendations -> rec history). If unset, it falls back to the local
# SQLite tables, so local dev without the URL is unchanged.
_COLLECTOR = None
if os.environ.get("COLLECTOR_RO_URL"):
    try:
        import collector_ro as _COLLECTOR
    except Exception:
        _COLLECTOR = None


def _readonly_block():
    """When the dashboard is READ-ONLY to the collector, management writes (add /
    edit / pause / import / account changes) must not run — the collector owns the
    watchlist. Returns a redirect Response in that mode, else None so local dev
    (SQLite) keeps working. Never writes to the collector."""
    if _COLLECTOR is not None:
        flash("Read-only here — manage the watchlist and accounts on the collector.")
        return redirect(request.referrer or "/products")
    return None


# Small link-out page used where management lives on the collector, not here.
MANAGE_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ title }} — BSR Repricer</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:720px;margin:56px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:36px;box-shadow:0 2px 8px rgba(0,0,0,.06);text-align:center}
 h2{margin:0 0 10px}
 .muted{color:#8a94a2;font-size:14px;line-height:1.5}
 .btn{display:inline-block;margin-top:20px;background:#0e5c5b;color:#fff;text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:600}
</style></head><body>
{{ nav|safe }}
<div class="wrap"><div class="card">
  <h2>{{ title }}</h2>
  <p class="muted">{{ message }}</p>
  <a class="btn" href="{{ collector_url }}" target="_blank" rel="noopener">Open the collector &#8599;</a>
</div></div></body></html>
"""



# ─────────────────────────────────────────────────────────────────────────────
# LANDING
# ─────────────────────────────────────────────────────────────────────────────

HOME_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BSR Repricer</title>
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c}
 .header{background:#0e5c5b;color:#eafcfb;padding:18px 30px;display:flex;justify-content:space-between;align-items:center}
 .header .nav a{color:#bfe9e7;font-size:13px;text-decoration:none;margin-left:18px}
 .wrap{max-width:1000px;margin:26px auto;padding:0 20px}
 .toolbar{display:flex;gap:10px;align-items:center;margin-bottom:18px}
 .pill{background:#fff;border:1px solid #dde3e9;border-radius:20px;padding:6px 14px;font-size:13px}
 .pill b{color:#0e5c5b}
 .grid{display:grid;grid-template-columns:1fr;gap:14px}
 a.card{display:block;background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06);
        text-decoration:none;color:inherit;border:1px solid transparent}
 a.card:hover{border-color:#0e5c5b}
 .row{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;flex-wrap:wrap}
 .name{font-size:17px;font-weight:700}
 .sub{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#8a94a2;margin-top:2px}
 .stats{display:flex;gap:22px;flex-wrap:wrap}
 .stat .lab{font-size:10.5px;color:#8a94a2;text-transform:uppercase;letter-spacing:.4px}
 .stat .val{font-family:ui-monospace,Menlo,monospace;font-size:17px;font-weight:700;margin-top:2px}
 .badge{font-size:11px;font-weight:800;padding:4px 10px;border-radius:14px}
 .badge.pending{background:#fbf1dd;color:#8a5906}
 .badge.ok{background:#e7f6ee;color:#166b3d}
 .empty{background:#fff;border-radius:12px;padding:40px;text-align:center;color:#8a94a2}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  <div class="toolbar">
    <span class="pill">Pending decisions: <b>{{ pending }}</b></span>
    <span class="pill">Products tracked: <b>{{ products|length }}</b></span>
  </div>
  <div class="grid">
    {% for p in products %}
    <a class="card" href="/product/{{ p.account_id }}/{{ p.asin }}">
      <div class="row">
        <div>
          <div class="name">{{ p.sku or p.asin }} <span style="font-weight:400;color:#8a94a2;font-size:13px;">· {{ p.brand }}</span></div>
          <div class="sub">{{ p.asin }} · {{ p.account_id }}</div>
        </div>
        <div class="stats">
          <div class="stat"><div class="lab">Price</div><div class="val">£{{ "%.2f"|format(p.price) }}</div></div>
          <div class="stat"><div class="lab">Root BSR</div><div class="val">{{ "{:,}".format(p.root_rank) if p.root_rank else "—" }}</div></div>
          <div class="stat"><div class="lab">Sub BSR</div><div class="val">{{ p.sub_rank if p.sub_rank else "—" }}</div></div>
          <div class="stat"><div class="lab">Units 7d</div><div class="val">{{ p.units7 }}</div></div>
          <div class="stat">
            {% if p.pending %}<span class="badge pending">{{ p.pending }} pending</span>
            {% else %}<span class="badge ok">up to date</span>{% endif %}
          </div>
        </div>
      </div>
    </a>
    {% endfor %}
    {% if not products %}
    <div class="empty">No products yet — they appear here once seeded and the daily job has run.</div>
    {% endif %}
  </div>
</div></body></html>
"""


@app.route("/")
def index():
    days7 = [(datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(1, 8)]
    products = []
    if _COLLECTOR is not None:
        # LIVE collector: whole Module 1 list is live, read-only
        for m in _COLLECTOR.list_products():
            acct, asin = m["account_id"], m["asin"]
            latest = _COLLECTOR.get_latest_ranks(acct, asin)
            daily = _COLLECTOR.get_daily_units(acct, asin, days=8)
            products.append({
                "account_id": acct, "asin": asin, "sku": m.get("sku"),
                "brand": m.get("brand"),
                "price": _COLLECTOR.get_current_price(acct, asin, fallback=m.get("floor_price")),
                "root_rank": latest["root_rank"] if latest else None,
                "sub_rank": latest["sub_rank"] if latest else None,
                "units7": sum(daily.get(d, 0) for d in days7),
                "pending": 0,   # collector auto-notifies; no approval queue
            })
        pending_total = 0
    else:
        # local SQLite fallback — unchanged behaviour
        conn = get_db()
        for m in get_managed_asins():
            acct, asin = m["account_id"], m["asin"]
            rank_row = conn.execute("""
                SELECT root_rank, sub_rank FROM rank_history
                WHERE account_id=? AND asin=? ORDER BY captured_at DESC LIMIT 1
            """, (acct, asin)).fetchone()
            pend = conn.execute("""
                SELECT COUNT(*) AS n FROM recommendations
                WHERE account_id=? AND asin=? AND status='pending'
            """, (acct, asin)).fetchone()["n"]
            daily = get_daily_units(acct, asin, days=8)
            products.append({
                "account_id": acct, "asin": asin, "sku": m.get("sku"),
                "brand": m.get("brand"),
                "price": get_current_price(acct, asin, fallback=m.get("current_price") or m["floor_price"]),
                "root_rank": rank_row["root_rank"] if rank_row else None,
                "sub_rank": rank_row["sub_rank"] if rank_row else None,
                "units7": sum(daily.get(d, 0) for d in days7),
                "pending": pend,
            })
        conn.close()
        pending_total = len(get_pending_recommendations())
    return render_template_string(HOME_HTML, products=products, pending=pending_total)


# ─────────────────────────────────────────────────────────────────────────────
# PRODUCT DETAIL — BSR line + daily units bars on one timeline
# ─────────────────────────────────────────────────────────────────────────────

PRODUCT_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{{ sku }} — BSR Repricer</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c}
 .header{background:#0e5c5b;color:#eafcfb;padding:18px 30px;display:flex;justify-content:space-between;align-items:center}
 .header a{color:#bfe9e7;font-size:13px;text-decoration:none;margin-left:18px}
 .wrap{max-width:1000px;margin:26px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:22px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 .title{font-size:19px;font-weight:700}
 .sub{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#8a94a2;margin-top:2px}
 .stats{display:flex;gap:26px;flex-wrap:wrap;margin-top:14px}
 .stat .lab{font-size:10.5px;color:#8a94a2;text-transform:uppercase;letter-spacing:.4px}
 .stat .val{font-family:ui-monospace,Menlo,monospace;font-size:20px;font-weight:700;margin-top:2px}
 .chart-note{font-size:12px;color:#8a94a2;margin:4px 0 12px}
 canvas{max-height:380px}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  <div class="card">
    <div class="title">{{ sku }} <span style="font-weight:400;color:#8a94a2;font-size:14px;">· {{ brand }}</span></div>
    <div class="sub">{{ asin }} · {{ account_id }}</div>
    <div class="stats">
      <div class="stat"><div class="lab">Current price</div><div class="val">£{{ "%.2f"|format(price) }}</div></div>
      <div class="stat"><div class="lab">Root BSR</div><div class="val">{{ "{:,}".format(root_rank) if root_rank else "—" }}</div></div>
      <div class="stat"><div class="lab">Sub BSR</div><div class="val">{{ sub_rank if sub_rank else "—" }}</div></div>
      <div class="stat"><div class="lab">Units last 7 days</div><div class="val">{{ units7 }}</div></div>
    </div>
  </div>
  <div class="card">
    <div class="title" style="font-size:15px;">BSR &amp; daily orders — last 30 days</div>
    <div class="chart-note">Teal line = live root BSR (axis reversed — up means improving). Bars = units/day. {% if has_history %}Grey line = imported historic BSR (Trellis, reference only).{% endif %}</div>
    <canvas id="chart"></canvas>
  </div>
  {% if not collector_mode %}
  <div class="card">
    <div class="title" style="font-size:15px;">Import historic BSR</div>
    <div class="chart-note">One-off: load this ASIN's past BSR/price from a Trellis or Helium 10 export (columns: Date, Best Seller Rank, Price). Reference layer only — never used for live decisions.</div>
    <form method="POST" action="/product/{{ account_id }}/{{ asin }}/import-bsr" enctype="multipart/form-data">
      <input type="file" name="csv" accept=".csv" required>
      <button type="submit" style="background:#0e5c5b;color:#eafcfb;border:none;border-radius:8px;padding:8px 16px;font-weight:600;cursor:pointer;margin-left:8px;">Import history</button>
    </form>
    {% if has_history %}<div class="chart-note" style="margin-top:8px;">{{ history_count }} historic days loaded, back to {{ history_from }}.</div>{% endif %}
  </div>
  {% endif %}
</div>
<script>
const labels = {{ labels|tojson }};
const bsr    = {{ bsr|tojson }};
const units  = {{ units|tojson }};
const hist   = {{ hist|tojson }};
new Chart(document.getElementById('chart'), {
  data: {
    labels: labels,
    datasets: [
      { type:'line', label:'Root BSR', data:bsr, yAxisID:'y',
        borderColor:'#0e5c5b', backgroundColor:'#0e5c5b',
        spanGaps:true, tension:.25, pointRadius:3 },
      { type:'bar', label:'Units/day', data:units, yAxisID:'y1',
        backgroundColor:'rgba(31,157,87,.45)', borderRadius:4 },
      { type:'line', label:'Historic BSR', data:hist, yAxisID:'y',
        borderColor:'#b7c0cc', backgroundColor:'#b7c0cc', borderDash:[4,3],
        spanGaps:true, tension:.25, pointRadius:0 }
    ]
  },
  options: {
    responsive:true, interaction:{mode:'index', intersect:false},
    scales: {
      y:  { position:'left', reverse:true,
            title:{display:true,text:'BSR (lower = better, shown upward)'} },
      y1: { position:'right', beginAtZero:true, grid:{drawOnChartArea:false},
            title:{display:true,text:'Units per day'} }
    }
  }
});
</script>
  <div class="card">
    <div class="title" style="font-size:15px;">Price changes</div>
    <div class="chart-note">When you moved price, for reference against the chart above.</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <tr style="text-align:left;color:#8a94a2;"><th style="padding:6px;">When (UTC)</th><th>Old</th><th>New</th><th>Via</th></tr>
      {% for c in price_changes %}
      <tr style="border-top:1px solid #eef1f4;">
        <td style="padding:6px;font-family:ui-monospace,Menlo,monospace;">{{ c.changed_at[:16] }}</td>
        <td>{% if c.old_price %}£{{ "%.2f"|format(c.old_price) }}{% else %}—{% endif %}</td>
        <td><b>{% if c.new_price %}£{{ "%.2f"|format(c.new_price) }}{% else %}—{% endif %}</b></td>
        <td>{{ c.applied_via }}</td>
      </tr>
      {% else %}<tr><td colspan="4" style="padding:10px;color:#8a94a2;">No price changes recorded yet.</td></tr>{% endfor %}
    </table>
  </div>
  <div class="card">
    <div class="title" style="font-size:15px;">Recommendation history</div>
    <table style="width:100%;border-collapse:collapse;font-size:13px;">
      <tr style="text-align:left;color:#8a94a2;"><th style="padding:6px;">When (UTC)</th><th>Signal</th><th>Advised</th><th>You did</th></tr>
      {% for r in rec_history %}
      <tr style="border-top:1px solid #eef1f4;">
        <td style="padding:6px;font-family:ui-monospace,Menlo,monospace;">{{ r.created_at[:16] }}</td>
        <td>{{ r.signal_state.replace('_',' ') }}</td>
        <td>{{ r.recommended_action }}{% if r.recommended_price %} £{{ "%.2f"|format(r.recommended_price) }}{% endif %}</td>
        <td>{{ r.status }}{% if r.decided_price %} £{{ "%.2f"|format(r.decided_price) }}{% endif %}</td>
      </tr>
      {% else %}<tr><td colspan="4" style="padding:10px;color:#8a94a2;">No recommendations yet.</td></tr>{% endfor %}
    </table>
  </div>
</body>
</html>
"""


@app.route("/product/<account_id>/<asin>")
def product_page(account_id, asin):
    # last 30 days, one rank point per day (latest snapshot that day)
    since = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")

    if _COLLECTOR is not None:
        # LIVE collector Postgres (read-only). Same shapes as the SQLite path.
        meta = _COLLECTOR.get_product_meta(account_id, asin)
        if not meta:
            return redirect("/")
        rank_rows = _COLLECTOR.get_rank_rows(account_id, asin, since)
        latest = _COLLECTOR.get_latest_ranks(account_id, asin)
        daily = _COLLECTOR.get_daily_units(account_id, asin, days=31)
        hist_all = _COLLECTOR.get_bsr_history_import(account_id, asin)
        price_changes = _COLLECTOR.get_price_changes(account_id, asin)
        rec_history = _COLLECTOR.get_recommendation_history(account_id, asin)
        current_price = _COLLECTOR.get_current_price(
            account_id, asin, fallback=meta.get("current_price") or meta.get("floor_price"))
    else:
        # local SQLite fallback (module1_db) — unchanged behaviour
        conn = get_db()
        meta = conn.execute(
            "SELECT * FROM managed_asins WHERE account_id=? AND asin=?",
            (account_id, asin)).fetchone()
        if not meta:
            conn.close()
            return redirect("/")
        meta = dict(meta)
        rank_rows = conn.execute("""
            SELECT substr(captured_at,1,10) AS day,
                   root_rank, sub_rank, MAX(captured_at) AS latest
            FROM rank_history
            WHERE account_id=? AND asin=? AND captured_at>=?
            GROUP BY substr(captured_at,1,10)
            ORDER BY day ASC
        """, (account_id, asin, since)).fetchall()
        latest = conn.execute("""
            SELECT root_rank, sub_rank FROM rank_history
            WHERE account_id=? AND asin=? ORDER BY captured_at DESC LIMIT 1
        """, (account_id, asin)).fetchone()
        conn.close()
        daily = get_daily_units(account_id, asin, days=31)
        hist_all = get_bsr_history_import(account_id, asin)
        price_changes = get_price_changes(account_id, asin)
        rec_history = get_recommendation_history(account_id, asin)
        current_price = get_current_price(
            account_id, asin, fallback=meta.get("current_price") or meta["floor_price"])

    rank_by_day = {r["day"]: (r["root_rank"] or r["sub_rank"]) for r in rank_rows}

    labels, bsr, units, hist = [], [], [], []
    for i in range(29, -1, -1):
        d = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        labels.append(d[5:])
        bsr.append(rank_by_day.get(d))
        units.append(daily.get(d, 0))
        hist.append(hist_all.get(d))

    days7 = [(datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(1, 8)]
    return render_template_string(
        PRODUCT_HTML,
        sku=meta.get("sku") or asin, brand=meta.get("brand") or "",
        asin=asin, account_id=account_id,
        price=current_price,
        root_rank=latest["root_rank"] if latest else None,
        sub_rank=latest["sub_rank"] if latest else None,
        units7=sum(daily.get(d, 0) for d in days7),
        labels=labels, bsr=bsr, units=units, hist=hist,
        has_history=bool(hist_all),
        history_count=len(hist_all),
        history_from=(min(hist_all) if hist_all else ""),
        price_changes=price_changes,
        rec_history=rec_history,
    )


# ─────────────────────────────────────────────────────────────────────────────
# RECOMMENDATIONS
# ─────────────────────────────────────────────────────────────────────────────

RECS_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Recommendations — BSR Repricer</title>
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c}
 .header{background:#0e5c5b;color:#eafcfb;padding:18px 30px;display:flex;justify-content:space-between;align-items:center}
 .header a{color:#bfe9e7;font-size:13px;text-decoration:none}
 .container{max-width:900px;margin:28px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:24px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
 .card-header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px}
 .sku-name{font-size:18px;font-weight:700}
 .asin-code{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:#8a94a2}
 .signal-badge{padding:5px 12px;border-radius:20px;font-size:11px;font-weight:800;text-transform:uppercase}
 .CONFIRMED_STRONG{background:#e7f6ee;color:#166b3d}
 .CONFIRMED_WEAK{background:#fbe8eb;color:#9e2d3c}
 .DIVERGENT{background:#fbf1dd;color:#8a5906}
 .NEUTRAL,.INSUFFICIENT_DATA{background:#eef1f4;color:#46525f}
 .metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0}
 .metric{background:#f5f7f9;border-radius:8px;padding:12px}
 .metric .label{font-size:10.5px;color:#8a94a2;text-transform:uppercase;letter-spacing:.4px}
 .metric .value{font-size:17px;font-weight:700;font-family:ui-monospace,Menlo,monospace;margin-top:3px}
 .action-box{border-left:4px solid #0e5c5b;padding:14px;background:#f7fbfb;border-radius:4px;margin:14px 0}
 .action-label{font-size:11px;color:#8a94a2;text-transform:uppercase}
 .action-text{font-size:16px;font-weight:700;margin:5px 0;font-family:ui-monospace,Menlo,monospace}
 .reasoning{font-size:13px;color:#5a6472;line-height:1.6}
 .decision-form{margin-top:18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
 .price-input{padding:9px 12px;border:1px solid #dde3e9;border-radius:8px;font-size:14px;width:120px;font-family:ui-monospace,Menlo,monospace}
 .note-input{padding:9px 12px;border:1px solid #dde3e9;border-radius:8px;font-size:14px;flex:1;min-width:180px}
 .btn{padding:9px 18px;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600}
 .btn-approve{background:#1f9d57;color:#fff}.btn-override{background:#0e5c5b;color:#fff}.btn-reject{background:#cf3f52;color:#fff}
 .btn:hover{opacity:.9}
 .empty{text-align:center;padding:56px;color:#8a94a2}
 .decided{opacity:.55}
 .decided-label{font-size:13px;color:#166b3d;font-weight:600;margin-top:12px}
 .time-label{font-size:12px;color:#aab2bd}
 .bounds{font-size:11.5px;color:#aab2bd;font-family:ui-monospace,Menlo,monospace}
</style></head><body>
{{ nav|safe }}
<div class="container">
  {% if not recs %}
  <div class="card"><div class="empty">No recommendations yet.<br>
    <small>Run <code>python module1_job.py</code> to generate today's.</small></div></div>
  {% endif %}
  {% for r in recs %}
  <div class="card {% if r.status != 'pending' %}decided{% endif %}">
    <div class="card-header">
      <div><div class="sku-name">{{ r.sku or r.asin }}
        {% if r.brand %}<span style="font-weight:400;color:#8a94a2;font-size:14px;"> · {{ r.brand }}</span>{% endif %}</div>
        <div class="asin-code">{{ r.asin }} · <span class="time-label">{{ r.created_at[:16] }} UTC</span></div>
      </div>
      <span class="signal-badge {{ r.signal_state }}">{{ r.signal_state.replace('_',' ') }}</span>
    </div>
    <div class="metrics">
      <div class="metric"><div class="label">Current price</div><div class="value">£{{ "%.2f"|format(r.current_price or 0) }}</div></div>
      <div class="metric"><div class="label">Root BSR</div><div class="value">{{ "{:,}".format(r.current_root_rank) if r.current_root_rank else "—" }}</div></div>
      <div class="metric"><div class="label">Sub BSR</div><div class="value">{{ "{:,}".format(r.current_sub_rank) if r.current_sub_rank else "—" }}</div></div>
      <div class="metric"><div class="label">7-day units</div><div class="value">{{ r.current_velocity if r.current_velocity is not none else "—" }}</div></div>
    </div>
    <div class="action-box">
      <div class="action-label">Recommendation</div>
      <div class="action-text">
        {{ "↑" if r.recommended_action=="RAISE" else ("↓" if r.recommended_action=="LOWER" else "→") }}
        {{ r.recommended_action }}{% if r.recommended_price %} → £{{ "%.2f"|format(r.recommended_price) }}{% endif %}
      </div>
      <div class="reasoning">{{ r.reasoning }}</div>
    </div>
    {% if r.status == 'pending' %}
    <form class="decision-form" method="POST" action="/recommendations/{{ r.id }}/decide">
      <input type="hidden" name="account_id" value="{{ r.account_id }}">
      <input type="hidden" name="asin" value="{{ r.asin }}">
      <input type="hidden" name="old_price" value="{{ r.current_price }}">
      <input class="price-input" type="number" name="decided_price" step="0.01"
             placeholder="£ price" value="{{ r.recommended_price or '' }}">
      <input class="note-input" type="text" name="decided_note" placeholder="Optional note...">
      <button class="btn btn-approve"  name="decision" value="approved">Approve</button>
      <button class="btn btn-override" name="decision" value="overridden">Override</button>
      <button class="btn btn-reject"   name="decision" value="rejected">Reject</button>
      {% if r.floor_price and r.ceiling_price %}<span class="bounds">floor {{ "%.2f"|format(r.floor_price) }} · ceiling {{ "%.2f"|format(r.ceiling_price) }}</span>{% endif %}
    </form>
    {% else %}
    <div class="decided-label">{{ r.status|upper }}
      {% if r.decided_price %} → £{{ "%.2f"|format(r.decided_price) }}{% endif %}
      {% if r.decided_note %} · {{ r.decided_note }}{% endif %}
      <span class="time-label"> {{ r.decided_at[:16] if r.decided_at else "" }} UTC</span></div>
    {% endif %}
  </div>
  {% endfor %}
</div></body></html>
"""


@app.route("/recommendations")
def recommendations_page():
    # Only pending items need attention. Full history lives on each product page.
    recs = (_COLLECTOR.get_pending_recommendations() if _COLLECTOR is not None
            else get_pending_recommendations())
    return render_template_string(RECS_HTML, recs=recs)


@app.route("/recommendations/<int:rec_id>/decide", methods=["POST"])
def decide_rec(rec_id):
    r = _readonly_block()
    if r:
        return r
    data          = request.form
    decision      = data.get("decision")
    decided_price = float(data["decided_price"]) if data.get("decided_price") else None
    decided_note  = data.get("decided_note", "")
    account_id    = data.get("account_id")
    asin          = data.get("asin")
    old_price     = float(data.get("old_price") or 0)
    decide_recommendation(rec_id, decision, decided_price, decided_note,
                          account_id, asin, old_price)
    return redirect("/recommendations")


@app.route("/recommendations/<int:rec_id>/approve")
def quick_approve(rec_id):
    """One-click approve (from email) at the recommended price."""
    _r = _readonly_block()
    if _r:
        return _r
    rec = get_recommendation(rec_id)
    if rec and rec["status"] == "pending" and rec.get("recommended_price"):
        decide_recommendation(rec_id, "approved", rec["recommended_price"],
                              "Approved via email link",
                              rec["account_id"], rec["asin"], rec["current_price"])
    return redirect("/recommendations")


@app.route("/recommendations/<int:rec_id>/reject")
def quick_reject(rec_id):
    _r = _readonly_block()
    if _r:
        return _r
    rec = get_recommendation(rec_id)
    if rec and rec["status"] == "pending":
        decide_recommendation(rec_id, "rejected", None, "Rejected via email link",
                              rec["account_id"], rec["asin"], rec["current_price"])
    return redirect("/recommendations")


# ─────────────────────────────────────────────────────────────────────────────
# MARKET BASELINE UPLOAD  (the "where do I add competitor data" page)
# ─────────────────────────────────────────────────────────────────────────────

BASELINE_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Market baseline — BSR Repricer</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .header{background:#0e5c5b;color:#eafcfb;padding:18px 30px;display:flex;justify-content:space-between;align-items:center}
 .header a{color:#bfe9e7;font-size:13px;text-decoration:none}
 .wrap{max-width:760px;margin:28px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:24px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
 .muted{color:#5a6472;font-size:13px;line-height:1.6}
 code{background:#f5f7f9;padding:1px 5px;border-radius:4px;font-size:12px}
 input[type=file]{margin:14px 0}
 .btn{background:#0e5c5b;color:#eafcfb;padding:10px 18px;border:none;border-radius:8px;font-weight:600;cursor:pointer}
 table{border-collapse:collapse;width:100%;margin-top:10px;font-size:13px}
 th,td{border:1px solid #dde3e9;padding:6px 9px;text-align:left}
 th{background:#f5f7f9}
 .flash{background:#e7f6ee;color:#166b3d;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:600}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}
  <div class="card">
    <div class="muted">
      Upload a one-off CSV of stable, consistently top-ranked competitor ASINs so the tool
      can tell seasonal market movement from your own. It feeds the "vs. market baseline"
      figure. Load it once; retire it once you have your own history. Re-uploading a
      (category, month) replaces it.
      <br><br><b>Required columns:</b>
      <table>
        <tr><th>category</th><th>month</th><th>avg_rank</th><th>avg_monthly_units</th><th>source_note</th></tr>
        <tr><td>Home &amp; Kitchen</td><td>2026-06</td><td>4200</td><td>640</td><td>5-ASIN cohort avg</td></tr>
        <tr><td>Home &amp; Kitchen</td><td>2026-07</td><td>3950</td><td>710</td><td>5-ASIN cohort avg</td></tr>
      </table>
      <br><code>category</code> must match your ASIN's root category exactly (e.g. "Home &amp; Kitchen").
      <code>month</code> is <code>YYYY-MM</code>.
    </div>
    <form method="POST" action="/baseline" enctype="multipart/form-data">
      <input type="file" name="csv" accept=".csv" required><br>
      <button class="btn" type="submit">Upload baseline</button>
    </form>
  </div>
  <div class="card">
    <div class="muted"><b>Currently loaded:</b>
    {% if rows %}
      <table><tr><th>Category</th><th>Month</th><th>Avg rank</th><th>Avg monthly units</th></tr>
      {% for r in rows %}<tr><td>{{ r.category }}</td><td>{{ r.month }}</td><td>{{ r.avg_rank }}</td><td>{{ r.avg_monthly_units }}</td></tr>{% endfor %}
      </table>
    {% else %} none yet.{% endif %}
    </div>
  </div>
</div></body></html>
"""


@app.route("/baseline", methods=["GET", "POST"])
def baseline_page():
    # Read-only mode: the collector owns market_baseline and collector_ro doesn't
    # map it, so querying local SQLite here would 500 (no such table). Link out.
    if _COLLECTOR is not None:
        return render_template_string(
            MANAGE_HTML, title="Market baseline",
            message="Competitor baseline data is uploaded to and stored on the "
                    "collector. This dashboard reads the collector read-only.",
            collector_url=COLLECTOR_STATUS_URL)
    if request.method == "POST":
        r = _readonly_block()
        if r:
            return r
        f = request.files.get("csv")
        if f and f.filename:
            fd, tmp = tempfile.mkstemp(suffix=".csv")
            os.close(fd)
            f.save(tmp)
            try:
                n = import_baseline_csv(tmp)
                flash(f"Imported {n} baseline row(s).")
            except Exception as e:
                flash(f"Import failed: {e}")
            finally:
                os.remove(tmp)
        return redirect("/baseline")

    conn = get_db()
    rows = conn.execute(
        "SELECT category, month, avg_rank, avg_monthly_units FROM market_baseline "
        "ORDER BY category, month"
    ).fetchall()
    conn.close()
    return render_template_string(BASELINE_HTML, rows=[dict(r) for r in rows])



# ─────────────────────────────────────────────────────────────────────────────
# PRODUCTS — add (fast) / list / pause
# ─────────────────────────────────────────────────────────────────────────────

PRODUCTS_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Products — BSR Repricer</title>
<style>
 *{box-sizing:border-box;margin:0;padding:0}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c}
 .header{background:#0e5c5b;color:#eafcfb;padding:18px 30px;display:flex;justify-content:space-between}
 .header a{color:#bfe9e7;font-size:13px;text-decoration:none;margin-left:16px}
 .wrap{max-width:960px;margin:24px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:22px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 .title{font-size:16px;font-weight:700;margin-bottom:12px}
 label{display:block;font-size:12px;color:#5a6472;margin:8px 0 3px}
 input,select{width:100%;padding:9px 11px;border:1px solid #dde3e9;border-radius:8px;font-size:14px}
 .grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
 .btn{margin-top:14px;background:#0e5c5b;color:#eafcfb;border:none;border-radius:8px;padding:10px 18px;font-weight:600;cursor:pointer}
 table{width:100%;border-collapse:collapse;font-size:14px}
 th,td{padding:9px;text-align:left;border-top:1px solid #eef1f4}
 th{color:#8a94a2;font-size:12px}
 .flash{background:#e7f6ee;color:#166b3d;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:600}
 a.edit{color:#0e5c5b;text-decoration:none;font-weight:600}
 .muted{color:#8a94a2}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}
  <div class="card">
    <div class="title">Add a product</div>
    {% if collector_mode %}<div class="muted">The watchlist is managed on the collector — this dashboard is read-only. <a class="edit" href="{{ collector_url }}" target="_blank" rel="noopener">Add or edit products on the collector &#8599;</a></div>
    {% elif not accounts %}<div class="muted">Add a seller account first on the <a href="/accounts">Accounts</a> page.</div>{% else %}
    <form method="POST" action="/products/add">
      <div style="font-size:11px;color:#8a94a2;text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px;">Amazon fills this from the ASIN</div>
      <div class="grid">
        <div><label>Account</label><select name="account_id">{% for a in accounts %}<option>{{ a.account_id }}</option>{% endfor %}</select></div>
        <div><label>ASIN</label><input name="asin" required placeholder="B0..."></div>
        <div><label>SKU (optional — yours)</label><input name="sku" placeholder="your SKU"></div>
      </div>
      <div style="font-size:11px;color:#0e5c5b;text-transform:uppercase;letter-spacing:.5px;margin:16px 0 4px;">You tell us — economics &amp; strategy</div>
      <div class="grid">
        <div><label>Current price (£)</label><input name="current_price" type="number" step="0.01" required></div>
        <div><label>COGS — unit cost (£)</label><input name="cogs" type="number" step="0.01"></div>
        <div><label>Postage per unit (£)</label><input name="postage" type="number" step="0.01"></div>
        <div><label>Floor (£)</label><input name="floor_price" type="number" step="0.01" placeholder="11.75"></div>
        <div><label>Ceiling (£)</label><input name="ceiling_price" type="number" step="0.01" placeholder="16.99"></div>
        <div><label>Target BSR</label><input name="target_bsr" type="number" placeholder="e.g. 5000"></div>
      </div>
      <button class="btn" type="submit">Add product</button>
      <span class="muted" style="margin-left:10px;font-size:12px;">Brand, title &amp; category auto-fetched. VAT, step, ACOS &amp; thresholds fine-tuned via Edit.</span>
    </form>{% endif %}
  </div>
  <div class="card">
    <div class="title">Tracked products</div>
    <table>
      <tr><th>Product</th><th>ASIN</th><th>Account</th><th>Price</th><th>Floor/Ceiling</th><th>Status</th><th></th></tr>
      {% for p in products %}
      <tr>
        <td><b>{{ p.sku or p.asin }}</b><br><span class="muted">{{ p.brand }}</span></td>
        <td style="font-family:ui-monospace,Menlo,monospace;">{{ p.asin }}</td>
        <td>{{ p.account_id }}</td>
        <td>£{{ "%.2f"|format(p.current_price) if p.current_price else "—" }}</td>
        <td>{{ "%.2f"|format(p.floor_price) }} / {{ "%.2f"|format(p.ceiling_price) }}</td>
        <td>{% if p.active %}active{% else %}<span class="muted">paused</span>{% endif %}</td>
        <td>
          {% if collector_mode %}<span class="muted">read-only</span>
          {% else %}<a class="edit" href="/products/{{ p.account_id }}/{{ p.asin }}/edit">Edit</a> ·
          <a class="edit" href="/products/{{ p.account_id }}/{{ p.asin }}/toggle">{{ "Pause" if p.active else "Resume" }}</a>{% endif %}
        </td>
      </tr>
      {% else %}<tr><td colspan="7" class="muted">None yet.</td></tr>{% endfor %}
    </table>
  </div>
</div></body></html>
"""

EDIT_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Edit {{ p.sku or p.asin }}</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .header{background:#0e5c5b;color:#eafcfb;padding:18px 30px}
 .header a{color:#bfe9e7;font-size:13px;text-decoration:none;margin-left:16px}
 .wrap{max-width:720px;margin:24px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:24px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
 label{display:block;font-size:12px;color:#5a6472;margin:10px 0 3px}
 input{width:100%;padding:9px 11px;border:1px solid #dde3e9;border-radius:8px;font-size:14px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
 .btn{margin-top:16px;background:#0e5c5b;color:#eafcfb;border:none;border-radius:8px;padding:10px 20px;font-weight:600;cursor:pointer}
 .hint{font-size:11px;color:#8a94a2}
</style></head><body>
<div class="header"><b>Edit {{ p.sku or p.asin }}</b> <a href="/products">← Products</a></div>
<div class="wrap"><div class="card">
<form method="POST" action="/products/{{ p.account_id }}/{{ p.asin }}/edit">
  <div class="grid">
    <div><label>SKU</label><input name="sku" value="{{ p.sku or '' }}"></div>
    <div><label>Brand</label><input name="brand" value="{{ p.brand or '' }}"></div>
    <div><label>Root category</label><input name="root_category" value="{{ p.root_category or '' }}"></div>
    <div><label>Current price (£)</label><input name="current_price" type="number" step="0.01" value="{{ p.current_price }}"></div>
    <div><label>COGS — unit cost (£)</label><input name="cogs" type="number" step="0.01" value="{{ p.cogs if p.cogs is not none else '' }}"></div>
    <div><label>Postage per unit (£)</label><input name="postage" type="number" step="0.01" value="{{ p.postage if p.postage is not none else '' }}"></div>
    <div><label>VAT rate</label><input name="vat_rate" type="number" step="0.01" value="{{ p.vat_rate if p.vat_rate is not none else '0.20' }}"><span class="hint">0.20 = 20%</span></div>
    <div><label>Target BSR</label><input name="target_bsr" type="number" value="{{ p.target_bsr if p.target_bsr is not none else '' }}"><span class="hint">where you're driving rank</span></div>
    <div><label>Target ACOS %</label><input name="target_acos" type="number" step="0.1" value="{{ p.target_acos if p.target_acos is not none else '' }}"><span class="hint">for Module 3 (ads)</span></div>
    <div><label>Floor (£)</label><input name="floor_price" type="number" step="0.01" value="{{ p.floor_price }}"></div>
    <div><label>Ceiling (£)</label><input name="ceiling_price" type="number" step="0.01" value="{{ p.ceiling_price }}"></div>
    <div><label>Step %</label><input name="step_pct" type="number" step="0.01" value="{{ p.step_pct }}"><span class="hint">0.05 = 5%</span></div>
    <div><label>Raise below (rank)</label><input name="raise_below" type="number" value="{{ p.raise_below }}"></div>
    <div><label>Lower above (rank)</label><input name="lower_above" type="number" value="{{ p.lower_above }}"></div>
    <div><label>Confirm days</label><input name="min_confirm_days" type="number" value="{{ p.min_confirm_days }}"></div>
    <div><label>Cooldown days</label><input name="cooldown_days" type="number" value="{{ p.cooldown_days }}"></div>
  </div>
  <button class="btn" type="submit">Save changes</button>
</form>
</div></div></body></html>
"""

ACCOUNTS_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Accounts — BSR Repricer</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .header{background:#0e5c5b;color:#eafcfb;padding:18px 30px}
 .header a{color:#bfe9e7;font-size:13px;text-decoration:none;margin-left:16px}
 .wrap{max-width:760px;margin:24px auto;padding:0 20px}
 .card{background:#fff;border-radius:12px;padding:24px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 label{display:block;font-size:12px;color:#5a6472;margin:10px 0 3px}
 input{width:100%;padding:9px 11px;border:1px solid #dde3e9;border-radius:8px;font-size:14px}
 .btn{margin-top:14px;background:#0e5c5b;color:#eafcfb;border:none;border-radius:8px;padding:10px 20px;font-weight:600;cursor:pointer}
 table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:9px;text-align:left;border-top:1px solid #eef1f4}th{color:#8a94a2;font-size:12px}
 .flash{background:#e7f6ee;color:#166b3d;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:600}
 .hint{font-size:11px;color:#8a94a2}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}
  <div class="card">
    <div style="font-weight:700;margin-bottom:8px;">Add / update a seller account</div>
    <div class="hint">Get the refresh token yourself via Seller Central → Develop Apps → Authorise, then paste it here. Leave it blank when editing to keep the existing one.</div>
    <form method="POST" action="/accounts/add">
      <label>Account label (e.g. M4Mart_UK)</label><input name="account_id" required>
      <label>Seller ID</label><input name="seller_id">
      <label>Marketplace ID</label><input name="marketplace_id" value="A1F83G8C2ARO7P">
      <label>Refresh token (Atzr|...)</label><input name="refresh_token" placeholder="paste the token you generated">
      <button class="btn" type="submit">Save account</button>
    </form>
  </div>
  <div class="card">
    <table><tr><th>Account</th><th>Seller ID</th><th>Marketplace</th><th>Token</th></tr>
    {% for a in accounts %}<tr><td><b>{{ a.account_id }}</b></td><td>{{ a.seller_id or '—' }}</td><td>{{ a.marketplace_id }}</td>
      <td>{{ 'set' if a.refresh_token else 'missing' }}</td></tr>
    {% else %}<tr><td colspan="4" style="color:#8a94a2;">None yet.</td></tr>{% endfor %}</table>
  </div>
</div></body></html>
"""


@app.route("/products")
def products_page():
    if _COLLECTOR is not None:
        return render_template_string(PRODUCTS_HTML,
            products=_COLLECTOR.list_products(), accounts=_COLLECTOR.list_accounts())
    return render_template_string(PRODUCTS_HTML,
        products=get_managed_asins(), accounts=get_accounts())


def _num_or_none(v):
    try:
        return float(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None

def _int_or_none(v):
    try:
        return int(v) if v not in (None, "") else None
    except (ValueError, TypeError):
        return None

@app.route("/products/add", methods=["POST"])
def products_add():
    r = _readonly_block()
    if r:
        return r
    f = request.form
    asin = f["asin"].strip()
    account_id = f["account_id"]
    # Try to auto-fetch brand/title/category from Amazon (best-effort).
    ident = {"brand": "", "title": "", "root_category": ""}
    try:
        import json as _json
        with open("config.json") as _cf:
            _cfg = _json.load(_cf)
        acct = next((a for a in _cfg.get("accounts", []) if a["account_id"] == account_id), None)
        db_accts = {a["account_id"]: a for a in get_accounts()}
        if account_id in db_accts and db_accts[account_id].get("refresh_token"):
            acct = {**(acct or {}), **db_accts[account_id]}
        if acct and acct.get("refresh_token"):
            from module1_collector import make_credentials, get_marketplace, fetch_identity
            creds = make_credentials(_cfg, acct)
            mk = get_marketplace(acct.get("marketplace_id", "A1F83G8C2ARO7P"))
            ident = fetch_identity(asin, creds, mk) or ident
    except Exception as e:
        app.logger.warning(f"identity fetch skipped: {e}")

    try:
        upsert_managed_asin(
            account_id=account_id, asin=asin,
            sku=f.get("sku") or None,
            brand=ident.get("brand") or None,
            title=ident.get("title") or None,
            root_category=ident.get("root_category") or "Home & Kitchen",
            current_price=float(f["current_price"]),
            cogs=_num_or_none(f.get("cogs")),
            postage=_num_or_none(f.get("postage")),
            target_bsr=_int_or_none(f.get("target_bsr")),
            floor_price=_num_or_none(f.get("floor_price")) or 11.75,
            ceiling_price=_num_or_none(f.get("ceiling_price")) or 16.99)
        nm = ident.get("brand") or asin
        flash(f"Added {asin} ({nm}). Identity auto-fetched where available; fine-tune via Edit.")
    except Exception as e:
        flash(f"Could not add: {e}")
    return redirect("/products")


@app.route("/products/<account_id>/<asin>/edit", methods=["GET", "POST"])
def products_edit(account_id, asin):
    r = _readonly_block()
    if r:
        return r
    p = get_asin(account_id, asin)
    if not p:
        return redirect("/products")
    if request.method == "POST":
        f = request.form
        upsert_managed_asin(
            account_id=account_id, asin=asin,
            sku=f.get("sku"), brand=f.get("brand"),
            root_category=f.get("root_category"),
            current_price=float(f["current_price"]),
            cogs=_num_or_none(f.get("cogs")),
            postage=_num_or_none(f.get("postage")),
            vat_rate=_num_or_none(f.get("vat_rate")) or 0.20,
            target_bsr=_int_or_none(f.get("target_bsr")),
            target_acos=_num_or_none(f.get("target_acos")),
            floor_price=float(f["floor_price"]), ceiling_price=float(f["ceiling_price"]),
            step_pct=float(f["step_pct"]), raise_below=int(f["raise_below"]),
            lower_above=int(f["lower_above"]),
            min_confirm_days=int(f["min_confirm_days"]),
            cooldown_days=int(f["cooldown_days"]))
        flash("Saved.")
        return redirect("/products")
    return render_template_string(EDIT_HTML, p=p)


@app.route("/products/<account_id>/<asin>/toggle")
def products_toggle(account_id, asin):
    r = _readonly_block()
    if r:
        return r
    p = get_asin(account_id, asin)
    if p:
        set_asin_active(account_id, asin, not p["active"])
    return redirect("/products")


@app.route("/accounts")
def accounts_page():
    # On Railway the collector is read-only and has no local `accounts` table
    # (this is what used to 500). Account management lives on the collector, so
    # link out instead of querying SQLite.
    if _COLLECTOR is not None:
        return render_template_string(
            MANAGE_HTML, title="Accounts",
            message="Seller accounts are configured on the collector. This dashboard "
                    "reads the collector read-only, so account changes are made there.",
            collector_url=COLLECTOR_STATUS_URL)
    return render_template_string(ACCOUNTS_HTML, accounts=get_accounts())


@app.route("/accounts/add", methods=["POST"])
def accounts_add():
    r = _readonly_block()
    if r:
        return r
    f = request.form
    upsert_account(f["account_id"].strip(), f.get("seller_id"),
                   f.get("marketplace_id") or "A1F83G8C2ARO7P",
                   f.get("refresh_token") or None)
    flash(f"Saved account {f['account_id'].strip()}.")
    return redirect("/accounts")



@app.route("/product/<account_id>/<asin>/import-bsr", methods=["POST"])
def product_import_bsr(account_id, asin):
    r = _readonly_block()
    if r:
        return r
    f = request.files.get("csv")
    if f and f.filename:
        import tempfile, os
        fd, tmp = tempfile.mkstemp(suffix=".csv"); os.close(fd); f.save(tmp)
        try:
            n = import_bsr_history_csv(account_id, asin, tmp)
            flash(f"Imported {n} historic BSR rows.")
        except Exception as e:
            flash(f"Import failed: {e}")
        finally:
            os.remove(tmp)
    return redirect(f"/product/{account_id}/{asin}")

