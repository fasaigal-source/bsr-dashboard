"""dashboard_ship.py — Module 5a ready-to-ship queue ("/ship").

READ-ONLY this phase: lists unshipped FBM orders (Orders API), and per order resolves each
SKU → canonical → package default, pre-filling weight/dims and prompting inline for anything
missing (saved as that SKU's default for reuse — the catalogue fills itself in as you ship).
No rate quote and no purchase yet — those are M5a-5 / M5a-6.
"""
import logging

from flask import request, redirect, flash, render_template_string

from dashboard_app import app
import pl_cogs
import pl_tracker
import module5_labels_db as m5
import module5_orders

log = logging.getLogger(__name__)


def _cfg_accounts():
    # config.json locally, Railway env vars in production (see load_spapi_config)
    return module5_orders.load_spapi_config()


def _account(accts, account_id):
    for a in accts:
        if a["account_id"] == account_id:
            return a
    return accts[0] if accts else None


def _num(v, cast):
    v = (v or "").strip()
    return cast(v) if v not in ("", None) else None


QUEUE_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ready to ship — BSR Repricer</title>
<style>
 *{box-sizing:border-box} body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1100px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 h2{margin:0 0 4px;font-size:18px} .muted{color:#8a94a2;font-size:13px}
 .bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:10px 0}
 select,input{padding:5px 8px;border-radius:6px;border:1px solid #dfe4e9;font-size:13px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:left;border-top:1px solid #eef1f4}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase}
 .btn{background:#0e5c5b;color:#fff;border:none;border-radius:7px;padding:6px 11px;font-size:13px;cursor:pointer;text-decoration:none}
 .pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;background:#eef4f4;color:#0e5c5b}
 .prime{background:#eaf3ff;color:#1257a3} .warn{background:#fbe7c6;color:#8a5906}
 .err{background:#fcebeb;color:#a32d2d;padding:10px 12px;border-radius:8px;margin:10px 0}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  <div class="card">
    <h2>Ready to ship <span class="muted">— unshipped FBM orders</span></h2>
    <div class="muted">Live from Amazon. Open an order to check its parcel data and (soon) buy a label.
      No purchases happen on this screen.</div>
    <div class="bar">
      <form method="GET" action="/ship" style="margin:0;display:flex;gap:8px;align-items:center">
        <label class="muted">Account:
          <select name="account" onchange="this.form.submit()">
            {% for a in accounts %}<option value="{{ a }}" {{ 'selected' if a==account else '' }}>{{ a }}</option>{% endfor %}
          </select></label>
        <label class="muted">Last <input type="number" name="days" value="{{ days }}" style="width:56px"> days</label>
        <button class="btn" type="submit">Refresh</button>
      </form>
      <span class="muted">{{ orders|length }} unshipped order(s)</span>
    </div>
  </div>
  {% if error %}<div class="card"><div class="err">Couldn't load orders: {{ error }}</div></div>{% endif %}
  <div class="card">
    <table>
      <tr><th>Order</th><th>Purchased</th><th>Ship by</th><th>Status</th><th>Items</th><th>Service</th><th>Total</th><th></th></tr>
      {% for o in orders %}
      <tr>
        <td><b>{{ o.order_id }}</b></td>
        <td>{{ o.purchase_date }}</td>
        <td>{{ o.ship_by }}</td>
        <td>{{ o.status }}</td>
        <td>{{ o.n_unshipped }}</td>
        <td>{% if o.prime %}<span class="pill prime">Prime</span> {% endif %}<span class="muted">{{ o.service }}</span></td>
        <td>{% if o.total %}£{{ o.total }}{% endif %}</td>
        <td><a class="btn" href="/ship/order/{{ o.order_id }}?account={{ account }}">Prepare</a></td>
      </tr>
      {% endfor %}
      {% if not orders and not error %}<tr><td colspan="8" class="muted">No unshipped orders in this window.</td></tr>{% endif %}
    </table>
  </div>
</div></body></html>
"""

ORDER_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Prepare {{ order_id }} — BSR Repricer</title>
<style>
 *{box-sizing:border-box} body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1000px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 h2{margin:0 0 4px;font-size:18px} .muted{color:#8a94a2;font-size:13px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:left;border-top:1px solid #eef1f4;vertical-align:middle}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase}
 input.n{width:76px;padding:4px 6px;font-size:12px;border:1px solid #dfe4e9;border-radius:5px;text-align:right}
 tr.miss{background:#fbf3e0}
 .btn{background:#0e5c5b;color:#fff;border:none;border-radius:7px;padding:6px 11px;font-size:13px;cursor:pointer;text-decoration:none}
 .btn.sec{background:#eef4f4;color:#0e5c5b;border:1px solid #cfe3e2}
 .pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px}
 .ok{background:#e4f6ec;color:#1f7a45}.no{background:#fbe7c6;color:#8a5906}
 .err{background:#fcebeb;color:#a32d2d;padding:10px 12px;border-radius:8px}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  <div class="card">
    <h2>Prepare order {{ order_id }}</h2>
    <div class="muted"><a href="/ship?account={{ account }}">← back to queue</a> · account {{ account }}
      {% if service %} · {{ service }}{% endif %}{% if prime %} · <b>Prime</b> (must use Buy Shipping){% endif %}</div>
  </div>
  {% if error %}<div class="card"><div class="err">Couldn't load items: {{ error }}</div></div>{% endif %}
  {% if items %}
  <div class="card">
    <table>
      <tr><th>SKU</th><th>Canonical</th><th>Qty</th><th>Title</th><th>Weight (g)</th><th>L</th><th>W</th><th>H</th><th>Status</th><th></th></tr>
      {% for it in items %}
      <tr class="{{ 'miss' if not it.complete else '' }}">
        <form method="POST" action="/ship/save-default" style="display:contents">
        <input type="hidden" name="account" value="{{ account }}">
        <input type="hidden" name="order_id" value="{{ order_id }}">
        <input type="hidden" name="canonical_sku" value="{{ it.canonical }}">
        <input type="hidden" name="asin" value="{{ it.asin }}">
        <td><b>{{ it.sku }}</b></td>
        <td class="muted">{{ it.canonical }}</td>
        <td>{{ it.qty }}</td>
        <td class="muted" style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ it.title }}</td>
        <td><input class="n" type="number" step="1" name="weight_g" value="{{ it.weight_g if it.weight_g is not none else '' }}"></td>
        <td><input class="n" type="number" step="0.1" name="length_cm" value="{{ it.length_cm if it.length_cm is not none else '' }}"></td>
        <td><input class="n" type="number" step="0.1" name="width_cm" value="{{ it.width_cm if it.width_cm is not none else '' }}"></td>
        <td><input class="n" type="number" step="0.1" name="height_cm" value="{{ it.height_cm if it.height_cm is not none else '' }}"></td>
        <td>{% if it.complete %}<span class="pill ok">ready</span>{% else %}<span class="pill no">enter</span>{% endif %}</td>
        <td><button class="btn sec" type="submit">Save</button></td>
        </form>
      </tr>
      {% endfor %}
    </table>
    <div class="muted" style="margin-top:10px">
      Combined parcel weight (Σ weight × qty): <b>{% if parcel_weight %}{{ parcel_weight }} g{% else %}—{% endif %}</b>.
      Enter weight + all three dims for every line to make this order <b>quote-ready</b>.
      {% if all_ready %}<span class="pill ok" style="margin-left:6px">order ready to quote</span>{% endif %}
    </div>
  </div>
  <div class="card muted">Rate quote &amp; label purchase arrive next (M5a-5 / M5a-6). Nothing here spends money.</div>
  {% endif %}
</div></body></html>
"""


@app.route("/ship")
def ship_queue():
    cfg, accts = _cfg_accounts()
    account = request.args.get("account") or (accts[0]["account_id"] if accts else "")
    days = _num(request.args.get("days"), int) or 30
    orders, error = [], None
    acct = _account(accts, account)
    if not acct:
        error = "no configured account with SP-API credentials."
    else:
        try:
            orders = module5_orders.list_unshipped(cfg, acct, days=days)
        except Exception as e:
            log.warning("ship queue load failed: %s", e)
            error = str(e)[:300]
    return render_template_string(QUEUE_HTML, accounts=[a["account_id"] for a in accts],
                                  account=account, days=days, orders=orders, error=error)


@app.route("/ship/order/<order_id>")
def ship_order(order_id):
    cfg, accts = _cfg_accounts()
    account = request.args.get("account") or (accts[0]["account_id"] if accts else "")
    acct = _account(accts, account)
    items, error, service, prime = [], None, "", False
    if not acct:
        error = "no configured account."
    else:
        try:
            raw = module5_orders.order_items(cfg, acct, order_id)
            for it in raw:
                canon = pl_cogs.resolve_to_canonical(it["sku"]) if it["sku"] else ""
                d = m5.get_package_default(account, canon) or {}
                complete = all(d.get(k) is not None for k in ("weight_g", "length_cm", "width_cm", "height_cm"))
                items.append(dict(sku=it["sku"], asin=it["asin"], qty=it["qty"], title=it["title"],
                                  canonical=canon, weight_g=d.get("weight_g"),
                                  length_cm=d.get("length_cm"), width_cm=d.get("width_cm"),
                                  height_cm=d.get("height_cm"), complete=complete))
        except Exception as e:
            log.warning("ship order %s load failed: %s", order_id, e)
            error = str(e)[:300]
    parcel_weight = None
    if items and all(i["weight_g"] is not None for i in items):
        parcel_weight = sum((i["weight_g"] or 0) * (i["qty"] or 1) for i in items)
    all_ready = bool(items) and all(i["complete"] for i in items)
    return render_template_string(ORDER_HTML, order_id=order_id, account=account, items=items,
                                  error=error, service=service, prime=prime,
                                  parcel_weight=parcel_weight, all_ready=all_ready)


@app.route("/ship/save-default", methods=["POST"])
def ship_save_default():
    account = request.form.get("account")
    canon = request.form.get("canonical_sku")
    order_id = request.form.get("order_id")
    try:
        m5.upsert_package_default(
            account, canon, asin=(request.form.get("asin") or None),
            weight_g=_num(request.form.get("weight_g"), int),
            length_cm=_num(request.form.get("length_cm"), float),
            width_cm=_num(request.form.get("width_cm"), float),
            height_cm=_num(request.form.get("height_cm"), float),
            source="manual")
        flash(f"Saved default for {canon}.")
    except Exception as e:
        flash(f"Could not save {canon}: {e}")
    if order_id:
        return redirect(f"/ship/order/{order_id}?account={account}")
    return redirect(f"/ship?account={account}")
