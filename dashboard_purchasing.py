"""dashboard_purchasing.py — /purchase-orders: suppliers, purchase orders, reorder suggestions.

Suggestions come from the live inventory + velocity (module7_purchasing.reorder_suggestions):
tick the SKUs you want and "Create PO from selected" drops them into a new draft PO with
suggested quantities. POs have suppliers, editable lines, and a status (draft→sent→received).
"""
import logging

from flask import request, redirect, render_template_string

from dashboard_app import app
import module7_purchasing as po

log = logging.getLogger(__name__)

_HEAD = """
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1140px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 h2{margin:0 0 3px;font-size:18px} h3{margin:0 0 8px;font-size:14px} .muted{color:#8a94a2;font-size:12.5px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:left;border-bottom:1px solid #eef1f4}
 th{color:#5a6472;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.03em}
 td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
 tr:hover td{background:#f8fafb}
 input,select{padding:7px 9px;border:1px solid #dfe4e9;border-radius:7px;font-size:13px;box-sizing:border-box}
 .btn{background:#0e5c5b;color:#fff;border:none;border-radius:7px;padding:8px 14px;font-size:13px;cursor:pointer;text-decoration:none;display:inline-block}
 .btn.sec{background:#eef1f4;color:#0e5c5b}
 .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
 .pill{font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px}
 .st-draft{background:#eef1f4;color:#5a6472}.st-sent{background:#fff6e0;color:#a76b00}
 .st-received{background:#e4f6ea;color:#1f7a45}.st-cancelled{background:#fdecec;color:#c0392b}
 .s-out{background:#fdecec;color:#c0392b}.s-reorder{background:#fde7cf;color:#b45309}
</style>
"""

LIST = _HEAD + """
{{ nav|safe }}
<div class="wrap">
  <div class="card"><h2>Purchase orders</h2>
    <div class="muted">Create orders to your suppliers. Suggestions below come from live stock and sales velocity.</div>
  </div>

  <div class="card">
    <h3>Reorder suggestions <span class="muted">— target {{ target }} days of cover</span></h3>
    {% if sugg %}
    <form method="POST" action="/purchase-orders/from-suggestions">
      <table>
        <thead><tr><th><input type="checkbox" onclick="for(const c of document.querySelectorAll('.sg'))c.checked=this.checked"></th>
          <th>SKU</th><th>Title</th><th class="num">Stock</th><th class="num">Rate/day</th><th class="num">Cover</th><th>Status</th><th class="num">Suggest</th></tr></thead>
        <tbody>
        {% for x in sugg %}
          <tr>
            <td><input class="sg" type="checkbox" name="sel" value="{{ x.canonical_sku }}"></td>
            <td>{{ x.seller_sku }}</td>
            <td>{{ (x.title or '')[:44] }}</td>
            <td class="num">{{ x.quantity if x.quantity is not none else '—' }}</td>
            <td class="num">{{ x.daily_rate }}</td>
            <td class="num">{% if x.days_cover is not none %}{{ '%.0f'|format(x.days_cover) }}d{% else %}—{% endif %}</td>
            <td><span class="pill s-{{ x.status }}">{{ 'Out' if x.status=='out' else 'Reorder' }}</span></td>
            <td class="num"><input name="qty_{{ x.canonical_sku }}" value="{{ x.suggested_qty }}" style="width:64px;text-align:right"></td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
      <div class="row" style="margin-top:12px">
        <select name="supplier_id"><option value="">— supplier (optional) —</option>
          {% for s in suppliers %}<option value="{{ s.id }}">{{ s.name }}</option>{% endfor %}</select>
        <button class="btn" type="submit">Create PO from selected</button>
      </div>
    </form>
    {% else %}<div class="muted">Nothing needs reordering right now. 🎉 (Pull inventory on the <a href="/inventory">Inventory</a> page first.)</div>{% endif %}
  </div>

  <div class="card">
    <h3>Purchase orders</h3>
    {% if pos %}
    <table>
      <thead><tr><th>#</th><th>Supplier</th><th>Reference</th><th>Status</th><th class="num">Lines</th><th class="num">Units</th><th class="num">Cost</th><th>Updated</th></tr></thead>
      <tbody>
      {% for p in pos %}
        <tr>
          <td><a href="/purchase-orders/{{ p.id }}">PO-{{ p.id }}</a></td>
          <td>{{ p.supplier_name or '—' }}</td>
          <td>{{ p.reference or '—' }}</td>
          <td><span class="pill st-{{ p.status }}">{{ p.status|capitalize }}</span></td>
          <td class="num">{{ p.line_count }}</td>
          <td class="num">{{ p.unit_count }}</td>
          <td class="num">{% if p.total_cost %}£{{ '%.2f'|format(p.total_cost|float) }}{% else %}—{% endif %}</td>
          <td class="muted">{{ (p.updated_at or '')[:16].replace('T',' ') }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}<div class="muted">No purchase orders yet.</div>{% endif %}
    <form class="row" method="POST" action="/purchase-orders/create" style="margin-top:12px">
      <select name="supplier_id"><option value="">— supplier (optional) —</option>
        {% for s in suppliers %}<option value="{{ s.id }}">{{ s.name }}</option>{% endfor %}</select>
      <input name="reference" placeholder="reference (optional)">
      <button class="btn sec" type="submit">+ Blank PO</button>
    </form>
  </div>

  <div class="card">
    <h3>Suppliers</h3>
    {% if suppliers %}
    <table>
      <thead><tr><th>Name</th><th>Contact</th><th>Email</th><th class="num">Lead days</th></tr></thead>
      <tbody>{% for s in suppliers %}<tr><td>{{ s.name }}</td><td>{{ s.contact or '—' }}</td><td>{{ s.email or '—' }}</td><td class="num">{{ s.lead_days }}</td></tr>{% endfor %}</tbody>
    </table>
    {% else %}<div class="muted">No suppliers yet.</div>{% endif %}
    <form class="row" method="POST" action="/purchase-orders/supplier" style="margin-top:12px">
      <input name="name" placeholder="Supplier name" required>
      <input name="contact" placeholder="Contact">
      <input name="email" placeholder="Email">
      <input name="lead_days" placeholder="Lead days" value="14" style="width:90px">
      <button class="btn sec" type="submit">+ Supplier</button>
    </form>
  </div>
</div>
"""

DETAIL = _HEAD + """
{{ nav|safe }}
<div class="wrap">
  <div class="card row" style="justify-content:space-between">
    <div><h2>PO-{{ p.id }} <span class="pill st-{{ p.status }}">{{ p.status|capitalize }}</span></h2>
      <div class="muted">{{ p.supplier_name or 'No supplier' }}{% if p.reference %} · {{ p.reference }}{% endif %} · created {{ (p.created_at or '')[:16].replace('T',' ') }}</div></div>
    <form class="row" method="POST" action="/purchase-orders/{{ p.id }}/status">
      <select name="status">{% for st in statuses %}<option value="{{ st }}" {{ 'selected' if st==p.status else '' }}>{{ st|capitalize }}</option>{% endfor %}</select>
      <button class="btn" type="submit">Update status</button>
      <a class="btn sec" href="/purchase-orders">← All POs</a>
    </form>
  </div>

  <div class="card">
    <h3>Lines</h3>
    {% if lines %}
    <table>
      <thead><tr><th>SKU</th><th>ASIN</th><th>Title</th><th class="num">Qty</th><th class="num">Unit £</th><th class="num">Line £</th><th></th></tr></thead>
      <tbody>
      {% for l in lines %}
        <tr>
          <td>{{ l.canonical_sku or '—' }}</td><td>{{ l.asin or '—' }}</td><td>{{ (l.title or '')[:40] }}</td>
          <td class="num">{{ l.quantity }}</td>
          <td class="num">{% if l.unit_cost is not none %}£{{ '%.2f'|format(l.unit_cost|float) }}{% else %}—{% endif %}</td>
          <td class="num">{% if l.unit_cost is not none %}£{{ '%.2f'|format((l.unit_cost|float)*(l.quantity|int)) }}{% else %}—{% endif %}</td>
          <td class="num"><form method="POST" action="/purchase-orders/{{ p.id }}/line/{{ l.id }}/delete" style="display:inline"><button class="btn sec" style="padding:3px 8px">✕</button></form></td>
        </tr>
      {% endfor %}
      </tbody>
      <tfoot><tr><th colspan="5" class="num">Total</th><th class="num">£{{ '%.2f'|format(total) }}</th><th></th></tr></tfoot>
    </table>
    {% else %}<div class="muted">No lines yet.</div>{% endif %}
    <form class="row" method="POST" action="/purchase-orders/{{ p.id }}/line" style="margin-top:12px">
      <input name="canonical_sku" placeholder="SKU" style="width:130px">
      <input name="asin" placeholder="ASIN" style="width:120px">
      <input name="title" placeholder="Title">
      <input name="quantity" placeholder="Qty" value="1" style="width:70px">
      <input name="unit_cost" placeholder="Unit £" style="width:90px">
      <button class="btn" type="submit">+ Add line</button>
    </form>
  </div>
</div>
"""


@app.route("/purchase-orders")
def purchasing_page():
    return render_template_string(
        LIST, pos=po.list_pos(), suppliers=po.list_suppliers(),
        sugg=po.reorder_suggestions(), target=po.DEFAULT_TARGET_DAYS)


@app.route("/purchase-orders/supplier", methods=["POST"])
def purchasing_add_supplier():
    name = (request.form.get("name") or "").strip()
    if name:
        po.add_supplier(name, request.form.get("contact"), request.form.get("email"),
                        request.form.get("lead_days") or 14, request.form.get("notes"))
    return redirect("/purchase-orders")


@app.route("/purchase-orders/create", methods=["POST"])
def purchasing_create():
    sid = request.form.get("supplier_id") or None
    pid = po.create_po(supplier_id=int(sid) if sid else None,
                       reference=(request.form.get("reference") or "").strip() or None)
    return redirect(f"/purchase-orders/{pid}")


@app.route("/purchase-orders/from-suggestions", methods=["POST"])
def purchasing_from_suggestions():
    sel = request.form.getlist("sel")
    if not sel:
        return redirect("/purchase-orders")
    sid = request.form.get("supplier_id") or None
    pid = po.create_po(supplier_id=int(sid) if sid else None, reference="Reorder")
    sugg = {x["canonical_sku"]: x for x in po.reorder_suggestions()}
    for csku in sel:
        x = sugg.get(csku) or {}
        try:
            qty = int(request.form.get(f"qty_{csku}") or x.get("suggested_qty") or 0)
        except ValueError:
            qty = x.get("suggested_qty") or 0
        po.add_line(pid, canonical_sku=csku, asin=x.get("asin"), title=x.get("title"), quantity=qty)
    return redirect(f"/purchase-orders/{pid}")


@app.route("/purchase-orders/<int:pid>")
def purchasing_detail(pid):
    p, lines = po.get_po(pid)
    if not p:
        return redirect("/purchase-orders")
    total = sum((l.get("unit_cost") or 0) * (l.get("quantity") or 0) for l in lines)
    return render_template_string(DETAIL, p=p, lines=lines, total=total, statuses=po.STATUSES)


@app.route("/purchase-orders/<int:pid>/line", methods=["POST"])
def purchasing_add_line(pid):
    def _f(k):
        v = (request.form.get(k) or "").strip()
        return v or None
    try:
        qty = int(request.form.get("quantity") or 0)
    except ValueError:
        qty = 0
    cost = request.form.get("unit_cost")
    po.add_line(pid, canonical_sku=_f("canonical_sku"), asin=_f("asin"), title=_f("title"),
                quantity=qty, unit_cost=float(cost) if cost else None)
    return redirect(f"/purchase-orders/{pid}")


@app.route("/purchase-orders/<int:pid>/line/<int:lid>/delete", methods=["POST"])
def purchasing_delete_line(pid, lid):
    po.delete_line(lid, pid)
    return redirect(f"/purchase-orders/{pid}")


@app.route("/purchase-orders/<int:pid>/status", methods=["POST"])
def purchasing_status(pid):
    st = request.form.get("status")
    if st in po.STATUSES:
        po.set_status(pid, st)
    return redirect(f"/purchase-orders/{pid}")
