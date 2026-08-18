"""dashboard_ship.py — Module 5a ready-to-ship queue ("/ship").

READ-ONLY this phase. The queue is CACHED (shipment_queue): a "Refresh from Amazon" fetch
pulls unshipped FBM orders + their items once, then the page loads instantly from cache.
Each order shows its item's canonical with weight PRE-FILLED from the package default and a
parcel-SIZE dropdown (presets that fill the dims on save) — so prepping an order is: check
weight, pick a size, Save. Anything saved becomes that SKU's default (learn-as-you-go).
No rate quote and no purchase yet — those are M5a-5 / M5a-6.
"""
import logging

from flask import request, redirect, flash, render_template_string

from dashboard_app import app
import pl_cogs
import module5_labels_db as m5
import module5_orders

log = logging.getLogger(__name__)


def _cfg_accounts():
    return module5_orders.load_spapi_config()   # config.json locally, env vars on Railway


def _account(accts, account_id):
    for a in accts:
        if a["account_id"] == account_id:
            return a
    return accts[0] if accts else None


def _num(v, cast):
    v = (v or "").strip()
    return cast(v) if v not in ("", None) else None


def _resolve_primary(account, items):
    """The order's primary line (first item) resolved to canonical + its saved package
    default. Returns (row_dict, extra_item_count)."""
    if not items:
        return None, 0
    it = items[0]
    canon = pl_cogs.resolve_to_canonical(it.get("sku")) if it.get("sku") else ""
    d = m5.get_package_default(account, canon) or {}
    complete = all(d.get(k) is not None for k in ("weight_g", "length_cm", "width_cm", "height_cm"))
    return (dict(sku=it.get("sku"), asin=it.get("asin"), qty=it.get("qty"), title=it.get("title"),
                 canonical=canon, weight_g=d.get("weight_g"), parcel_size=d.get("parcel_size"),
                 length_cm=d.get("length_cm"), width_cm=d.get("width_cm"), height_cm=d.get("height_cm"),
                 complete=complete),
            max(0, len(items) - 1))


QUEUE_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ready to ship — BSR Repricer</title>
<style>
 *{box-sizing:border-box} body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1200px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:16px 18px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:14px}
 h2{margin:0 0 4px;font-size:18px} .muted{color:#8a94a2;font-size:12.5px}
 .bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:8px 0}
 select,input{padding:5px 7px;border-radius:6px;border:1px solid #dfe4e9;font-size:13px}
 input.n{width:70px;text-align:right}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:6px 8px;text-align:left;border-top:1px solid #eef1f4;vertical-align:middle}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase}
 tr.miss{background:#fbf3e0}
 .btn{background:#0e5c5b;color:#fff;border:none;border-radius:7px;padding:6px 11px;font-size:13px;cursor:pointer;text-decoration:none}
 .btn.sec{background:#eef4f4;color:#0e5c5b;border:1px solid #cfe3e2}
 .pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px}
 .ok{background:#e4f6ec;color:#1f7a45}.no{background:#fbe7c6;color:#8a5906}.prime{background:#eaf3ff;color:#1257a3}
 .err{background:#fcebeb;color:#a32d2d;padding:10px 12px;border-radius:8px;margin:8px 0}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  <div class="card">
    <h2>Ready to ship <span class="muted">— unshipped FBM orders</span></h2>
    <div class="muted">Check weight (pre-filled) · pick a parcel size · Save. Saved data becomes that SKU's default. No purchases here.</div>
    <div class="bar">
      <form method="GET" action="/ship" style="margin:0;display:flex;gap:8px;align-items:center">
        <label class="muted">Account:
          <select name="account" onchange="this.form.submit()">
            {% for a in accounts %}<option value="{{ a }}" {{ 'selected' if a==account else '' }}>{{ a }}</option>{% endfor %}
          </select></label>
        <label class="muted">Last <input type="number" name="days" value="{{ days }}" style="width:52px"> days</label>
        <button class="btn sec" type="submit">Apply</button>
      </form>
      <a class="btn" href="/ship?account={{ account }}&days={{ days }}&refresh=1">⟳ Refresh from Amazon</a>
      <span class="muted">{{ rows|length }} order(s){% if refreshed_at %} · cached {{ refreshed_at[:16].replace('T',' ') }}{% endif %}</span>
    </div>
  </div>
  {% if error %}<div class="card"><div class="err">{{ error }}</div></div>{% endif %}
  <div class="card">
    <table>
      <tr><th>Order</th><th>Ship by</th><th>Service</th><th>SKU → canonical</th><th>Qty</th><th>Weight (g)</th><th>Parcel size</th><th>Status</th><th></th></tr>
      {% for r in rows %}
      <tr class="{{ 'miss' if (r.item and not r.item.complete) else '' }}">
        <td><b>{{ r.order_id }}</b><br><span class="muted">£{{ r.total }} · {{ r.status }}</span></td>
        <td>{{ r.ship_by }}</td>
        <td>{% if r.prime %}<span class="pill prime">Prime</span> {% endif %}<span class="muted">{{ r.service }}</span></td>
        {% if r.item %}
        <form method="POST" action="/ship/save-default" style="display:contents">
          <input type="hidden" name="account" value="{{ account }}">
          <input type="hidden" name="canonical_sku" value="{{ r.item.canonical }}">
          <input type="hidden" name="asin" value="{{ r.item.asin }}">
          <td><b>{{ r.item.sku }}</b> <span class="muted">→ {{ r.item.canonical }}</span>
              {% if r.extra %}<br><a class="muted" href="/ship/order/{{ r.order_id }}?account={{ account }}">+{{ r.extra }} more item(s)</a>{% endif %}</td>
          <td>{{ r.item.qty }}</td>
          <td><input class="n" type="number" step="1" name="weight_g" value="{{ r.item.weight_g if r.item.weight_g is not none else '' }}"></td>
          <td><select name="parcel_size">
              <option value="">— size —</option>
              {% for name,l,w,h in sizes %}<option value="{{ name }}" {{ 'selected' if name==r.item.parcel_size else '' }}>{{ name }} ({{ l }}×{{ w }}×{{ h }})</option>{% endfor %}
            </select></td>
          <td>{% if r.item.complete %}<span class="pill ok">ready</span>{% else %}<span class="pill no">enter</span>{% endif %}</td>
          <td><button class="btn sec" type="submit">Save</button></td>
        </form>
        {% else %}
          <td class="muted" colspan="5">no items — <a href="/ship/order/{{ r.order_id }}?account={{ account }}">open</a></td><td></td>
        {% endif %}
      </tr>
      {% endfor %}
      {% if not rows and not error %}<tr><td colspan="9" class="muted">Nothing cached. Click <b>Refresh from Amazon</b>.</td></tr>{% endif %}
    </table>
  </div>
</div></body></html>
"""


@app.route("/ship")
def ship_queue():
    cfg, accts = _cfg_accounts()
    account = request.args.get("account") or (accts[0]["account_id"] if accts else "")
    days = _num(request.args.get("days"), int) or 30
    want_refresh = request.args.get("refresh") == "1"
    error = None
    acct = _account(accts, account)
    if not acct:
        error = "No configured account with SP-API credentials."
    else:
        cached = m5.list_queue(account)
        if want_refresh or not cached:
            try:
                orders = module5_orders.sync_orders(cfg, acct, days=days)
                m5.replace_queue(account, orders)
            except Exception as e:
                log.warning("ship refresh failed: %s", e)
                error = f"Couldn't refresh from Amazon: {str(e)[:200]}"
    cached = m5.list_queue(account) if acct else []
    rows = []
    for o in cached:
        item, extra = _resolve_primary(account, o.get("items"))
        rows.append(dict(order_id=o["order_id"], ship_by=o.get("ship_by"), status=o.get("status"),
                         service=o.get("service"), prime=o.get("prime"), total=o.get("total"),
                         item=item, extra=extra))
    return render_template_string(QUEUE_HTML, accounts=[a["account_id"] for a in accts],
                                  account=account, days=days, rows=rows, error=error,
                                  sizes=m5.PARCEL_SIZES, refreshed_at=m5.queue_refreshed_at(account) if acct else None)


@app.route("/ship/order/<order_id>")
def ship_order(order_id):
    """Multi-item detail — all lines of one order (from cache), each with weight + size."""
    cfg, accts = _cfg_accounts()
    account = request.args.get("account") or (accts[0]["account_id"] if accts else "")
    order = next((o for o in m5.list_queue(account) if o["order_id"] == order_id), None)
    items = []
    for it in (order or {}).get("items", []):
        canon = pl_cogs.resolve_to_canonical(it.get("sku")) if it.get("sku") else ""
        d = m5.get_package_default(account, canon) or {}
        items.append(dict(sku=it.get("sku"), asin=it.get("asin"), qty=it.get("qty"), title=it.get("title"),
                          canonical=canon, weight_g=d.get("weight_g"), parcel_size=d.get("parcel_size"),
                          complete=all(d.get(k) is not None for k in ("weight_g", "length_cm", "width_cm", "height_cm"))))
    return render_template_string(ORDER_HTML, order_id=order_id, account=account, items=items,
                                  sizes=m5.PARCEL_SIZES)


ORDER_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{{ order_id }}</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1000px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:16px 18px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:14px}
 table{width:100%;border-collapse:collapse;font-size:13px} th,td{padding:7px 9px;text-align:left;border-top:1px solid #eef1f4}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase} input.n{width:70px;text-align:right;padding:4px 6px;border:1px solid #dfe4e9;border-radius:5px}
 select{padding:5px 7px;border-radius:6px;border:1px solid #dfe4e9} tr.miss{background:#fbf3e0}
 .btn{background:#0e5c5b;color:#fff;border:none;border-radius:7px;padding:6px 11px;font-size:13px;cursor:pointer}
 .pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px}.ok{background:#e4f6ec;color:#1f7a45}.no{background:#fbe7c6;color:#8a5906}</style>
</head><body>{{ nav|safe }}
<div class="wrap"><div class="card"><h2>Order {{ order_id }}</h2>
<div><a href="/ship?account={{ account }}">← back to queue</a> · {{ account }}</div></div>
<div class="card"><table>
<tr><th>SKU → canonical</th><th>Qty</th><th>Title</th><th>Weight (g)</th><th>Parcel size</th><th>Status</th><th></th></tr>
{% for it in items %}
<tr class="{{ 'miss' if not it.complete else '' }}">
<form method="POST" action="/ship/save-default" style="display:contents">
<input type="hidden" name="account" value="{{ account }}"><input type="hidden" name="canonical_sku" value="{{ it.canonical }}"><input type="hidden" name="asin" value="{{ it.asin }}"><input type="hidden" name="order_id" value="{{ order_id }}">
<td><b>{{ it.sku }}</b> <span style="color:#8a94a2">→ {{ it.canonical }}</span></td><td>{{ it.qty }}</td>
<td style="color:#8a94a2;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{{ it.title }}</td>
<td><input class="n" type="number" name="weight_g" value="{{ it.weight_g if it.weight_g is not none else '' }}"></td>
<td><select name="parcel_size"><option value="">— size —</option>
{% for name,l,w,h in sizes %}<option value="{{ name }}" {{ 'selected' if name==it.parcel_size else '' }}>{{ name }} ({{ l }}×{{ w }}×{{ h }})</option>{% endfor %}</select></td>
<td>{% if it.complete %}<span class="pill ok">ready</span>{% else %}<span class="pill no">enter</span>{% endif %}</td>
<td><button class="btn" type="submit">Save</button></td>
</form></tr>{% endfor %}
</table></div></div></body></html>
"""


@app.route("/ship/save-default", methods=["POST"])
def ship_save_default():
    account = request.form.get("account")
    canon = request.form.get("canonical_sku")
    order_id = request.form.get("order_id")
    try:
        m5.upsert_package_default(
            account, canon, asin=(request.form.get("asin") or None),
            weight_g=_num(request.form.get("weight_g"), int),
            parcel_size=(request.form.get("parcel_size") or None),
            source="manual")
        flash(f"Saved {canon}.")
    except Exception as e:
        flash(f"Could not save {canon}: {e}")
    if order_id:
        return redirect(f"/ship/order/{order_id}?account={account}")
    return redirect(f"/ship?account={account}")
