"""dashboard_ppc_objective.py — /ppc/objective: set the optimiser goal + preview per-SKU targets.

Pick the objective (profit / target-ACOS / grow-sales) and, for profit mode, the margin to
keep. Shows each SKU's break-even ACOS and the resulting target ACOS from your live P&L, plus
the account-wide default used for anything not mapped to a SKU. Phase 3 uses these targets.
"""
import logging

from flask import request, redirect, render_template_string

from dashboard_app import app
import ppc_objective as pobj

log = logging.getLogger(__name__)

PAGE = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>PPC optimiser goal — BSR Repricer</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1000px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 h2{margin:0 0 3px;font-size:18px} h3{margin:0 0 8px;font-size:14px} .muted{color:#8a94a2;font-size:12.5px}
 .opt{display:block;border:1px solid #e4e8ec;border-radius:10px;padding:12px 14px;margin:8px 0;cursor:pointer}
 .opt.sel{border-color:#0e5c5b;background:#f2fbfa}
 .opt b{font-size:14px} .opt .d{font-size:12px;color:#5a6472}
 label{font-size:12px;color:#5a6472} input{padding:7px 9px;border:1px solid #dfe4e9;border-radius:7px;font-size:13px;width:90px}
 .btn{background:#0e5c5b;color:#fff;border:none;border-radius:8px;padding:9px 16px;font-size:13px;cursor:pointer;margin-top:12px}
 .row{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:left;border-bottom:1px solid #eef1f4}
 th{color:#5a6472;font-weight:600;font-size:11px;text-transform:uppercase}
 td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
 .big{font-size:22px;font-weight:700;color:#0e5c5b}
</style></head><body>{{ nav|safe }}
<div class="wrap">
  <div class="card"><h2>PPC optimiser — goal</h2>
    <div class="muted">What should the optimiser aim for? This sets the target ACOS each keyword/target is measured against. <a href="/ppc/data">← PPC data</a></div>
  </div>

  <form method="POST" action="/ppc/objective/save">
  <div class="card">
    <h3>Objective</h3>
    <label class="opt {{ 'sel' if o.mode=='profit' else '' }}">
      <input type="radio" name="mode" value="profit" {{ 'checked' if o.mode=='profit' else '' }}>
      <b>Maximise profit (recommended)</b>
      <div class="d">Each SKU's target ACOS comes from its own margin — spend on ads only up to the point that still leaves your chosen profit. Uses your live COGS/fees/postage.</div>
      <div class="row" style="margin-top:8px"><label>Keep this profit margin:</label>
        <input name="target_margin" value="{{ '%.0f'|format(o.target_margin*100) }}">%</div>
    </label>
    <label class="opt {{ 'sel' if o.mode=='acos' else '' }}">
      <input type="radio" name="mode" value="acos" {{ 'checked' if o.mode=='acos' else '' }}>
      <b>Target ACOS</b>
      <div class="d">One flat ACOS goal for everything.</div>
      <div class="row" style="margin-top:8px"><label>Target ACOS:</label>
        <input name="target_acos" value="{{ '%.0f'|format(o.target_acos*100) }}">%</div>
    </label>
    <label class="opt {{ 'sel' if o.mode=='sales' else '' }}">
      <input type="radio" name="mode" value="sales" {{ 'checked' if o.mode=='sales' else '' }}>
      <b>Grow sales</b>
      <div class="d">Spend right up to break-even (zero margin) to push volume/rank, within a daily budget.</div>
      <div class="row" style="margin-top:8px"><label>Daily budget (optional):</label>
        £<input name="daily_budget" value="{{ '%.0f'|format(o.daily_budget) }}"></div>
    </label>
    <button class="btn" type="submit">Save goal</button>
  </div>
  </form>

  <div class="card">
    <h3>Account default target ACOS</h3>
    <div class="big">{% if default_acos is not none %}{{ '%.0f'|format(default_acos*100) }}%{% else %}—{% endif %}</div>
    <div class="muted">Applied to keywords/campaigns we can't tie to a specific SKU. {% if not targets %}<b>No P&amp;L data in range yet</b> — run the P&amp;L sync so profit-mode targets can be computed.{% endif %}</div>
  </div>

  <div class="card">
    <h3>Per-SKU targets <span class="muted">(from your live P&amp;L)</span></h3>
    {% if targets %}
    <table>
      <thead><tr><th>Canonical SKU</th><th class="num">Units</th><th class="num">Break-even ACOS</th><th class="num">Target ACOS</th></tr></thead>
      <tbody>
      {% for sku, t in targets %}
        <tr>
          <td><a href="/pl/sku/{{ sku }}">{{ sku }}</a></td>
          <td class="num">{{ t.units }}</td>
          <td class="num">{% if t.breakeven_acos is not none %}{{ '%.0f'|format(t.breakeven_acos*100) }}%{% else %}—{% endif %}</td>
          <td class="num"><b>{% if t.target_acos is not none %}{{ '%.0f'|format(t.target_acos*100) }}%{% else %}—{% endif %}</b></td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    <div class="muted" style="margin-top:8px">Break-even ACOS = the point ads stop being profitable for that SKU. Target ACOS is what the optimiser will push each keyword toward.</div>
    {% else %}<div class="muted">No per-SKU targets yet — needs settled sales in the P&amp;L.</div>{% endif %}
  </div>
</div></body></html>
"""


@app.route("/ppc/objective")
def ppc_objective_page():
    o = pobj.get_objective()
    tmap = pobj.sku_targets()
    # top by revenue
    targets = sorted(tmap.items(), key=lambda kv: -(kv[1].get("rev") or 0))[:80]
    return render_template_string(PAGE, o=o, targets=targets,
                                  default_acos=pobj.account_default_acos())


@app.route("/ppc/objective/save", methods=["POST"])
def ppc_objective_save():
    def _pct(name):
        v = (request.form.get(name) or "").strip().replace("%", "")
        try:
            return float(v) / 100.0
        except ValueError:
            return None
    def _num(name):
        v = (request.form.get(name) or "").strip().replace("£", "")
        try:
            return float(v)
        except ValueError:
            return None
    pobj.set_objective(mode=request.form.get("mode"),
                       target_acos=_pct("target_acos"),
                       target_margin=_pct("target_margin"),
                       daily_budget=_num("daily_budget"))
    return redirect("/ppc/objective")
