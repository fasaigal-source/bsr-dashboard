"""dashboard_ship.py — Module 5a ready-to-ship queue ("/ship").

READ-ONLY this phase. Cached queue (shipment_queue) with everything on ONE page:
per order — weight (pre-filled) + parcel-size dropdown; tick rows and Quote/Save in bulk.
Quoting is inline (getEligibleShipmentServices, cheapest service meeting the delivery
promise), shown right in the row. No purchase yet — that's M5a-6.
"""
import logging

from flask import request, redirect, flash, render_template_string

from dashboard_app import app
import pl_cogs
import module5_labels_db as m5
import module5_orders

log = logging.getLogger(__name__)


def _cfg_accounts():
    return module5_orders.load_spapi_config()


def _account(accts, account_id):
    for a in accts:
        if a["account_id"] == account_id:
            return a
    return accts[0] if accts else None


def _num(v, cast):
    v = (v or "").strip()
    return cast(v) if v not in ("", None) else None


def _resolve_primary(account, items):
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


def _quote_one(cfg, acct, account, order):
    """Auto-picked service for one order, or an {error}. Aggregates weight across lines,
    uses the first line's dims (single-item is the norm)."""
    items, ok, total_w, dims = [], True, 0, None
    for it in order.get("items", []):
        canon = pl_cogs.resolve_to_canonical(it.get("sku")) if it.get("sku") else ""
        d = m5.get_package_default(account, canon) or {}
        if not all(d.get(k) is not None for k in ("weight_g", "length_cm", "width_cm", "height_cm")):
            ok = False
        total_w += (d.get("weight_g") or 0) * (it.get("qty") or 1)
        if dims is None and d.get("length_cm") is not None:
            dims = (d["length_cm"], d["width_cm"], d["height_cm"])
        items.append(dict(it, canonical=canon))
    if not ok or not dims:
        return {"error": "not ready"}
    if not module5_orders.load_ship_from():
        return {"error": "set SHIP_FROM"}
    try:
        req = module5_orders.build_shipment_request(order["order_id"], items,
                                                    module5_orders.load_ship_from(),
                                                    total_w, dims[0], dims[1], dims[2])
        services, _notes = module5_orders.eligible_services(cfg, acct, req)
        picked, met = module5_orders.pick_cheapest(services, order.get("deliver_by"))
        if not picked:
            return {"error": "no services"}
        return {"name": picked["name"], "carrier": picked.get("carrier"),
                "amount": picked["amount"], "latest": picked.get("latest"), "met": met,
                "n": len(services)}
    except Exception as e:
        log.warning("quote failed %s: %s", order["order_id"], e)
        return {"error": str(e)[:120]}


QUEUE_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ready to ship — BSR Repricer</title>
<style>
 *{box-sizing:border-box} body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1280px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:16px 18px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:14px}
 h2{margin:0 0 4px;font-size:18px} .muted{color:#8a94a2;font-size:12.5px}
 .bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:8px 0}
 select,input{padding:5px 7px;border-radius:6px;border:1px solid #dfe4e9;font-size:13px}
 input.n{width:66px;text-align:right}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:6px 8px;text-align:left;border-top:1px solid #eef1f4;vertical-align:middle}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase}
 tr.miss{background:#fbf3e0}
 .btn{background:#0e5c5b;color:#fff;border:none;border-radius:7px;padding:6px 11px;font-size:13px;cursor:pointer;text-decoration:none}
 .btn.sec{background:#eef4f4;color:#0e5c5b;border:1px solid #cfe3e2}
 .pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px}
 .ok{background:#e4f6ec;color:#1f7a45}.no{background:#fbe7c6;color:#8a5906}.prime{background:#eaf3ff;color:#1257a3}.errp{background:#fcebeb;color:#a32d2d}
 .err{background:#fcebeb;color:#a32d2d;padding:10px 12px;border-radius:8px;margin:8px 0}
</style>
<script>
 function selAll(cb){document.querySelectorAll('input.qsel:not(:disabled)').forEach(function(x){x.checked=cb.checked});}
</script></head><body>
{{ nav|safe }}
<div class="wrap">
  <div class="card">
    <h2>Ready to ship <span class="muted">— unshipped FBM orders</span></h2>
    <div class="muted">Check weight (pre-filled) · pick a parcel size · tick rows and Quote/Save in bulk. Quotes show in the row. No purchases here.</div>
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
    <form method="POST" action="/ship/bulk">
      <input type="hidden" name="account" value="{{ account }}"><input type="hidden" name="days" value="{{ days }}">
      <div class="bar">
        <button class="btn" type="submit" name="action" value="quote">Quote selected</button>
        <button class="btn sec" type="submit" name="action" value="save">Save selected</button>
        <span class="muted">tick the rows you want, then Quote or Save</span>
      </div>
      <table>
        <tr><th><input type="checkbox" onclick="selAll(this)"></th><th>Order</th><th>Ship by</th><th>Service</th><th>SKU → canonical</th><th>Qty</th><th>Weight (g)</th><th>Parcel size</th><th>Status</th><th>Quote</th></tr>
        {% for r in rows %}
        <tr class="{{ 'miss' if (r.item and not r.item.complete) else '' }}">
          <td><input type="checkbox" class="qsel" name="sel" value="{{ r.order_id }}" {{ 'disabled' if not r.item else '' }}></td>
          <td><b>{{ r.order_id }}</b><br><span class="muted">£{{ r.total }} · {{ r.status }}</span></td>
          <td>{{ r.ship_by }}</td>
          <td>{% if r.prime %}<span class="pill prime">Prime</span> {% endif %}<span class="muted">{{ r.service }}</span></td>
          {% if r.item %}
          <input type="hidden" name="canon__{{ r.order_id }}" value="{{ r.item.canonical }}">
          <input type="hidden" name="asin__{{ r.order_id }}" value="{{ r.item.asin }}">
          <td><b>{{ r.item.sku }}</b> <span class="muted">→ {{ r.item.canonical }}</span>
              {% if r.extra %}<br><a class="muted" href="/ship/order/{{ r.order_id }}?account={{ account }}">+{{ r.extra }} more</a>{% endif %}</td>
          <td>{{ r.item.qty }}</td>
          <td><input class="n" type="number" step="1" name="w__{{ r.order_id }}" value="{{ r.item.weight_g if r.item.weight_g is not none else '' }}"></td>
          <td><select name="s__{{ r.order_id }}">
              <option value="">— size —</option>
              {% for name,l,w,h in sizes %}<option value="{{ name }}" {{ 'selected' if name==r.item.parcel_size else '' }}>{{ name }} ({{ l }}×{{ w }}×{{ h }})</option>{% endfor %}
            </select></td>
          <td>{% if r.item.complete %}<span class="pill ok">ready</span>{% else %}<span class="pill no">enter</span>{% endif %}</td>
          <td>{% if r.quote %}{% if r.quote.error %}<span class="pill errp">{{ r.quote.error }}</span>{% else %}<b>£{{ "%.2f"|format(r.quote.amount|float) }}</b> {{ r.quote.name }}{% if not r.quote.met %} <span class="pill no">late</span>{% endif %} <a class="muted" href="/ship/quote/{{ r.order_id }}?account={{ account }}">options</a>{% endif %}{% endif %}</td>
          {% else %}
          <td class="muted" colspan="6">no items — <a href="/ship/order/{{ r.order_id }}?account={{ account }}">open</a></td>
          {% endif %}
        </tr>
        {% endfor %}
        {% if not rows and not error %}<tr><td colspan="10" class="muted">Nothing cached. Click <b>Refresh from Amazon</b>.</td></tr>{% endif %}
      </table>
    </form>
  </div>
</div></body></html>
"""


@app.route("/ship")
def ship_queue():
    cfg, accts = _cfg_accounts()
    account = request.args.get("account") or (accts[0]["account_id"] if accts else "")
    days = _num(request.args.get("days"), int) or 30
    want_refresh = request.args.get("refresh") == "1"
    quote_ids = [x for x in (request.args.get("quote") or "").split(",") if x][:25]
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
        row = dict(order_id=o["order_id"], ship_by=o.get("ship_by"), status=o.get("status"),
                   service=o.get("service"), prime=o.get("prime"), total=o.get("total"),
                   item=item, extra=extra, quote=None)
        if o["order_id"] in quote_ids and item and item["complete"]:
            row["quote"] = _quote_one(cfg, acct, account, o)
        rows.append(row)
    return render_template_string(QUEUE_HTML, accounts=[a["account_id"] for a in accts],
                                  account=account, days=days, rows=rows, error=error,
                                  sizes=m5.PARCEL_SIZES,
                                  refreshed_at=m5.queue_refreshed_at(account) if acct else None)


@app.route("/ship/bulk", methods=["POST"])
def ship_bulk():
    account = request.form.get("account")
    days = request.form.get("days") or "30"
    action = request.form.get("action")
    selected = request.form.getlist("sel")
    # persist any weight/size edits on the selected rows first
    saved = 0
    for oid in selected:
        canon = request.form.get(f"canon__{oid}")
        if not canon:
            continue
        try:
            m5.upsert_package_default(
                account, canon, asin=(request.form.get(f"asin__{oid}") or None),
                weight_g=_num(request.form.get(f"w__{oid}"), int),
                parcel_size=(request.form.get(f"s__{oid}") or None),
                source="manual")
            saved += 1
        except Exception as e:
            log.warning("bulk save %s: %s", canon, e)
    if action == "quote":
        return redirect(f"/ship?account={account}&days={days}&quote={','.join(selected)}")
    flash(f"Saved {saved} row(s).")
    return redirect(f"/ship?account={account}&days={days}")


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


@app.route("/ship/order/<order_id>")
def ship_order(order_id):
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


QUOTE_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Quote {{ order_id }}</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:900px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:16px 18px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:14px}
 h2{margin:0 0 4px;font-size:18px} .muted{color:#8a94a2;font-size:12.5px}
 table{width:100%;border-collapse:collapse;font-size:13px} th,td{padding:8px 10px;text-align:left;border-top:1px solid #eef1f4}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase} tr.pick{background:#e9f6ef} .price{font-variant-numeric:tabular-nums;font-weight:600}
 .pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px}.ok{background:#e4f6ec;color:#1f7a45}.no{background:#fbe7c6;color:#8a5906}
 .err{background:#fcebeb;color:#a32d2d;padding:10px 12px;border-radius:8px}</style></head><body>{{ nav|safe }}
<div class="wrap">
  <div class="card"><h2>Rate quote — order {{ order_id }}</h2>
    <div class="muted"><a href="/ship?account={{ account }}">← back to queue</a> · {{ account }}
      {% if order %} · deliver by <b>{{ order.deliver_by }}</b>{% endif %}</div>
    {% if parcel %}<div class="muted" style="margin-top:6px">Parcel: <b>{{ parcel.weight_g }} g</b>, {{ parcel.length_cm }}×{{ parcel.width_cm }}×{{ parcel.height_cm }} cm{% if parcel.multi %} · <span class="pill no">multi-item — first line's dims</span>{% endif %}</div>{% endif %}
  </div>
  {% if error %}<div class="card"><div class="err">{{ error }}</div></div>{% endif %}
  {% if notes %}<div class="card muted">{% for n in notes %}⚠ {{ n }}<br>{% endfor %}</div>{% endif %}
  {% if services %}<div class="card"><table>
    <tr><th>Service</th><th>Carrier</th><th>Est. delivery</th><th>Price</th><th></th></tr>
    {% for s in services %}<tr class="{{ 'pick' if picked and s.id==picked.id else '' }}">
      <td><b>{{ s.name }}</b></td><td>{{ s.carrier }}</td>
      <td>{{ s.latest }}{% if order and order.deliver_by and s.latest and s.latest > order.deliver_by %} <span class="pill no">late</span>{% endif %}</td>
      <td class="price">{% if s.amount is not none %}£{{ "%.2f"|format(s.amount|float) }}{% else %}—{% endif %}</td><td>{% if picked and s.id==picked.id %}<span class="pill ok">auto-pick</span>{% endif %}</td>
    </tr>{% endfor %}</table>
    <div class="muted" style="margin-top:10px">Buy label — next step (M5a-6).</div>
  </div>{% elif not error %}<div class="card muted">No eligible services returned.</div>{% endif %}
</div></body></html>
"""


@app.route("/ship/quote/<order_id>")
def ship_quote(order_id):
    cfg, accts = _cfg_accounts()
    account = request.args.get("account") or (accts[0]["account_id"] if accts else "")
    acct = _account(accts, account)
    order = next((o for o in m5.list_queue(account) if o["order_id"] == order_id), None)
    services, notes, picked, met, parcel, error = [], [], None, False, None, None
    if not acct or not order:
        error = "Order not in cache — Refresh the queue first."
    else:
        items, ok, total_w, dims = [], True, 0, None
        for it in order.get("items", []):
            canon = pl_cogs.resolve_to_canonical(it.get("sku")) if it.get("sku") else ""
            d = m5.get_package_default(account, canon) or {}
            if not all(d.get(k) is not None for k in ("weight_g", "length_cm", "width_cm", "height_cm")):
                ok = False
            total_w += (d.get("weight_g") or 0) * (it.get("qty") or 1)
            if dims is None and d.get("length_cm") is not None:
                dims = (d["length_cm"], d["width_cm"], d["height_cm"])
            items.append(dict(it, canonical=canon))
        if not ok or not dims:
            flash("This order isn't quote-ready — fill weight + size for every line first.")
            return redirect(f"/ship?account={account}")
        if not module5_orders.load_ship_from():
            error = "Set the SHIP_FROM variable (your dispatch address, as JSON) in Railway to get quotes."
        else:
            parcel = dict(weight_g=total_w, length_cm=dims[0], width_cm=dims[1], height_cm=dims[2], multi=len(items) > 1)
            try:
                req = module5_orders.build_shipment_request(order_id, items, module5_orders.load_ship_from(),
                                                            total_w, dims[0], dims[1], dims[2])
                services, notes = module5_orders.eligible_services(cfg, acct, req)
                picked, met = module5_orders.pick_cheapest(services, order.get("deliver_by"))
            except Exception as e:
                error = f"Quote failed: {str(e)[:250]}"
    return render_template_string(QUOTE_HTML, order_id=order_id, account=account, order=order,
                                  services=services, notes=notes, picked=picked, met=met, parcel=parcel, error=error)


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
