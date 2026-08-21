"""ppc_ads_api.py — Amazon Advertising API layer for Module 3 Phase C.

The ONLY place that writes to Amazon Ads. It pauses/enables a Sponsored Products
campaign by ID (v2 endpoint). Credentials come from environment variables (never
the repo) — same discipline as the SP-API config:

  ADS_CLIENT_ID          LWA client id of your Amazon Ads API application
  ADS_CLIENT_SECRET      its client secret
  ADS_REFRESH_TOKEN      refresh token authorised for the ad account(s)
  ADS_PROFILE_<ACCOUNT>  the Ads profile id for that account, e.g.
                         ADS_PROFILE_M4MART_UK=123456789  (also accepts ADS_PROFILE_ID
                         as a single-account fallback)
  ADS_REGION             na | eu | fe   (UK/EU = eu, the default)
  PPC_DRY_RUN=1          force dry-run even when creds are present (logs, no write)

SAFETY: if credentials for an account are missing, calls are DRY-RUN — they log
what they WOULD do and write nothing. Every call (live or dry-run) is recorded in
ppc_action_log. Nothing here runs on its own; the scheduler / UI decide when to call.
"""
import os
import re
import time
import logging

import httpx
import pl_ppc

log = logging.getLogger(__name__)

_REGION_HOST = {
    "na": "advertising-api.amazon.com",
    "eu": "advertising-api-eu.amazon.com",
    "fe": "advertising-api-fe.amazon.com",
}
_TOKEN_URL = "https://api.amazon.com/auth/o2/token"
# LWA token endpoint is region-specific for the Ads API. Refreshing an EU-issued token
# against the NA endpoint can fail, so pick by region (falls back to the global URL).
_TOKEN_URL_BY_REGION = {
    "na": "https://api.amazon.com/auth/o2/token",
    "eu": "https://api.amazon.co.uk/auth/o2/token",
    "fe": "https://api.amazon.co.jp/auth/o2/token",
}
_token_cache = {}   # refresh_token -> (access_token, expiry_epoch)


def _profile_env_key(account_id):
    return "ADS_PROFILE_" + re.sub(r"[^A-Z0-9]+", "_", (account_id or "").upper()).strip("_")


def creds_for(account_id):
    """Resolve Ads API credentials for one account, or None if not fully configured
    (→ dry-run). Values are read fresh from the environment each call."""
    cid = os.environ.get("ADS_CLIENT_ID")
    csec = os.environ.get("ADS_CLIENT_SECRET")
    rtok = os.environ.get("ADS_REFRESH_TOKEN")
    profile = os.environ.get(_profile_env_key(account_id)) or os.environ.get("ADS_PROFILE_ID")
    region = (os.environ.get("ADS_REGION") or "eu").lower()
    if cid and csec and rtok and profile:
        return dict(client_id=cid, client_secret=csec, refresh_token=rtok,
                    profile_id=profile, region=region if region in _REGION_HOST else "eu")
    return None


def is_configured(account_id):
    return creds_for(account_id) is not None and os.environ.get("PPC_DRY_RUN") != "1"


def _get_access_token(creds):
    key = creds["refresh_token"]
    tok, exp = _token_cache.get(key, (None, 0))
    if tok and time.time() < exp - 60:
        return tok
    token_url = _TOKEN_URL_BY_REGION.get(creds.get("region", "eu"), _TOKEN_URL)
    r = httpx.post(token_url, data={
        "grant_type": "refresh_token",
        "refresh_token": creds["refresh_token"],
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
    }, timeout=30)
    r.raise_for_status()
    j = r.json()
    tok = j["access_token"]
    _token_cache[key] = (tok, time.time() + int(j.get("expires_in", 3600)))
    return tok


def set_campaign_state(account_id, campaign_id, state, campaign_name=None):
    """Set a Sponsored Products campaign to 'enabled' or 'paused'. Dry-run (log only)
    if creds are missing or PPC_DRY_RUN=1. Always logs to ppc_action_log. Returns a
    dict with 'result' in {ok, error, dry_run}."""
    assert state in ("enabled", "paused")
    action = "enable" if state == "enabled" else "pause"
    creds = creds_for(account_id)
    dry = creds is None or os.environ.get("PPC_DRY_RUN") == "1"
    if dry:
        why = "no Ads API creds for account" if creds is None else "PPC_DRY_RUN=1"
        pl_ppc.log_action(account_id, campaign_id, campaign_name, action, state, "dry_run", why)
        return {"result": "dry_run", "detail": why}
    try:
        token = _get_access_token(creds)
        host = _REGION_HOST[creds["region"]]
        r = httpx.put(
            f"https://{host}/v2/sp/campaigns",
            headers={
                "Authorization": f"Bearer {token}",
                "Amazon-Advertising-API-ClientId": creds["client_id"],
                "Amazon-Advertising-API-Scope": str(creds["profile_id"]),
                "Content-Type": "application/json",
            },
            json=[{"campaignId": int(campaign_id), "state": state}],
            timeout=30,
        )
        ok = r.status_code in (200, 207)
        detail = f"HTTP {r.status_code}: {r.text[:300]}"
        pl_ppc.log_action(account_id, campaign_id, campaign_name, action, state,
                          "ok" if ok else "error", detail)
        return {"result": "ok" if ok else "error", "status": r.status_code, "detail": detail}
    except Exception as e:
        pl_ppc.log_action(account_id, campaign_id, campaign_name, action, state, "error", str(e)[:300])
        return {"result": "error", "detail": str(e)[:300]}
