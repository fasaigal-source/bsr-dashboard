"""
pl_price.py — module2_sku_detail: recorded selling price + change-log.

=== What this is (Option A — read-only to Amazon, active now) ===
The per-SKU detail page lets the seller record what they're actually
selling a product for. That number:
  - feeds the P&L's gross-sales assumption where the account owner wants a
    manually-confirmed price instead of (or alongside) whatever real orders
    already show,
  - is stored HERE, locally, in pl_recorded_price (current value) and
    pl_price_changelog (append-only old -> new, timestamped, from day one
    per the account owner's explicit instruction),
  - NEVER calls Amazon. There is no SP-API write anywhere in this module.
    Editing this field does not change the live Seller Central listing —
    the dashboard labels this unambiguously wherever the field appears.

=== What this is NOT yet (Option B — built B-ready, NOT wired) ===
The account owner asked for the price-edit flow (field, confirm step,
change-log) to be structured so a future "Push to Amazon" action slots in
without restructuring — but explicitly NOT built or connected now. Per
their spec, the future Option B upgrade would add:
  - SP-API write scope (Listings Items API `patchListingsItem` or
    equivalent) — NOT requested/added in this pass,
  - a price floor/ceiling guardrail,
  - a 5%-per-day-change cap,
  - an idempotency lock (so a double-click / retry can't double-apply),
  - seller-click-only — never automatic/scheduled.
`push_to_amazon_price()` below is a placeholder that raises
NotImplementedError and documents this — exactly the same pattern already
used for the Ads API placeholder in pl_ads.fetch_from_ads_api(). Nothing
calls it. There is no code path in this app that can write to Amazon.

=== Why a separate module ===
Same reasoning as pl_postage.py/pl_ads.py: new, still-settling surface
area gets its own tiny schema and its own read/write functions, touching
nothing else. dashboard.py is the only caller.
"""

import logging
import sqlite3
import db
from datetime import datetime, timezone

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
CREATE TABLE IF NOT EXISTS pl_recorded_price (
    account_id      TEXT NOT NULL,
    canonical_sku   TEXT NOT NULL,
    price_exvat     REAL NOT NULL,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (account_id, canonical_sku)
);

CREATE TABLE IF NOT EXISTS pl_price_changelog (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      TEXT NOT NULL,
    canonical_sku   TEXT NOT NULL,
    old_price_exvat REAL,             -- NULL for the very first entry (nothing recorded before)
    new_price_exvat REAL NOT NULL,
    changed_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_changelog_sku
    ON pl_price_changelog(account_id, canonical_sku, changed_at);
"""


def init_price_schema(db_path=DB_PATH):
    conn = get_db(db_path)
    if not db.is_postgres():          # on Postgres the schema is owned by migrate.py
        conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    log.info("Module 2 recorded-price schema initialised.")


# ─────────────────────────────────────────────────────────────────────────────
# READ / WRITE
# ─────────────────────────────────────────────────────────────────────────────

def get_recorded_price(account_id, canonical_sku, db_path=DB_PATH):
    """Current recorded price, or None if nothing's been entered yet (the
    detail page should show the field blank, never a fabricated £0)."""
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT price_exvat FROM pl_recorded_price WHERE account_id=? AND canonical_sku=?",
        (account_id, canonical_sku)
    ).fetchone()
    conn.close()
    return row["price_exvat"] if row else None


def set_recorded_price(account_id, canonical_sku, new_price_exvat, db_path=DB_PATH):
    """Records the new price AND appends a changelog row (old -> new,
    timestamped) in the same transaction — the changelog exists from the
    very first edit, per the account owner's explicit "from day one"
    instruction, not bolted on later. Read-only toward Amazon: this
    function only ever writes to this app's own SQLite file."""
    conn = get_db(db_path)
    old = conn.execute(
        "SELECT price_exvat FROM pl_recorded_price WHERE account_id=? AND canonical_sku=?",
        (account_id, canonical_sku)
    ).fetchone()
    old_price = old["price_exvat"] if old else None
    now = _now()
    conn.execute("""
        INSERT INTO pl_recorded_price (account_id, canonical_sku, price_exvat, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(account_id, canonical_sku) DO UPDATE SET
            price_exvat=excluded.price_exvat, updated_at=excluded.updated_at
    """, (account_id, canonical_sku, new_price_exvat, now))
    conn.execute("""
        INSERT INTO pl_price_changelog (account_id, canonical_sku, old_price_exvat, new_price_exvat, changed_at)
        VALUES (?,?,?,?,?)
    """, (account_id, canonical_sku, old_price, new_price_exvat, now))
    conn.commit()
    conn.close()
    return {"old_price": old_price, "new_price": new_price_exvat, "changed_at": now}


def get_price_changelog(account_id, canonical_sku, db_path=DB_PATH, limit=20):
    """Most recent changes first — the detail page's "price history" list."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT old_price_exvat, new_price_exvat, changed_at
        FROM pl_price_changelog
        WHERE account_id=? AND canonical_sku=?
        ORDER BY changed_at DESC
        LIMIT ?
    """, (account_id, canonical_sku, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# OPTION B PLACEHOLDER — NOT wired, NOT called anywhere. Documents the shape
# of the future upgrade so the UI's "Push to Amazon" affordance has
# something concrete to point at once it's actually built. Calling this
# today always fails loudly rather than silently doing nothing.
# ─────────────────────────────────────────────────────────────────────────────

def push_to_amazon_price(account_id, canonical_sku, new_price_exvat, db_path=DB_PATH):
    """Placeholder for the future SP-API price-push (Option B). NOT
    implemented, NOT wired to any button, NOT called by any route in this
    pass. When this IS built, it must add (per the account owner's explicit
    spec): SP-API write scope, a price floor/ceiling guardrail, a
    5%-per-day-change cap, an idempotency lock (no double-apply on
    retry/double-click), and remain seller-click-only — never automatic or
    scheduled. Until then this exists only so the disabled "Push to Amazon"
    button in the UI has a named, documented target instead of nothing."""
    raise NotImplementedError(
        "Push-to-Amazon price writes are not built yet (Option B, deferred). "
        "This app is read-only toward Amazon in this pass — see pl_price.py "
        "module docstring."
    )
