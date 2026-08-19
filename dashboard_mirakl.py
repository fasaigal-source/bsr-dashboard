"""dashboard_mirakl.py — Mirakl connection test + status ("/mirakl/test").

Runs the READ-ONLY smoke test (GET /api/shops) for the Tesco + B&Q accounts so you can
confirm the API keys/URLs are live BEFORE un-gating writes. Creds come from the
MIRAKL_ACCOUNTS env var (see mirakl_client._load_config). Writes stay DRY-RUN until you set
MIRAKL_DRY_RUN=0 — this page never writes to Mirakl.
"""
import logging

from flask import render_template_string

from dashboard_app import app
import mirakl_client

log = logging.getLogger(__name__)
ACCOUNTS = [("tesco", "Tesco"), ("bandq", "B&Q")]


PAGE = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Mirakl — connection</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:820px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:16px 18px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:14px}
 h2{margin:0 0 4px;font-size:18px} .muted{color:#8a94a2;font-size:12.5px}
 .row{display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-top:1px solid #eef1f4}
 .pill{display:inline-block;font-size:11px;padding:2px 9px;border-radius:10px}
 .ok{background:#e4f6ec;color:#1f7a45}.no{background:#fcebeb;color:#a32d2d}.warn{background:#fbe7c6;color:#8a5906}
 code{background:#f2f4f2;padding:1px 6px;border-radius:4px}</style></head><body>{{ nav|safe }}
<div class="wrap">
  <div class="card"><h2>Mirakl — connection test</h2>
    <div class="muted">Read-only probes (<code>/api/version</code>, <code>/api/account</code>, <code>/api/shops</code>) per account. Writes are
      <b>{{ 'DRY-RUN' if dry_run else 'LIVE' }}</b> (set <code>MIRAKL_DRY_RUN=0</code> to enable writes once these pass).</div>
  </div>
  <div class="card">
    {% for r in results %}
    <div class="row">
      <div><b>{{ r.label }}</b> <span class="muted">({{ r.key }})</span>
        {% if r.shop_name %}<br><span class="muted">shop: {{ r.shop_name }}</span>{% endif %}
        {% if r.probes %}<br><span class="muted">{% for p in r.probes %}<code>{{ p.endpoint }}→{{ p.status or 'err' }}</code>{% if not loop.last %} {% endif %}{% endfor %}</span>{% endif %}
        {% if r.detail and not r.ok %}<br><span class="muted">{{ r.detail }}</span>{% endif %}</div>
      <div style="text-align:right">
        {% if not r.configured %}<span class="pill warn">not configured</span>
        {% elif r.ok %}<span class="pill ok">connected{% if r.status %} · {{ r.status }}{% endif %}</span>
          {% if r.sandbox %}<br><span class="pill warn" style="margin-top:4px">looks like sandbox</span>{% endif %}
        {% else %}<span class="pill no">failed{% if r.status %} · {{ r.status }}{% endif %}</span>{% endif %}
      </div>
    </div>
    {% endfor %}
  </div>
  <div class="card muted">
    Enter keys on the <a href="/settings">Settings → Channels</a> page (Tesco / B&amp;Q cards).
    <br><br><b>All three endpoints 403?</b> The key reached Mirakl but was refused everywhere — usually the operator
    <b>IP-allowlists</b> API traffic and this server's IP isn't registered, or the key/role has no API access. Ask the
    operator to allowlist the app's outbound IP (or check the key's permissions).
    <br><b>Only <code>/api/shops</code> 403 but the others 200?</b> The key is valid — <code>/api/shops</code> is just
    operator-scoped; you're connected. Once a probe returns 200, set <code>MIRAKL_DRY_RUN=0</code> to go live.
  </div>
</div></body></html>
"""


@app.route("/mirakl/test")
def mirakl_test():
    results = []
    for key, label in ACCOUNTS:
        creds = mirakl_client.creds_for(key)
        if not creds:
            results.append(dict(key=key, label=label, configured=False, ok=False, status=None,
                                sandbox=False, shop_name=None,
                                detail="not configured — add to MIRAKL_ACCOUNTS"))
            continue
        try:
            r = mirakl_client.smoke_test(key)
        except Exception as e:
            r = {"ok": False, "status": None, "sandbox": False, "shop": None,
                 "shop_name": None, "detail": str(e)[:300], "probes": []}
        results.append(dict(key=key, label=label, configured=True, ok=r.get("ok"),
                            status=r.get("status"), sandbox=r.get("sandbox"),
                            shop_name=r.get("shop_name"), detail=r.get("detail"),
                            probes=r.get("probes") or []))
    return render_template_string(PAGE, results=results, dry_run=mirakl_client.is_dry_run())
