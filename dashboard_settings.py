"""dashboard_settings.py — website Settings hub ("/settings").

Settings is the MAIN section; inside it two sub-tabs:
  • Channels — a Veeqo-style grid of channel cards. Each card shows a live
    connection status and opens a popup modal to connect/configure that channel:
    Amazon (SP-API), Mirakl — Tesco, Mirakl — B&Q. Planned channels (eBay,
    TikTok, Etsy) render greyed as "coming soon".
  • Dispatch address — the shared ship-from used for Buy-Shipping quotes.

Credentials are stored in app_settings (DB); the loaders read the DB first, then
fall back to Railway env vars / config.json. So a channel can already be
"Connected" via env vars without anything typed here — the modal only overrides
when you actually enter a value. Secrets show masked and are kept if left blank.

Note: the dashboard has no login, so treat its URL as private.
"""
import logging

from flask import request, redirect, render_template_string

from dashboard_app import app
import module5_labels_db as m5

log = logging.getLogger(__name__)
UK = "A1F83G8C2ARO7P"


def _kept(field, current):
    v = (request.form.get(field) or "").strip()
    return v if v else current


# ── connection status detection ──────────────────────────────────────────────

def _amazon_status():
    """Connected if load_spapi_config resolves at least one account with a token
    (DB, or Railway env vars, or config.json)."""
    try:
        import module5_orders
        _cfg, accts = module5_orders.load_spapi_config()
        for a in (accts or []):
            if a.get("refresh_token"):
                return True, (a.get("account_id") or "account")
    except Exception as e:
        log.warning("amazon status check failed: %s", e)
    return False, None


def _mirakl_status(account):
    """Connected if mirakl_client.creds_for resolves base_url + api_key."""
    try:
        import mirakl_client
        c = mirakl_client.creds_for(account)
        if c:
            return True, c.get("shop_id")
    except Exception as e:
        log.warning("mirakl status check (%s) failed: %s", account, e)
    return False, None


# ── page ──────────────────────────────────────────────────────────────────────

PAGE = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Settings — BSR Repricer</title>
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:940px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .card{background:#fff;border-radius:12px;padding:18px 20px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 h2{margin:0 0 2px;font-size:19px} h3{margin:14px 0 4px;font-size:14px}
 .muted{color:#8a94a2;font-size:12.5px}
 /* sub-tabs */
 .tabs{display:flex;gap:6px;margin:14px 0 4px}
 .tabs a{padding:8px 16px;border-radius:9px 9px 0 0;font-size:13.5px;font-weight:600;text-decoration:none;color:#5a6472;background:transparent}
 .tabs a.on{background:#fff;color:#0e5c5b;box-shadow:0 -2px 6px rgba(0,0,0,.04)}
 /* channel grid */
 .secttl{font-size:13px;font-weight:700;color:#3a4450;margin:2px 0 10px}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
 .ch{border:1px solid #e4e8ec;border-radius:11px;padding:15px 16px;background:#fff;display:flex;flex-direction:column;gap:9px;transition:box-shadow .12s,border-color .12s}
 .ch.live{cursor:pointer} .ch.live:hover{box-shadow:0 4px 14px rgba(0,0,0,.09);border-color:#0e5c5b}
 .ch.soon{opacity:.6}
 .ch .top{display:flex;align-items:center;gap:10px}
 .logo{width:34px;height:34px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-weight:800;font-size:15px;color:#fff}
 .ch .nm{font-weight:700;font-size:14px} .ch .sub{font-size:11.5px;color:#8a94a2}
 .badge{align-self:flex-start;font-size:11px;font-weight:600;padding:3px 9px;border-radius:20px}
 .b-on{background:#e4f6ea;color:#1f7a45} .b-off{background:#f1f3f5;color:#8a94a2} .b-soon{background:#eef2ff;color:#5566cc}
 .cfg{font-size:12px;color:#0e5c5b;font-weight:600}
 /* forms */
 label{display:block;font-size:12px;color:#5a6472;margin:8px 0 2px}
 input{width:100%;padding:7px 9px;border:1px solid #dfe4e9;border-radius:7px;font-size:13px;box-sizing:border-box}
 .fg{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px}
 .btn{background:#0e5c5b;color:#fff;border:none;border-radius:7px;padding:9px 16px;font-size:13px;cursor:pointer;margin-top:14px}
 .set{color:#1f7a45;font-size:11px}
 /* modal */
 .ov{display:none;position:fixed;inset:0;background:rgba(10,20,25,.45);z-index:50;align-items:flex-start;justify-content:center;overflow:auto;padding:40px 16px}
 .ov.open{display:flex}
 .modal{background:#fff;border-radius:14px;width:100%;max-width:560px;padding:22px 24px;box-shadow:0 20px 60px rgba(0,0,0,.3)}
 .modal .x{float:right;font-size:22px;color:#8a94a2;text-decoration:none;line-height:1;cursor:pointer}
 .modal h2{font-size:17px;margin:0 0 3px}
</style></head><body>{{ nav|safe }}
<div class="wrap">
  <div class="card" style="padding:16px 20px">
    <h2>Settings</h2>
    <div class="muted">Connect your sales channels and set your dispatch address. Credentials live in the app (DB) with Railway env vars as fallback — a channel can read as Connected from env vars without re-entering anything here.</div>
  </div>

  <div class="tabs">
    <a href="/settings?tab=channels" class="{{ 'on' if tab=='channels' else '' }}">Channels</a>
    <a href="/settings?tab=dispatch" class="{{ 'on' if tab=='dispatch' else '' }}">Dispatch address</a>
  </div>

  {% if tab == 'channels' %}
  <div class="card">
    <div class="secttl">Your channels <span class="muted">— click a card to connect or manage</span></div>
    <div class="grid">
      {% for c in channels if not c.soon %}
        <div class="ch live" onclick="openM('{{ c.id }}')">
          <div class="top"><div class="logo" style="background:{{ c.color }}">{{ c.mark }}</div>
            <div><div class="nm">{{ c.name }}</div><div class="sub">{{ c.kind }}</div></div></div>
          {% if c.connected %}<span class="badge b-on">● Connected{% if c.detail %} · {{ c.detail }}{% endif %}</span>
          {% else %}<span class="badge b-off">Not connected</span>{% endif %}
          <span class="cfg">{{ 'Manage ⚙' if c.connected else 'Connect →' }}</span>
        </div>
      {% endfor %}
    </div>

    <div class="secttl" style="margin-top:22px">Available to connect <span class="muted">— coming soon</span></div>
    <div class="grid">
      {% for c in channels if c.soon %}
        <div class="ch soon">
          <div class="top"><div class="logo" style="background:{{ c.color }}">{{ c.mark }}</div>
            <div><div class="nm">{{ c.name }}</div><div class="sub">{{ c.kind }}</div></div></div>
          <span class="badge b-soon">Coming soon</span>
        </div>
      {% endfor %}
    </div>
    <div class="secttl" style="margin-top:22px">Carriers <span class="muted">— shipping services for labels</span></div>
    <div class="grid">
      {% for c in carriers %}
        <div class="ch{{ '' if c.on else ' soon' }}">
          <div class="top"><div class="logo" style="background:{{ c.color }}">{{ c.mark }}</div>
            <div><div class="nm">{{ c.name }}</div><div class="sub">{{ c.kind }}</div></div></div>
          {% if c.on %}<span class="badge b-on">● Active</span>{% else %}<span class="badge b-soon">Coming soon</span>{% endif %}
        </div>
      {% endfor %}
    </div>

    <div class="muted" style="margin-top:14px">After connecting Mirakl, check <a href="/mirakl/test">/mirakl/test</a>. Sales by channel lives under <a href="/channels">Analytics → Channels report</a>. Multiple accounts per channel and the extra carriers are on the roadmap.</div>
  </div>

  <!-- Amazon modal -->
  <div class="ov" id="m_amazon"><div class="modal">
    <a class="x" onclick="closeM('amazon')">×</a>
    <h2>Amazon — SP-API</h2>
    <div class="muted">Already connected via Railway env vars? Leave blank. Type here only to store/override creds in the app.</div>
    <form method="POST" action="/settings/spapi">
      <div class="fg">
        <div><label>Account id</label><input name="account_id" value="{{ acct.account_id or '' }}" placeholder="M4Mart_UK"></div>
        <div><label>Seller id</label><input name="seller_id" value="{{ acct.seller_id or '' }}"></div>
        <div><label>Marketplace id</label><input name="marketplace_id" value="{{ acct.marketplace_id or 'A1F83G8C2ARO7P' }}"></div>
        <div><label>Refresh token {% if acct.refresh_token %}<span class="set">✓ saved</span>{% endif %}</label><input name="refresh_token" type="password" placeholder="{{ '•••• leave blank to keep' if acct.refresh_token else 'Atzr|…' }}"></div>
        <div><label>LWA app id</label><input name="lwa_app_id" value="{{ spapi.lwa_app_id or '' }}" placeholder="amzn1.application-oa2-client.…"></div>
        <div><label>LWA client secret {% if spapi.lwa_client_secret %}<span class="set">✓ saved</span>{% endif %}</label><input name="lwa_client_secret" type="password" placeholder="{{ '•••• leave blank to keep' if spapi.lwa_client_secret else 'amzn1.oa2-cs.…' }}"></div>
      </div>
      <button class="btn" type="submit">Save Amazon</button>
    </form>
  </div></div>

  <!-- Mirakl Tesco modal -->
  <div class="ov" id="m_tesco"><div class="modal">
    <a class="x" onclick="closeM('tesco')">×</a>
    <h2>Mirakl — Tesco</h2>
    <form method="POST" action="/settings/mirakl">
      <div class="fg">
        <div><label>Base URL</label><input name="tesco_base_url" value="{{ tesco.base_url or '' }}" placeholder="https://tescouk-prod.mirakl.net"></div>
        <div><label>Shop id</label><input name="tesco_shop_id" value="{{ tesco.shop_id or '' }}" placeholder="5797"></div>
      </div>
      <label>API key {% if tesco.api_key %}<span class="set">✓ saved</span>{% endif %}</label>
      <input name="tesco_api_key" type="password" placeholder="{{ '•••• leave blank to keep' if tesco.api_key else 'paste API key' }}">
      <button class="btn" type="submit">Save Tesco</button>
    </form>
  </div></div>

  <!-- Mirakl B&Q modal -->
  <div class="ov" id="m_bandq"><div class="modal">
    <a class="x" onclick="closeM('bandq')">×</a>
    <h2>Mirakl — B&amp;Q</h2>
    <form method="POST" action="/settings/mirakl">
      <div class="fg">
        <div><label>Base URL</label><input name="bandq_base_url" value="{{ bandq.base_url or '' }}" placeholder="https://marketplace.kingfisher.com"></div>
        <div><label>Shop id</label><input name="bandq_shop_id" value="{{ bandq.shop_id or '' }}" placeholder="6973"></div>
      </div>
      <label>API key {% if bandq.api_key %}<span class="set">✓ saved</span>{% endif %}</label>
      <input name="bandq_api_key" type="password" placeholder="{{ '•••• leave blank to keep' if bandq.api_key else 'paste API key' }}">
      <button class="btn" type="submit">Save B&amp;Q</button>
    </form>
  </div></div>

  {% else %}
  <div class="card"><h2>Dispatch address <span class="muted">(ship-from for Buy-Shipping quotes)</span></h2>
    <form method="POST" action="/settings/ship-from">
      <div class="fg">
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
  {% endif %}
</div>
<script>
 function openM(id){document.getElementById('m_'+id).classList.add('open');document.body.style.overflow='hidden';}
 function closeM(id){document.getElementById('m_'+id).classList.remove('open');document.body.style.overflow='';}
 document.querySelectorAll('.ov').forEach(function(o){o.addEventListener('click',function(e){if(e.target===o){o.classList.remove('open');document.body.style.overflow='';}});});
 document.addEventListener('keydown',function(e){if(e.key==='Escape'){document.querySelectorAll('.ov.open').forEach(function(o){o.classList.remove('open');});document.body.style.overflow='';}});
</script>
</body></html>
"""


@app.route("/settings")
def settings_page():
    tab = request.args.get("tab", "channels")
    if tab not in ("channels", "dispatch"):
        tab = "channels"
    spapi = m5.get_setting("spapi") or {}
    ship = m5.get_setting("ship_from") or {}
    mk = m5.get_setting("mirakl_accounts") or {}
    acct = (spapi.get("accounts") or [{}])[0]
    tesco = mk.get("tesco") or {}
    bandq = mk.get("bandq") or {}

    amz_on, amz_detail = _amazon_status()
    tesco_on, tesco_shop = _mirakl_status("tesco")
    bandq_on, bandq_shop = _mirakl_status("bandq")

    channels = [
        {"id": "amazon", "name": "Amazon", "kind": "Marketplace · SP-API",
         "color": "#ff9900", "mark": "a", "connected": amz_on, "detail": amz_detail},
        {"id": "tesco", "name": "Tesco", "kind": "Mirakl marketplace",
         "color": "#00539f", "mark": "T", "connected": tesco_on,
         "detail": ("shop " + str(tesco_shop)) if tesco_shop else None},
        {"id": "bandq", "name": "B&Q", "kind": "Mirakl · Kingfisher",
         "color": "#ff6600", "mark": "B", "connected": bandq_on,
         "detail": ("shop " + str(bandq_shop)) if bandq_shop else None},
        {"soon": True, "name": "eBay", "kind": "Marketplace", "color": "#e53238", "mark": "e"},
        {"soon": True, "name": "OnBuy", "kind": "Marketplace", "color": "#14213d", "mark": "O"},
        {"soon": True, "name": "Etsy", "kind": "Marketplace", "color": "#f56400", "mark": "E"},
        {"soon": True, "name": "Shein", "kind": "Marketplace", "color": "#111827", "mark": "Sh"},
        {"soon": True, "name": "Temu", "kind": "Marketplace", "color": "#fb7701", "mark": "T"},
        {"soon": True, "name": "Wayfair", "kind": "Marketplace", "color": "#7b189f", "mark": "W"},
        {"soon": True, "name": "Facebook", "kind": "Marketplace", "color": "#1877f2", "mark": "f"},
        {"soon": True, "name": "Wowcher", "kind": "Marketplace", "color": "#e6007e", "mark": "w"},
        {"soon": True, "name": "Groupon", "kind": "Marketplace", "color": "#53a318", "mark": "G"},
        {"soon": True, "name": "Shopify", "kind": "eCommerce", "color": "#5a8f3d", "mark": "S"},
    ]
    carriers = [
        {"name": "Amazon Buy Shipping", "kind": "Carrier · live on Ship labels", "color": "#ff9900", "mark": "a", "on": True},
        {"name": "Evri", "kind": "Carrier", "color": "#0a2b4e", "mark": "Ev"},
        {"name": "Royal Mail", "kind": "Carrier", "color": "#da291c", "mark": "RM"},
        {"name": "Parcel2Go", "kind": "Carrier", "color": "#00a5e0", "mark": "P2"},
    ]
    return render_template_string(PAGE, tab=tab, channels=channels, carriers=carriers,
                                  spapi=spapi, acct=acct, ship=ship,
                                  tesco=tesco, bandq=bandq)


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
    return redirect("/settings?tab=channels")


@app.route("/settings/ship-from", methods=["POST"])
def settings_ship_from():
    fields = ["Name", "AddressLine1", "AddressLine2", "City", "StateOrProvinceCode",
              "PostalCode", "CountryCode", "Phone", "Email"]
    val = {k: (request.form.get(k) or "").strip() for k in fields if (request.form.get(k) or "").strip()}
    m5.set_setting("ship_from", val)
    return redirect("/settings?tab=dispatch")


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
    return redirect("/settings?tab=channels")
