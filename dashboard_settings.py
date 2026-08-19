"""dashboard_settings.py — website Settings page ("/settings").

Manage credentials + config from the app instead of Railway env vars: SP-API (Amazon),
the dispatch address used for Buy-Shipping quotes, and Mirakl accounts (Tesco + B&Q).
Stored in app_settings (DB); the loaders read the DB first, then fall back to env vars.
Secrets are shown masked and kept if you leave the field blank.

Note: the dashboard has no login, so treat its URL as private. Env vars remain a valid
(and slightly more secure) alternative — this page just makes day-to-day setup easier.
"""
import logging

from flask import request, redirect, flash, render_template_string

from dashboard_app import app
import module5_labels_db as m5

log = logging.getLogger(__name__)
UK = "A1F83G8C2ARO7P"


def _kept(field, current):
    v = (request.form.get(field) or "").strip()
    return v if v else current


PAGE = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Settings — BSR Repricer</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:820px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 h2{margin:0 0 2px;font-size:18px} h3{margin:14px 0 4px;font-size:14px} .muted{color:#8a94a2;font-size:12.5px}
 label{display:block;font-size:12px;color:#5a6472;margin:8px 0 2px}
 input{width:100%;padding:7px 9px;border:1px solid #dfe4e9;border-radius:7px;font-size:13px}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px}
 .btn{background:#0e5c5b;color:#fff;border:none;border-radius:7px;padding:8px 14px;font-size:13px;cursor:pointer;margin-top:12px}
 .set{color:#1f7a45;font-size:11px}
</style></head><body>{{ nav|safe }}
<div class="wrap">
  <div class="card"><h2>Settings</h2><div class="muted">Credentials &amp; config, editable here instead of Railway. Secrets show as saved — leave blank to keep, type to replace.</div></div>

  <div class="card"><h2>Amazon SP-API</h2>
    <form method="POST" action="/settings/spapi">
      <div class="grid">
        <div><label>Account id</label><input name="account_id" value="{{ acct.account_id or '' }}" placeholder="M4Mart_UK"></div>
        <div><label>Seller id</label><input name="seller_id" value="{{ acct.seller_id or '' }}"></div>
        <div><label>Marketplace id</label><input name="marketplace_id" value="{{ acct.marketplace_id or 'A1F83G8C2ARO7P' }}"></div>
        <div><label>Refresh token {% if acct.refresh_token %}<span class="set">✓ saved</span>{% endif %}</label><input name="refresh_token" type="password" placeholder="{{ '•••• leave blank to keep' if acct.refresh_token else 'Atzr|…' }}"></div>
        <div><label>LWA app id</label><input name="lwa_app_id" value="{{ spapi.lwa_app_id or '' }}" placeholder="amzn1.application-oa2-client.…"></div>
        <div><label>LWA client secret {% if spapi.lwa_client_secret %}<span class="set">✓ saved</span>{% endif %}</label><input name="lwa_client_secret" type="password" placeholder="{{ '•••• leave blank to keep' if spapi.lwa_client_secret else 'amzn1.oa2-cs.…' }}"></div>
      </div>
      <button class="btn" type="submit">Save SP-API</button>
    </form>
  </div>

  <div class="card"><h2>Dispatch address <span class="muted">(for Buy-Shipping quotes)</span></h2>
    <form method="POST" action="/settings/ship-from">
      <div class="grid">
        <div><label>Name</label><input name="Name" value="{{ ship.Name or '' }}"></div>
        <div><label>Phone</label><input name="Phone" value="{{ ship.Phone or '' }}"></div>
        <div><label>Address line 1</label><input name="AddressLine1" value="{{ ship.AddressLine1 or '' }}"></div>
        <div><label>Address line 2</label><input name="AddressLine2" value="{{ ship.AddressLine2 or '' }}"></div>
        <div><label>City</label><input name="City" value="{{ ship.City or '' }}"></div>
        <div><label>County / state</label><input name="StateOrProvinceCode" value="{{ ship.StateOrProvinceCode or '' }}"></div>
        <div><label>Postcode</label><input name="PostalCode" value="{{ ship.PostalCode or '' }}"></div>
        <div><label>Country code</label><input name="CountryCode" value="{{ ship.CountryCode or 'GB' }}"></div>
        <div><label>Email</label><input name="Email" value="{{ ship.Email or '' }}"></div>
      </div>
      <button class="btn" type="submit">Save dispatch address</button>
    </form>
  </div>

  <div class="card"><h2>Mirakl <span class="muted">(Tesco + B&amp;Q)</span></h2>
    <form method="POST" action="/settings/mirakl">
      {% for key,label,acc in [('tesco','Tesco',tesco),('bandq','B&Q',bandq)] %}
      <h3>{{ label }}</h3>
      <div class="grid">
        <div><label>Base URL</label><input name="{{ key }}_base_url" value="{{ acc.base_url or '' }}" placeholder="https://…mirakl.net"></div>
        <div><label>Shop id</label><input name="{{ key }}_shop_id" value="{{ acc.shop_id or '' }}"></div>
        <div style="grid-column:1/3"><label>API key {% if acc.api_key %}<span class="set">✓ saved</span>{% endif %}</label><input name="{{ key }}_api_key" type="password" placeholder="{{ '•••• leave blank to keep' if acc.api_key else 'paste API key' }}"></div>
      </div>
      {% endfor %}
      <button class="btn" type="submit">Save Mirakl</button>
    </form>
    <div class="muted" style="margin-top:8px">Then check <a href="/mirakl/test">/mirakl/test</a>.</div>
  </div>
</div></body></html>
"""


@app.route("/settings")
def settings_page():
    spapi = m5.get_setting("spapi") or {}
    ship = m5.get_setting("ship_from") or {}
    mk = m5.get_setting("mirakl_accounts") or {}
    acct = (spapi.get("accounts") or [{}])[0]
    return render_template_string(PAGE, spapi=spapi, acct=acct, ship=ship,
                                  tesco=(mk.get("tesco") or {}), bandq=(mk.get("bandq") or {}))


@app.route("/settings/spapi", methods=["POST"])
def settings_spapi():
    cur = m5.get_setting("spapi") or {}
    ca = (cur.get("accounts") or [{}])[0]
    acct = {
        "account_id": (request.form.get("account_id") or ca.get("account_id") or "").strip(),
        "seller_id": (request.form.get("seller_id") or "").strip() or ca.get("seller_id"),
        "marketplace_id": (request.form.get("marketplace_id") or "").strip() or ca.get("marketplace_id") or UK,
        "refresh_token": _kept("refresh_token", ca.get("refresh_token")),
    }
    m5.set_setting("spapi", {
        "lwa_app_id": (request.form.get("lwa_app_id") or "").strip() or cur.get("lwa_app_id"),
        "lwa_client_secret": _kept("lwa_client_secret", cur.get("lwa_client_secret")),
        "accounts": [acct],
    })
    flash("Saved SP-API settings.")
    return redirect("/settings")


@app.route("/settings/ship-from", methods=["POST"])
def settings_ship_from():
    fields = ["Name", "AddressLine1", "AddressLine2", "City", "StateOrProvinceCode",
              "PostalCode", "CountryCode", "Phone", "Email"]
    val = {k: (request.form.get(k) or "").strip() for k in fields if (request.form.get(k) or "").strip()}
    m5.set_setting("ship_from", val)
    flash("Saved dispatch address.")
    return redirect("/settings")


@app.route("/settings/mirakl", methods=["POST"])
def settings_mirakl():
    cur = m5.get_setting("mirakl_accounts") or {}
    out = {}
    for acc in ("tesco", "bandq"):
        c = cur.get(acc) or {}
        base = (request.form.get(f"{acc}_base_url") or "").strip() or c.get("base_url")
        key = _kept(f"{acc}_api_key", c.get("api_key"))
        shop = (request.form.get(f"{acc}_shop_id") or "").strip() or c.get("shop_id")
        if base or key:
            out[acc] = {"base_url": base, "api_key": key, "shop_id": shop}
    m5.set_setting("mirakl_accounts", out)
    flash("Saved Mirakl accounts.")
    return redirect("/settings")
