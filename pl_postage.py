"""
pl_postage.py — Module 2 Phase 3 (module2_true_profit): missing-postage
worklist.

=== Why "missing" replaces the old flat per-SKU postage estimate ===
Before this module, an off-Amazon order (no real Amazon Buy-Shipping label
found -- see module2_postage_bugfix) silently got a GUESSED cost:
managed_asins.postage, a flat per-SKU default the seller once entered as a
rough figure. Per the account owner's explicit instruction for this phase:
"Never fabricate an estimate. A guessed £2.50 that was really £4.10 silently
corrupts margin. A blank is visible and gets fixed." That flat-default
fallback is retired as of this module -- see pl_db.recompute_line_item's
regime-2 branch, which now calls `get_manual_postage_for_order()` below
instead of reading managed_asins.postage. The three real states an order's
postage can be in now:

  exact    — a real Amazon Buy-Shipping label event was found (pl_db.py,
             unchanged, still the ~80-90% majority case).
  manual   — no Amazon label, but the seller has entered the real amount
             they actually paid a courier for this specific order (this
             module). Treated as exact for every downstream calculation —
             it IS a real, seller-confirmed cost, just not sourced from
             Amazon's API.
  missing  — no Amazon label, and the seller hasn't entered one yet. Cost is
             £0 in the P&L (never a guess), and the order appears on the
             /pl/postage worklist until it's filled in or confirmed
             genuinely free/irrelevant.

Postage is a property of an ORDER (one courier charge per parcel), not of a
line item or a product -- `pl_manual_postage` is keyed one row per
(account_id, order_id). A multi-item order's entered amount is split evenly
across its order_item_id rows by pl_db.recompute_line_item, the same "no
better per-item allocation signal exists" policy pl_tracker.py already uses
for real Amazon label costs on multi-item orders.

=== Why this is a separate module from pl_db.py ===
Same reasoning as pl_cogs.py (see its module docstring): keep the new,
still-settling surface area (its own tiny schema, its own worklist/bulk-fill
logic) isolated and independently testable, touching pl_db.py's own
already-verified internals only at the one necessary point (the regime-2
branch of recompute_line_item).
"""

import json
import logging
import sqlite3
import db
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)
DB_PATH = "bsr_history.db"   # same DB file as the rest of Module 2


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_db(db_path=DB_PATH):
    conn = db.connect(db_path)
    if not db.is_postgres():
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS pl_manual_postage (
    account_id     TEXT NOT NULL,
    order_id       TEXT NOT NULL,
    amount_exvat   REAL NOT NULL,   -- ex-VAT, same convention as cogs_families.unit_price_exvat
    entered_at     TEXT NOT NULL,
    PRIMARY KEY (account_id, order_id)
);
CREATE INDEX IF NOT EXISTS idx_manual_postage_entered ON pl_manual_postage(entered_at);
"""


def init_postage_schema(db_path=DB_PATH):
    conn = get_db(db_path)
    if not db.is_postgres():          # on Postgres the schema is owned by migrate.py
        conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    log.info("Module 2 postage-worklist schema initialised.")


# ─────────────────────────────────────────────────────────────────────────────
# READ / WRITE — the one function pl_db.recompute_line_item calls
# (get_manual_postage_for_order), plus the seller-facing write paths.
# ─────────────────────────────────────────────────────────────────────────────

def get_manual_postage_for_order(account_id, order_id, db_path=DB_PATH, conn=None):
    """Returns the ex-VAT amount the seller entered for this order, or None
    if nothing has been entered yet (the order should read postage_source=
    'missing'). Pass an already-open `conn` (pl_db.recompute_line_item does)
    to avoid a fresh connection per line item, same perf pattern as
    pl_cogs.get_cogs_for_sku."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db(db_path)
    row = conn.execute(
        "SELECT amount_exvat FROM pl_manual_postage WHERE account_id=? AND order_id=?",
        (account_id, order_id)
    ).fetchone()
    if owns_conn:
        conn.close()
    return row["amount_exvat"] if row else None


def set_manual_postage(account_id, order_id, amount_exvat, db_path=DB_PATH):
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO pl_manual_postage (account_id, order_id, amount_exvat, entered_at)
        VALUES (?,?,?,?)
        ON CONFLICT(account_id, order_id) DO UPDATE SET
            amount_exvat=excluded.amount_exvat, entered_at=excluded.entered_at
    """, (account_id, order_id, amount_exvat, _now()))
    conn.commit()
    conn.close()


def bulk_set_manual_postage(account_id, order_ids, amount_exvat, db_path=DB_PATH):
    """Bulk-fill: one value applied to every order_id in the list — the
    worklist's 'select several rows, enter one value' action, since most
    off-Amazon orders in a given week are the same courier/parcel size.
    Returns the number of orders written."""
    if not order_ids:
        return 0
    conn = get_db(db_path)
    now = _now()
    for order_id in order_ids:
        conn.execute("""
            INSERT INTO pl_manual_postage (account_id, order_id, amount_exvat, entered_at)
            VALUES (?,?,?,?)
            ON CONFLICT(account_id, order_id) DO UPDATE SET
                amount_exvat=excluded.amount_exvat, entered_at=excluded.entered_at
        """, (account_id, order_id, amount_exvat, now))
    conn.commit()
    conn.close()
    return len(order_ids)


def get_last_manual_postage_value(account_id=None, db_path=DB_PATH):
    """Most recently-entered amount (optionally scoped to one account) — used
    to PREFILL the worklist's bulk-fill box, since courier rates rarely
    change week to week and the seller usually just confirms the same
    number. Returns None if nothing has ever been entered."""
    conn = get_db(db_path)
    if account_id and account_id != "all":
        row = conn.execute("""
            SELECT amount_exvat FROM pl_manual_postage WHERE account_id=?
            ORDER BY entered_at DESC LIMIT 1
        """, (account_id,)).fetchone()
    else:
        row = conn.execute("""
            SELECT amount_exvat FROM pl_manual_postage ORDER BY entered_at DESC LIMIT 1
        """).fetchone()
    conn.close()
    return row["amount_exvat"] if row else None


# ─────────────────────────────────────────────────────────────────────────────
# WORKLIST — order-level (postage is a property of an order, not a product
# or a line item), newest first, only genuinely postage_source='missing'
# orders — an order with a real Amazon label or a manual entry must never
# appear here.
# ─────────────────────────────────────────────────────────────────────────────

def get_missing_postage_worklist(account_id=None, db_path=DB_PATH, limit=500):
    """Order-level worklist. Includes postage_source='missing' AND the
    legacy 'estimated' state (pre-Phase-3 rows that haven't been through a
    `python pl_tracker.py --reprocess` yet) -- an old flat-default GUESS is
    exactly as untrustworthy as a blank, so it belongs here too rather than
    silently waiting for a reprocess to surface it. Once a row is
    reprocessed it becomes 'missing' (cost reset to £0) or 'exact'/'manual'
    if data already exists; either way it stops appearing here for the
    'estimated' reason specifically."""
    conn = get_db(db_path)
    # module2_postage_badge_split: 'provisional' = a retired timestamp-heuristic
    # guess whose fabricated amount has been blanked; it needs a real per-order
    # label fetch or a manual entry, so it belongs on this worklist too.
    where = "WHERE postage_source IN ('missing', 'estimated', 'provisional')"
    params = []
    if account_id and account_id != "all":
        where += " AND account_id=?"
        params.append(account_id)
    rows = conn.execute(f"""
        SELECT account_id, order_id,
               GROUP_CONCAT(DISTINCT sku) AS skus,
               MAX(posted_date) AS posted_date,
               SUM(quantity) AS units
        FROM pl_line_items
        {where}
        GROUP BY account_id, order_id
        ORDER BY posted_date DESC
        LIMIT ?
    """, (*params, limit)).fetchall()
    order_ids = [r["order_id"] for r in rows]
    shipping = _get_buyer_paid_shipping(account_id, order_ids, db_path=db_path, conn=conn)
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["buyer_paid_shipping"] = shipping.get(d["order_id"], 0.0)
        out.append(d)
    return out


def _get_buyer_paid_shipping(account_id, order_ids, db_path=DB_PATH, conn=None):
    """Sums the 'ShippingCharge' component (what the BUYER paid Amazon for
    shipping on this order -- distinct from what the SELLER pays a courier,
    which is exactly the number this worklist is collecting) out of
    pl_raw_events.other_charge_types_json for each order. Returns
    {order_id: amount}. This is display-only context for the seller filling
    in the worklist (e.g. 'buyer paid £3.99 shipping, courier actually cost
    me...') — never used in any P&L calculation itself."""
    if not order_ids:
        return {}
    owns_conn = conn is None
    if owns_conn:
        conn = get_db(db_path)
    placeholders = ",".join("?" * len(order_ids))
    params = list(order_ids)
    where_acct = ""
    if account_id and account_id != "all":
        where_acct = "AND account_id=?"
        params = [account_id] + params
    rows = conn.execute(f"""
        SELECT order_id, other_charge_types_json FROM pl_raw_events
        WHERE event_type IN ('shipment','refund') {where_acct}
          AND order_id IN ({placeholders})
          AND other_charge_types_json IS NOT NULL
    """, params).fetchall()
    if owns_conn:
        conn.close()
    out = {}
    for r in rows:
        try:
            types = json.loads(r["other_charge_types_json"])
        except (TypeError, ValueError):
            continue
        amt = (types or {}).get("ShippingCharge")
        if amt:
            out[r["order_id"]] = out.get(r["order_id"], 0.0) + amt
    return out


# ─────────────────────────────────────────────────────────────────────────────
# BUG-SIGNAL WARNING — a long worklist (per the account owner: >15/week) is
# not something the seller should just type through; it's a sign the real
# Amazon label-cost pipeline (module2_postage_bugfix) isn't fully catching
# orders it should. Surface it as a warning, not a chore.
# ─────────────────────────────────────────────────────────────────────────────

def count_missing_postage_last_n_days(account_id=None, days=7, min_age_days=None, db_path=DB_PATH):
    conn = get_db(db_path)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    where = "WHERE postage_source IN ('missing', 'estimated', 'provisional') AND posted_date >= ?"
    params = [cutoff]
    # module2_postage_badge_split: optionally EXCLUDE the freshest orders, whose
    # label cost simply hasn't posted yet -- FBM Buy Shipping PostageBilling
    # posts ~1-2 days after the order and self-heals on the next sync's
    # recheck-window re-fetch. Counting them cries wolf (empirically: a batch of
    # "79 in last 7 days" fully cleared to 0 within a day). min_age_days=3 ->
    # only orders posted >= 3 days ago that are STILL missing, i.e. past the
    # normal posting lag = a genuine gap worth flagging.
    if min_age_days is not None:
        older_than = (datetime.now(timezone.utc) - timedelta(days=min_age_days)).strftime("%Y-%m-%d")
        where += " AND posted_date <= ?"
        params.append(older_than)
    if account_id and account_id != "all":
        where += " AND account_id=?"
        params.append(account_id)
    row = conn.execute(f"""
        SELECT COUNT(DISTINCT order_id) AS n FROM pl_line_items {where}
    """, params).fetchone()
    conn.close()
    return row["n"] or 0
