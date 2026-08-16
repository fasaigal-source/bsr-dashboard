"""dashboard_calculator.py — the "/calculator" Scale-up profit calculator.

A PURE client-side tool: model a product line's monthly net profit as price,
volume and staffing change. It reads and writes NOTHING — no DB, no SP-API, no
env vars, and it imports nothing from pl_db / the sync / any Module 1/2/3 data.
The only server involvement is Flask rendering this static page plus the shared
nav; every calculation runs in the browser.
"""
from flask import render_template_string

from dashboard_app import app

# The page is served whole (its own <head>/<style>), matching how every other
# page in this dashboard is structured; the only injected server value is the
# shared nav bar ({{ nav|safe }}), exactly like /channels, /pl, /ppc, etc.
CALC_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Scale-up profit calculator — eStoreAssist</title>
<style>
  :root{
    --ink:#12211b; --ink-soft:#4a5a52; --ink-faint:#7d8b83;
    --line:#e3e8e4; --line-soft:#eef2ee;
    --paper:#ffffff; --panel:#f6f9f6; --panel-2:#eef4ef;
    --pine:#0f6e56; --pine-deep:#0a4a3a; --pine-soft:#e1f2ec;
    --gold:#b07a12; --gold-soft:#faeeda;
    --loss:#a32d2d; --loss-soft:#fcebeb;
    --radius:12px;
    --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  }
  *{box-sizing:border-box;}
  body{
    margin:0; font-family:var(--font); color:var(--ink);
    background:var(--panel); line-height:1.5;
    -webkit-font-smoothing:antialiased;
  }
  .wrap{max-width:820px; margin:0 auto; padding:28px 20px 48px;}
  header{margin-bottom:22px;}
  .eyebrow{font-size:12px; letter-spacing:.14em; text-transform:uppercase; color:var(--pine); font-weight:600; margin:0 0 6px;}
  h1{font-size:26px; font-weight:600; margin:0 0 6px; letter-spacing:-.01em;}
  .sub{font-size:15px; color:var(--ink-soft); margin:0; max-width:56ch;}

  .grid-controls{display:grid; grid-template-columns:1fr 1fr; gap:14px; margin:22px 0;}
  @media (max-width:640px){ .grid-controls{grid-template-columns:1fr;} }

  .card{background:var(--paper); border:1px solid var(--line); border-radius:var(--radius); padding:18px 20px;}
  .card h2{font-size:13px; letter-spacing:.10em; text-transform:uppercase; color:var(--ink-faint); font-weight:600; margin:0 0 14px;}

  .row{margin-bottom:16px;}
  .row:last-child{margin-bottom:0;}
  .row-head{display:flex; justify-content:space-between; align-items:baseline; margin-bottom:7px;}
  .row-head label{font-size:14px; color:var(--ink-soft);}
  .row-head .val{font-size:15px; font-weight:600; color:var(--ink); font-variant-numeric:tabular-nums;}

  input[type=range]{
    -webkit-appearance:none; appearance:none; width:100%; height:4px;
    background:var(--line); border-radius:4px; outline:none; margin:0;
  }
  input[type=range]::-webkit-slider-thumb{
    -webkit-appearance:none; appearance:none; width:20px; height:20px; border-radius:50%;
    background:var(--pine); border:2px solid #fff; box-shadow:0 1px 3px rgba(0,0,0,.2); cursor:pointer;
  }
  input[type=range]::-moz-range-thumb{
    width:20px; height:20px; border-radius:50%; background:var(--pine);
    border:2px solid #fff; box-shadow:0 1px 3px rgba(0,0,0,.2); cursor:pointer;
  }

  .toggle{display:flex; gap:7px; margin-top:10px;}
  .toggle button{
    flex:1; font:inherit; font-size:13px; padding:6px 0; cursor:pointer;
    background:var(--paper); color:var(--ink-soft); border:1px solid var(--line); border-radius:8px;
    transition:all .12s;
  }
  .toggle button.on{background:var(--pine-soft); color:var(--pine-deep); border-color:var(--pine); font-weight:600;}

  .result{
    background:var(--paper); border:1px solid var(--line); border-radius:var(--radius);
    padding:22px 24px; margin-bottom:14px; position:relative; overflow:hidden;
  }
  .result::before{content:""; position:absolute; left:0; top:0; bottom:0; width:5px; background:var(--pine);}
  .result.loss::before{background:var(--loss);}
  .result .label{font-size:13px; color:var(--ink-faint); margin:0 0 4px;}
  .result .big{font-size:42px; font-weight:700; line-height:1.05; letter-spacing:-.02em; font-variant-numeric:tabular-nums; color:var(--pine-deep);}
  .result.loss .big{color:var(--loss);}
  .result .year{font-size:15px; color:var(--ink-soft); margin-top:5px;}
  .warn{margin-top:12px; font-size:13px; color:var(--gold); background:var(--gold-soft); border-radius:8px; padding:9px 12px; display:none;}
  .warn.show{display:block;}

  .stats{display:grid; grid-template-columns:repeat(4,1fr); gap:12px;}
  @media (max-width:640px){ .stats{grid-template-columns:1fr 1fr;} }
  .stat{background:var(--paper); border:1px solid var(--line); border-radius:10px; padding:14px 16px;}
  .stat .k{font-size:12px; color:var(--ink-faint); margin:0 0 4px;}
  .stat .v{font-size:22px; font-weight:600; font-variant-numeric:tabular-nums; margin:0;}
  .stat .note{font-size:11px; color:var(--ink-faint); margin-top:2px;}

  .breakdown{margin-top:14px; background:var(--panel-2); border-radius:10px; padding:14px 18px; font-size:14px;}
  .breakdown .bd-row{display:flex; justify-content:space-between; padding:3px 0; color:var(--ink-soft);}
  .breakdown .bd-row.total{border-top:1px solid var(--line); margin-top:5px; padding-top:8px; color:var(--ink); font-weight:600;}
  .breakdown .bd-row .n{font-variant-numeric:tabular-nums;}

  footer{margin-top:22px; font-size:12px; color:var(--ink-faint); line-height:1.6;}
</style>
</head>
<body>
{{ nav|safe }}
<div class="wrap">
  <header>
    <p class="eyebrow">eStoreAssist</p>
    <h1>Scale-up profit calculator</h1>
    <p class="sub">Model the monthly net profit of a product line as you push price, volume and staffing. Set the per-unit economics for any product, then size the operation around it.</p>
  </header>

  <div class="grid-controls">
    <div class="card">
      <h2>Per-product economics</h2>
      <div class="row">
        <div class="row-head"><label>Selling price (inc-VAT)</label><span class="val" id="p-out">£16.99</span></div>
        <input type="range" id="price" min="4.99" max="29.99" step="0.50" value="16.99" />
      </div>
      <div class="row">
        <div class="row-head"><label>Cost of goods (COGS)</label><span class="val" id="c-out">£4.23</span></div>
        <input type="range" id="cogs" min="0.50" max="20.00" step="0.10" value="4.23" />
      </div>
      <div class="row">
        <div class="row-head"><label>Postage per order</label><span class="val" id="pg-out">£3.02</span></div>
        <input type="range" id="postage" min="0.00" max="12.00" step="0.10" value="3.02" />
      </div>
      <div class="row">
        <div class="row-head"><label>Amazon referral fee</label><span class="val" id="ref-out">15%</span></div>
        <input type="range" id="referral" min="8" max="20" step="1" value="15" />
      </div>
    </div>

    <div class="card">
      <h2>The operation</h2>
      <div class="row">
        <div class="row-head"><label>Orders / month</label><span class="val" id="o-out">5,000</span></div>
        <input type="range" id="orders" min="500" max="8000" step="250" value="5000" />
      </div>
      <div class="row">
        <div class="row-head"><label>Production workers</label><span class="val" id="w-out">4</span></div>
        <input type="range" id="workers" min="1" max="8" step="1" value="4" />
      </div>
      <div class="row">
        <div class="row-head"><label>Wage per worker</label><span class="val" id="wg-out">£1,400</span></div>
        <input type="range" id="wage" min="1000" max="2500" step="50" value="1400" />
        <div class="toggle">
          <button id="per-month" class="on">per month</button>
          <button id="per-week">per week</button>
        </div>
      </div>
      <div class="row">
        <div class="row-head"><label>Rent / month</label><span class="val" id="r-out">£3,000</span></div>
        <input type="range" id="rent" min="1000" max="4000" step="100" value="3000" />
      </div>
    </div>
  </div>

  <div class="result" id="result">
    <p class="label">Net profit per month — this product line</p>
    <div class="big" id="net">£10,799</div>
    <div class="year" id="net-year">≈ £129,588 per year</div>
    <div class="warn" id="cap-warn"></div>
  </div>

  <div class="stats">
    <div class="stat"><p class="k">Profit per order</p><p class="v" id="contrib">£4.36</p></div>
    <div class="stat"><p class="k">Monthly contribution</p><p class="v" id="mc">£21,799</p></div>
    <div class="stat"><p class="k">Wages + rent</p><p class="v" id="fixed">£11,000</p></div>
    <div class="stat"><p class="k">Break-even wage</p><p class="v" id="be">£4,700</p><p class="note">per worker / month</p></div>
  </div>

  <div class="breakdown">
    <div class="bd-row"><span>Net revenue (ex-VAT)</span><span class="n" id="bd-rev">£14.16</span></div>
    <div class="bd-row"><span>− Referral fee</span><span class="n" id="bd-ref">−£2.55</span></div>
    <div class="bd-row"><span>− Postage</span><span class="n" id="bd-pg">−£3.02</span></div>
    <div class="bd-row"><span>− COGS</span><span class="n" id="bd-cogs">−£4.23</span></div>
    <div class="bd-row total"><span>= Profit per order</span><span class="n" id="bd-total">£4.36</span></div>
  </div>

  <footer id="foot"></footer>
</div>

<script>
(function(){
  var wageMode='month';
  var $=function(id){return document.getElementById(id);};
  var gbp=function(n){return '£'+Math.round(n).toLocaleString();};
  var gbp2=function(n){return '£'+n.toFixed(2);};

  function calc(){
    var price=+$('price').value, cogs=+$('cogs').value, postage=+$('postage').value,
        refPct=+$('referral').value, orders=+$('orders').value, workers=+$('workers').value,
        wage=+$('wage').value, rent=+$('rent').value;

    var netRev=price/1.2;
    var referral=price*(refPct/100);
    var contrib=netRev-referral-postage-cogs;
    var mc=contrib*orders;
    var wagePerMonth = wageMode==='week' ? wage*4.333 : wage;
    var wageTotal=wagePerMonth*workers;
    var fixed=wageTotal+rent;
    var net=mc-fixed;
    var beWage=(mc-rent)/workers;

    $('p-out').textContent=gbp2(price);
    $('c-out').textContent=gbp2(cogs);
    $('pg-out').textContent=gbp2(postage);
    $('ref-out').textContent=refPct+'%';
    $('o-out').textContent=orders.toLocaleString();
    $('w-out').textContent=workers;
    $('wg-out').textContent=gbp(wage);
    $('r-out').textContent=gbp(rent);

    $('contrib').textContent=gbp2(contrib);
    $('mc').textContent=gbp(mc);
    $('fixed').textContent=gbp(fixed);
    $('be').textContent=gbp(beWage);

    $('bd-rev').textContent=gbp2(netRev);
    $('bd-ref').textContent='−'+gbp2(referral);
    $('bd-pg').textContent='−'+gbp2(postage);
    $('bd-cogs').textContent='−'+gbp2(cogs);
    $('bd-total').textContent=gbp2(contrib);

    var res=$('result');
    $('net').textContent=gbp(net);
    $('net-year').textContent='≈ '+gbp(net*12)+' per year';
    if(net<0){res.classList.add('loss');} else {res.classList.remove('loss');}

    var capOrders=workers*125*22;
    var warn=$('cap-warn');
    if(orders>capOrders){
      warn.classList.add('show');
      warn.textContent='Above production capacity — '+workers+' workers make about '+capOrders.toLocaleString()+' orders a month (1,000 pillows/day per 2 workers). Add workers or a machine to sell this many.';
    } else { warn.classList.remove('show'); }

    $('foot').textContent='Profit per order = price ÷ 1.2 (ex-VAT) − '+refPct+'% referral − postage − COGS. '+
      'Capacity assumes 2 workers make 1,000 pillows/day (250 packs of 4) over 22 working days. '+
      'COGS may already include some hand-labour; if you also count workers as staff, the wage line slightly double-counts labour, so real profit runs a little above this. Overheads shown are rent only — add other fixed costs to the rent figure for a fuller picture.';
  }

  ['price','cogs','postage','referral','orders','workers','wage','rent'].forEach(function(id){
    $(id).addEventListener('input',calc);
  });
  $('per-month').addEventListener('click',function(){
    wageMode='month'; this.classList.add('on'); $('per-week').classList.remove('on'); calc();
  });
  $('per-week').addEventListener('click',function(){
    wageMode='week'; this.classList.add('on'); $('per-month').classList.remove('on'); calc();
  });
  calc();
})();
</script>
</body>
</html>
"""


@app.route("/calculator")
def scaleup_calculator():
    """Render the static, client-side scale-up calculator. No data dependencies:
    reads/writes nothing, imports no pl_* / sync / Module data — just the page."""
    return render_template_string(CALC_HTML)
