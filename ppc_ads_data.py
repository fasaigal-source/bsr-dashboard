"""ppc_ads_data.py — Module 3 Phase 1: full Amazon Ads API data pull (READ-ONLY).

"Get all the data": pulls the current state of your Sponsored Products account (campaigns,
ad groups, keywords, targets, negatives — with their live bids/budgets/state) plus
performance from the Amazon Ads **v3 async reporting** API (campaign, targeting and
search-term reports), and stores it locally so the optimiser (later phases) has a
complete, current picture without CSV uploads.

Auth is reused from ppc_ads_api (env vars ADS_CLIENT_ID / ADS_CLIENT_SECRET /
ADS_REFRESH_TOKEN / ADS_PROFILE_<ACCOUNT> / ADS_REGION). Nothing here writes to Amazon.

Flow per account:
  1. list_entities() for each entity type       (POST /sp/<type>/list, paginated)
  2. pull_report() for spCampaigns/spTargeting/spSearchTerm
        (POST /reporting/reports -> poll -> download gzip JSON)
  3. upsert into ppc_ad_entities / ppc_ad_metrics / ppc_ad_search_terms

A "Pull now" button runs this in the background (status in ppc_pull_status), same UX as
the inventory refresh.
"""
import gzip
import json
import time
import logging
import threading
from datetime import datetime, timezone, date, timedelta

import httpx

import db
import ppc_ads_api   # auth + region host reuse

log = logging.getLogger(__name__)
DB_PATH = "bsr_history.db"
DEFAULT_WINDOW_DAYS = 60


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_db(db_path=DB_PATH):
    conn = db.connect(db_path)
    if not db.is_postgres():
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── schema ───────────────────────────────────────────────────────────────────

def init_ads_data_schema(db_path=DB_PATH):
    conn = get_db(db_path)
    pg = db.is_postgres()
    MONEY = "NUMERIC(14,4)" if pg else "REAL"
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS ppc_ad_entities (
            account_id   TEXT NOT NULL,
            entity_type  TEXT NOT NULL,          -- campaign | adGroup | keyword | target | negativeKeyword
            entity_id    TEXT NOT NULL,
            campaign_id  TEXT,
            ad_group_id  TEXT,
            name         TEXT,                    -- campaign/ad-group name or keyword text
            match_type   TEXT,
            expression   TEXT,                    -- product-target expression (targets)
            state        TEXT,
            bid          {MONEY},
            budget       {MONEY},
            updated_at   TEXT,
            PRIMARY KEY (account_id, entity_type, entity_id)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_ppce_campaign ON ppc_ad_entities(account_id, campaign_id)")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS ppc_ad_metrics (
            account_id   TEXT NOT NULL,
            entity_type  TEXT NOT NULL,          -- campaign | keyword | target
            entity_id    TEXT NOT NULL,
            window_days  INTEGER NOT NULL,
            impressions  INTEGER,
            clicks       INTEGER,
            cost         {MONEY},
            sales        {MONEY},
            orders       INTEGER,
            fetched_at   TEXT,
            PRIMARY KEY (account_id, entity_type, entity_id, window_days)
        )""")
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS ppc_ad_search_terms (
            account_id   TEXT NOT NULL,
            search_term  TEXT NOT NULL,
            campaign_id  TEXT,
            ad_group_id  TEXT,
            keyword      TEXT,
            match_type   TEXT,
            window_days  INTEGER NOT NULL,
            impressions  INTEGER,
            clicks       INTEGER,
            cost         {MONEY},
            sales        {MONEY},
            orders       INTEGER,
            fetched_at   TEXT,
            PRIMARY KEY (account_id, search_term, campaign_id, ad_group_id, window_days)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ppc_pull_status (
            account_id  TEXT PRIMARY KEY,
            state       TEXT,
            message     TEXT,
            started_at  TEXT,
            finished_at TEXT
        )""")
    if hasattr(conn, "commit"):
        conn.commit()
    log.info("Module 3 Ads-data schema initialised.")


# ── status ───────────────────────────────────────────────────────────────────

def _set_status(account_id, **kw):
    conn = get_db()
    cols = ["account_id"] + list(kw.keys())
    ph = ",".join("?" for _ in cols)
    upd = ",".join(f"{k}=?" for k in kw.keys())
    conn.execute(
        f"INSERT INTO ppc_pull_status ({','.join(cols)}) VALUES ({ph}) "
        f"ON CONFLICT(account_id) DO UPDATE SET {upd}",
        [account_id] + list(kw.values()) + list(kw.values()))
    if hasattr(conn, "commit"):
        conn.commit()


def get_status(account_id=None):
    conn = get_db()
    try:
        rows = (conn.execute("SELECT * FROM ppc_pull_status WHERE account_id=?", (account_id,)).fetchall()
                if account_id else conn.execute("SELECT * FROM ppc_pull_status").fetchall())
    except Exception:
        return {}
    return {dict(r)["account_id"]: dict(r) for r in rows}


def is_running():
    return any(s.get("state") == "running" for s in get_status().values())


# ── auth helpers (reuse ppc_ads_api) ─────────────────────────────────────────

def _host(creds):
    return ppc_ads_api._REGION_HOST[creds["region"]]


def _base_headers(creds, token):
    return {
        "Authorization": f"Bearer {token}",
        "Amazon-Advertising-API-ClientId": creds["client_id"],
        "Amazon-Advertising-API-Scope": str(creds["profile_id"]),
    }


def test_connection(account_id):
    """Lightweight probe: GET /v2/profiles. Returns {ok, status, detail, profiles}."""
    creds = ppc_ads_api.creds_for(account_id)
    if not creds:
        return {"ok": False, "status": None, "detail": "no Ads API creds configured (ADS_* env vars)"}
    try:
        token = ppc_ads_api._get_access_token(creds)
        r = httpx.get(f"https://{_host(creds)}/v2/profiles",
                      headers=_base_headers(creds, token), timeout=30)
        ok = r.status_code == 200
        prof = None
        if ok:
            try:
                prof = [{"profileId": p.get("profileId"),
                         "country": p.get("countryCode"),
                         "type": (p.get("accountInfo") or {}).get("type")}
                        for p in (r.json() or [])]
            except Exception:
                prof = None
        return {"ok": ok, "status": r.status_code,
                "detail": None if ok else (r.text or "")[:300], "profiles": prof}
    except Exception as e:
        return {"ok": False, "status": None, "detail": str(e)[:300]}


# ── entity lists (current bids / state / budget) ─────────────────────────────

_ENTITY_EP = {
    "campaign":        ("/sp/campaigns/list",       "application/vnd.spCampaign.v3+json",         "campaigns"),
    "adGroup":         ("/sp/adGroups/list",        "application/vnd.spAdGroup.v3+json",          "adGroups"),
    "keyword":         ("/sp/keywords/list",        "application/vnd.spKeyword.v3+json",          "keywords"),
    "target":          ("/sp/targets/list",         "application/vnd.spTargetingClause.v3+json",  "targetingClauses"),
    "negativeKeyword": ("/sp/negativeKeywords/list", "application/vnd.spNegativeKeyword.v3+json", "negativeKeywords"),
}


def list_entities(creds, token, kind, timeout=60, max_pages=200):
    """Paginated POST /sp/<kind>/list. Returns the raw entity dicts (verbatim)."""
    path, ctype, key = _ENTITY_EP[kind]
    url = f"https://{_host(creds)}{path}"
    headers = {**_base_headers(creds, token), "Content-Type": ctype, "Accept": ctype}
    out, token_next = [], None
    for _ in range(max_pages):
        body = {"maxResults": 500,
                "stateFilter": {"include": ["ENABLED", "PAUSED", "ARCHIVED"]}}
        if token_next:
            body["nextToken"] = token_next
        r = httpx.post(url, headers=headers, json=body, timeout=timeout)
        r.raise_for_status()
        j = r.json() or {}
        out.extend(j.get(key) or [])
        token_next = j.get("nextToken")
        if not token_next:
            break
    return out


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def normalize_entity(kind, raw):
    """Flatten a raw v3 entity into our ppc_ad_entities row shape."""
    if kind == "campaign":
        b = raw.get("budget") or {}
        return dict(entity_type="campaign", entity_id=str(raw.get("campaignId")),
                    campaign_id=str(raw.get("campaignId")), ad_group_id=None,
                    name=raw.get("name"), match_type=None, expression=None,
                    state=raw.get("state"), bid=None,
                    budget=_num(b.get("budget") if isinstance(b, dict) else b))
    if kind == "adGroup":
        return dict(entity_type="adGroup", entity_id=str(raw.get("adGroupId")),
                    campaign_id=str(raw.get("campaignId")), ad_group_id=str(raw.get("adGroupId")),
                    name=raw.get("name"), match_type=None, expression=None,
                    state=raw.get("state"), bid=_num(raw.get("defaultBid")), budget=None)
    if kind == "keyword":
        return dict(entity_type="keyword", entity_id=str(raw.get("keywordId")),
                    campaign_id=str(raw.get("campaignId")), ad_group_id=str(raw.get("adGroupId")),
                    name=raw.get("keywordText"), match_type=raw.get("matchType"), expression=None,
                    state=raw.get("state"), bid=_num(raw.get("bid")), budget=None)
    if kind == "target":
        expr = raw.get("expression")
        return dict(entity_type="target", entity_id=str(raw.get("targetId")),
                    campaign_id=str(raw.get("campaignId")), ad_group_id=str(raw.get("adGroupId")),
                    name=None, match_type=raw.get("expressionType"),
                    expression=json.dumps(expr) if expr is not None else None,
                    state=raw.get("state"), bid=_num(raw.get("bid")), budget=None)
    # negativeKeyword
    return dict(entity_type="negativeKeyword", entity_id=str(raw.get("keywordId")),
                campaign_id=str(raw.get("campaignId")), ad_group_id=str(raw.get("adGroupId")),
                name=raw.get("keywordText"), match_type=raw.get("matchType"), expression=None,
                state=raw.get("state"), bid=None, budget=None)


def upsert_entities(account_id, kind, raws):
    conn = get_db()
    now = _now()
    n = 0
    for raw in raws:
        e = normalize_entity(kind, raw)
        if not e["entity_id"] or e["entity_id"] == "None":
            continue
        conn.execute(
            "INSERT INTO ppc_ad_entities (account_id, entity_type, entity_id, campaign_id, ad_group_id, "
            "name, match_type, expression, state, bid, budget, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id, entity_type, entity_id) DO UPDATE SET "
            "campaign_id=excluded.campaign_id, ad_group_id=excluded.ad_group_id, name=excluded.name, "
            "match_type=excluded.match_type, expression=excluded.expression, state=excluded.state, "
            "bid=excluded.bid, budget=excluded.budget, updated_at=excluded.updated_at",
            (account_id, e["entity_type"], e["entity_id"], e["campaign_id"], e["ad_group_id"],
             e["name"], e["match_type"], e["expression"], e["state"], e["bid"], e["budget"], now))
        n += 1
    if hasattr(conn, "commit"):
        conn.commit()
    return n


# ── v3 async reporting ───────────────────────────────────────────────────────

_REPORTS = {
    "spCampaigns": {"groupBy": ["campaign"],
                    "columns": ["campaignId", "campaignName", "campaignStatus",
                                "impressions", "clicks", "cost", "purchases7d", "sales7d"]},
    "spTargeting": {"groupBy": ["targeting"],
                    "columns": ["campaignId", "adGroupId", "keywordId", "targetId", "keyword",
                                "targeting", "matchType", "impressions", "clicks", "cost",
                                "purchases7d", "sales7d"]},
    "spSearchTerm": {"groupBy": ["searchTerm"],
                     "columns": ["campaignId", "adGroupId", "keywordId", "keyword", "searchTerm",
                                 "matchType", "impressions", "clicks", "cost", "purchases7d", "sales7d"]},
}


def request_report(creds, token, report_type_id, start, end, timeout=60):
    url = f"https://{_host(creds)}/reporting/reports"
    ct = "application/vnd.createasyncreportrequest.v3+json"
    headers = {**_base_headers(creds, token), "Content-Type": ct, "Accept": ct}
    spec = _REPORTS[report_type_id]
    body = {
        "name": f"{report_type_id} {start}..{end}",
        "startDate": start, "endDate": end,
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": spec["groupBy"],
            "columns": spec["columns"],
            "reportTypeId": report_type_id,
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        },
    }
    r = httpx.post(url, headers=headers, json=body, timeout=timeout)
    r.raise_for_status()
    return r.json().get("reportId")


def poll_report(creds, token, report_id, poll_timeout=300, interval=10):
    url = f"https://{_host(creds)}/reporting/reports/{report_id}"
    headers = _base_headers(creds, token)
    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        r = httpx.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        j = r.json() or {}
        st = j.get("status")
        if st in ("COMPLETED", "SUCCESS"):
            return j.get("url")
        if st in ("FAILURE", "CANCELLED", "FATAL"):
            raise RuntimeError(f"report {report_id} ended {st}: {j.get('failureReason') or ''}")
        time.sleep(interval)
    raise RuntimeError(f"report {report_id} did not finish within {poll_timeout}s")


def download_report(url, timeout=120):
    r = httpx.get(url, timeout=timeout)
    r.raise_for_status()
    raw = r.content
    try:
        raw = gzip.decompress(raw)
    except OSError:
        pass   # already decompressed
    return json.loads(raw.decode("utf-8"))


def pull_report(creds, token, report_type_id, days):
    end = date.today()
    start = end - timedelta(days=days)
    rid = request_report(creds, token, report_type_id, start.isoformat(), end.isoformat())
    if not rid:
        raise RuntimeError(f"{report_type_id}: no reportId returned")
    url = poll_report(creds, token, rid)
    return download_report(url)


def _int(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def upsert_campaign_metrics(account_id, rows, days):
    conn = get_db(); now = _now(); n = 0
    for r in rows:
        cid = r.get("campaignId")
        if cid is None:
            continue
        conn.execute(
            "INSERT INTO ppc_ad_metrics (account_id, entity_type, entity_id, window_days, impressions, "
            "clicks, cost, sales, orders, fetched_at) VALUES (?, 'campaign', ?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id, entity_type, entity_id, window_days) DO UPDATE SET "
            "impressions=excluded.impressions, clicks=excluded.clicks, cost=excluded.cost, "
            "sales=excluded.sales, orders=excluded.orders, fetched_at=excluded.fetched_at",
            (account_id, str(cid), days, _int(r.get("impressions")), _int(r.get("clicks")),
             _num(r.get("cost")), _num(r.get("sales7d")), _int(r.get("purchases7d")), now))
        n += 1
    if hasattr(conn, "commit"):
        conn.commit()
    return n


def upsert_targeting_metrics(account_id, rows, days):
    conn = get_db(); now = _now(); n = 0
    for r in rows:
        kid = r.get("keywordId") or r.get("targetId")
        if kid is None:
            continue
        etype = "keyword" if r.get("keywordId") else "target"
        conn.execute(
            "INSERT INTO ppc_ad_metrics (account_id, entity_type, entity_id, window_days, impressions, "
            "clicks, cost, sales, orders, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id, entity_type, entity_id, window_days) DO UPDATE SET "
            "impressions=excluded.impressions, clicks=excluded.clicks, cost=excluded.cost, "
            "sales=excluded.sales, orders=excluded.orders, fetched_at=excluded.fetched_at",
            (account_id, etype, str(kid), days, _int(r.get("impressions")), _int(r.get("clicks")),
             _num(r.get("cost")), _num(r.get("sales7d")), _int(r.get("purchases7d")), now))
        n += 1
    if hasattr(conn, "commit"):
        conn.commit()
    return n


def upsert_search_terms(account_id, rows, days):
    conn = get_db(); now = _now(); n = 0
    for r in rows:
        term = (r.get("searchTerm") or "").strip()
        if not term:
            continue
        conn.execute(
            "INSERT INTO ppc_ad_search_terms (account_id, search_term, campaign_id, ad_group_id, keyword, "
            "match_type, window_days, impressions, clicks, cost, sales, orders, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(account_id, search_term, campaign_id, ad_group_id, window_days) DO UPDATE SET "
            "keyword=excluded.keyword, match_type=excluded.match_type, impressions=excluded.impressions, "
            "clicks=excluded.clicks, cost=excluded.cost, sales=excluded.sales, orders=excluded.orders, "
            "fetched_at=excluded.fetched_at",
            (account_id, term, str(r.get("campaignId") or ""), str(r.get("adGroupId") or ""),
             r.get("keyword"), r.get("matchType"), days, _int(r.get("impressions")),
             _int(r.get("clicks")), _num(r.get("cost")), _num(r.get("sales7d")),
             _int(r.get("purchases7d")), now))
        n += 1
    if hasattr(conn, "commit"):
        conn.commit()
    return n


# ── orchestration ────────────────────────────────────────────────────────────

def pull_account(account_id, days=DEFAULT_WINDOW_DAYS):
    creds = ppc_ads_api.creds_for(account_id)
    if not creds:
        _set_status(account_id, state="error", message="no Ads API creds (ADS_* env vars)",
                    finished_at=_now())
        raise RuntimeError("no Ads API creds")
    _set_status(account_id, state="running", message="starting…", started_at=_now(), finished_at=None)
    token = ppc_ads_api._get_access_token(creds)
    counts = {}
    # entities
    for kind in ("campaign", "adGroup", "keyword", "target", "negativeKeyword"):
        _set_status(account_id, state="running", message=f"listing {kind}s…")
        try:
            counts[kind] = upsert_entities(account_id, kind, list_entities(creds, token, kind))
        except Exception as e:
            log.warning("ppc pull %s entities failed: %s", kind, e)
            counts[kind] = f"error: {str(e)[:80]}"
    # reports
    _set_status(account_id, state="running", message="campaign report…")
    counts["campaign_metrics"] = upsert_campaign_metrics(
        account_id, pull_report(creds, token, "spCampaigns", days), days)
    _set_status(account_id, state="running", message="targeting report…")
    counts["targeting_metrics"] = upsert_targeting_metrics(
        account_id, pull_report(creds, token, "spTargeting", days), days)
    _set_status(account_id, state="running", message="search-term report…")
    counts["search_terms"] = upsert_search_terms(
        account_id, pull_report(creds, token, "spSearchTerm", days), days)
    msg = ", ".join(f"{k}:{v}" for k, v in counts.items())
    _set_status(account_id, state="done", message=msg, finished_at=_now())
    return counts


def _accounts():
    """Account ids that have Ads creds. Falls back to the P&L accounts list."""
    try:
        import pl_db
        ids = [a.get("account_id") for a in pl_db.get_accounts() if a.get("account_id")]
    except Exception:
        ids = []
    ids = [a for a in ids if ppc_ads_api.creds_for(a)]
    if not ids and ppc_ads_api.creds_for("default"):
        ids = ["default"]
    return ids


def _pull_worker(days):
    ids = _accounts()
    if not ids:
        _set_status("account", state="error",
                    message="no account has Ads API creds (set ADS_* env vars)", finished_at=_now())
        return
    for aid in ids:
        try:
            pull_account(aid, days)
        except Exception as e:
            log.warning("ppc pull failed for %s: %s", aid, e)
            _set_status(aid, state="error", message=str(e)[:200], finished_at=_now())


def start_pull_async(days=DEFAULT_WINDOW_DAYS):
    if is_running():
        return False
    threading.Thread(target=_pull_worker, args=(days,), daemon=True).start()
    return True


# ── reads for the page ───────────────────────────────────────────────────────

def summary():
    conn = get_db()
    def _count(t, where=""):
        try:
            return dict(conn.execute(f"SELECT COUNT(*) AS n FROM {t} {where}").fetchone())["n"]
        except Exception:
            return 0
    def _updated():
        try:
            return dict(conn.execute("SELECT MAX(updated_at) AS m FROM ppc_ad_entities").fetchone() or {}).get("m")
        except Exception:
            return None
    return {
        "campaigns": _count("ppc_ad_entities", "WHERE entity_type='campaign'"),
        "keywords": _count("ppc_ad_entities", "WHERE entity_type='keyword'"),
        "targets": _count("ppc_ad_entities", "WHERE entity_type='target'"),
        "negatives": _count("ppc_ad_entities", "WHERE entity_type='negativeKeyword'"),
        "search_terms": _count("ppc_ad_search_terms"),
        "updated": _updated(),
    }


def top_search_terms(limit=100, order="cost"):
    conn = get_db()
    col = "cost" if order == "cost" else "sales"
    try:
        rows = conn.execute(
            f"SELECT search_term, keyword, match_type, impressions, clicks, cost, sales, orders "
            f"FROM ppc_ad_search_terms ORDER BY ({col} IS NULL), {col} DESC LIMIT ?", (limit,)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["acos"] = (d["cost"] / d["sales"] * 100) if d.get("sales") else None
        out.append(d)
    return out


def top_campaigns(limit=100):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT e.name AS name, e.state AS state, e.budget AS budget, "
            "m.impressions, m.clicks, m.cost, m.sales, m.orders "
            "FROM ppc_ad_metrics m LEFT JOIN ppc_ad_entities e "
            "ON e.account_id=m.account_id AND e.entity_type='campaign' AND e.entity_id=m.entity_id "
            "WHERE m.entity_type='campaign' ORDER BY (m.cost IS NULL), m.cost DESC LIMIT ?", (limit,)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        d = dict(r)
        d["acos"] = (d["cost"] / d["sales"] * 100) if d.get("sales") else None
        out.append(d)
    return out
