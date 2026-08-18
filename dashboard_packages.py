"""dashboard_packages.py — Module 5a package-defaults settings page ("/packages").

Per-canonical parcel WEIGHT + DIMENSIONS used for Amazon Buy-Shipping quotes. Amazon's
catalogue has no usable package data for these FBM listings (only unfolded item sizes, no
weight), so this page is where YOUR OWN data lives — filled inline or in bulk via CSV
export/import. Seed-once, dashboard is the source of truth (same pattern as COGS/prices).

Client-of: module5_labels_db (storage) + cogs_aliases (canonical resolution). No SP-API.
"""
import io
import csv
import logging

from flask import request, redirect, flash, Response, render_template_string

from dashboard_app import app
import db
import pl_cogs
import pl_tracker
import module5_labels_db as m5

log = logging.getLogger(__name__)


def _accounts():
    ids = []
    try:
        cfg = pl_tracker.load_config()
        ids = [a["account_id"] for a in pl_tracker.get_effective_accounts(cfg)]
    except Exception:
        pass
    for r in m5.list_package_defaults():          # include any account already with data
        if r["account_id"] not in ids:
            ids.append(r["account_id"])
    return ids or ["M4Mart_UK"]


def active_canonicals():
    """{canonical: representative_asin} for CURRENT active listings — the products that
    need package data. Account-agnostic (physical parcels), resolved via cogs_aliases."""
    out = {}
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT variant_sku, asin FROM cogs_sku_asin WHERE source='active_listings_report'"
        ).fetchall()
        for r in rows:
            asin = (r["asin"] or "").strip()
            if not asin:
                continue
            canon = pl_cogs.resolve_to_canonical(r["variant_sku"], conn=conn)
            out.setdefault(canon, asin)
    except Exception as e:
        log.warning("active_canonicals failed: %s", e)
    finally:
        conn.close()
    return out


def _rows_for(account):
    """Every active canonical (plus any extra that already has a saved default), merged
    with saved package data, for display/export."""
    defaults = {d["canonical_sku"]: d for d in m5.list_package_defaults(account)}
    universe = active_canonicals()
    rows = []
    for c in sorted(set(universe) | set(defaults)):
        d = defaults.get(c) or {}
        rows.append(dict(
            canonical_sku=c,
            asin=d.get("asin") or universe.get(c) or "",
            weight_g=d.get("weight_g"), length_cm=d.get("length_cm"),
            width_cm=d.get("width_cm"), height_cm=d.get("height_cm"),
            source=d.get("source"),
            complete=all(d.get(k) is not None for k in ("weight_g", "length_cm", "width_cm", "height_cm")),
        ))
    return rows


def _num(v, cast):
    v = (v or "").strip()
    return cast(v) if v not in ("", None) else None


PAGE = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Package defaults — BSR Repricer</title>
<style>
 *{box-sizing:border-box}
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:1100px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 h2{margin:0 0 4px;font-size:18px} .muted{color:#8a94a2;font-size:13px}
 .bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:10px 0}
 .btn{background:#0e5c5b;color:#fff;border:none;border-radius:7px;padding:7px 12px;font-size:13px;cursor:pointer;text-decoration:none}
 .btn.sec{background:#eef4f4;color:#0e5c5b;border:1px solid #cfe3e2}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:6px 8px;text-align:left;border-top:1px solid #eef1f4;white-space:nowrap}
 th{color:#8a94a2;font-size:11px;text-transform:uppercase}
 input.n{width:74px;padding:4px 6px;font-size:12px;border:1px solid #dfe4e9;border-radius:5px;text-align:right}
 tr.miss{background:#fbf3e0}
 .pill{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px}
 .ok{background:#e4f6ec;color:#1f7a45}.no{background:#fbe7c6;color:#8a5906}
 select{padding:5px 8px;border-radius:6px}
</style></head><body>
{{ nav|safe }}
<div class="wrap">
  <div class="card">
    <h2>Package defaults <span class="muted">— parcel weight &amp; dimensions for Buy-Shipping quotes</span></h2>
    <div class="muted">Amazon has no usable package data for these listings, so enter your own. Fill inline and Save a row,
      or use <b>Export CSV → fill in a spreadsheet → Import CSV</b> for the whole catalogue in one pass.
      Weight in <b>grams</b>, dimensions in <b>cm</b>.</div>
    <div class="bar">
      <form method="GET" action="/packages" style="margin:0">
        <label class="muted">Account:
          <select name="account" onchange="this.form.submit()">
            {% for a in accounts %}<option value="{{ a }}" {{ 'selected' if a==account else '' }}>{{ a }}</option>{% endfor %}
          </select>
        </label>
      </form>
      <a class="btn sec" href="/packages/export.csv?account={{ account }}">⬇ Export CSV</a>
      <form method="POST" action="/packages/import" enctype="multipart/form-data" style="margin:0;display:flex;gap:6px;align-items:center">
        <input type="hidden" name="account" value="{{ account }}">
        <input type="file" name="csv" accept=".csv" required>
        <button class="btn" type="submit">⬆ Import CSV</button>
      </form>
      <span class="muted">{{ complete }}/{{ total }} products have complete data</span>
    </div>
  </div>

  <div class="card">
    <table>
      <tr><th>Canonical SKU</th><th>ASIN</th><th>Weight (g)</th><th>Length</th><th>Width</th><th>Height</th><th>Status</th><th></th></tr>
      {% for r in rows %}
      <tr class="{{ 'miss' if not r.complete else '' }}">
        <form method="POST" action="/packages/save" style="display:contents">
        <input type="hidden" name="account" value="{{ account }}">
        <input type="hidden" name="canonical_sku" value="{{ r.canonical_sku }}">
        <td><b>{{ r.canonical_sku }}</b></td>
        <td class="muted">{{ r.asin }}<input type="hidden" name="asin" value="{{ r.asin }}"></td>
        <td><input class="n" type="number" step="1" name="weight_g" value="{{ r.weight_g if r.weight_g is not none else '' }}"></td>
        <td><input class="n" type="number" step="0.1" name="length_cm" value="{{ r.length_cm if r.length_cm is not none else '' }}"></td>
        <td><input class="n" type="number" step="0.1" name="width_cm" value="{{ r.width_cm if r.width_cm is not none else '' }}"></td>
        <td><input class="n" type="number" step="0.1" name="height_cm" value="{{ r.height_cm if r.height_cm is not none else '' }}"></td>
        <td>{% if r.complete %}<span class="pill ok">ready</span>{% else %}<span class="pill no">needs data</span>{% endif %}</td>
        <td><button class="btn sec" type="submit">Save</button></td>
        </form>
      </tr>
      {% endfor %}
    </table>
  </div>
</div></body></html>
"""


@app.route("/packages")
def packages_page():
    accts = _accounts()
    account = request.args.get("account") or accts[0]
    rows = _rows_for(account)
    complete = sum(1 for r in rows if r["complete"])
    return render_template_string(PAGE, accounts=accts, account=account, rows=rows,
                                  total=len(rows), complete=complete)


@app.route("/packages/save", methods=["POST"])
def packages_save():
    account = request.form.get("account")
    canon = request.form.get("canonical_sku")
    try:
        m5.upsert_package_default(
            account, canon, asin=(request.form.get("asin") or None),
            weight_g=_num(request.form.get("weight_g"), int),
            length_cm=_num(request.form.get("length_cm"), float),
            width_cm=_num(request.form.get("width_cm"), float),
            height_cm=_num(request.form.get("height_cm"), float),
            source="manual")
        flash(f"Saved {canon}.")
    except Exception as e:
        flash(f"Could not save {canon}: {e}")
    return redirect(f"/packages?account={account}")


@app.route("/packages/export.csv")
def packages_export():
    account = request.args.get("account") or _accounts()[0]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["account_id", "canonical_sku", "asin", "weight_g", "length_cm", "width_cm", "height_cm"])
    for r in _rows_for(account):
        w.writerow([account, r["canonical_sku"], r["asin"],
                    "" if r["weight_g"] is None else r["weight_g"],
                    "" if r["length_cm"] is None else r["length_cm"],
                    "" if r["width_cm"] is None else r["width_cm"],
                    "" if r["height_cm"] is None else r["height_cm"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=package_defaults_{account}.csv"})


@app.route("/packages/import", methods=["POST"])
def packages_import():
    account = request.form.get("account") or _accounts()[0]
    f = request.files.get("csv")
    if not f or not f.filename:
        flash("No file selected.")
        return redirect(f"/packages?account={account}")
    try:
        rdr = csv.DictReader(io.StringIO(f.read().decode("utf-8-sig")))
        n = 0
        for row in rdr:
            canon = (row.get("canonical_sku") or "").strip()
            if not canon:
                continue
            acct = (row.get("account_id") or account).strip()
            m5.upsert_package_default(
                acct, canon, asin=(row.get("asin") or None),
                weight_g=_num(row.get("weight_g"), int),
                length_cm=_num(row.get("length_cm"), float),
                width_cm=_num(row.get("width_cm"), float),
                height_cm=_num(row.get("height_cm"), float),
                source="manual")
            n += 1
        flash(f"Imported {n} row(s).")
    except Exception as e:
        flash(f"Import failed: {e}")
    return redirect(f"/packages?account={account}")
