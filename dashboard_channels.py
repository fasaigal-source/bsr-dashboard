"""dashboard_channels.py — one read-only multi-channel overview ("/channels").

The single place to see what's going on across every sales channel. It reads the
shared canonical pl_line_items table GROUPED BY `channel`, so any new channel
(eBay, TikTok, Etsy, …) appears automatically the moment its orders land with its
own channel tag — no page change needed. Plus a per-integration status strip
(configured? dry-run? last activity?) built from a small registry.

Strictly read-only: no writes to any channel, no credentials handled here.
"""
import os
import logging

import db
from dashboard_app import app
from flask import render_template_string

log = logging.getLogger(__name__)
DB_PATH = "bsr_history.db"

# Channels we know about. "live" ones read pl_line_items automatically; "planned"
# ones are shown as roadmap so the overview reflects the whole business direction.
LIVE_CHANNELS = {
    "amazon": "Amazon",
    "mirakl": "Mirakl — Tesco + B&Q",
}
PLANNED_CHANNELS = ["eBay", "TikTok Shop", "Etsy"]


def _conn():
    return db.connect(DB_PATH)


def channel_summary():
    """Per-channel totals from the canonical order table. Extensible for free —
    grouping on `channel` picks up any future channel with zero code change."""
    conn = _conn()
    try:
        rows = conn.execute("""
            SELECT COALESCE(NULLIF(channel,''),'amazon') AS channel,
                   COUNT(DISTINCT account_id || '|' || order_id) AS orders,
                   COALESCE(SUM(quantity),0) AS units,
                   ROUND(COALESCE(SUM(sale_price_exvat),0),2) AS revenue_exvat,
                   ROUND(COALESCE(SUM(net_profit),0),2) AS net_profit,
                   SUM(CASE WHEN net_profit IS NULL THEN 1 ELSE 0 END) AS provisional_lines,
                   COUNT(*) AS lines,
                   MAX(posted_date) AS last_order
            FROM pl_line_items
            GROUP BY COALESCE(NULLIF(channel,''),'amazon')
        """).fetchall()
    except Exception as e:
        log.warning("channel_summary failed: %s", e)
        rows = []
    conn.close()
    return {r["channel"]: dict(r) for r in rows}


def _amazon_status():
    conn = _conn()
    try:
        row = conn.execute("SELECT MAX(latest_synced) AS m FROM pl_sync_state").fetchone()
        last = row["m"] if row else None
    except Exception:
        last = None
    conn.close()
    return {"label": "Amazon (SP-API)", "accounts": "M4Mart_UK + Nod Off",
            "connected": True, "mode": "live read", "last_activity": last, "note": "settlement P&L"}


def _mirakl_status():
    try:
        import mirakl_client
        import mirakl_db
    except Exception:
        return {"label": "Mirakl (Tesco + B&Q)", "accounts": "tesco, bandq",
                "connected": False, "mode": "not built", "last_activity": None, "note": ""}
    configured = [a for a in mirakl_db.MIRAKL_ACCOUNTS if mirakl_client.creds_for(a)]
    dry = mirakl_client.is_dry_run()
    # order-state + txn counts
    conn = _conn()
    states, txns, last = {}, 0, None
    try:
        for r in conn.execute("SELECT state, COUNT(*) n FROM mirakl_order_state GROUP BY state").fetchall():
            states[r["state"] or "?"] = r["n"]
        txns = conn.execute("SELECT COUNT(*) n FROM mirakl_transactions").fetchone()["n"]
        row = conn.execute("SELECT MAX(updated_at) m FROM mirakl_order_state").fetchone()
        last = row["m"] if row else None
    except Exception:
        pass
    conn.close()
    return {"label": "Mirakl (Tesco + B&Q)",
            "accounts": ", ".join(configured) if configured else "none configured",
            "connected": bool(configured),
            "mode": ("dry-run" if dry else "LIVE writes") if configured else "awaiting keys",
            "last_activity": last, "note": f"{txns} txns · states: " +
            (", ".join(f"{k}:{v}" for k, v in states.items()) if states else "—")}


CHANNELS_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Channels — BSR Repricer</title>
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1300px;margin:22px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:20px 22px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
 h2{margin:0 0 4px;font-size:17px} .muted{color:#8a94a2;font-size:13px}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}
 .ch{border:1px solid #e4edec;border-radius:12px;padding:16px 16px;background:#f7fafa}
 .ch h3{margin:0 0 8px;font-size:15px} .ch .big{font-size:24px;font-weight:800;font-family:ui-monospace,Menlo,monospace}
 .kv{display:flex;justify-content:space-between;font-size:13px;padding:2px 0;color:#5a6472}
 .kv b{color:#12161c;font-family:ui-monospace,Menlo,monospace}
 table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
 th,td{padding:8px 9px;text-align:left;border-top:1px solid #eef1f4}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase}
 .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px;vertical-align:0}
 .on{background:#1f9d57}.dry{background:#e0a400}.off{background:#c2c9d1}
 .pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;background:#eef4f4;color:#0e5c5b}
 .prov{color:#8a5906;font-weight:700}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  <div class="card">
    <h2>Channels <span class="muted">— one view of the whole business</span></h2>
    <div class="muted">Every sales channel in one place, from the shared order table. New channels
      (eBay, TikTok, Etsy) appear here automatically once their orders are ingested with a channel tag.</div>
  </div>

  <div class="card">
    <h2 style="font-size:15px;">Live channels</h2>
    <div class="cards" style="margin-top:10px;">
      {% for key, label in live.items() %}
        {% set s = summary.get(key, {}) %}
        <div class="ch">
          <h3>{{ label }}</h3>
          <div class="big">£{{ "%.0f"|format(s.get('revenue_exvat') or 0) }}</div>
          <div class="muted" style="margin-bottom:8px;">ex-VAT revenue (all-time)</div>
          <div class="kv"><span>Orders</span><b>{{ s.get('orders') or 0 }}</b></div>
          <div class="kv"><span>Units</span><b>{{ s.get('units') or 0 }}</b></div>
          <div class="kv"><span>Net profit</span><b>£{{ "%.0f"|format(s.get('net_profit') or 0) }}</b></div>
          {% if s.get('provisional_lines') %}<div class="kv"><span>Provisional lines</span><b class="prov">{{ s.get('provisional_lines') }}</b></div>{% endif %}
          <div class="kv"><span>Last order</span><b>{{ (s.get('last_order') or '—')[:10] }}</b></div>
        </div>
      {% endfor %}
    </div>
    <div class="muted" style="margin-top:10px;">Mirakl net profit shows £0/provisional until its commission-VAT treatment is confirmed — revenue is landed, the P&amp;L formula is deliberately deferred.</div>
  </div>

  <div class="card">
    <h2 style="font-size:15px;">Integration status</h2>
    <table>
      <thead><tr><th>Channel</th><th>Accounts</th><th>Status</th><th>Mode</th><th>Last activity</th><th>Notes</th></tr></thead>
      <tbody>
      {% for st in statuses %}
        <tr>
          <td>{{ st.label }}</td>
          <td>{{ st.accounts }}</td>
          <td><span class="dot {{ 'on' if st.connected and 'dry' not in st.mode and 'await' not in st.mode else ('dry' if st.connected else 'off') }}"></span>{{ 'connected' if st.connected else 'not connected' }}</td>
          <td>{{ st.mode }}</td>
          <td>{{ (st.last_activity or '—')[:16] }}</td>
          <td class="muted">{{ st.note }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h2 style="font-size:15px;">Planned channels</h2>
    <div style="margin-top:8px;">{% for p in planned %}<span class="pill" style="margin-right:6px;">{{ p }}</span>{% endfor %}</div>
    <div class="muted" style="margin-top:8px;">Same pattern: one adapter per marketplace, orders land in the shared table tagged by channel, and they light up on this page — so the whole business stays in one place.</div>
  </div>
</div></body></html>
"""


@app.route("/channels")
def channels_page():
    summary = channel_summary()
    statuses = [_amazon_status(), _mirakl_status()]
    return render_template_string(CHANNELS_HTML, live=LIVE_CHANNELS, planned=PLANNED_CHANNELS,
                                  summary=summary, statuses=statuses)
