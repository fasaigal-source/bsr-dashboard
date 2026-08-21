"""dashboard_ppc_data.py — /ppc/data: pull + view the full Amazon Ads API dataset.

Connection test, a background "Pull from Amazon" (campaigns/ad-groups/keywords/targets/
negatives + campaign/targeting/search-term reports), a status pill that polls, and
preview tables (top campaigns by spend, top search terms). Feeds the optimiser phases.
"""
import logging

from flask import request, redirect, jsonify, render_template_string

from dashboard_app import app
import ppc_ads_data as pad
import ppc_ads_api

log = logging.getLogger(__name__)

PAGE = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>PPC data — BSR Repricer</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1200px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 h2{margin:0 0 3px;font-size:18px} h3{margin:0 0 8px;font-size:14px} .muted{color:#8a94a2;font-size:12.5px}
 .row{display:flex;gap:14px;align-items:center;flex-wrap:wrap;justify-content:space-between}
 .kpis{display:flex;gap:24px;margin-top:8px;flex-wrap:wrap}
 .kpi b{display:block;font-size:20px} .kpi span{font-size:11.5px;color:#8a94a2}
 .btn{background:#0e5c5b;color:#fff;border:none;border-radius:8px;padding:9px 16px;font-size:13px;cursor:pointer;text-decoration:none;display:inline-block}
 .btn[disabled]{opacity:.5;cursor:default} .btn.sec{background:#eef1f4;color:#0e5c5b}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:left;border-bottom:1px solid #eef1f4}
 th{color:#5a6472;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.03em}
 td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
 tr:hover td{background:#f8fafb}
 .pill{font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px;margin-left:6px}
 .p-run{background:#fff6e0;color:#a76b00}.p-done{background:#e4f6ea;color:#1f7a45}.p-err{background:#fdecec;color:#c0392b}
 .ok{color:#1f7a45;font-weight:600}.no{color:#c0392b;font-weight:600}
 .hi{color:#c0392b;font-weight:700}
</style></head><body>{{ nav|safe }}
<div class="wrap">
  <div class="card">
    <div class="row">
      <div><h2>PPC data</h2><div class="muted">Full Amazon Ads pull (last {{ days }} days). {% if s.updated %}Updated {{ s.updated[:16].replace('T',' ') }} UTC{% else %}Not pulled yet{% endif %}.</div></div>
      <div class="row" style="gap:8px">
        <span id="pill"></span>
        <a class="btn sec" href="/ppc/data/test">Test connection</a>
        <a class="btn" id="pullbtn" href="/ppc/data/pull">↻ Pull from Amazon</a>
      </div>
    </div>
    <div class="kpis">
      <div class="kpi"><b>{{ s.campaigns }}</b><span>campaigns</span></div>
      <div class="kpi"><b>{{ s.keywords }}</b><span>keywords</span></div>
      <div class="kpi"><b>{{ s.targets }}</b><span>targets</span></div>
      <div class="kpi"><b>{{ s.negatives }}</b><span>negatives</span></div>
      <div class="kpi"><b>{{ s.search_terms }}</b><span>search terms</span></div>
    </div>
    {% if conn is not none %}
    <div class="muted" style="margin-top:12px">Connection: {% if conn.ok %}<span class="ok">● OK ({{ conn.status }}){% if conn.profiles %} · {{ conn.profiles|length }} profile(s){% endif %}</span>{% else %}<span class="no">failed{% if conn.status %} · {{ conn.status }}{% endif %}</span> — {{ conn.detail }}{% endif %}</div>
    {% endif %}
  </div>

  <div class="card">
    <h3>Top campaigns by spend</h3>
    {% if campaigns %}
    <table>
      <thead><tr><th>Campaign</th><th>State</th><th class="num">Budget</th><th class="num">Impr</th><th class="num">Clicks</th><th class="num">Spend</th><th class="num">Sales</th><th class="num">ACOS</th></tr></thead>
      <tbody>
      {% for c in campaigns %}
        <tr>
          <td>{{ (c.name or '—')[:46] }}</td>
          <td>{{ c.state or '—' }}</td>
          <td class="num">{% if c.budget is not none %}£{{ '%.0f'|format(c.budget|float) }}{% else %}—{% endif %}</td>
          <td class="num">{{ c.impressions if c.impressions is not none else '—' }}</td>
          <td class="num">{{ c.clicks if c.clicks is not none else '—' }}</td>
          <td class="num">{% if c.cost is not none %}£{{ '%.2f'|format(c.cost|float) }}{% else %}—{% endif %}</td>
          <td class="num">{% if c.sales is not none %}£{{ '%.2f'|format(c.sales|float) }}{% else %}—{% endif %}</td>
          <td class="num">{% if c.acos is not none %}<span class="{{ 'hi' if c.acos > 40 else '' }}">{{ '%.0f'|format(c.acos) }}%</span>{% else %}—{% endif %}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}<div class="muted">No campaign data yet — click <b>Pull from Amazon</b>.</div>{% endif %}
  </div>

  <div class="card">
    <h3>Search terms by spend</h3>
    {% if terms %}
    <table>
      <thead><tr><th>Search term</th><th>Matched keyword</th><th>Match</th><th class="num">Clicks</th><th class="num">Spend</th><th class="num">Sales</th><th class="num">ACOS</th></tr></thead>
      <tbody>
      {% for t in terms %}
        <tr>
          <td>{{ (t.search_term or '')[:48] }}</td>
          <td>{{ (t.keyword or '—')[:28] }}</td>
          <td>{{ t.match_type or '—' }}</td>
          <td class="num">{{ t.clicks if t.clicks is not none else '—' }}</td>
          <td class="num">{% if t.cost is not none %}£{{ '%.2f'|format(t.cost|float) }}{% else %}—{% endif %}</td>
          <td class="num">{% if t.sales is not none %}£{{ '%.2f'|format(t.sales|float) }}{% else %}—{% endif %}</td>
          <td class="num">{% if t.acos is not none %}<span class="{{ 'hi' if t.acos > 40 else '' }}">{{ '%.0f'|format(t.acos) }}%</span>{% elif t.cost %}<span class="hi">no sale</span>{% else %}—{% endif %}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}<div class="muted">No search-term data yet.</div>{% endif %}
    <div class="muted" style="margin-top:10px">This dataset feeds the optimiser (next phase): bid moves, negatives, harvests and pauses will be recommended from it.</div>
  </div>
</div>
<script>
 function paint(st){
   var pill=document.getElementById('pill'), btn=document.getElementById('pullbtn'), running=false, parts=[];
   for(var k in st){var s=st[k]; if(s.state==='running')running=true;
     if(s.state){var c=s.state==='running'?'p-run':(s.state==='error'?'p-err':'p-done');
       parts.push('<span class="pill '+c+'">'+k+': '+(s.message||s.state)+'</span>');}}
   pill.innerHTML=parts.join(' ');
   if(running){btn.setAttribute('disabled','');btn.style.pointerEvents='none';}else{btn.removeAttribute('disabled');btn.style.pointerEvents='';}
   return running;
 }
 var was=false;
 function poll(){fetch('/ppc/data/status').then(r=>r.json()).then(function(st){
   var run=paint(st); if(was&&!run){location.href='/ppc/data';return;} was=run; if(run)setTimeout(poll,3000);
 }).catch(()=>{});}
 poll();
</script>
</body></html>
"""


@app.route("/ppc/data")
def ppc_data_page():
    s = pad.summary()
    return render_template_string(
        PAGE, s=s, days=pad.DEFAULT_WINDOW_DAYS, conn=None,
        campaigns=pad.top_campaigns(limit=50), terms=pad.top_search_terms(limit=100))


@app.route("/ppc/data/test")
def ppc_data_test():
    ids = pad._accounts() or ["default"]
    conn = pad.test_connection(ids[0])
    s = pad.summary()
    return render_template_string(
        PAGE, s=s, days=pad.DEFAULT_WINDOW_DAYS, conn=conn,
        campaigns=pad.top_campaigns(limit=50), terms=pad.top_search_terms(limit=100))


@app.route("/ppc/data/pull")
def ppc_data_pull():
    pad.start_pull_async()
    return redirect("/ppc/data")


@app.route("/ppc/data/status")
def ppc_data_status():
    return jsonify(pad.get_status())
