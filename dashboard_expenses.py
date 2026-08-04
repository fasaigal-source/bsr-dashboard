"""dashboard_expenses.py — Expenses / Overheads page (Module 2 side-car).

Captures fixed business overheads (recurring monthly + one-off) so the P&L summary
can show a true business-wide bottom line. Own data only — no Amazon, no credentials,
no writes anywhere but the local overheads table. Registered on the shared `app`.
"""
from flask import request, redirect, render_template_string, flash

from dashboard_app import app
import pl_expenses
from pl_db import get_accounts


EXPENSES_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Expenses / Overheads — BSR Repricer</title>
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1200px;margin:22px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:20px 22px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:18px}
 h2{margin:0 0 4px;font-size:17px} .muted{color:#8a94a2;font-size:13px}
 .flash{background:#e7f6ee;color:#166b3d;padding:10px 14px;border-radius:8px;margin-bottom:14px;font-weight:600}
 label{font-size:12px;color:#5a6472;display:block;margin-bottom:3px}
 select,input[type=text],input[type=number],input[type=date]{padding:8px 10px;border:1px solid #dde3e9;border-radius:8px;font-size:14px;background:#fff;width:100%}
 .row{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;align-items:end}
 .btn{background:#0e5c5b;color:#eafcfb;border:none;border-radius:8px;padding:9px 16px;font-weight:600;cursor:pointer;font-size:14px}
 .btn.sm{padding:5px 10px;font-size:12px} .btn.grey{background:#6b7684} .btn.red{background:#9e2d3c}
 table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}
 th,td{padding:8px 9px;text-align:right;border-top:1px solid #eef1f4;white-space:nowrap}
 th:first-child,td:first-child{text-align:left} th{color:#8a94a2;font-size:11px;text-transform:uppercase}
 .kpis{display:flex;gap:14px;flex-wrap:wrap;margin-top:6px}
 .kpi{flex:1;min-width:150px;background:#f7fafa;border:1px solid #e4edec;border-radius:10px;padding:12px 14px}
 .kpi .lab{font-size:11px;color:#8a94a2;text-transform:uppercase;letter-spacing:.4px} .kpi .val{font-size:20px;font-weight:800;margin-top:3px;font-family:ui-monospace,Menlo,monospace}
 .pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;background:#eef4f4;color:#0e5c5b}
 .pill.shared{background:#f3ecfb;color:#6b3fa0}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  {% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="flash">{{ m }}</div>{% endfor %}{% endwith %}

  <div class="card">
    <h2>Expenses / Overheads</h2>
    <div class="muted">Fixed business overheads the per-order P&amp;L doesn't include. Kept <b>separate</b> from per-order COGS —
      never mixed into a product's cost; subtracted at the <a href="/pl">P&amp;L</a> summary level only, business-wide.
      Recurring = a monthly figure (pro-rated to whatever date range you view). One-off = a single dated cost.</div>
    <div class="kpis">
      <div class="kpi"><div class="lab">Monthly recurring run-rate</div><div class="val">£{{ "%.2f"|format(monthly_runrate) }}</div></div>
      <div class="kpi"><div class="lab">Recurring lines</div><div class="val">{{ recurring|length }}</div></div>
      <div class="kpi"><div class="lab">One-off entries</div><div class="val">{{ oneoffs|length }}</div></div>
    </div>
    <div class="muted" style="margin-top:8px;">Run-rate = sum of currently-active monthly overheads (across all accounts). The <a href="/pl">P&amp;L page</a> shows the period-pro-rated total for the dates you're viewing.</div>
  </div>

  <div class="card">
    <h2 style="font-size:15px;">Add an expense</h2>
    <form method="POST" action="/pl/expenses/add" style="margin-top:10px;">
      <div class="row">
        <div><label>Type</label>
          <select name="etype" id="kindSel" onchange="expToggleKind()">
            <option value="monthly">Recurring — monthly</option>
            <option value="weekly">Recurring — weekly</option>
            <option value="oneoff">One-off (dated)</option>
          </select></div>
        <div><label>Category</label><input type="text" name="category" list="catList" placeholder="rent, labour, packaging…" required>
          <datalist id="catList"><option>Warehouse rent</option><option>Labour</option><option>Utilities</option><option>Software</option><option>Packaging</option><option>Prep centre</option><option>Equipment</option></datalist></div>
        <div><label>Account</label>
          <select name="account_id">
            {% for a in accounts %}<option value="{{ a.account_id }}">{{ a.account_id }}</option>{% endfor %}
            <option value="shared">shared</option>
          </select></div>
        <div><label>Amount (£)<span id="amtHint" class="muted"> /month</span></label><input type="number" step="0.01" min="0" name="amount" required></div>
        <div id="fldStart"><label>Start date</label><input type="date" name="start_date"></div>
        <div id="fldEnd"><label>End date <span class="muted">(optional)</span></label><input type="date" name="end_date"></div>
      </div>
      <div class="row" style="margin-top:12px;">
        <div id="fldOn" style="display:none;"><label>Date</label><input type="date" name="on_date"></div>
        <div style="grid-column:span 4;"><label>Note (optional)</label><input type="text" name="note" placeholder="e.g. rent increase from Aug; bulk box order 500 units"></div>
        <div><label>&nbsp;</label><button class="btn" type="submit">Add expense</button></div>
      </div>
    </form>
  </div>

  <div class="card">
    <h2 style="font-size:15px;">Recurring overheads</h2>
    <div class="muted">These apply to <b>every</b> month/week from their start date onward — pro-rated to whatever date range you view on the <a href="/pl">P&amp;L</a>. "To date" shows what's accrued from the start until today, so you can see it really is charged every period.</div>
    {% if recurring %}
    <table style="margin-top:8px;">
      <thead><tr><th>Category</th><th>Account</th><th>Amount</th><th>Every</th><th>Active from</th><th>To date</th><th>Until</th><th>Note</th><th></th></tr></thead>
      <tbody>
      {% for r in recurring %}
        <tr>
          <td>{{ r.category }}</td>
          <td><span class="pill {{ 'shared' if r.account_id=='shared' else '' }}">{{ r.account_id }}</span></td>
          <td>£{{ "%.2f"|format(r.amount or 0) }}</td>
          <td>{{ r.frequency or 'monthly' }}</td>
          <td>{{ r.start_date or '—' }}</td>
          <td title="Accrued from {{ r.start_date }} to today">£{{ "%.2f"|format(r.to_date or 0) }}</td>
          <td>
            {% if r.end_date %}{{ r.end_date }}{% else %}
            <form method="POST" action="/pl/expenses/end" style="display:inline-flex;gap:4px;align-items:center;">
              <input type="hidden" name="id" value="{{ r.id }}"><input type="date" name="end_date" required style="width:150px;padding:4px 6px;">
              <button class="btn sm grey" type="submit">Set end</button>
            </form>{% endif %}
          </td>
          <td class="muted" style="text-align:left;white-space:normal;">{{ r.note or '' }}</td>
          <td><form method="POST" action="/pl/expenses/delete" onsubmit="return confirm('Delete this overhead?');" style="display:inline;"><input type="hidden" name="id" value="{{ r.id }}"><button class="btn sm red" type="submit">Delete</button></form></td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}<div class="muted" style="margin-top:8px;">No recurring overheads yet.</div>{% endif %}
  </div>

  <div class="card">
    <h2 style="font-size:15px;">One-off expenses</h2>
    {% if oneoffs %}
    <table>
      <thead><tr><th>Category</th><th>Account</th><th>Date</th><th>Amount</th><th>Note</th><th></th></tr></thead>
      <tbody>
      {% for r in oneoffs %}
        <tr>
          <td>{{ r.category }}</td>
          <td><span class="pill {{ 'shared' if r.account_id=='shared' else '' }}">{{ r.account_id }}</span></td>
          <td>{{ r.on_date or '—' }}</td>
          <td>£{{ "%.2f"|format(r.amount or 0) }}</td>
          <td class="muted" style="text-align:left;white-space:normal;">{{ r.note or '' }}</td>
          <td><form method="POST" action="/pl/expenses/delete" onsubmit="return confirm('Delete this expense?');" style="display:inline;"><input type="hidden" name="id" value="{{ r.id }}"><button class="btn sm red" type="submit">Delete</button></form></td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% else %}<div class="muted" style="margin-top:8px;">No one-off expenses yet.</div>{% endif %}
  </div>
</div>
<script>
  function expToggleKind(){
    var k = document.getElementById("kindSel").value;
    var rec = (k === "monthly" || k === "weekly");
    document.getElementById("fldStart").style.display = rec ? "" : "none";
    document.getElementById("fldEnd").style.display = rec ? "" : "none";
    document.getElementById("fldOn").style.display = rec ? "none" : "";
    document.getElementById("amtHint").textContent = k === "weekly" ? " /week" : (k === "monthly" ? " /month" : " (total)");
  }
  expToggleKind();
</script>
</body></html>
"""


@app.route("/pl/expenses")
def expenses_page():
    try:
        accounts = get_accounts()
    except Exception:
        accounts = []
    rows = pl_expenses.list_overheads()
    recurring = [dict(r) for r in rows if r["kind"] == "recurring"]
    for r in recurring:
        r["to_date"] = pl_expenses.to_date(r)
    oneoffs = [r for r in rows if r["kind"] == "oneoff"]
    runrate = pl_expenses.monthly_run_rate(rows)   # weekly folded in as ×52/12
    return render_template_string(
        EXPENSES_HTML, accounts=accounts, recurring=recurring, oneoffs=oneoffs,
        monthly_runrate=runrate)


@app.route("/pl/expenses/add", methods=["POST"])
def expenses_add():
    f = request.form
    etype = (f.get("etype") or f.get("kind") or "monthly").lower()
    if etype in ("monthly", "weekly"):
        kind, frequency = "recurring", etype
    else:
        kind, frequency = "oneoff", None
    try:
        pl_expenses.add_overhead(
            account_id=f.get("account_id") or "shared",
            kind=kind, frequency=frequency, category=f.get("category"),
            amount=f.get("amount"), start_date=f.get("start_date"),
            end_date=f.get("end_date"), on_date=f.get("on_date"), note=f.get("note"))
        flash("Expense added.")
    except Exception as e:
        flash(f"Could not add expense: {e}")
    return redirect("/pl/expenses")


@app.route("/pl/expenses/end", methods=["POST"])
def expenses_end():
    try:
        pl_expenses.set_end_date(int(request.form.get("id")), request.form.get("end_date"))
        flash("End date set — the overhead stops pro-rating after that date.")
    except Exception as e:
        flash(f"Could not set end date: {e}")
    return redirect("/pl/expenses")


@app.route("/pl/expenses/delete", methods=["POST"])
def expenses_delete():
    try:
        pl_expenses.delete_overhead(int(request.form.get("id")))
        flash("Expense deleted.")
    except Exception as e:
        flash(f"Could not delete: {e}")
    return redirect("/pl/expenses")
