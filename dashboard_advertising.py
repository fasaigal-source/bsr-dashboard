"""dashboard_advertising.py — /advertising: a hub for ad channels (Amazon + eBay).

Doesn't replace the PPC tooling — it summarises the last 30 days of Amazon ad spend
(from the ad_spend table) and links out to the existing PPC dashboard / schedule.
eBay Advertising is a placeholder until that channel is connected.
"""
import logging
from datetime import date, timedelta

from flask import render_template_string

from dashboard_app import app
import db

log = logging.getLogger(__name__)


def _amazon_30d():
    """Trailing-30-day Amazon ad totals from ad_spend, if the table exists."""
    try:
        conn = db.connect()
        if not db.table_exists(conn, "ad_spend"):
            return None
        cut = (date.today() - timedelta(days=30)).isoformat()
        r = conn.execute(
            "SELECT COALESCE(SUM(spend),0) AS spend, COALESCE(SUM(ad_sales),0) AS sales, "
            "COALESCE(SUM(clicks),0) AS clicks, COALESCE(SUM(orders),0) AS orders "
            "FROM ad_spend WHERE date >= ?", (cut,)).fetchone()
        d = dict(r) if r else {}
        spend = float(d.get("spend") or 0)
        sales = float(d.get("sales") or 0)
        d["acos"] = (spend / sales * 100) if sales else None
        return d
    except Exception as e:
        log.warning("advertising amazon summary failed: %s", e)
        return None


PAGE = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Advertising — BSR Repricer</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:940px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 h2{margin:0 0 2px;font-size:19px} .muted{color:#8a94a2;font-size:12.5px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px}
 .ch{border:1px solid #e4e8ec;border-radius:12px;padding:16px 18px;background:#fff}
 .ch.soon{opacity:.6}
 .top{display:flex;align-items:center;gap:10px;margin-bottom:10px}
 .logo{width:34px;height:34px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff}
 .nm{font-weight:700;font-size:15px}
 .kpis{display:flex;gap:20px;margin:10px 0}
 .kpi b{display:block;font-size:19px} .kpi span{font-size:11px;color:#8a94a2}
 .badge{font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px}
 .b-on{background:#e4f6ea;color:#1f7a45} .b-soon{background:#eef2ff;color:#5566cc}
 .btn{background:#0e5c5b;color:#fff;border-radius:7px;padding:7px 12px;font-size:12.5px;text-decoration:none;display:inline-block;margin-top:6px}
 .btn.sec{background:#eef1f4;color:#0e5c5b}
</style></head><body>{{ nav|safe }}
<div class="wrap">
  <div class="card"><h2>Advertising</h2><div class="muted">Ad channels in one place. Amazon summary is last 30 days; open the PPC dashboard for full detail.</div></div>
  <div class="grid">
    <div class="ch">
      <div class="top"><div class="logo" style="background:#ff9900">a</div><div class="nm">Amazon Advertising</div></div>
      <span class="badge b-on">● Connected</span>
      {% if amz %}
      <div class="kpis">
        <div class="kpi"><b>£{{ '%.0f'|format(amz.spend|float) }}</b><span>spend 30d</span></div>
        <div class="kpi"><b>£{{ '%.0f'|format(amz.sales|float) }}</b><span>ad sales 30d</span></div>
        <div class="kpi"><b>{% if amz.acos is not none %}{{ '%.0f'|format(amz.acos) }}%{% else %}—{% endif %}</b><span>ACOS</span></div>
      </div>
      {% else %}<div class="muted" style="margin:10px 0">No ad-spend data yet — upload a report on the PPC page.</div>{% endif %}
      <div><a class="btn" href="/ppc">Open PPC dashboard</a> <a class="btn sec" href="/ppc/schedule">Schedule</a></div>
    </div>

    <div class="ch soon">
      <div class="top"><div class="logo" style="background:#e53238">e</div><div class="nm">eBay Advertising</div></div>
      <span class="badge b-soon">Coming soon</span>
      <div class="muted" style="margin-top:10px">Promoted Listings performance will appear here once eBay is connected.</div>
    </div>
  </div>
</div></body></html>
"""


@app.route("/advertising")
def advertising_page():
    return render_template_string(PAGE, amz=_amazon_30d())
