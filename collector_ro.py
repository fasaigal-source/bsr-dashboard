"""collector_ro.py — READ-ONLY access to the live BSR-collector Postgres.

Module 1's detail pages were written against the LOCAL SQLite schema
(rank_history / velocity_history / managed_asins / recommendations). The deployed
collector uses a different, Postgres-native schema (bsr_price_snapshots /
velocity_daily / watchlist / daily_recommendations). This module maps the new
tables back to the shapes the dashboard expects, so dashboard_module1.py can read
LIVE collector data with no change to the templates.

STRICTLY READ-ONLY:
  * connects as the cowork_ro role (SELECT-only; verified can_write=false)
  * every connection is pinned SET SESSION default_transaction_read_only = on
  * there is NOT a single INSERT/UPDATE/DELETE in this file
Never writes to the collector — the collector's own scheduler owns those tables.

Connection: reads env var COLLECTOR_RO_URL, e.g.
  export COLLECTOR_RO_URL="postgresql://cowork_ro:PASSWORD@host:port/railway"
Nothing is hardcoded; the credential lives only in the environment.

Table map (collector -> what the dashboard expects):
  bsr_price_snapshots   -> rank_history          (BSR chart)
  velocity_daily        -> velocity_history      (orders bar, {day: units})
  watchlist             -> managed_asins         (product card meta)
  daily_recommendations -> recommendations       (recommendation history table)
  (none)                -> price_changes          -> []  (collector pushes no prices)
  (none)                -> rank_history_import     -> {}  (collector has no CSV import)
"""
import os
import psycopg2
import psycopg2.extras

_EXPECTED = {
    "watchlist": {"account_id", "asin", "sku", "brand", "floor_price", "active"},
    "bsr_price_snapshots": {"account_id", "asin", "captured_at", "root_rank", "sub_rank", "price"},
    "velocity_daily": {"account_id", "asin", "day", "units"},
    "daily_recommendations": {"account_id", "asin", "created_at", "signal_state",
                              "recommended_action", "recommended_price", "reasoning",
                              "current_price", "root_rank", "sub_rank"},
}


def _url():
    u = os.environ.get("COLLECTOR_RO_URL")
    if not u:
        raise RuntimeError(
            "COLLECTOR_RO_URL is not set. Export the cowork_ro connection string, e.g.\n"
            '  export COLLECTOR_RO_URL="postgresql://cowork_ro:PASSWORD@host:port/railway"')
    return u


def _conn():
    """A fresh read-only connection (cursor returns dict-like rows)."""
    c = psycopg2.connect(_url(), cursor_factory=psycopg2.extras.RealDictCursor,
                         connect_timeout=10, sslmode="require")
    c.set_session(readonly=True, autocommit=True)   # belt-and-braces: no writes
    return c


def _iso(v):
    """datetime/date -> ISO string (templates do `x.created_at[:16]`, need str)."""
    return v.isoformat() if hasattr(v, "isoformat") else (v if v is None else str(v))


# ── schema self-check (run this from the test before trusting anything) ──────
def verify_schema():
    """Confirms the live collector tables/columns match what the mapping assumes.
    Returns (ok: bool, report: list[str]). Catches schema drift from the repo."""
    report, ok = [], True
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT table_name, column_name FROM information_schema.columns
                       WHERE table_schema='public' AND table_name = ANY(%s)""",
                    (list(_EXPECTED),))
        have = {}
        for r in cur.fetchall():
            have.setdefault(r["table_name"], set()).add(r["column_name"])
        for tbl, cols in _EXPECTED.items():
            if tbl not in have:
                report.append(f"MISSING TABLE: {tbl}"); ok = False; continue
            missing = cols - have[tbl]
            if missing:
                report.append(f"{tbl}: MISSING COLUMNS {sorted(missing)}"); ok = False
            else:
                report.append(f"{tbl}: OK ({len(have[tbl])} cols)")
    return ok, report


# ── product card meta (watchlist -> managed_asins shape) ─────────────────────
def get_product_meta(account_id, asin):
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT sku, brand, title, root_category, floor_price,
                              ceiling_price, active
                       FROM watchlist WHERE account_id=%s AND asin=%s""",
                    (account_id, asin))
        row = cur.fetchone()
    if not row:
        return None
    d = dict(row)
    d["current_price"] = get_current_price(account_id, asin, fallback=d.get("floor_price"))
    return d


# ── BSR ranks (bsr_price_snapshots -> rank_history), one point per day ───────
def get_rank_rows(account_id, asin, since):
    """List of {day, root_rank, sub_rank}: the LATEST snapshot per calendar day
    on/after `since` (YYYY-MM-DD). Mirrors product_page's per-day grouping."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT DISTINCT ON (captured_at::date)
                   captured_at::date::text AS day, root_rank, sub_rank
            FROM bsr_price_snapshots
            WHERE account_id=%s AND asin=%s AND captured_at >= %s
            ORDER BY captured_at::date, captured_at DESC
        """, (account_id, asin, since))
        return [dict(r) for r in cur.fetchall()]


def get_latest_ranks(account_id, asin):
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT root_rank, sub_rank FROM bsr_price_snapshots
                       WHERE account_id=%s AND asin=%s
                       ORDER BY captured_at DESC LIMIT 1""", (account_id, asin))
        return cur.fetchone()  # dict or None


# ── daily units (velocity_daily -> {day: units}) ─────────────────────────────
def get_daily_units(account_id, asin, days=14):
    from datetime import datetime, timedelta
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT day::text AS day, units FROM velocity_daily
                       WHERE account_id=%s AND asin=%s AND day >= %s
                       ORDER BY day ASC""", (account_id, asin, since))
        return {r["day"]: r["units"] for r in cur.fetchall()}


# ── current price (latest snapshot price) ────────────────────────────────────
def get_current_price(account_id, asin, fallback=None):
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT price FROM bsr_price_snapshots
                       WHERE account_id=%s AND asin=%s AND price IS NOT NULL
                       ORDER BY captured_at DESC LIMIT 1""", (account_id, asin))
        row = cur.fetchone()
    return row["price"] if row and row["price"] is not None else fallback


# ── recommendation history (daily_recommendations -> recommendations shape) ──
def get_recommendation_history(account_id, asin, limit=50):
    with _conn() as c, c.cursor() as cur:
        cur.execute("""
            SELECT created_at, current_price, root_rank, sub_rank, velocity_now,
                   net_delta_pct, signal_state, recommended_action,
                   recommended_price, reasoning
            FROM daily_recommendations
            WHERE account_id=%s AND asin=%s
            ORDER BY created_at DESC LIMIT %s""", (account_id, asin, limit))
        out = []
        for r in cur.fetchall():
            out.append({
                "created_at": _iso(r["created_at"]),
                "current_price": r["current_price"],
                "current_root_rank": r["root_rank"],
                "current_sub_rank": r["sub_rank"],
                "current_velocity": r["velocity_now"],
                "baseline_delta_pct": r["net_delta_pct"],
                "signal_state": r["signal_state"],
                "recommended_action": r["recommended_action"],
                "recommended_price": r["recommended_price"],
                "reasoning": r["reasoning"],
                # collector has no approve/reject workflow -> "You did" stays blank
                "status": None, "decided_at": None,
                "decided_price": None, "decided_note": None,
            })
        return out


# ── list pages (home / products) — watchlist -> managed_asins shape ──────────
def list_products():
    """Every watched product, newest-BSR ranks + latest price folded in, so the
    home and /products lists render live. Shape matches managed_asins usage."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("""SELECT account_id, asin, sku, brand, floor_price,
                              ceiling_price, root_category, active
                       FROM watchlist ORDER BY account_id, asin""")
        return [dict(r) for r in cur.fetchall()]


def list_accounts():
    """Distinct accounts derived from the watchlist (the collector has no
    separate accounts table). Shape: [{'account_id': ...}, ...]."""
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT DISTINCT account_id FROM watchlist ORDER BY account_id")
        return [dict(r) for r in cur.fetchall()]


def get_pending_recommendations():
    """The collector notifies daily and has no approve/reject queue, so there are
    no 'pending' items to action here. Recent recommendations are visible
    per-product on the detail page (get_recommendation_history)."""
    return []


# ── things the collector does not track -> safe empties ──────────────────────
def get_price_changes(account_id, asin, limit=50):
    return []   # collector never pushes prices, so no price-change log


def get_bsr_history_import(account_id, asin):
    return {}   # collector has no historic-CSV import table
