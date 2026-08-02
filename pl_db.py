"""
pl_db.py — Database schema and helpers for Module 2 (P&L Tracker)

=== v3: anchor-based net_profit (see module2_pl_formula_fix prompt) ===
The original formula hand-summed a fixed set of named charge/fee lines and
silently missed ones it didn't know about (e.g. a Digital Services Fee).
v3 anchors on Amazon's own per-order "change to seller account balance" —
the algebraic sum of EVERY charge and EVERY fee line Amazon sends for that
order, whatever they're called — and only then subtracts the two things
Amazon does NOT already net into that figure: the real settled shipping
label cost, and the seller's COGS.

    net_profit = balance_change − label_cost − cogs

`categorize_item()` is the one place that reads ItemChargeList/ItemFeeList/
PromotionList. It buckets by STRUCTURE (is this "Principal"? Is this
"Commission"? Is this "Tax"? — everything else is "other", summed and
logged by name, never excluded) rather than hand-picking a fixed set of
names. This is what makes new fee types (DSF, and whatever Amazon adds
next) get captured automatically instead of silently dropped.

Also fixes: Principal is already ex-VAT (the old code divided by (1+vat)
a second time, which was the main source of the wrong numbers). The
separate "Tax" charge/fee lines ARE the VAT — captured explicitly now so
both a cash (inc-VAT) and an ex-VAT view can be produced and stored side by
side (`vat_treatment` replaces the old `deduct_vat_from_fees` flag; both
figures are always computed, the flag just decides the dashboard headline).

Design note on why there are two tables, not one
--------------------------------------------------
Amazon settles a single order line over time: a ShipmentEvent can post
before fees are finalised, a shipping-LABEL purchase is its own financial
event (often in a later date window), and a RefundEvent (partial or full,
sometimes more than one) can post weeks or months later. This job
deliberately RE-WALKS a trailing "recheck window" of history on every run
so pending→settled transitions, late-arriving labels, and late refunds all
get picked up. That means the same event can be fetched by more than one
run.

To upsert correctly without either double-counting or clobbering, raw
financial events are first landed in an append-only, de-duplicated ledger
(`pl_raw_events`), keyed so re-fetching the same event is a no-op.
`pl_line_items` is a materialised aggregate recomputed FROM that ledger for
whichever (account_id, order_id, order_item_id) keys were touched. Because
the full original item JSON is kept in `raw_json`, historical rows can be
**recomputed from stored data with a corrected formula, with no re-pull
from Amazon required** — see `reprocess_all_from_raw_events()`.
"""

import sqlite3
import json
import logging
from datetime import datetime, timezone, timedelta

import db
import pl_cogs
import pl_postage

log = logging.getLogger(__name__)
DB_PATH = "bsr_history.db"   # same DB file as Module 1 — one app, one database


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_db(db_path=DB_PATH):
    # db.connect() returns SQLite locally (row_factory=Row) or a psycopg2-backed
    # facade when DATABASE_URL is set. WAL is a SQLite-only pragma — skip it on PG.
    conn = db.connect(db_path)
    if not db.is_postgres():
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS pl_raw_events (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id            TEXT NOT NULL,
    order_id              TEXT NOT NULL,
    order_item_id         TEXT NOT NULL,
    event_type            TEXT NOT NULL,        -- 'shipment' | 'refund' | 'label'
    posted_date           TEXT NOT NULL,
    sku                   TEXT,
    quantity              INTEGER,
    principal             REAL NOT NULL DEFAULT 0,   -- ChargeType=="Principal", signed, ALREADY ex-VAT
    charge_tax            REAL NOT NULL DEFAULT 0,   -- ChargeType=="Tax" (output VAT), signed
    other_charges         REAL NOT NULL DEFAULT 0,   -- any other ChargeType (e.g. ShippingCharge), signed
    referral_fee          REAL NOT NULL DEFAULT 0,   -- FeeType=="Commission", signed (negative)
    fee_tax               REAL NOT NULL DEFAULT 0,   -- FeeType=="Tax" (input VAT on fees), signed
    other_fees            REAL NOT NULL DEFAULT 0,   -- any other FeeType (ShippingHB, DigitalServicesFee, ...), signed
    other_amazon_fees     REAL NOT NULL DEFAULT 0,   -- = other_fees; kept for backward compatibility
    promotion_total       REAL NOT NULL DEFAULT 0,   -- PromotionList, signed (negative)
    currency              TEXT,
    financial_event_group_id TEXT,
    other_charge_types_json  TEXT,   -- {ChargeType: amount} breakdown behind other_charges (audit / "don't drop a charge")
    fee_types_json            TEXT,  -- {FeeType: amount} breakdown behind other_fees
    raw_json              TEXT,      -- the ORIGINAL item dict -- lets us recompute with a corrected formula later
    ingested_at           TEXT NOT NULL,
    UNIQUE(account_id, order_id, order_item_id, event_type, posted_date)
);
CREATE INDEX IF NOT EXISTS idx_pl_raw_key
    ON pl_raw_events(account_id, order_id, order_item_id);

CREATE TABLE IF NOT EXISTS pl_line_items (
    account_id                TEXT NOT NULL,
    order_id                  TEXT NOT NULL,
    order_item_id             TEXT NOT NULL,
    posted_date               TEXT,
    asin                      TEXT,
    sku                       TEXT,
    quantity                  INTEGER,
    sale_price_incvat         REAL,   -- "Sales Proceeds": principal + other_charges + charge_tax
    sale_price_exvat          REAL,   -- principal + other_charges (excl. VAT)
    referral_fee              REAL,   -- abs magnitude
    other_amazon_fees         REAL,   -- abs magnitude, all non-Commission/non-Tax fees (ShippingHB, DSF, ...)
    promotion_total           REAL,   -- abs magnitude
    refund_total              REAL,   -- kept for backward compat; net effect of refunds, already folded into balance_change
    output_vat                REAL,   -- VAT collected from buyer, owed to HMRC (= charge_tax, abs)
    input_vat_reclaimed       REAL,   -- VAT reclaimable on fees + label (abs)
    balance_change            REAL,   -- Amazon's own bottom line for the sale/refund events (anchor)
    label_cost                REAL,   -- total settled shipping-label cost (inc VAT), abs
    label_cost_exvat          REAL,   -- ex-VAT portion of the label cost, abs
    postage_source            TEXT,   -- 'exact' (real Amazon label event found) | 'estimated' (per-SKU default fallback)
    cogs                      REAL,
    postage                   REAL,   -- = label_cost_exvat if exact, else per-SKU default (managed_asins.postage)
    vat_rate                  REAL,
    net_profit                REAL,   -- headline = net_profit_exvat (default view)
    margin_pct                REAL,   -- headline = margin_pct_exvat
    net_profit_cash           REAL,   -- balance_change - label_cost - cogs (inc-VAT / cash-in-bank view)
    net_profit_exvat          REAL,   -- net_profit_cash - output_vat + input_vat_reclaimed
    margin_pct_cash           REAL,
    margin_pct_exvat          REAL,
    currency                  TEXT,
    settlement_status         TEXT NOT NULL DEFAULT 'pending',   -- 'settled' | 'pending'
    financial_event_group_id  TEXT,
    ad_spend                  REAL,             -- reserved for Module 3 -- left NULL here
    raw_event_json            TEXT,
    created_at                TEXT NOT NULL,
    updated_at                TEXT NOT NULL,
    PRIMARY KEY (account_id, order_id, order_item_id)
);
CREATE INDEX IF NOT EXISTS idx_pl_line_asin ON pl_line_items(account_id, asin);
CREATE INDEX IF NOT EXISTS idx_pl_line_posted ON pl_line_items(posted_date);

CREATE TABLE IF NOT EXISTS pl_sync_state (
    account_id           TEXT PRIMARY KEY,
    earliest_synced      TEXT,   -- oldest PostedAfter we've walked back to (ISO)
    latest_synced        TEXT,   -- newest PostedBefore we've walked up to (ISO)
    last_run_at          TEXT
);

-- module2_pl_dashboard_bugfix round 6: Amazon's real Finances feed for this
-- account posts shipping-label costs via AdjustmentEventList (AdjustmentType
-- 'PostageBilling_Postage' / '_VAT' / '_DeliveryConfirmation' / etc., one
-- event per cost COMPONENT, all sharing one common PostedDate per label
-- purchase) -- confirmed via a real diagnostic pull. These events carry NO
-- order-linking field at all (no AmazonOrderId/OrderItemId/ShipmentId/
-- SellerSKU) -- a real, confirmed limitation of this part of the Finances
-- API, not a parsing gap. The only way to attach a real cost to an order is
-- a best-effort nearest-unlabeled-shipment-by-timestamp match (see the
-- matching pass inside pl_tracker.run_pl_job, which calls
-- find_nearest_unlabeled_shipment() below), which the user has explicitly
-- signed off on given this constraint (labels can post arbitrarily late,
-- per the account owner -- no fixed cutoff).
-- This table records EVERY successful match permanently, keyed by the
-- adjustment group's own (shared) PostedDate -- which is the closest thing
-- this account's feed has to a "label purchase id" -- so a later recheck-
-- window re-walk that re-fetches the SAME adjustment events never
-- re-matches (and potentially reassigns) them differently. Unmatched groups
-- are deliberately NOT recorded here, so they're retried on every run (a
-- shipment that hasn't been ingested yet, or existed outside a --since
-- window at match time, gets a chance to match once it's inserted).
CREATE TABLE IF NOT EXISTS pl_label_adjustment_matches (
    account_id             TEXT NOT NULL,
    adjustment_posted_date TEXT NOT NULL,
    matched_order_id       TEXT NOT NULL,
    matched_order_item_id  TEXT NOT NULL,
    match_gap_days         REAL,
    matched_at             TEXT NOT NULL,
    PRIMARY KEY (account_id, adjustment_posted_date)
);
"""

# Columns added after the original v1/v2 release. Kept as an explicit migration
# list (rather than just executescript's CREATE TABLE IF NOT EXISTS) because
# existing installs already have pl_raw_events / pl_line_items with the old,
# narrower column set and real data in them -- CREATE TABLE IF NOT EXISTS alone
# would silently skip adding these to an existing table.
_RAW_EVENT_MIGRATIONS = {
    "charge_tax": "REAL NOT NULL DEFAULT 0",
    "other_charges": "REAL NOT NULL DEFAULT 0",
    "fee_tax": "REAL NOT NULL DEFAULT 0",
    "other_fees": "REAL NOT NULL DEFAULT 0",
}
_LINE_ITEM_MIGRATIONS = {
    "output_vat": "REAL",
    "input_vat_reclaimed": "REAL",
    "balance_change": "REAL",
    "label_cost": "REAL",
    "label_cost_exvat": "REAL",
    "postage_source": "TEXT",
    "net_profit_cash": "REAL",
    "net_profit_exvat": "REAL",
    "margin_pct_cash": "REAL",
    "margin_pct_exvat": "REAL",
    # module2_cogs_integration: canonical_sku is the resolved (alias-mapped-or-
    # already-clean) SKU this line's COGS was actually derived against — this
    # is what the P&L is regrouped by, replacing the old "(unmapped SKU)"
    # ASIN-based bucket (see pl_cogs.py). cogs_priced records whether that
    # canonical's pricing family had a real price at the time of this
    # recompute (0 = COGS is a placeholder 0.0, not "no cost" — surfaced on
    # the dashboard as an unpriced-orders count, not silently treated as free).
    "canonical_sku": "TEXT",
    "cogs_priced": "INTEGER NOT NULL DEFAULT 0",
}


def init_pl_schema(db_path=DB_PATH):
    conn = get_db(db_path)
    # On SQLite the app owns the schema: executescript (not a naive split(";")) so
    # semicolons inside inline SQL comments can never mis-split a statement.
    # On Postgres the schema is created by migrate.py with the correct types
    # (NUMERIC(14,4) money, BIGSERIAL ids) — SCHEMA here uses SQLite-only
    # AUTOINCREMENT DDL which Postgres rejects even under CREATE IF NOT EXISTS,
    # so skip it. The additive column checks below run on both backends.
    if not db.is_postgres():
        conn.executescript(SCHEMA)
    if db.table_exists(conn, "pl_raw_events"):
        for col, decl in _RAW_EVENT_MIGRATIONS.items():
            if col not in db.table_columns(conn, "pl_raw_events"):
                conn.execute(f"ALTER TABLE pl_raw_events ADD COLUMN {col} {decl}")
                log.info(f"Migrated pl_raw_events: added {col} column.")
    if db.table_exists(conn, "pl_line_items"):
        for col, decl in _LINE_ITEM_MIGRATIONS.items():
            if col not in db.table_columns(conn, "pl_line_items"):
                conn.execute(f"ALTER TABLE pl_line_items ADD COLUMN {col} {decl}")
                log.info(f"Migrated pl_line_items: added {col} column.")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pl_line_canonical ON pl_line_items(account_id, canonical_sku)")
    # Standalone index on canonical_sku alone (no account_id prefix) -- the
    # missing-prices worklist and other COGS lookups filter by canonical_sku
    # ONLY (no account_id in the WHERE clause), so the composite index above
    # can't be used for those and every lookup fell back to a full table
    # scan. On the real 33,720-row table this made /pl/cogs take a very long
    # time (looked like an infinite load) to build the worklist.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pl_line_canonical_only ON pl_line_items(canonical_sku)")
    conn.commit()
    conn.close()
    pl_cogs.init_cogs_schema(db_path=db_path)
    pl_postage.init_postage_schema(db_path=db_path)
    log.info("Module 2 (P&L) schema initialised.")


# ─────────────────────────────────────────────────────────────────────────────
# CATEGORIZATION — the one place that reads ItemChargeList / ItemFeeList /
# PromotionList. Buckets by STRUCTURE, not a fixed name whitelist, so a brand
# new fee type is captured (as "other") rather than silently dropped.
# ─────────────────────────────────────────────────────────────────────────────

def _amount(component, key):
    obj = (component or {}).get(key) or {}
    try:
        return float(obj.get("CurrencyAmount") or 0), obj.get("CurrencyCode")
    except (TypeError, ValueError):
        return 0.0, obj.get("CurrencyCode")


def categorize_item(item):
    """
    item = one ShipmentItem / RefundItem dict (SellerSKU, OrderItemId,
    QuantityShipped, ItemChargeList, ItemFeeList, PromotionList).

    Returns signed totals bucketed by structure:
      principal        ChargeType=="Principal" (already ex-VAT)
      charge_tax       ChargeType=="Tax"        (output VAT on the sale)
      other_charges    anything else in ItemChargeList (e.g. ShippingCharge)
      referral_fee     FeeType=="Commission"
      fee_tax          FeeType=="Tax"           (input VAT on fees)
      other_fees       anything else in ItemFeeList (ShippingHB, DigitalServicesFee, ...)
      promotion_total  PromotionList (signed, negative)
      has_principal    bool -- False means this is very likely a fee-only event
                        (e.g. a shipping-label / Buy-Shipping purchase), not a sale.
    Nothing is excluded from the numeric totals: an unrecognised ChargeType or
    FeeType still lands in other_charges / other_fees (and is named in the
    *_types dicts for the run-summary log) -- it is never dropped.
    """
    principal = 0.0
    charge_tax = 0.0
    other_charges = 0.0
    other_charge_types = {}
    for chg in item.get("ItemChargeList", []) or []:
        ctype = chg.get("ChargeType")
        amt, _cur = _amount(chg, "ChargeAmount")
        if ctype == "Principal":
            principal += amt
        elif ctype == "Tax":
            charge_tax += amt
        else:
            other_charges += amt
            if amt:
                other_charge_types[ctype] = other_charge_types.get(ctype, 0.0) + amt

    referral_fee = 0.0
    fee_tax = 0.0
    other_fees = 0.0
    fee_types = {}
    for fee in item.get("ItemFeeList", []) or []:
        ftype = fee.get("FeeType")
        amt, _cur = _amount(fee, "FeeAmount")
        if ftype == "Commission":
            referral_fee += amt
        elif ftype == "Tax":
            fee_tax += amt
        else:
            other_fees += amt
            if amt:
                fee_types[ftype] = fee_types.get(ftype, 0.0) + amt

    promotion_total = 0.0
    for promo in item.get("PromotionList", []) or []:
        amt, _cur = _amount(promo, "PromotionAmount")
        promotion_total += amt

    currency = None
    for lst_key, amt_key in (("ItemChargeList", "ChargeAmount"), ("ItemFeeList", "FeeAmount"),
                              ("PromotionList", "PromotionAmount")):
        for c in item.get(lst_key, []) or []:
            _, cur = _amount(c, amt_key)
            if cur:
                currency = cur
                break
        if currency:
            break

    return dict(
        principal=principal, charge_tax=charge_tax, other_charges=other_charges,
        other_charge_types=other_charge_types,
        referral_fee=referral_fee, fee_tax=fee_tax, other_fees=other_fees,
        fee_types=fee_types, promotion_total=promotion_total,
        has_principal=bool(item.get("ItemChargeList")) and any(
            c.get("ChargeType") == "Principal" for c in item.get("ItemChargeList", [])),
        currency=currency,
        sku=item.get("SellerSKU"), order_item_id=item.get("OrderItemId"),
        quantity=item.get("QuantityShipped"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# RAW EVENT LEDGER
# ─────────────────────────────────────────────────────────────────────────────

def insert_raw_event(account_id, order_id, order_item_id, event_type, posted_date,
                      sku, quantity, principal, charge_tax, other_charges,
                      referral_fee, fee_tax, other_fees, promotion_total,
                      currency, financial_event_group_id,
                      other_charge_types, fee_types, raw_json, db_path=DB_PATH):
    """
    INSERT OR IGNORE — the UNIQUE(account_id, order_id, order_item_id, event_type,
    posted_date) constraint makes re-fetching the same event (recheck windows,
    overlapping pagination) a safe no-op. A genuinely distinct event for the same
    order/line (e.g. a second, later partial refund, or a label purchase posted
    on a later date) has a different posted_date and lands as its own row.
    Returns True if a new row was actually inserted.
    """
    conn = get_db(db_path)
    cur = conn.execute("""
        INSERT INTO pl_raw_events
            (account_id, order_id, order_item_id, event_type, posted_date, sku,
             quantity, principal, charge_tax, other_charges, referral_fee, fee_tax,
             other_fees, other_amazon_fees, promotion_total,
             currency, financial_event_group_id, other_charge_types_json,
             fee_types_json, raw_json, ingested_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT DO NOTHING
    """, (account_id, order_id, order_item_id, event_type, posted_date, sku,
          quantity, principal, charge_tax, other_charges, referral_fee, fee_tax,
          other_fees, other_fees, promotion_total,
          currency, financial_event_group_id,
          json.dumps(other_charge_types) if other_charge_types else None,
          json.dumps(fee_types) if fee_types else None,
          json.dumps(raw_json) if raw_json is not None else None,
          _now()))
    conn.commit()
    inserted = cur.rowcount > 0
    conn.close()
    return inserted


def get_all_keys(account_id=None, db_path=DB_PATH):
    """Every (account_id, order_id, order_item_id) key present in the ledger --
    used by the one-off reprocess to rebuild pl_line_items from raw_json alone,
    with no re-pull from Amazon."""
    conn = get_db(db_path)
    if account_id:
        rows = conn.execute(
            "SELECT DISTINCT account_id, order_id, order_item_id FROM pl_raw_events WHERE account_id=?",
            (account_id,)).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT account_id, order_id, order_item_id FROM pl_raw_events").fetchall()
    conn.close()
    return [(r["account_id"], r["order_id"], r["order_item_id"]) for r in rows]


def get_keys_for_family(family, account_id=None, db_path=DB_PATH):
    """module2_save_scope_fix: just the (account_id, order_id, order_item_id)
    keys whose already-resolved canonical_sku belongs to `family` -- used to
    reprocess ONLY the rows a single family-price edit can possibly change,
    instead of all ~34k rows. pl_line_items.canonical_sku was written by the
    last recompute, so this is a cheap indexed lookup; any row whose SKU
    resolves into this family already carries one of the family's canonical
    SKUs here. (Merge/define edits, which CHANGE a SKU's canonical, still use
    the full reprocess -- this scoped path is only wired to pure price edits,
    where the family's membership is stable.)"""
    conn = get_db(db_path)
    sql = """
        SELECT DISTINCT li.account_id, li.order_id, li.order_item_id
        FROM pl_line_items li
        JOIN cogs_canonical cc ON cc.canonical_sku = li.canonical_sku
        WHERE cc.family = ?
    """
    params = [family]
    if account_id:
        sql += " AND li.account_id = ?"
        params.append(account_id)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [(r["account_id"], r["order_id"], r["order_item_id"]) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATE / UPSERT pl_line_items FROM pl_raw_events
# ─────────────────────────────────────────────────────────────────────────────

def _categorize_row(r):
    """
    Re-derive one pl_raw_events row's categorized numbers (and its TRUE
    has_principal-ness) fresh from the preserved raw_json, rather than
    trusting the per-row numeric columns / event_type that were written to
    the ledger at ingestion time.

    Why this matters (module2_pl_dashboard_bugfix): `pl_raw_events` gained
    charge_tax / other_charges / fee_tax / other_fees as NEW columns
    (`_RAW_EVENT_MIGRATIONS`) after some rows already existed in the ledger.
    `ALTER TABLE ... ADD COLUMN ... DEFAULT 0` backfills a literal 0 for
    every pre-existing row -- it cannot retroactively derive the real value
    from history. Likewise, event_type=='label' didn't exist before this
    fix, so every historical shipping-label purchase is still sitting in the
    ledger tagged 'shipment'. Trusting those stored fields for old rows:
      (a) silently drops real negative fee/cost lines (they read as 0),
          inflating balance_change / net_profit above what's even possible
          against gross sales, and
      (b) permanently misses every historical label event, so
          postage_source is stuck on "estimated" for orders that actually
          had a real Amazon-bought label.
    raw_json is the original Amazon item, preserved from day one specifically
    so a formula/categorization fix never needs a re-pull -- so it, not the
    derived columns, must be the source of truth every time we recompute.
    Falls back to the stored columns only if raw_json is missing/unparseable
    (should not happen for anything ingested by this code), OR if raw_json is
    present but isn't a real Amazon item shape at all -- module2_pl_dashboard_
    bugfix round 6's AdjustmentEventList-matched label rows (see pl_tracker's
    matching pass) have no ItemChargeList/ItemFeeList of their own to
    re-derive from; their raw_json is audit metadata (which components
    matched, the time gap, which shipment) tagged with a "matched_via" key,
    not source data. For those rows the stored columns (computed once,
    correctly, at match time, and never touched again since the match itself
    is idempotent) ARE the source of truth -- calling categorize_item() on
    that metadata dict would silently zero every amount out (no
    ItemChargeList/ItemFeeList keys for it to find).
    """
    raw = r.get("raw_json")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "matched_via" in parsed:
                return dict(
                    principal=r["principal"] or 0, charge_tax=r["charge_tax"] or 0,
                    other_charges=r["other_charges"] or 0, referral_fee=r["referral_fee"] or 0,
                    fee_tax=r["fee_tax"] or 0, other_fees=r["other_fees"] or 0,
                    promotion_total=r["promotion_total"] or 0, has_principal=False,
                    from_raw_json=True,
                )
            cat = categorize_item(parsed)
            return dict(
                principal=cat["principal"], charge_tax=cat["charge_tax"],
                other_charges=cat["other_charges"], referral_fee=cat["referral_fee"],
                fee_tax=cat["fee_tax"], other_fees=cat["other_fees"],
                promotion_total=cat["promotion_total"], has_principal=cat["has_principal"],
                from_raw_json=True,
            )
        except (ValueError, TypeError):
            pass
    return dict(
        principal=r["principal"] or 0, charge_tax=r["charge_tax"] or 0,
        other_charges=r["other_charges"] or 0, referral_fee=r["referral_fee"] or 0,
        fee_tax=r["fee_tax"] or 0, other_fees=r["other_fees"] or 0,
        promotion_total=r["promotion_total"] or 0,
        has_principal=(r["event_type"] != "label"),
        from_raw_json=False,
    )


def recompute_line_item(account_id, order_id, order_item_id, asin_lookup,
                         group_status=None, db_path=DB_PATH, conn=None):
    """
    Recompute one pl_line_items row from every pl_raw_events row for that key.

    Every row's numeric buckets AND its sale-vs-label classification are
    re-derived fresh from raw_json via categorize_item() (see
    _categorize_row) rather than trusted from the stored ledger
    columns/event_type -- this is what makes both the live pull and
    `--reprocess` produce IDENTICAL, correct figures for old and new data
    alike, with no re-pull from Amazon required (see
    reprocess_all_from_raw_events).

    `asin_lookup(sku) -> dict|None` resolves SellerSKU to the managed_asins row
    (asin, vat_rate). `managed_asins.postage` is NO LONGER read here as of
    module2_true_profit Phase 3 -- an off-Amazon order's real cost now comes
    ONLY from a real Amazon label (regime 1) or a seller-entered manual
    amount (pl_postage, regime 2); see the regime-2 branch below for why the
    old flat-default guess was retired.
    `group_status(financial_event_group_id) -> 'Open'|'Closed'|None` resolves
    settlement status via the real FinancialEventGroup ProcessingStatus.

    Returns ('inserted'|'updated', row_dict). row_dict also carries
    `_reclassified_as_label` (count of rows stored as 'shipment' that were
    actually label events) and `_raw_json_fallback_count` (rows that had to
    fall back to stored columns) so callers can aggregate these into a
    run-summary log line instead of per-row spam.
    """
    # module2_save_hang_fix: accept an optional caller-owned `conn`. When one
    # is passed (the --reprocess / _reprocess_after_cogs_change path now does),
    # this function does NOT open its own connection, does NOT commit per row,
    # and does NOT close -- the caller owns one connection and commits once.
    # This is the fix for the "Save family price spins forever" bug: the old
    # per-row connect()+commit() (an fsync each) across all ~34k rows made a
    # single synchronous reprocess take ~11 min on a real (OneDrive-synced)
    # disk; one shared connection + one commit does the same work in ~2s.
    # Passing conn=None keeps the original behaviour for the live-pull path.
    owns_conn = conn is None
    if owns_conn:
        conn = get_db(db_path)
    rows = conn.execute("""
        SELECT * FROM pl_raw_events
        WHERE account_id=? AND order_id=? AND order_item_id=?
    """, (account_id, order_id, order_item_id)).fetchall()
    rows = [dict(r) for r in rows]
    if not rows:
        if owns_conn:
            conn.close()
        return None, None

    cats = [_categorize_row(r) for r in rows]
    raw_json_fallback_count = sum(1 for c in cats if not c["from_raw_json"])

    def _is_label(r, c):
        if r["event_type"] == "refund":
            return False           # a refund is never a label purchase
        if r["event_type"] == "label":
            return True
        # event_type == "shipment": re-derive -- older ingestion tagged every
        # ShipmentEventList item "shipment" even when it had no Principal
        # charge (i.e. was really a fee-only label purchase).
        return not c["has_principal"]

    sale_idx  = [i for i, r in enumerate(rows) if not _is_label(r, cats[i])]
    label_idx = [i for i, r in enumerate(rows) if _is_label(r, cats[i])]
    sale_rows  = [rows[i] for i in sale_idx]
    label_rows = [rows[i] for i in label_idx]
    reclassified_as_label = sum(
        1 for i in label_idx if rows[i]["event_type"] == "shipment")

    def s(col, idxs):
        return sum((cats[i][col] or 0) for i in idxs)

    principal      = s("principal", sale_idx)
    charge_tax     = s("charge_tax", sale_idx)
    other_charges  = s("other_charges", sale_idx)
    referral_fee   = s("referral_fee", sale_idx)
    fee_tax        = s("fee_tax", sale_idx)
    other_fees     = s("other_fees", sale_idx)
    promotion_total = s("promotion_total", sale_idx)

    # Amazon's own bottom line for the sale/refund side -- the anchor. Sum of
    # EVERY charge and EVERY fee, whatever they're called. This is what makes
    # a brand-new fee type (e.g. Digital Services Fee) get captured
    # automatically instead of needing a code change.
    balance_change = (principal + charge_tax + other_charges
                       + referral_fee + fee_tax + other_fees + promotion_total)

    sku = next((r["sku"] for r in sale_rows if r["sku"]), None) or \
          next((r["sku"] for r in label_rows if r["sku"]), None)
    quantity = None
    for r in [r for r in sale_rows if r["event_type"] == "shipment"]:
        if r["quantity"] is not None:
            quantity = (quantity or 0) + r["quantity"]

    currency = next((r["currency"] for r in rows if r["currency"]), None)
    fegid = next((r["financial_event_group_id"] for r in rows if r["financial_event_group_id"]), None)
    posted_dates = [r["posted_date"] for r in sale_rows if r["posted_date"]]
    posted_date = min(posted_dates) if posted_dates else \
        (min((r["posted_date"] for r in label_rows), default=None))

    # Label / shipping-purchase events. module2_postage_badge_split (precedence):
    # partition them into REAL, order-scoped labels (fetch_label_events_for_order,
    # matched_via 'per_order_finances_api') and superseded timestamp-heuristic
    # GUESSES. A real per-order label ALWAYS wins over a heuristic one for the
    # same line, regardless of posted_date -- "latest wins" is wrong here because
    # the heuristic event is fabricated in a LATER backfill run and so always
    # carries the later date; a guess must never outrank real data, now or on any
    # future run. Raw events stay immutable -- heuristic events are simply ignored
    # whenever a real one exists for the line.
    def _is_heuristic_label(i):
        return "timestamp_heuristic" in (rows[i].get("raw_json") or "")
    real_label_idx = [i for i in label_idx if not _is_heuristic_label(i)]
    has_real_label = len(real_label_idx) > 0
    heuristic_only = bool(label_idx) and not has_real_label

    # Label cost comes ONLY from real label events (never a heuristic guess).
    # Everything Tax-typed in them is reclaimable input VAT; everything else is
    # the ex-VAT label cost -- which is exactly what the `postage` column stores,
    # so postage is the ex-VAT base and is never VAT-inflated. A label event is
    # its own financial event (often a different date window than the sale) but
    # nets onto this SAME order/line via the ledger regardless of which run
    # fetched it.
    label_tax_signed  = s("charge_tax", real_label_idx) + s("fee_tax", real_label_idx)
    label_base_signed = (s("principal", real_label_idx) + s("other_charges", real_label_idx)
                         + s("referral_fee", real_label_idx) + s("other_fees", real_label_idx)
                         + s("promotion_total", real_label_idx))

    # Resolve SKU -> product config (VAT rate / ASIN / fallback postage --
    # managed_asins.cogs is no longer used; see below).
    product = asin_lookup(sku) if sku else None
    asin = (product or {}).get("asin")
    vat_rate = (product or {}).get("vat_rate")
    if vat_rate is None:
        vat_rate = 0.20
    units = quantity or 0

    # module2_cogs_integration: COGS now comes from the canonical-SKU/
    # pricing-family system (pl_cogs.py), not managed_asins.cogs (which was
    # always 0 for every real order -- no product ever had a COGS number
    # entered against it there). Resolution rule: alias-table hit -> its
    # canonical_sku; no hit -> the SKU IS already its own canonical (never
    # "unmapped"). `priced` is False (cogs_total 0.0) until the seller enters
    # a price for this canonical's family -- surfaced on the dashboard as an
    # unpriced-orders count, not silently treated as a real zero-cost item.
    #
    # module2_debug_fix_pass FIX 3 perf note: pass THIS function's own,
    # already-open `conn` through so get_cogs_for_sku doesn't open 3 fresh
    # sqlite3 connections per line item -- at 33,720 rows that was 100,000+
    # connection opens per reprocess, slow enough to make a Merge/Define
    # action (which triggers a synchronous reprocess) look hung. Safe here
    # because nothing has been written on `conn` yet at this point in the
    # function (only SELECTs above) -- see ensure_canonical's docstring.
    cogs_info = pl_cogs.get_cogs_for_sku(sku, units, db_path=db_path, conn=conn)
    canonical_sku = cogs_info["canonical_sku"]
    cogs_priced = cogs_info["priced"]
    cogs = cogs_info["cogs_total"]

    # module2_postage_badge_split: 'exact' must mean a REAL, order-scoped
    # Amazon Buy-Shipping label (fetch_label_events_for_order — Amazon scopes
    # it to the order server-side). The SUPERSEDED nearest-in-time heuristic
    # (_match_adjustment_group -> find_nearest_unlabeled_shipment) attaches a
    # PostageBilling event to the nearest not-yet-labelled shipment purely on
    # timestamp, with no order-linking field — a GUESS, and on multi-item
    # orders it over-charges ~N×. Such events self-tag matched_via
    # "timestamp_heuristic" in raw_json. Never let a guess read as 'exact':
    # treat it as UNLABELLED, blank its fabricated amount (per the standing
    # never-fabricate-postage rule — a guessed number silently corrupts
    # margin), and badge it 'provisional' so it surfaces on /pl/postage for a
    # real per-order label fetch or a manual entry — distinct from a genuine
    # off-Amazon order that was never labelled at all ('missing').
    if has_real_label:
        label_cost_exvat = abs(label_base_signed)
        label_vat = abs(label_tax_signed)
        label_cost = label_cost_exvat + label_vat
        postage_source = "exact"
    else:
        # Regime 2 (~10-20%): off-Amazon courier (InPost/Evri/Royal Mail
        # direct), no label event in Amazon's feed at all -- Amazon has no
        # cost for this order and never will.
        #
        # module2_true_profit Phase 3: this used to fall back to a flat
        # per-SKU GUESSED default (managed_asins.postage) and mark the order
        # postage_source='estimated'. Per the account owner's explicit
        # instruction for this phase -- "Never fabricate an estimate. A
        # guessed £2.50 that was really £4.10 silently corrupts margin. A
        # blank is visible and gets fixed." -- that guess is retired. Manual
        # entry (pl_postage, the /pl/postage worklist) is now the ONLY way
        # an off-Amazon order gets a real cost:
        #   seller has entered one for this order -> postage_source='manual',
        #     treated as exact from here on (it IS a real, confirmed cost).
        #   nothing entered yet -> cost is £0 (never a guess), postage_source
        #     ='missing', and the order surfaces on /pl/postage until fixed.
        # A multi-item order's single entered amount is split evenly across
        # its order_item_id rows -- the same "no better per-item allocation
        # signal exists" policy pl_tracker.fetch_label_events_for_order
        # already applies to real multi-item Amazon label costs.
        manual_total = pl_postage.get_manual_postage_for_order(account_id, order_id, conn=conn)
        if manual_total is not None:
            sibling_items = get_order_item_ids(account_id, order_id, db_path=db_path)
            item_count = len(sibling_items) or 1
            label_cost_exvat = manual_total / item_count
            label_vat = 0.0
            label_cost = label_cost_exvat
            postage_source = "manual"
        else:
            label_cost_exvat = 0.0
            label_vat = 0.0
            label_cost = 0.0
            # badge-split: a retired timestamp-heuristic guess (with no real
            # per-order label present) reads 'provisional' -- its real label can
            # still be fetched per-order; a genuine never-labelled off-Amazon
            # order stays 'missing'.
            postage_source = "provisional" if heuristic_only else "missing"

    sale_price_incvat = principal + other_charges + charge_tax   # "Sales Proceeds"
    sale_price_exvat = principal + other_charges

    output_vat = abs(charge_tax)
    input_vat_reclaimed = abs(fee_tax) + label_vat

    net_profit_cash = balance_change - label_cost - cogs
    net_profit_exvat = net_profit_cash - output_vat + input_vat_reclaimed

    margin_pct_cash = (net_profit_cash / sale_price_exvat) if sale_price_exvat else None
    margin_pct_exvat = (net_profit_exvat / sale_price_exvat) if sale_price_exvat else None

    # Sanity invariant (module2_pl_dashboard_bugfix): net profit can never
    # legitimately exceed gross sales -- if it does, a cost/VAT term is being
    # added instead of subtracted (or mis-derived) for this row. Logged loudly
    # per-row here (a genuine violation is rare and actionable), aggregated
    # into one summary count by reprocess_all_from_raw_events.
    tol = 0.01
    sanity_anomaly = (
        (bool(sale_price_exvat) and net_profit_exvat > sale_price_exvat + tol) or
        (bool(sale_price_incvat) and net_profit_cash > sale_price_incvat + tol)
    )
    if sanity_anomaly:
        log.warning(
            f"[P&L SANITY] {account_id}/{order_id}/{order_item_id} (sku={sku}): "
            f"net_profit exceeds gross sales -- net_profit_exvat={net_profit_exvat:.2f} vs "
            f"sale_price_exvat={sale_price_exvat:.2f}; net_profit_cash={net_profit_cash:.2f} vs "
            f"sale_price_incvat={sale_price_incvat:.2f}. A cost/VAT term is likely mis-signed or "
            f"missing for this row -- inspect raw_event_json.")

    # Settlement status: authoritative via FinancialEventGroup ProcessingStatus
    # when we have a group id; otherwise fall back to "have we seen fee data
    # at all yet" -- Amazon can post a bare shipment before fees finalise.
    shipment_rows = [r for r in sale_rows if r["event_type"] == "shipment"]
    status = "pending"
    if fegid and group_status:
        gs = group_status(fegid)
        if gs == "Closed":
            status = "settled"
        elif gs == "Open":
            status = "pending"
        else:
            status = "settled" if (shipment_rows and referral_fee) else "pending"
    elif shipment_rows and referral_fee:
        status = "settled"
    if not shipment_rows:
        # We've only seen a label (or only a refund) for this key so far --
        # hold it as pending rather than reporting a partial/misleading number.
        status = "pending"

    raw_event_json = json.dumps({
        "shipment_events": [json.loads(r["raw_json"]) for r in sale_rows
                             if r["event_type"] == "shipment" and r["raw_json"]],
        "refund_events":   [json.loads(r["raw_json"]) for r in sale_rows
                             if r["event_type"] == "refund" and r["raw_json"]],
        "label_events":    [json.loads(r["raw_json"]) for r in label_rows if r["raw_json"]],
    })

    now = _now()
    existing = conn.execute("""
        SELECT 1 FROM pl_line_items WHERE account_id=? AND order_id=? AND order_item_id=?
    """, (account_id, order_id, order_item_id)).fetchone()

    conn.execute("""
        INSERT INTO pl_line_items
            (account_id, order_id, order_item_id, posted_date, asin, sku, quantity,
             sale_price_incvat, sale_price_exvat, referral_fee, other_amazon_fees,
             promotion_total, refund_total, output_vat, input_vat_reclaimed,
             balance_change, label_cost, label_cost_exvat, postage_source,
             cogs, postage, vat_rate, net_profit, margin_pct,
             net_profit_cash, net_profit_exvat, margin_pct_cash, margin_pct_exvat,
             currency, settlement_status, financial_event_group_id,
             ad_spend, raw_event_json, created_at, updated_at, canonical_sku, cogs_priced)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?)
        ON CONFLICT(account_id, order_id, order_item_id) DO UPDATE SET
            posted_date=excluded.posted_date, asin=excluded.asin, sku=excluded.sku,
            quantity=excluded.quantity, sale_price_incvat=excluded.sale_price_incvat,
            sale_price_exvat=excluded.sale_price_exvat, referral_fee=excluded.referral_fee,
            other_amazon_fees=excluded.other_amazon_fees, promotion_total=excluded.promotion_total,
            refund_total=excluded.refund_total, output_vat=excluded.output_vat,
            input_vat_reclaimed=excluded.input_vat_reclaimed, balance_change=excluded.balance_change,
            label_cost=excluded.label_cost, label_cost_exvat=excluded.label_cost_exvat,
            postage_source=excluded.postage_source,
            cogs=excluded.cogs, postage=excluded.postage, vat_rate=excluded.vat_rate,
            net_profit=excluded.net_profit, margin_pct=excluded.margin_pct,
            net_profit_cash=excluded.net_profit_cash, net_profit_exvat=excluded.net_profit_exvat,
            margin_pct_cash=excluded.margin_pct_cash, margin_pct_exvat=excluded.margin_pct_exvat,
            currency=excluded.currency, settlement_status=excluded.settlement_status,
            financial_event_group_id=excluded.financial_event_group_id,
            raw_event_json=excluded.raw_event_json, updated_at=excluded.updated_at,
            canonical_sku=excluded.canonical_sku, cogs_priced=excluded.cogs_priced
    """, (account_id, order_id, order_item_id, posted_date, asin, sku, units,
          sale_price_incvat, sale_price_exvat, abs(referral_fee), abs(other_fees),
          abs(promotion_total), None,  # refund_total: legacy column, superseded by balance_change
          output_vat, input_vat_reclaimed,
          balance_change, label_cost, label_cost_exvat, postage_source,
          cogs, label_cost_exvat, vat_rate,
          net_profit_exvat, margin_pct_exvat,
          net_profit_cash, net_profit_exvat, margin_pct_cash, margin_pct_exvat,
          currency, status, fegid, raw_event_json, now, now,
          canonical_sku, 1 if cogs_priced else 0))
    if owns_conn:
        conn.commit()
        conn.close()

    row = dict(account_id=account_id, order_id=order_id, order_item_id=order_item_id,
               asin=asin, sku=sku, quantity=units,
               sale_price_incvat=sale_price_incvat, sale_price_exvat=sale_price_exvat,
               referral_fee=abs(referral_fee), other_amazon_fees=abs(other_fees),
               promotion_total=abs(promotion_total),
               output_vat=output_vat, input_vat_reclaimed=input_vat_reclaimed,
               balance_change=balance_change, label_cost=label_cost,
               label_cost_exvat=label_cost_exvat, postage_source=postage_source,
               cogs=cogs, postage=label_cost_exvat, vat_rate=vat_rate,
               net_profit_cash=net_profit_cash, net_profit_exvat=net_profit_exvat,
               margin_pct_cash=margin_pct_cash, margin_pct_exvat=margin_pct_exvat,
               net_profit=net_profit_exvat, margin_pct=margin_pct_exvat,
               currency=currency, settlement_status=status,
               canonical_sku=canonical_sku, cogs_priced=cogs_priced,
               sanity_anomaly=sanity_anomaly,
               _reclassified_as_label=reclassified_as_label,
               _raw_json_fallback_count=raw_json_fallback_count)
    return ("updated" if existing else "inserted"), row


def reprocess_all_from_raw_events(account_id=None, asin_lookup=None, group_status=None, db_path=DB_PATH, keys=None):
    """
    One-off (or repeatable) recompute of every pl_line_items row directly from
    the already-ingested pl_raw_events ledger -- NO re-pull from Amazon. This
    is how a formula fix (like this one) gets applied to historical data: the
    original item JSON was preserved on every raw event, and recompute_line_item
    re-derives its categorized numbers AND sale-vs-label classification fresh
    from that JSON every time (see _categorize_row) -- it never trusts the
    ledger's own numeric columns/event_type, which can be stale for rows
    ingested before a categorization fix (see module2_pl_dashboard_bugfix).

    `asin_lookup` defaults to a no-op (no product match) if not supplied --
    pass the real module1_db-backed lookup in production so COGS/postage/VAT
    resolve correctly.
    """
    if asin_lookup is None:
        asin_lookup = lambda sku: None
    # module2_save_scope_fix: callers can pass an explicit `keys` subset (e.g.
    # only the rows in one edited price family) to avoid rebuilding all ~34k
    # line items on a single-family price change. keys=None keeps the original
    # "every key in the ledger" behaviour (--reprocess, first build).
    if keys is None:
        keys = get_all_keys(account_id=account_id, db_path=db_path)
    updated = 0
    reclassified_total = 0
    fallback_total = 0
    anomaly_total = 0
    # module2_save_hang_fix: one shared connection for the whole pass, with a
    # single commit at the end (plus periodic checkpoints so a very long run
    # isn't one giant uncommitted transaction). get_all_keys() above has
    # already closed its own read connection, so there's no second writer.
    conn = get_db(db_path)
    try:
        for i, (acct, order_id, order_item_id) in enumerate(keys):
            kind, row = recompute_line_item(acct, order_id, order_item_id, asin_lookup,
                                            group_status=group_status, db_path=db_path, conn=conn)
            if kind:
                updated += 1
                reclassified_total += row.get("_reclassified_as_label") or 0
                fallback_total += row.get("_raw_json_fallback_count") or 0
                if row.get("sanity_anomaly"):
                    anomaly_total += 1
            if (i + 1) % 5000 == 0:
                conn.commit()
        conn.commit()
    finally:
        conn.close()
    log.info(f"Reprocessed {updated} line item(s) from stored raw events "
             f"({'account ' + account_id if account_id else 'all accounts'}).")
    if reclassified_total:
        log.info(f"  {reclassified_total} historical raw event row(s) were stored as 'shipment' "
                 f"but re-derived from raw_json as real shipping-label events -- postage_source "
                 f"for those orders should now read 'exact' instead of 'estimated'.")
    if fallback_total:
        log.warning(f"  {fallback_total} raw event row(s) had no usable raw_json and fell back to "
                    f"stored ledger columns (may be stale/pre-migration figures) -- these could not "
                    f"be corrected by this reprocess.")
    if anomaly_total:
        log.warning(f"  [P&L SANITY] {anomaly_total} line item(s) still show net_profit exceeding "
                    f"gross sales after reprocessing -- see per-row [P&L SANITY] warnings above for "
                    f"the specific account/order/item keys to investigate.")
    return updated


def check_sanity_invariant(account_id=None, db_path=DB_PATH):
    """
    Standalone post-hoc scan of pl_line_items enforcing the module2_pl_dashboard_bugfix
    invariant: net profit can never legitimately exceed gross sales. Returns a
    list of violating rows (empty list = all clear); each has enough keys to
    look the order up. Intended to be called after a reprocess (or on a
    schedule) as a cheap regression guard -- see run_sanity_invariant_check()
    in test_pl_tracker.py for the assertion-style use of this.
    """
    conn = get_db(db_path)
    where = "WHERE account_id=?" if account_id else ""
    params = (account_id,) if account_id else ()
    rows = conn.execute(f"""
        SELECT account_id, order_id, order_item_id, sku, asin,
               sale_price_exvat, sale_price_incvat, net_profit_exvat, net_profit_cash
        FROM pl_line_items
        {where}
    """, params).fetchall()
    conn.close()
    tol = 0.01
    violations = []
    for r in rows:
        d = dict(r)
        exvat_bad = (d["sale_price_exvat"] or 0) and d["net_profit_exvat"] is not None \
            and d["net_profit_exvat"] > (d["sale_price_exvat"] or 0) + tol
        cash_bad = (d["sale_price_incvat"] or 0) and d["net_profit_cash"] is not None \
            and d["net_profit_cash"] > (d["sale_price_incvat"] or 0) + tol
        if exvat_bad or cash_bad:
            violations.append(d)
    return violations


# ─────────────────────────────────────────────────────────────────────────────
# SYNC STATE (per-account watermarks)
# ─────────────────────────────────────────────────────────────────────────────

def get_sync_state(account_id, db_path=DB_PATH):
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM pl_sync_state WHERE account_id=?", (account_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_sync_state(account_id, earliest_synced, latest_synced, db_path=DB_PATH):
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO pl_sync_state (account_id, earliest_synced, latest_synced, last_run_at)
        VALUES (?,?,?,?)
        ON CONFLICT(account_id) DO UPDATE SET
            earliest_synced = MIN(COALESCE(earliest_synced, excluded.earliest_synced), excluded.earliest_synced),
            latest_synced = MAX(COALESCE(latest_synced, excluded.latest_synced), excluded.latest_synced),
            last_run_at = excluded.last_run_at
    """, (account_id, earliest_synced, latest_synced, _now()))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# LABEL-ADJUSTMENT MATCHING -- SUPERSEDED (module2_postage_bugfix)
#
# module2_pl_dashboard_bugfix round 6 built the functions below on the
# correct premise (real label costs post via AdjustmentEventList,
# AdjustmentType 'PostageBilling_*') but the wrong matching mechanism: a
# nearest-in-time heuristic against the windowed bulk pull, because those
# adjustment events carry no order-linking field. A real diagnostic pull
# (module2_postage_bugfix) proved this heuristic unreliable on THREE
# independent counts, not just one:
#   1. No order-linking field at all -- confirmed again, still true.
#   2. Different orders' postage-billing batches can share an IDENTICAL
#      PostedDate (Amazon settles many unrelated orders' labels in the same
#      batch) -- "nearest" is genuinely ambiguous, not just approximate.
#   3. This account's PostageBilling_* events are frequently posted BEFORE
#      the order's own ShipmentEvent PostedDate by a week or more --
#      find_nearest_unlabeled_shipment's `s.posted_date <= target_posted_date`
#      filter ("a label should never precede its own sale") therefore
#      rejected essentially every real candidate, which is the actual root
#      cause of the live dashboard showing ~0% exact / ~100% estimated.
#
# The real fix (see pl_tracker.fetch_label_events_for_order) calls SP-API's
# PER-ORDER Finances endpoint instead, which Amazon itself scopes to one
# order server-side -- no heuristic, no ambiguity, no direction assumption.
# is_adjustment_matched / find_nearest_unlabeled_shipment / pl_label_
# adjustment_matches / record_label_adjustment_match are kept only so
# nothing here breaks; the live pipeline no longer calls them.
# ─────────────────────────────────────────────────────────────────────────────

def is_adjustment_matched(account_id, adjustment_posted_date, db_path=DB_PATH):
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT 1 FROM pl_label_adjustment_matches
        WHERE account_id=? AND adjustment_posted_date=?
    """, (account_id, adjustment_posted_date)).fetchone()
    conn.close()
    return row is not None


def find_nearest_unlabeled_shipment(account_id, target_posted_date, max_lookback_days=None, db_path=DB_PATH):
    """The nearest real 'shipment' ledger row (by PostedDate, ISO8601 text --
    lexicographically comparable, same convention as pl_sync_state) that:
      (a) belongs to this account,
      (b) was posted at or before target_posted_date (a label purchase
          should never precede its own sale),
      (c) does not already have an event_type='label' row for the same
          (account_id, order_id, order_item_id) key -- i.e. hasn't already
          been given a label by an earlier match (or a real fee-only
          ShipmentItem label, for accounts where that mechanism DOES apply).

    `max_lookback_days` is None (unbounded) by default per the account owner's
    explicit instruction: Amazon can post a charge against an order arbitrarily
    late, and this must not silently give up just because it's outside some
    fixed window -- "nearest in time, whenever it lands" beats a hard cutoff
    that could miss a genuinely late but real charge. Nearest-neighbor ordering
    still means a CLOSER candidate always wins over a far one when both exist;
    an optional lookback bound remains available (e.g. for tests) but is not
    applied by default. See pl_tracker.LATE_CHARGE_ALERT_DAYS for the
    complementary "tell me when this happens" notification, which fires
    regardless of how this bound is set.
    Returns {"order_id", "order_item_id", "posted_date"} or None if nothing
    qualifies."""
    conn = get_db(db_path)
    where_lower = ""
    params = [account_id, target_posted_date]
    if max_lookback_days is not None:
        target_dt = datetime.fromisoformat(target_posted_date.replace("Z", "+00:00"))
        # Same "%Y-%m-%dT%H:%M:%SZ" format Amazon itself uses (and that
        # pl_tracker.py's own window-boundary formatting already uses) -- keeping
        # both bounds in this exact format (rather than e.g. isoformat()'s
        # "+00:00" suffix) avoids a lexicographic-comparison mismatch at an
        # exact-second boundary.
        lower_bound = (target_dt - timedelta(days=max_lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        where_lower = "AND s.posted_date >= ?"
        params.append(lower_bound)
    params.append(target_posted_date)
    row = conn.execute(f"""
        SELECT s.order_id, s.order_item_id, s.posted_date
        FROM pl_raw_events s
        WHERE s.account_id = ?
          AND s.event_type = 'shipment'
          AND s.posted_date <= ?
          {where_lower}
          AND NOT EXISTS (
              SELECT 1 FROM pl_raw_events lbl
              WHERE lbl.account_id = s.account_id
                AND lbl.order_id = s.order_id
                AND lbl.order_item_id = s.order_item_id
                AND lbl.event_type = 'label'
          )
        ORDER BY ABS(julianday(?) - julianday(s.posted_date)) ASC
        LIMIT 1
    """, params).fetchone()
    conn.close()
    return dict(row) if row else None


def get_earliest_posted_date(account_id, order_id, order_item_id, db_path=DB_PATH):
    """The earliest posted_date currently in the ledger for this key, BEFORE
    any new event about to be inserted -- used to detect "this new charge
    landed N days after we first saw this order" (module2_pl_dashboard_bugfix
    round 6: notify on any charge arriving >LATE_CHARGE_ALERT_DAYS after an
    order's first event, whatever kind of event it is). Returns None if this
    is the first event ever seen for this key."""
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT MIN(posted_date) AS earliest FROM pl_raw_events
        WHERE account_id=? AND order_id=? AND order_item_id=?
    """, (account_id, order_id, order_item_id)).fetchone()
    conn.close()
    return row["earliest"] if row and row["earliest"] else None


def record_label_adjustment_match(account_id, adjustment_posted_date, order_id, order_item_id,
                                   gap_days, db_path=DB_PATH):
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO pl_label_adjustment_matches
            (account_id, adjustment_posted_date, matched_order_id, matched_order_item_id,
             match_gap_days, matched_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT DO NOTHING
    """, (account_id, adjustment_posted_date, order_id, order_item_id, gap_days, _now()))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# PER-ORDER LABEL LOOKUP (module2_postage_bugfix) -- the real fix. Every join
# here is on Amazon's own order_id, guaranteed correct because the per-order
# Finances endpoint itself scopes the response -- no heuristic required.
# ─────────────────────────────────────────────────────────────────────────────

def get_order_ids_without_label(account_id, order_ids, db_path=DB_PATH):
    """Filters order_ids down to those with NO 'label' raw event yet for this
    account -- these are the ones that still need a per-order Finances
    lookup. Cheap set-membership filter, not a per-order query, so it's safe
    to call with hundreds of order_ids at once."""
    if not order_ids:
        return []
    conn = get_db(db_path)
    placeholders = ",".join("?" * len(order_ids))
    labeled = {r["order_id"] for r in conn.execute(f"""
        SELECT DISTINCT order_id FROM pl_raw_events
        WHERE account_id=? AND event_type='label' AND order_id IN ({placeholders})
    """, (account_id, *order_ids)).fetchall()}
    conn.close()
    return [oid for oid in order_ids if oid not in labeled]


def get_order_item_ids(account_id, order_id, db_path=DB_PATH):
    """Every order_item_id already known for this order (from its 'shipment'/
    'refund' raw events) -- used to attach a per-order label cost to the
    right line item(s). A single-item order returns one id; a multi-item
    order returns all of them (see pl_tracker's even-split policy for that
    case -- there's no per-item allocation signal in the postage-billing
    event itself)."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT DISTINCT order_item_id FROM pl_raw_events
        WHERE account_id=? AND order_id=? AND event_type IN ('shipment','refund')
    """, (account_id, order_id)).fetchall()
    conn.close()
    return [r["order_item_id"] for r in rows]


def get_orders_missing_label(account_id=None, limit=None, since_date=None, db_path=DB_PATH):
    """Every (account_id, order_id) currently postage_source='estimated' in
    pl_line_items -- the backfill worklist (module2_postage_bugfix). Ordered
    newest-first so the most recently affected/most-relevant orders (the
    ones the seller is actually looking at on the dashboard right now) get
    fixed first during a long backfill run.

    since_date (optional, inclusive ISO date/datetime string) caps the
    worklist to orders posted on or after this date. Per the account owner's
    explicit instruction: the backfill must NOT default to walking the full
    ~2-year history -- backfill_label_costs.py's --months flag (default 3)
    computes this cutoff and passes it through here, so a plain re-run never
    silently balloons back out to the full backlog."""
    conn = get_db(db_path)
    # module2_postage_badge_split: also pick up 'provisional' orders (retired
    # timestamp-heuristic guesses, amount blanked) so a backfill run fetches
    # their REAL per-order label and promotes them to 'exact'.
    where = "WHERE postage_source IN ('estimated', 'provisional')"
    params = []
    if account_id and account_id != "all":
        where += " AND account_id=?"
        params.append(account_id)
    if since_date:
        where += " AND posted_date>=?"
        params.append(since_date)
    sql = f"""
        SELECT account_id, order_id, MAX(posted_date) AS posted_date
        FROM pl_line_items
        {where}
        GROUP BY account_id, order_id
        ORDER BY posted_date DESC
    """
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# ROLLUPS — for the dashboard
# ─────────────────────────────────────────────────────────────────────────────

def get_pl_accounts(db_path=DB_PATH):
    conn = get_db(db_path)
    rows = conn.execute("SELECT DISTINCT account_id FROM pl_line_items ORDER BY account_id").fetchall()
    conn.close()
    return [r["account_id"] for r in rows]


def get_pending_count(account_id=None, db_path=DB_PATH):
    conn = get_db(db_path)
    if account_id and account_id != "all":
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM pl_line_items WHERE account_id=? AND settlement_status='pending'",
            (account_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM pl_line_items WHERE settlement_status='pending'").fetchone()
    conn.close()
    return row["n"]


_ROLLUP_SELECT = """
    SELECT {group_cols}
           COUNT(DISTINCT order_id) AS orders,
           SUM(quantity) AS units,
           SUM(sale_price_exvat) AS gross_sales_exvat,
           SUM(sale_price_incvat) AS gross_sales_incvat,
           SUM(referral_fee) AS referral_fees,
           SUM(other_amazon_fees) AS other_fees,
           SUM(promotion_total) AS promotions,
           SUM(cogs) AS cogs,
           SUM(postage) AS postage,
           SUM(net_profit_cash) AS net_profit_cash,
           SUM(net_profit_exvat) AS net_profit_exvat,
           SUM(ad_spend) AS ad_spend,
           SUM(CASE WHEN settlement_status='pending' THEN 1 ELSE 0 END) AS pending_count,
           SUM(CASE WHEN postage_source='exact' THEN 1 ELSE 0 END) AS postage_exact_count,
           SUM(CASE WHEN postage_source='manual' THEN 1 ELSE 0 END) AS postage_manual_count,
           SUM(CASE WHEN postage_source='missing' THEN 1 ELSE 0 END) AS postage_missing_count,
           SUM(CASE WHEN postage_source='estimated' THEN 1 ELSE 0 END) AS postage_estimated_count,
           SUM(CASE WHEN postage_source='provisional' THEN 1 ELSE 0 END) AS postage_provisional_count,
           SUM(CASE WHEN cogs_priced=0 THEN 1 ELSE 0 END) AS unpriced_count
    FROM pl_line_items
    {where}
    GROUP BY {group_cols_bare}
    {order_by}
"""


def resolve_pl_date_range(account_id=None, start_date=None, end_date=None, db_path=DB_PATH):
    """module2_debug_fix_pass FIX 1: the ACTUAL earliest/latest posted_date
    among orders covered by the given filter, for /pl's 'Showing orders
    from X to Y' text -- mirrors pl_cogs.resolve_worklist_date_range's
    reasoning (show the real span, not the requested filter bounds, since a
    'Last 90 days' filter on an account with only 40 days of history should
    say so). Returns (min_date, max_date) as ISO date strings, or
    (None, None) if nothing falls in range."""
    conn = get_db(db_path)
    where, params = _build_rollup_where(account_id, start_date, end_date)
    row = conn.execute(
        f"SELECT MIN(posted_date) AS mn, MAX(posted_date) AS mx FROM pl_line_items {where}",
        params
    ).fetchone()
    conn.close()
    mn = (row["mn"] or "")[:10] or None
    mx = (row["mx"] or "")[:10] or None
    return mn, mx


def _finish_rollup_row(d, vat_treatment):
    headline = d["net_profit_exvat"] if vat_treatment != "cash" else d["net_profit_cash"]
    d["net_profit"] = headline
    d["margin_pct"] = (headline / d["gross_sales_exvat"]) if d["gross_sales_exvat"] else None
    d["true_profit"] = headline - (d["ad_spend"] or 0) if headline is not None else None

    # module2_true_profit Phase 4: a row is PROVISIONAL if any order behind
    # it has missing COGS (cogs_priced=0) or missing postage
    # (postage_source='missing'). The net_profit above is already "before
    # the missing cost" for free -- a missing COGS/postage line item already
    # contributes £0 for that cost (never a guess, see pl_postage.py), so no
    # separate calculation is needed here; this just makes the gap visible
    # instead of letting an incomplete row present a clean-looking margin
    # (the exact "78% margin on an unpriced product" failure mode this phase
    # exists to fix). Legacy 'estimated' rows (pre-Phase-3 data that hasn't
    # been reprocessed yet) are treated the same as 'missing' here, since
    # they're also not a real, seller-confirmed cost.
    unpriced_n = d.get("unpriced_count") or 0
    # module2_postage_badge_split: 'provisional' (a retired timestamp-heuristic
    # guess, amount blanked) counts as a postage gap too -- same "not a real,
    # seller-confirmed cost" reasoning as 'missing'/'estimated'.
    missing_postage_n = ((d.get("postage_missing_count") or 0)
                         + (d.get("postage_estimated_count") or 0)
                         + (d.get("postage_provisional_count") or 0))
    d["provisional"] = bool(unpriced_n or missing_postage_n)
    reasons = []
    if unpriced_n:
        reasons.append(f"{unpriced_n} order(s) missing COGS")
    if missing_postage_n:
        reasons.append(f"{missing_postage_n} order(s) missing postage")
    d["provisional_reasons"] = reasons
    return d


def _build_rollup_where(account_id=None, start_date=None, end_date=None, canonical_sku=None):
    """Shared WHERE-clause builder for every _ROLLUP_SELECT-based query.

    module2_debug_fix_pass FIX 1: /pl's Daily/Weekly/Monthly control only
    ever changed how the chart BUCKETS dates (strftime format), it never
    filtered which orders were included at all -- there was no date-range
    filter on the page, so the per-product rollup always showed all-time
    totals regardless of that dropdown. This helper adds a real
    [start_date, end_date] filter (both inclusive ISO date strings compared
    against posted_date) that every rollup function below now accepts, so
    the SAME filter can drive both the chart data and the per-product
    table, with day/week/month staying a separate, chart-only bucketing
    choice within whatever window is selected.

    canonical_sku (module2_sku_detail): optional extra filter, scoping the
    same account+date-range logic down to ONE product -- used by the
    per-SKU detail page's trend chart and order list so they stay
    byte-for-byte consistent with the same numbers /pl shows."""
    clauses = []
    params = []
    if account_id and account_id != "all":
        clauses.append("account_id=?")
        params.append(account_id)
    if start_date:
        clauses.append("posted_date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("posted_date <= ?")
        params.append(end_date)
    if canonical_sku:
        clauses.append("canonical_sku=?")
        params.append(canonical_sku)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, tuple(params)


def get_asin_rollup(account_id=None, vat_treatment="ex_vat", db_path=DB_PATH,
                     start_date=None, end_date=None):
    conn = get_db(db_path)
    where, params = _build_rollup_where(account_id, start_date, end_date)
    sql = _ROLLUP_SELECT.format(group_cols="account_id, asin,", where=where,
                                 group_cols_bare="account_id, asin",
                                 order_by="ORDER BY net_profit_exvat DESC")
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_finish_rollup_row(dict(r), vat_treatment) for r in rows]


def get_canonical_rollup(account_id=None, vat_treatment="ex_vat", db_path=DB_PATH,
                          start_date=None, end_date=None, canonical_sku=None):
    """module2_cogs_integration: the P&L's real per-product view, replacing
    the old ASIN-based "(unmapped SKU)" bucket (ASIN is populated on well
    under 1% of real orders here -- see the SKU/ASIN coverage diagnostic).
    Grouped by canonical_sku (the alias-resolved-or-already-clean SKU COGS
    was actually derived against). Each row is additionally annotated with
    its pricing family/product_type, whether that family currently has a
    price (so the dashboard can flag which canonical products still show
    COGS=0 because no price has been entered yet, NOT because they're free),
    and every ASIN observed for it (module2_ux_and_merge_tool).

    start_date/end_date (module2_debug_fix_pass FIX 1): optional inclusive
    ISO date bounds on posted_date -- this is what makes /pl's date-range
    filter actually change the rollup, not just the chart.

    canonical_sku (module2_sku_detail): optional -- scope down to exactly
    ONE product (0 or 1 rows back) so the per-SKU detail page's headline
    numbers are computed by the exact same code path as the /pl rollup,
    never a separately-maintained calculation that could drift.

    Perf note: this used to open a NEW connection and query cogs_canonical
    ONCE PER ROW inside the loop -- the same N+1 shape that made /pl/cogs
    slow at real scale (see get_missing_prices_worklist's perf note). Fixed
    the same way: one query loads every cogs_canonical row up front into a
    dict, then the per-row loop is pure Python, no DB calls."""
    conn = get_db(db_path)
    where, params = _build_rollup_where(account_id, start_date, end_date, canonical_sku=canonical_sku)
    sql = _ROLLUP_SELECT.format(group_cols="account_id, canonical_sku,", where=where,
                                 group_cols_bare="account_id, canonical_sku",
                                 order_by="ORDER BY net_profit_exvat DESC")
    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    canonical_rows = {r["canonical_sku"]: dict(r) for r in
                       conn.execute("SELECT * FROM cogs_canonical").fetchall()}
    conn.close()

    families = {f["family"]: f for f in pl_cogs.get_all_families(db_path=db_path)}
    asin_map = pl_cogs.get_asin_map_for_canonicals(db_path=db_path)
    out = []
    for r in rows:
        canonical = r["canonical_sku"]
        cls = canonical_rows.get(canonical) if canonical else None
        if canonical and cls is None:
            cls = pl_cogs.classify_sku(canonical)   # not yet materialised, classify on the fly
        fam = families.get((cls or {}).get("family")) if cls else None
        r["product_type"] = (cls or {}).get("product_type")
        r["family"] = (cls or {}).get("family")
        r["priced"] = bool(fam and fam.get("unit_price_exvat") is not None)
        # module2_dashboard_fixes A3: VAT rate is family-level metadata,
        # same source as "priced" above -- display/cross-check only, never
        # read by _finish_rollup_row or any profit calculation.
        r["vat_rate"] = fam.get("vat_rate") if fam else None
        r["asins"] = asin_map.get(canonical, []) if canonical else []
        out.append(_finish_rollup_row(r, vat_treatment))
    return out


def get_period_rollup(account_id=None, period="day", vat_treatment="ex_vat", db_path=DB_PATH,
                       start_date=None, end_date=None):
    """period: 'day' | 'week' | 'month' -- chart BUCKETING only.
    start_date/end_date (module2_debug_fix_pass FIX 1): the actual
    date-range FILTER, independent of period -- e.g. period='day' with
    start_date=30-days-ago buckets only the last 30 days' orders into daily
    points, instead of all-time history bucketed daily."""
    fmt = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m"}[period]
    conn = get_db(db_path)
    where, params = _build_rollup_where(account_id, start_date, end_date)
    sql = _ROLLUP_SELECT.format(
        group_cols=f"account_id, strftime('{fmt}', posted_date) AS period,",
        where=where, group_cols_bare="account_id, period", order_by="ORDER BY period ASC")
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_finish_rollup_row(dict(r), vat_treatment) for r in rows]


def get_combined_period_rollup(period="day", vat_treatment="ex_vat", db_path=DB_PATH,
                                start_date=None, end_date=None):
    """Same as get_period_rollup but summed across all accounts per period."""
    fmt = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m"}[period]
    conn = get_db(db_path)
    where, params = _build_rollup_where(None, start_date, end_date)
    sql = _ROLLUP_SELECT.format(
        group_cols=f"strftime('{fmt}', posted_date) AS period,",
        where=where, group_cols_bare="period", order_by="ORDER BY period ASC")
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_finish_rollup_row(dict(r), vat_treatment) for r in rows]


def get_sku_period_rollup(canonical_sku, account_id=None, period="day", vat_treatment="ex_vat",
                           db_path=DB_PATH, start_date=None, end_date=None):
    """module2_sku_detail: same shape/bucketing as get_period_rollup, scoped
    to ONE canonical SKU -- feeds the detail page's profit/margin trend
    chart. Respects the same account+date-range filter as the rest of the
    page; period is chart-bucketing only, exactly like the account-wide
    version."""
    fmt = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m"}[period]
    conn = get_db(db_path)
    where, params = _build_rollup_where(account_id, start_date, end_date, canonical_sku=canonical_sku)
    sql = _ROLLUP_SELECT.format(
        group_cols=f"strftime('{fmt}', posted_date) AS period,",
        where=where, group_cols_bare="period", order_by="ORDER BY period ASC")
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_finish_rollup_row(dict(r), vat_treatment) for r in rows]


def get_sku_order_list(canonical_sku, account_id=None, db_path=DB_PATH,
                        start_date=None, end_date=None):
    """module2_sku_detail: every real order LINE (not aggregated) for ONE
    canonical SKU in range -- feeds the detail page's order table. Newest
    first by posted_date. Deliberately returns the raw per-line-item
    postage_source (exact/manual/missing/estimated) and both net_profit
    views (cash/ex-VAT) so the template can pick per the page's VAT toggle,
    same convention as everywhere else in Module 2."""
    conn = get_db(db_path)
    where, params = _build_rollup_where(account_id, start_date, end_date, canonical_sku=canonical_sku)
    rows = conn.execute(f"""
        SELECT account_id, order_id, order_item_id, posted_date, asin, sku, quantity,
               sale_price_exvat, sale_price_incvat, referral_fee, other_amazon_fees,
               promotion_total, cogs, postage, postage_source, cogs_priced, ad_spend,
               net_profit_exvat, net_profit_cash, settlement_status
        FROM pl_line_items
        {where}
        ORDER BY posted_date DESC
    """, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]
