"""dashboard_inventory.py — /inventory: live Amazon stock snapshot.

Shows the cached merchant-listings snapshot (quantity per SKU) with search and a
low-stock-first sort. "Refresh from Amazon" kicks off a background report pull
(module6_inventory.start_refresh_async); a status pill polls until it lands, then
the page reloads. Read-only view of Amazon-held stock; other channels come later.
"""
import logging

from flask import request, redirect, jsonify, render_template_string

from dashboard_app import app
import module6_inventory as inv

log = logging.getLogger(__name__)


PAGE = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Inventory — BSR Repricer</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1180px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 h2{margin:0 0 2px;font-size:19px} .muted{color:#8a94a2;font-size:12.5px}
 .row{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
 .kpis{display:flex;gap:26px;margin-top:6px}
 .kpi b{display:block;font-size:22px} .kpi span{font-size:12px;color:#8a94a2}
 .btn{background:#0e5c5b;color:#fff;border:none;border-radius:8px;padding:9px 16px;font-size:13px;cursor:pointer;text-decoration:none;display:inline-block}
 .btn[disabled]{opacity:.5;cursor:default}
 input.search{padding:8px 11px;border:1px solid #dfe4e9;border-radius:8px;font-size:13px;min-width:240px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:8px 10px;text-align:left;border-bottom:1px solid #eef1f4}
 th{color:#5a6472;font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.03em}
 td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
 tr:hover td{background:#f8fafb}
 .q0{color:#c0392b;font-weight:700} .qlow{color:#c77700;font-weight:600}
 .pill{font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px}
 .p-run{background:#fff6e0;color:#a76b00} .p-done{background:#e4f6ea;color:#1f7a45} .p-err{background:#fdecec;color:#c0392b}
 .chip{font-size:11px;color:#5a6472;background:#f1f3f5;padding:2px 7px;border-radius:6px}
 .empty{text-align:center;color:#8a94a2;padding:40px 0}
</style></head><body>{{ nav|safe }}
<div class="wrap">
  <div class="card">
    <div class="row" style="justify-content:space-between">
      <div>
        <h2>Inventory</h2>
        <div class="muted">Live Amazon stock (merchant listings). {% if s.updated %}Updated {{ s.updated[:16].replace('T',' ') }} UTC{% else %}Not pulled yet{% endif %}.</div>
      </div>
      <div class="row">
        <span id="pill"></span>
        <a class="btn" id="refbtn" href="/inventory/refresh">↻ Refresh from Amazon</a>
      </div>
    </div>
    <div class="kpis">
      <div class="kpi"><b>{{ s.skus }}</b><span>SKUs</span></div>
      <div class="kpi"><b>{{ s.units }}</b><span>units in stock</span></div>
      <div class="kpi"><b>{{ s.oos }}</b><span>out of stock</span></div>
    </div>
  </div>

  <div class="card">
    <form class="row" method="GET" action="/inventory" style="margin-bottom:12px">
      <input class="search" name="q" value="{{ q or '' }}" placeholder="Search SKU, ASIN, or title…">
      <button class="btn" type="submit">Search</button>
      {% if q %}<a class="chip" href="/inventory">clear</a>{% endif %}
      <span class="muted">Lowest stock first · {{ rows|length }} shown</span>
    </form>
    {% if rows %}
    <table>
      <thead><tr>
        <th>SKU</th><th>Canonical</th><th>ASIN</th><th>Title</th>
        <th class="num">Price</th><th class="num">Qty</th><th>Channel</th>
      </tr></thead>
      <tbody>
      {% for r in rows %}
        <tr>
          <td>{{ r.seller_sku }}</td>
          <td>{% if r.canonical_sku and r.canonical_sku != r.seller_sku %}<a href="/pl/sku/{{ r.canonical_sku }}">{{ r.canonical_sku }}</a>{% else %}<span class="muted">—</span>{% endif %}</td>
          <td>{% if r.asin %}<a href="https://www.amazon.co.uk/dp/{{ r.asin }}" target="_blank" rel="noopener">{{ r.asin }}</a>{% else %}—{% endif %}</td>
          <td>{{ (r.title or '')[:60] }}</td>
          <td class="num">{% if r.price is not none %}£{{ '%.2f'|format(r.price|float) }}{% else %}—{% endif %}</td>
          <td class="num"><span class="{{ 'q0' if not r.quantity else ('qlow' if r.quantity|int < 5 else '') }}">{{ r.quantity if r.quantity is not none else '—' }}</span></td>
          <td><span class="chip">{{ r.channel or '—' }}</span></td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div class="empty">
      {% if q %}No listings match “{{ q }}”.{% else %}No inventory pulled yet. Click <b>Refresh from Amazon</b> to fetch your current listings and stock levels.{% endif %}
    </div>
    {% endif %}
  </div>
</div>
<script>
 function paint(st){
   var pill=document.getElementById('pill'), btn=document.getElementById('refbtn');
   var running=false, parts=[];
   for(var k in st){var s=st[k]; if(s.state==='running'){running=true;}
     if(s.state){var cls=s.state==='running'?'p-run':(s.state==='error'?'p-err':'p-done');
       parts.push('<span class="pill '+cls+'">'+k+': '+(s.message||s.state)+'</span>');}}
   pill.innerHTML=parts.join(' ');
   if(running){btn.setAttribute('disabled','');btn.style.pointerEvents='none';}
   else{btn.removeAttribute('disabled');btn.style.pointerEvents='';}
   return running;
 }
 var wasRunning=false;
 function poll(){
   fetch('/inventory/status').then(r=>r.json()).then(function(st){
     var running=paint(st);
     if(wasRunning && !running){location.href='/inventory';return;}
     wasRunning=running;
     if(running) setTimeout(poll,3000);
   }).catch(()=>{});
 }
 poll();
</script>
</body></html>
"""


@app.route("/inventory")
def inventory_page():
    q = request.args.get("q", "").strip() or None
    rows = inv.list_inventory(q=q)
    s = inv.summary()
    return render_template_string(PAGE, rows=rows, s=s, q=q)


@app.route("/inventory/refresh")
def inventory_refresh():
    inv.start_refresh_async()
    return redirect("/inventory")


@app.route("/inventory/status")
def inventory_status():
    return jsonify(inv.get_status())
