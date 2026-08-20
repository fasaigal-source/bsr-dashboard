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
 .sb{font-size:10.5px;font-weight:700;padding:2px 8px;border-radius:20px;white-space:nowrap}
 .s-out{background:#fdecec;color:#c0392b} .s-reorder{background:#fde7cf;color:#b45309}
 .s-low{background:#fff6e0;color:#a76b00} .s-ok{background:#e4f6ea;color:#1f7a45}
 .s-idle{background:#eef1f4;color:#8a94a2} .s-unknown{background:#eef1f4;color:#8a94a2}
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
      <div class="kpi"><b style="color:#c0392b">{{ reorder }}</b><span>need reorder</span></div>
      <div class="kpi"><b>{{ s.oos }}</b><span>out of stock</span></div>
    </div>
  </div>

  <div class="card">
    <form class="row" method="GET" action="/inventory" style="margin-bottom:12px">
      <input class="search" name="q" value="{{ q or '' }}" placeholder="Search SKU, ASIN, or title…">
      <button class="btn" type="submit">Search</button>
      {% if filter == 'reorder' %}<input type="hidden" name="filter" value="reorder">{% endif %}
      {% if q %}<a class="chip" href="/inventory{{ '?filter=reorder' if filter=='reorder' else '' }}">clear search</a>{% endif %}
      {% if filter == 'reorder' %}<a class="chip" href="/inventory{{ ('?q='+q) if q else '' }}">show all</a>
      {% else %}<a class="chip" href="/inventory?filter=reorder{{ ('&q='+q) if q else '' }}">reorder only</a>{% endif %}
      <span class="muted">Reorder first · {{ rows|length }} shown · sold = units in trailing days</span>
    </form>
    {% if rows %}
    <table>
      <thead><tr>
        <th>SKU</th><th>ASIN</th><th>Title</th>
        <th class="num">Qty</th><th class="num">7d</th><th class="num">14d</th><th class="num">30d</th>
        <th class="num">Cover</th><th>Status</th>
      </tr></thead>
      <tbody>
      {% for r in rows %}
        <tr>
          <td>{% if r.canonical_sku and r.canonical_sku != r.seller_sku %}<a href="/pl/sku/{{ r.canonical_sku }}">{{ r.seller_sku }}</a>{% else %}{{ r.seller_sku }}{% endif %}</td>
          <td>{% if r.asin %}<a href="https://www.amazon.co.uk/dp/{{ r.asin }}" target="_blank" rel="noopener">{{ r.asin }}</a>{% else %}—{% endif %}</td>
          <td>{{ (r.title or '')[:46] }}</td>
          <td class="num"><span class="{{ 'q0' if not r.quantity else ('qlow' if r.quantity|int < 5 else '') }}">{{ r.quantity if r.quantity is not none else '—' }}</span></td>
          <td class="num">{{ r.u7 }}</td>
          <td class="num">{{ r.u14 }}</td>
          <td class="num">{{ r.u30 }}</td>
          <td class="num">{% if r.days_cover is not none %}{{ '%.0f'|format(r.days_cover) }}d{% else %}—{% endif %}</td>
          <td><span class="sb s-{{ r.stock_status }}">{{
              {'out':'Out','reorder':'Reorder','low':'Low','ok':'OK','idle':'No sales','unknown':'—'}[r.stock_status] }}</span></td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}
    <div class="empty">
      {% if filter == 'reorder' %}Nothing needs reordering right now. 🎉{% elif q %}No listings match “{{ q }}”.{% else %}No inventory pulled yet. Click <b>Refresh from Amazon</b> to fetch your current listings and stock levels.{% endif %}
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


_STATUS_ORDER = {"out": 0, "reorder": 1, "low": 2, "idle": 3, "ok": 4, "unknown": 5}


@app.route("/inventory")
def inventory_page():
    q = request.args.get("q", "").strip() or None
    filt = request.args.get("filter", "").strip() or None
    rows = inv.list_inventory(q=q)
    reorder = inv.enrich_with_prediction(rows)
    # most-urgent first: status bucket, then fewest days of cover
    rows.sort(key=lambda r: (_STATUS_ORDER.get(r.get("stock_status"), 9),
                             r.get("days_cover") if r.get("days_cover") is not None else 1e9))
    if filt == "reorder":
        rows = [r for r in rows if r.get("stock_status") in ("out", "reorder")]
    s = inv.summary()
    return render_template_string(PAGE, rows=rows, s=s, q=q, filter=filt, reorder=reorder)


@app.route("/inventory/refresh")
def inventory_refresh():
    inv.start_refresh_async()
    return redirect("/inventory")


@app.route("/inventory/status")
def inventory_status():
    return jsonify(inv.get_status())
