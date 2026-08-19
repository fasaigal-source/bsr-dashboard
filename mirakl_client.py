"""mirakl_client.py — Module 5 Mirakl Shop API adapter (Tesco + B&Q).

ONE module, parametrized per account ('tesco' | 'bandq'). Tesco and B&Q run
separate Mirakl instances but share the identical API shape, so the same code
serves both — the base_url / api_key / shop_id come from config.json's
`mirakl_accounts` block (never the repo; same discipline as the SP-API creds).

Auth: the API key is sent DIRECTLY as the `Authorization` header value (no OAuth).

SAFETY — writes are gated exactly like the PPC Ads layer:
  * accept_order / set_tracking / confirm_shipment DO NOTHING but log unless
    creds exist AND MIRAKL_DRY_RUN != "1". Default is DRY-RUN.
  * Reads (smoke_test / get_new_orders / get_transactions) run whenever creds
    exist — they only GET, never mutate an order.
Nothing here is scheduled; mirakl_worker decides when to call these.

  MIRAKL_DRY_RUN=1   force dry-run for all writes (default when unset → treat as dry-run
                     until you deliberately set it to 0 in the creds session)
"""
import os
import json
import logging

import httpx

log = logging.getLogger(__name__)
CONFIG_PATH = "config.json"
_ACCOUNTS = ("tesco", "bandq")


# ── config / creds ───────────────────────────────────────────────────────────

def _load_config():
    """config.json locally; on Railway (no config.json) the mirakl_accounts block comes
    from the MIRAKL_ACCOUNTS env var (JSON), e.g.
        {"tesco":{"base_url":"https://...","api_key":"...","shop_id":"..."},
         "bandq":{"base_url":"https://...","api_key":"...","shop_id":"..."}}
    Env entries win over config.json. Secrets live only in Railway variables, never the repo."""
    cfg = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        pass
    raw = os.environ.get("MIRAKL_ACCOUNTS")
    if raw:
        try:
            env_accounts = json.loads(raw)
            cfg.setdefault("mirakl_accounts", {})
            cfg["mirakl_accounts"].update(env_accounts)
        except Exception as e:
            log.warning("MIRAKL_ACCOUNTS is not valid JSON: %s", e)
    # settings page (DB) — wins over env + config.json
    try:
        import module5_labels_db as m5
        db_accounts = m5.get_setting("mirakl_accounts")
        if db_accounts:
            cfg.setdefault("mirakl_accounts", {})
            cfg["mirakl_accounts"].update(db_accounts)
    except Exception:
        pass
    return cfg


def creds_for(account):
    """{'base_url','api_key','shop_id'} for an account, or None if not configured.
    Read fresh each call so adding creds needs no restart."""
    acc = (_load_config().get("mirakl_accounts") or {}).get(account) or {}
    base = (acc.get("base_url") or "").rstrip("/")
    key = acc.get("api_key")
    if base and key and "<" not in base and "<" not in str(key):   # reject placeholder values
        return {"base_url": base, "api_key": key, "shop_id": acc.get("shop_id")}
    return None


def is_dry_run():
    # default to dry-run unless explicitly disabled
    return os.environ.get("MIRAKL_DRY_RUN", "1") == "1"


def is_configured(account):
    """Creds present AND writes enabled (not dry-run)."""
    return creds_for(account) is not None and not is_dry_run()


def looks_like_sandbox(creds, shop_json=None):
    """Best-effort sandbox detector — flag only if the HOST clearly names a non-prod
    instance. Host-only on purpose: scanning the account JSON for substrings like
    'test'/'uat' produced false positives (they appear inside unrelated field values),
    so a live prod host such as tescouk-prod.mirakl.net no longer trips the flag."""
    host = (creds or {}).get("base_url", "").lower()
    hints = ("sandbox", "preprod", "pre-prod", "staging", ".uat.", "-uat.", "uat-", "test.")
    return any(h in host for h in hints)


def _headers(creds):
    return {"Authorization": creds["api_key"], "Accept": "application/json"}


def _require(account):
    creds = creds_for(account)
    if not creds:
        raise RuntimeError(
            f"Mirakl account {account!r} is not configured. Add a real mirakl_accounts."
            f"{account} block (base_url + api_key + shop_id) to config.json.")
    return creds


# ── reads ────────────────────────────────────────────────────────────────────

# Probe endpoints, cheapest/most-permissive first. /api/version is a bare liveness
# check (any authenticated user); /api/account is the canonical seller identity; only
# then /api/shops, which on some operators (Tesco/Kingfisher) is operator-scoped and
# 403s for a plain shop key even when the key is perfectly valid.
_PROBE_ENDPOINTS = ("/api/version", "/api/account", "/api/shops")


def _shop_name_from(payload):
    """Pull a human shop name out of /api/account or /api/shops JSON (shapes vary)."""
    if not isinstance(payload, dict):
        return None
    for k in ("shop_name", "name", "shopName"):
        if payload.get(k):
            return payload[k]
    shops = payload.get("shops")
    if isinstance(shops, list) and shops:
        s0 = shops[0]
        return s0.get("shop_name") or s0.get("name")
    return None


def smoke_test(account, timeout=30):
    """Read-only connection test. Probes several endpoints so a 403 on one (a permission
    scope) can be told apart from every endpoint failing (a key-level block or IP
    allowlist). Returns {ok, status, sandbox, shop, shop_name, detail, probes}.

    ok = any probe returned 200. `probes` lists each endpoint's status for diagnosis."""
    creds = _require(account)
    probes, connected, shop_json = [], False, None
    for ep in _PROBE_ENDPOINTS:
        try:
            r = httpx.get(f"{creds['base_url']}{ep}", headers=_headers(creds), timeout=timeout)
            ok = r.status_code == 200
            body = None
            if ok:
                try:
                    body = r.json()
                except Exception:
                    body = None
            probes.append({"endpoint": ep, "status": r.status_code, "ok": ok,
                           "detail": None if ok else (r.text or "")[:200]})
            if ok:
                connected = True
                if ep in ("/api/account", "/api/shops") and isinstance(body, (dict, list)):
                    shop_json = body if isinstance(body, dict) else {"shops": body}
        except Exception as e:
            probes.append({"endpoint": ep, "status": None, "ok": False, "detail": str(e)[:200]})
    best = next((p for p in probes if p["ok"]), None) or probes[-1]
    # a concise failure summary: which endpoints returned what
    detail = None
    if not connected:
        detail = "; ".join(f"{p['endpoint']}→{p['status'] or 'err'}" for p in probes)
    return {"ok": connected, "status": best["status"], "probes": probes,
            "sandbox": looks_like_sandbox(creds, shop_json),
            "shop": shop_json, "shop_name": _shop_name_from(shop_json), "detail": detail}


def get_new_orders(account, since=None, max_pages=50, timeout=60):
    """GET /api/orders, paginated, optionally filtered to orders updated on/after
    `since` (ISO). Returns a flat list of order dicts (verbatim Mirakl JSON)."""
    creds = _require(account)
    out, offset = [], 0
    for _ in range(max_pages):
        params = {"max": 100, "offset": offset}
        if since:
            params["start_update_date"] = since
        r = httpx.get(f"{creds['base_url']}/api/orders",
                      headers=_headers(creds), params=params, timeout=timeout)
        r.raise_for_status()
        j = r.json() or {}
        page = j.get("orders") or j.get("data") or []
        out.extend(page)
        if len(page) < params["max"]:
            break
        offset += params["max"]
    return out


def get_transactions(account, date_from, date_to, max_pages=100, timeout=60):
    """Pull the financial ledger for [date_from, date_to] (ISO dates). Endpoint is
    Mirakl's seller-payment transaction logs; the exact path/param names can vary by
    operator config, so confirm against the backoffice in the creds session. Returns
    a flat list of transaction dicts (verbatim)."""
    creds = _require(account)
    out, offset = [], 0
    for _ in range(max_pages):
        params = {"max": 100, "offset": offset,
                  "start_date": date_from, "end_date": date_to}
        r = httpx.get(f"{creds['base_url']}/api/sellerpayment/transactions_logs",
                      headers=_headers(creds), params=params, timeout=timeout)
        r.raise_for_status()
        j = r.json() or {}
        page = j.get("data") or j.get("transactions") or j.get("lines") or []
        out.extend(page)
        if len(page) < params["max"]:
            break
        offset += params["max"]
    return out


# ── writes (DRY-RUN gated) ───────────────────────────────────────────────────

def _write(account, method, path, body, what):
    """Shared write path: no-op + log when dry-run / unconfigured; else the real call."""
    creds = creds_for(account)
    if creds is None or is_dry_run():
        why = "no creds" if creds is None else "MIRAKL_DRY_RUN=1"
        log.info("[mirakl DRY-RUN %s] %s %s %s body=%s", why, account, method, path, body)
        return {"result": "dry_run", "detail": why}
    try:
        r = httpx.request(method, f"{creds['base_url']}{path}",
                          headers={**_headers(creds), "Content-Type": "application/json"},
                          json=body, timeout=60)
        ok = r.status_code in (200, 201, 204)
        log.info("[mirakl %s] %s %s -> HTTP %s", what, account, path, r.status_code)
        return {"result": "ok" if ok else "error", "status": r.status_code,
                "detail": None if ok else r.text[:300]}
    except Exception as e:
        return {"result": "error", "detail": str(e)[:300]}


def accept_order(account, order_id, order_lines=None):
    """PUT /api/orders/{id}/accept — accept lines in WAITING_ACCEPTANCE. Dry-run safe."""
    body = {"order_lines": order_lines} if order_lines else {"accept": True}
    return _write(account, "PUT", f"/api/orders/{order_id}/accept", body, "accept")


def set_tracking(account, order_id, carrier_code_or_none, tracking_number,
                 tracking_url_fallback=None, registered_carriers=None):
    """PUT /api/orders/{id}/tracking. If the carrier isn't in the operator's
    registered list, send the full tracking URL instead of a carrier code — Evri/
    InPost are NOT assumed to be pre-registered on either operator. Dry-run safe."""
    body = {"tracking_number": tracking_number}
    registered = {c.lower() for c in (registered_carriers or [])}
    if carrier_code_or_none and (not registered or carrier_code_or_none.lower() in registered):
        body["carrier_code"] = carrier_code_or_none
    else:
        # carrier not registered → fall back to a plain URL + free-text name
        if tracking_url_fallback:
            body["tracking_url"] = tracking_url_fallback
        if carrier_code_or_none:
            body["carrier_name"] = carrier_code_or_none
    return _write(account, "PUT", f"/api/orders/{order_id}/tracking", body, "tracking")


def confirm_shipment(account, order_id):
    """PUT /api/orders/{id}/ship — moves the order to SHIPPING on the operator side.
    Ready to call once a tracking number exists; the carrier/label piece is built
    elsewhere. Dry-run safe."""
    return _write(account, "PUT", f"/api/orders/{order_id}/ship", {}, "ship")
