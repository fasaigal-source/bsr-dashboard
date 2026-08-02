"""
pl_amazon.py — module2_dashboard_fixes Groups A2/B3/C: read-only SP-API
enrichment (Amazon's product_tax_code, live selling price, and ASIN titles),
stored entirely in Module 2's own SQLite tables. NOTHING here writes to
Amazon, and NOTHING here writes to Module 1 (module1_db.py / managed_asins)
or the hosted bsr-collector Railway project -- title/tax-code/live-price
data collected here lives ONLY in this app's own new tables
(pl_asin_titles, pl_asin_tax_code, pl_asin_live_price).

=== Reuse, not duplication ===
Per the account owner's explicit instruction ("reuse that approach; do not
duplicate logic badly"), the actual SP-API calls are the SAME functions
Module 1's collector already uses successfully:
  - module1_collector.fetch_identity(asin, credentials, marketplace)
    -> title (getCatalogItem, summaries.itemName)
  - module1_collector.fetch_live_price(asin, marketplace_id, credentials, marketplace)
    -> live listing price (Product Pricing, CompetitivePrices -> ListingPrice)
Both are imported directly, not copy-pasted. Only product_tax_code has no
existing fetch function anywhere in this codebase (Module 1 never needed
it), so fetch_tax_code() below is genuinely new -- built against the
documented SP-API Listings Items shape (getListingsItem, includedData=
["attributes"], attributes.product_tax_code). I cannot call the real
Amazon endpoint from this sandbox to confirm the exact response shape
against this seller's real account, so this is written defensively: any
parse failure or unexpected shape falls back to "not available" rather
than guessing, and the raw payload is logged so a real run's logs can
show exactly what came back if the parse needs adjusting.

=== Rate limits ===
Titles are a one-off ~81-ASIN backfill (backfill_titles) -- small, but
still spaced out (default 1.1s between calls, comfortably under SP-API's
Catalog Items rate limits) rather than fired in a tight loop. Tax code and
live price are fetched lazily, one ASIN at a time, only when a SKU detail
page is actually opened for that product (not bulk), and cached locally so
repeat page views don't re-hit Amazon -- see FRESHNESS windows below.
"""

import json
import logging
import sqlite3
import db
import time
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)
DB_PATH = "bsr_history.db"
CONFIG_PATH = "config.json"

# How long a cached tax-code / live-price fetch is considered fresh before
# a SKU-page view will try Amazon again. Tax code essentially never
# changes; live price can change daily -- different windows on purpose.
TAX_CODE_FRESHNESS = timedelta(days=30)
LIVE_PRICE_FRESHNESS = timedelta(hours=1)


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_db(db_path=DB_PATH):
    conn = db.connect(db_path)
    if not db.is_postgres():
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA — three small tables, all Module 2's own, all read-only-derived.
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS pl_asin_titles (
    asin        TEXT PRIMARY KEY,
    title       TEXT,
    fetched_at  TEXT NOT NULL,
    available   INTEGER NOT NULL DEFAULT 1   -- 0 = tried, Amazon had nothing / fetch failed
);

CREATE TABLE IF NOT EXISTS pl_asin_tax_code (
    asin              TEXT PRIMARY KEY,
    product_tax_code  TEXT,
    fetched_at        TEXT NOT NULL,
    available         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS pl_asin_live_price (
    asin        TEXT PRIMARY KEY,
    price       REAL,
    fetched_at  TEXT NOT NULL,
    available   INTEGER NOT NULL DEFAULT 1
);
"""


def init_amazon_schema(db_path=DB_PATH):
    conn = get_db(db_path)
    if not db.is_postgres():          # on Postgres the schema is owned by migrate.py
        conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    log.info("Module 2 Amazon-enrichment schema initialised (titles / tax code / live price).")


# ─────────────────────────────────────────────────────────────────────────────
# CREDENTIALS — same pattern pl_tracker.py already uses for Finances calls,
# reusing module1_collector's pure credential-building helpers (no Module 1
# data is written by importing these).
# ─────────────────────────────────────────────────────────────────────────────

def _load_config(config_path=CONFIG_PATH):
    with open(config_path) as f:
        return json.load(f)


def _credentials_for_account(account, cfg=None):
    """Returns (credentials, marketplace, marketplace_id, seller_id) for one
    accounts-table row, or (None, None, None, None) if this account has no
    refresh_token on file yet (can't call SP-API for it)."""
    from module1_collector import make_credentials, get_marketplace
    if not account.get("refresh_token"):
        return None, None, None, None
    cfg = cfg or _load_config()
    credentials = make_credentials(cfg, account)
    marketplace_id = account.get("marketplace_id") or "A1F83G8C2ARO7P"
    marketplace = get_marketplace(marketplace_id)
    return credentials, marketplace, marketplace_id, account.get("seller_id")


def _accounts_to_try(preferred_account_id=None, db_path=DB_PATH):
    """cogs_sku_asin has no account_id column (an ASIN can in principle be
    tried against whichever accounts are configured), so when the caller
    doesn't already know which account owns an ASIN, we just try every
    configured account in turn (small catalogue, 1-2 real accounts) and use
    the first one that answers. If a preferred account is known (e.g. the
    SKU detail page's current account_filter), it's tried first."""
    from module1_db import get_accounts
    accounts = get_accounts(db_path=db_path)
    if preferred_account_id and preferred_account_id != "all":
        accounts = sorted(accounts, key=lambda a: a["account_id"] != preferred_account_id)
    return accounts


# ─────────────────────────────────────────────────────────────────────────────
# TITLES — Group C. One-off backfill across every known ASIN, plus a
# picked-up-going-forward top-up pass (call periodically / on demand for
# any ASIN not yet in pl_asin_titles).
# ─────────────────────────────────────────────────────────────────────────────

def get_title(asin, db_path=DB_PATH):
    conn = get_db(db_path)
    row = conn.execute("SELECT title FROM pl_asin_titles WHERE asin=? AND available=1", (asin,)).fetchone()
    conn.close()
    return row["title"] if row else None


def get_titles_map(asins, db_path=DB_PATH):
    """{asin: title} for every asin in the list that has one on file."""
    if not asins:
        return {}
    conn = get_db(db_path)
    placeholders = ",".join("?" * len(asins))
    rows = conn.execute(
        f"SELECT asin, title FROM pl_asin_titles WHERE asin IN ({placeholders}) AND available=1 AND title IS NOT NULL AND title != ''",
        list(asins)
    ).fetchall()
    conn.close()
    return {r["asin"]: r["title"] for r in rows}


def _all_known_asins(db_path=DB_PATH):
    conn = get_db(db_path)
    rows = conn.execute("SELECT DISTINCT asin FROM cogs_sku_asin WHERE asin IS NOT NULL AND asin != ''").fetchall()
    conn.close()
    return sorted({r["asin"] for r in rows})


def backfill_titles(db_path=DB_PATH, sleep_seconds=1.1, limit=None, only_missing=True):
    """One-off (and re-runnable) backfill across every ASIN this app knows
    about (from cogs_sku_asin -- the crosswalk populated by Orders-report
    sync + the Active Listings Report import, ~81 ASINs for this seller).
    only_missing=True (default) skips ASINs already in pl_asin_titles, so a
    re-run only picks up NEW ASINs -- "picked up for new ASINs going
    forward" per spec. Read-only against Amazon; writes ONLY to
    pl_asin_titles in this app's own DB, never to managed_asins.

    Returns a report dict. Run via: python -c "import pl_amazon;
    print(pl_amazon.backfill_titles())" or a small wrapper script -- same
    pattern as this engagement's other one-off backfill scripts."""
    from module1_collector import fetch_identity

    init_amazon_schema(db_path)
    asins = _all_known_asins(db_path)
    if only_missing:
        conn = get_db(db_path)
        have = {r["asin"] for r in conn.execute("SELECT asin FROM pl_asin_titles").fetchall()}
        conn.close()
        asins = [a for a in asins if a not in have]
    if limit:
        asins = asins[:limit]

    accounts = _accounts_to_try(db_path=db_path)
    cfg = None
    try:
        cfg = _load_config()
    except Exception as e:
        log.warning(f"backfill_titles: could not load config.json ({e}) -- skipping, nothing fetched.")
        return {"attempted": 0, "fetched": 0, "not_available": 0, "errors": 0, "skipped_no_config": True}

    fetched, not_available, errors = 0, 0, 0
    for i, asin in enumerate(asins):
        title = None
        got_response = False
        for account in accounts:
            credentials, marketplace, _mid, _sid = _credentials_for_account(account, cfg)
            if not credentials:
                continue
            try:
                identity = fetch_identity(asin, credentials, marketplace)
                got_response = True
                if identity.get("title"):
                    title = identity["title"]
                    break
            except Exception as e:
                log.warning(f"backfill_titles: {asin} via {account['account_id']} failed: {e}")
                errors += 1
        now = _now()
        conn = get_db(db_path)
        if title:
            conn.execute("""
                INSERT INTO pl_asin_titles (asin, title, fetched_at, available) VALUES (?,?,?,1)
                ON CONFLICT(asin) DO UPDATE SET title=excluded.title, fetched_at=excluded.fetched_at, available=1
            """, (asin, title, now))
            fetched += 1
        elif got_response:
            # Amazon answered but genuinely had no title for this ASIN --
            # never fabricate one; record available=0 so we don't re-hit it
            # every single backfill run, but it's distinguishable from "we
            # never even reached Amazon" in the DB if that matters later.
            conn.execute("""
                INSERT INTO pl_asin_titles (asin, title, fetched_at, available) VALUES (?,NULL,?,0)
                ON CONFLICT(asin) DO UPDATE SET title=NULL, fetched_at=excluded.fetched_at, available=0
            """, (asin, now))
            not_available += 1
        conn.commit()
        conn.close()
        if (i + 1) % 10 == 0:
            log.info(f"backfill_titles: {i + 1}/{len(asins)} ASINs processed...")
        time.sleep(sleep_seconds)

    return {
        "attempted": len(asins), "fetched": fetched,
        "not_available": not_available, "errors": errors,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TAX CODE — Group A2. Lazy, per-ASIN, cached. Cross-check ONLY -- never
# written into the profit formula, never auto-overwrites the seller's own
# recorded VAT rate (see pl_cogs.upsert_family_vat_rate, a completely
# separate, seller-driven write path).
# ─────────────────────────────────────────────────────────────────────────────

def get_tax_code(asin, db_path=DB_PATH):
    """Cached value only -- does not call Amazon. Returns dict(product_tax_code,
    fetched_at, available) or None if never fetched."""
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT product_tax_code, fetched_at, available FROM pl_asin_tax_code WHERE asin=?", (asin,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _is_stale(fetched_at_iso, freshness):
    try:
        fetched_at = datetime.fromisoformat(fetched_at_iso)
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - fetched_at) > freshness
    except Exception:
        return True


def _parse_product_tax_code(listings_payload):
    """Defensive parse of getListingsItem's attributes for product_tax_code.
    SP-API's JSON-schema-based attributes come back as
    {"product_tax_code": [{"value": "A_GEN_STANDARD", "marketplace_id": "..."}]}
    -- but this is exactly the piece I can't verify against a real response
    from this sandbox, so every reasonable shape is tried before giving up."""
    attrs = (listings_payload or {}).get("attributes") or {}
    val = attrs.get("product_tax_code")
    if isinstance(val, list) and val:
        first = val[0]
        if isinstance(first, dict):
            return first.get("value")
        if isinstance(first, str):
            return first
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return val.get("value")
    return None


def fetch_tax_code(asin, sku_hint, account, db_path=DB_PATH, cfg=None, force=False):
    """Fetch (or return cached) Amazon product_tax_code for one ASIN, via
    SP-API Listings Items (getListingsItem needs a sellerSku, not just an
    ASIN -- sku_hint should be any real seller SKU known to resolve to this
    ASIN, e.g. from pl_cogs.get_sku_identity()['asins']). Read-only.
    force=True bypasses the cache (the SKU page's "Refresh from Amazon"
    link). Always returns a dict; on any failure it's
    {"product_tax_code": None, "available": 0, ...} -- "not available",
    never a guess."""
    init_amazon_schema(db_path)
    if not force:
        cached = get_tax_code(asin, db_path)
        if cached and not _is_stale(cached["fetched_at"], TAX_CODE_FRESHNESS):
            return cached

    result = {"asin": asin, "product_tax_code": None, "fetched_at": _now(), "available": 0}
    if not sku_hint or not account or not account.get("seller_id"):
        _store_tax_code(asin, result, db_path)
        return result

    try:
        from sp_api.api import ListingsItems
        from sp_api.base import SellingApiException
    except ImportError:
        log.warning("fetch_tax_code: python-amazon-sp-api not available.")
        _store_tax_code(asin, result, db_path)
        return result

    try:
        credentials, marketplace, marketplace_id, seller_id = _credentials_for_account(account, cfg)
        if not credentials:
            _store_tax_code(asin, result, db_path)
            return result
        client = ListingsItems(credentials=credentials, marketplace=marketplace)
        resp = client.get_listings_item(
            sellerId=seller_id, sku=sku_hint, marketplaceIds=[marketplace_id],
            includedData=["attributes"]
        )
        payload = resp.payload or {}
        log.info(f"  [{asin}/{sku_hint}] Listings Items attributes raw: {json.dumps(payload.get('attributes', {}))}")
        code = _parse_product_tax_code(payload)
        if code:
            result["product_tax_code"] = code
            result["available"] = 1
    except SellingApiException as e:
        log.warning(f"fetch_tax_code: SP-API error for {asin}/{sku_hint}: {e}")
    except Exception as e:
        log.warning(f"fetch_tax_code: unexpected error for {asin}/{sku_hint}: {e}")

    _store_tax_code(asin, result, db_path)
    return result


def _store_tax_code(asin, result, db_path):
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO pl_asin_tax_code (asin, product_tax_code, fetched_at, available) VALUES (?,?,?,?)
        ON CONFLICT(asin) DO UPDATE SET
            product_tax_code=excluded.product_tax_code, fetched_at=excluded.fetched_at, available=excluded.available
    """, (asin, result.get("product_tax_code"), result["fetched_at"], result["available"]))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# LIVE SELLING PRICE — Group B3. Same lazy/cached/force-refresh shape as tax
# code, but reuses module1_collector.fetch_live_price directly instead of
# rebuilding the Product Pricing call.
# ─────────────────────────────────────────────────────────────────────────────

def get_live_price(asin, db_path=DB_PATH):
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT price, fetched_at, available FROM pl_asin_live_price WHERE asin=?", (asin,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def fetch_live_price_cached(asin, account, db_path=DB_PATH, cfg=None, force=False):
    """Cached wrapper around module1_collector.fetch_live_price. Read-only;
    never writes to Amazon or to Module 1. Always returns a dict; on
    failure {"price": None, "available": 0}."""
    init_amazon_schema(db_path)
    if not force:
        cached = get_live_price(asin, db_path)
        if cached and not _is_stale(cached["fetched_at"], LIVE_PRICE_FRESHNESS):
            return cached

    result = {"asin": asin, "price": None, "fetched_at": _now(), "available": 0}
    if not account:
        _store_live_price(asin, result, db_path)
        return result
    try:
        from module1_collector import fetch_live_price
        credentials, marketplace, marketplace_id, _sid = _credentials_for_account(account, cfg)
        if not credentials:
            _store_live_price(asin, result, db_path)
            return result
        price = fetch_live_price(asin, marketplace_id, credentials, marketplace)
        if price is not None:
            result["price"] = price
            result["available"] = 1
    except Exception as e:
        log.warning(f"fetch_live_price_cached: unexpected error for {asin}: {e}")

    _store_live_price(asin, result, db_path)
    return result


def _store_live_price(asin, result, db_path):
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO pl_asin_live_price (asin, price, fetched_at, available) VALUES (?,?,?,?)
        ON CONFLICT(asin) DO UPDATE SET
            price=excluded.price, fetched_at=excluded.fetched_at, available=excluded.available
    """, (asin, result.get("price"), result["fetched_at"], result["available"]))
    conn.commit()
    conn.close()
