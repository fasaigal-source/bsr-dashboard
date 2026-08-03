"""
pl_ads.py — Module 2 Phase 1 (module2_true_profit): advertising spend.

Amazon Ads spend cannot be attributed to an individual order — Sponsored
Products reports it per advertised ASIN/SKU PER DAY, never per order. It is
therefore stored in its own table (`ad_spend`) and joined into the P&L
rollup at the PRODUCT level (grouped by ASIN, the same key `pl_cogs`'s
ASIN-consolidation already uses), over whatever date range the dashboard is
showing — never written onto individual `pl_line_items` rows.

=== Import path: CSV now, Ads API later ===
The Amazon Ads API application is still pending, so `import_advertised_
product_csv()` is the only real ingest path today. It is deliberately kept
completely separate from storage/join/display: `ad_spend` rows are tagged
`source='csv'|'api'`, and everything downstream (the rollup join, the
coverage check) reads the table generically, with no knowledge of where a
row came from. When Ads API credentials arrive, a new `fetch_from_ads_api()`
function populates the exact same table via the exact same
`_upsert_ad_spend_rows()` helper this CSV path already uses — nothing about
storage, joining, or display needs to change. Note for that future work: the
Ads API scopes requests by a per-account **Profile ID** header, a
completely different scoping mechanism from SP-API's refresh-token-per-
account model already used elsewhere in this app — the account_id -> Profile
ID mapping will need its own small config/table when that lands.

=== Idempotency ===
Keyed on (account_id, asin, date) — a PRIMARY KEY, upserted via
`INSERT ... ON CONFLICT DO UPDATE` (replace, not add). Re-uploading the same
(or an overlapping) CSV date range is therefore always safe: the same input
produces the same stored row every time, never a duplicate or a double-
counted total. If a single day's export has more than one campaign row for
the same ASIN, they are summed together in Python BEFORE the upsert (so the
stored row is always the true per-ASIN/day total), never summed again on
re-upload.

=== Header matching ===
Amazon's exact column header wording drifts slightly across report vintages
and marketplaces (e.g. "Spend" vs "Spend(£)", "7 Day Total Orders (#)" vs
"7 Day Total Orders"). Rather than hardcoding one exact header string per
field (fragile — a real re-export could silently import zero rows), headers
are matched by normalised substring rules — see `_match_column()`. This is
the same "don't hardcode a name whitelist, match by structure" philosophy
`pl_db.categorize_item()` already uses for ItemChargeList/ItemFeeList.
"""

import csv
import io
import json
import logging
import os
import re
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
CREATE TABLE IF NOT EXISTS ad_spend (
    account_id   TEXT NOT NULL,
    asin         TEXT NOT NULL,
    sku          TEXT,             -- informational only (last-seen SKU for this ASIN/date) -- NOT part of the key
    date         TEXT NOT NULL,    -- ISO YYYY-MM-DD
    spend        REAL NOT NULL DEFAULT 0,
    ad_sales     REAL NOT NULL DEFAULT 0,   -- Amazon's own "N Day Total Sales" for the ad (attributed sales,
                                             -- kept for reference -- TACOS deliberately does NOT use this,
                                             -- see get_ad_spend_by_asin's docstring)
    clicks       INTEGER NOT NULL DEFAULT 0,
    orders       INTEGER NOT NULL DEFAULT 0,
    -- module2_ads_halo: Amazon's Advertised Product Report splits attributed
    -- sales/units into 'promoted' (the advertised ASIN's OWN sales) and 'halo'
    -- (OTHER ASINs driven by the ad). `ad_sales` above is the sum of both
    -- (the report's plain 'Sales'). Storing the split lets the P&L stop
    -- charging full spend to an ASIN while crediting it only its own sales --
    -- which made healthy variation families look catastrophic.
    sales_promoted  REAL NOT NULL DEFAULT 0,
    sales_halo      REAL NOT NULL DEFAULT 0,
    units_promoted  INTEGER NOT NULL DEFAULT 0,
    units_halo      INTEGER NOT NULL DEFAULT 0,
    -- Amazon variation parent (the report's 'Advertised product parent ID').
    -- A THIRD grouping, deliberately distinct from the COGS pricing family --
    -- never conflate the two (see pl_ads_parent_rollup).
    parent_asin     TEXT,
    source       TEXT NOT NULL DEFAULT 'csv',   -- 'csv' | 'api'
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (account_id, asin, date)
);
CREATE INDEX IF NOT EXISTS idx_ad_spend_date ON ad_spend(date);
CREATE INDEX IF NOT EXISTS idx_ad_spend_account_date ON ad_spend(account_id, date);
"""


def init_ads_schema(db_path=DB_PATH):
    conn = get_db(db_path)
    if not db.is_postgres():          # on Postgres the schema is owned by migrate.py
        conn.executescript(SCHEMA)
    # module2_ads_halo: additive migration for an ad_spend created before the
    # promoted/halo/parent split existed (CREATE TABLE IF NOT EXISTS won't add
    # columns to an existing table). ADD COLUMN is cheap and non-destructive;
    # each is guarded so re-running init is harmless. Existing rows get the
    # column default (0 / NULL) until the CSV is re-imported with the split.
    if db.table_exists(conn, "ad_spend"):
        existing = db.table_columns(conn, "ad_spend")
        for col, ddl in (
            ("sales_promoted", "REAL NOT NULL DEFAULT 0"),
            ("sales_halo",     "REAL NOT NULL DEFAULT 0"),
            ("units_promoted", "INTEGER NOT NULL DEFAULT 0"),
            ("units_halo",     "INTEGER NOT NULL DEFAULT 0"),
            ("parent_asin",    "TEXT"),
        ):
            if col not in existing:
                conn.execute(f"ALTER TABLE ad_spend ADD COLUMN {col} {ddl}")
    conn.commit()
    conn.close()
    log.info("Module 2 ad-spend schema initialised.")


# ─────────────────────────────────────────────────────────────────────────────
# CSV IMPORT — Sponsored Products "Advertised Product Report" from Seller
# Central / Amazon Ads console.
# ─────────────────────────────────────────────────────────────────────────────

def _parse_money(raw):
    """Strip £ / , / % (established convention in this codebase, see
    pl_cogs.py's CSV handling) then float(). Blank/unparseable -> 0.0."""
    if raw is None:
        return 0.0
    s = str(raw).strip()
    if not s:
        return 0.0
    s = s.replace("£", "").replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_int(raw):
    return int(round(_parse_money(raw)))


_DATE_FORMATS = ("%b %d, %Y", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d")


def _parse_ads_date(raw):
    """Amazon's Advertised Product Report date format varies by marketplace/
    export path (seen: plain ISO, DD/MM/YYYY for UK-locale exports, and
    'Jun 20, 2026' -- %b %d, %Y -- from the Ads console's current CSV
    export, per module2_dashboard_fixes_ads_import). Tries each known
    format in turn; returns an ISO 'YYYY-MM-DD' string, or None if
    genuinely unparseable (that row is skipped, counted, and reported
    rather than silently guessed at)."""
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _normalize_header(h):
    return re.sub(r"[^a-z0-9]+", " ", (h or "").lower()).strip()


def _match_column(fieldnames, *, must_contain=(), must_not_contain=()):
    """Finds the first real CSV header whose normalised form contains every
    token in must_contain and none of must_not_contain. Returns the ORIGINAL
    header string (for dict lookups against csv.DictReader rows), or None if
    nothing matches."""
    for h in fieldnames or []:
        norm = _normalize_header(h)
        if all(tok in norm for tok in must_contain) and not any(tok in norm for tok in must_not_contain):
            return h
    return None


def _match_first(fieldnames, candidates):
    """Tries each (must_contain, must_not_contain) candidate in order,
    returns the first real header matched by any of them.

    module2_dashboard_fixes_ads_import: the ORIGINAL bug here wasn't a bad
    file -- it was this resolver only knowing one header spelling per
    field. Amazon's Ads console currently exports the Advertised Product
    Report with 'Advertised product ID' (not 'ASIN'), 'Total cost' (not
    'Spend'), and 'Purchases' (not 'Orders') -- confirmed against the
    seller's real July 2026 export. Each field below now tries the
    CURRENT real header first, then falls back to older/alternate
    spellings, so the next time Amazon renames a column this importer
    degrades to 'try the next candidate' instead of hard-failing again.
    Every candidate's must_not_contain list exists to rule out genuine
    lookalike columns in the same report (e.g. 'Sales (promoted)',
    'Sales (halo)', 'Cost per purchase', 'Purchases (new to brand)') --
    picking one of those instead of the real column would silently under-
    or over-count spend/sales, which is worse than failing loudly."""
    for must_contain, must_not_contain in candidates:
        hit = _match_column(fieldnames, must_contain=must_contain, must_not_contain=must_not_contain)
        if hit:
            return hit
    return None


def _resolve_columns(fieldnames):
    """Best-effort header resolution -- see module docstring's 'Header
    matching' section, and _match_first's docstring for why several
    fields now try more than one candidate spelling. Returns a dict of
    column-name -> real header (or None if that column genuinely isn't
    present in this export)."""
    return dict(
        date=_match_column(fieldnames, must_contain=("date",)),
        asin=_match_first(fieldnames, [
            (("advertised", "product", "id"), ("parent",)),  # current: "Advertised product ID"
            (("advertised", "asin"), ()),                     # older/alternate naming
            (("asin",), ("parent",)),                         # plain "ASIN"
        ]),
        sku=_match_column(fieldnames, must_contain=("sku",), must_not_contain=("other",)),
        spend=_match_first(fieldnames, [
            (("total", "cost"), ()),          # current: "Total cost"
            (("spend",), ()),                 # older/alternate naming
            (("cost",), ("per", "detail")),   # last-resort bare "cost" column
        ]),
        ad_sales=_match_first(fieldnames, [
            # current: "Sales" -- explicitly NOT "Sales (promoted)"/"(halo)"/
            # "(new to brand)", which are separate columns in the same report.
            (("sales",), ("promoted", "halo", "brand", "other")),
        ]),
        clicks=_match_column(fieldnames, must_contain=("click",), must_not_contain=("rate", "thru", "through")),
        orders=_match_first(fieldnames, [
            (("order",), ("unit",)),          # older/alternate naming
            # current: "Purchases" -- not "Cost per purchase"/"Purchase rate"/
            # "Purchases (promoted)"/"(halo)"/"(new to brand)".
            (("purchase",), ("promoted", "halo", "brand", "per", "rate")),
        ]),
        # module2_ads_halo: the promoted/halo split + the variation parent.
        # 'Sales (new to brand)' also contains "sales" but not "promoted"/"halo",
        # so the must_contain tokens keep these unambiguous.
        sales_promoted=_match_column(fieldnames, must_contain=("sales", "promoted")),
        sales_halo=_match_column(fieldnames, must_contain=("sales", "halo")),
        units_promoted=_match_column(fieldnames, must_contain=("units", "promoted")),
        units_halo=_match_column(fieldnames, must_contain=("units", "halo")),
        parent_asin=_match_column(fieldnames, must_contain=("parent",)),
    )


def import_advertised_product_csv(account_id, file_obj_or_path, source="csv", db_path=DB_PATH):
    """Parses an Amazon Ads Sponsored Products 'Advertised Product Report'
    CSV (BOM-prefixed UTF-8, per the established convention -- read with
    encoding='utf-8-sig') and upserts into `ad_spend`, keyed on
    (account_id, asin, date).

    Multiple rows for the same ASIN/date within ONE upload (e.g. several
    campaigns advertising the same product on the same day) are summed
    together before the upsert, so the stored row is always the true daily
    total for that ASIN -- never split across several rows that would each
    under-report spend if joined naively.

    Returns a report dict for the upload-confirmation banner:
      rows_read           total CSV data rows processed
      rows_skipped         rows with no usable date/ASIN (blank required
                           fields, or a genuinely unparseable date) --
                           always reported, never silently dropped
      distinct_asins       count of distinct ASINs touched by this upload
      date_min / date_max  the date range this upload actually covers
      total_spend          sum of spend across every row in this upload
      account_id
    Raises ValueError if the file has no header row, or no recognisable
    ASIN/date/spend columns at all (a genuinely wrong file, not just a
    header-wording drift `_resolve_columns` can shrug off).
    """
    init_ads_schema(db_path=db_path)

    if hasattr(file_obj_or_path, "read"):
        raw = file_obj_or_path.read()
        text = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
        f = io.StringIO(text)
    else:
        f = open(file_obj_or_path, "r", encoding="utf-8-sig", newline="")

    try:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        cols = _resolve_columns(fieldnames)
        # module2_dashboard_fixes_ads_import: name the SPECIFIC column(s)
        # actually missing, rather than a generic "Date and/or ASIN" message
        # that used to fire (and list 'Date' right there in the headers)
        # even when only ASIN resolution had failed -- that contradiction
        # cost real debugging time and is exactly what this fix addresses.
        missing = []
        if not cols["date"]:
            missing.append("Date")
        if not cols["asin"]:
            missing.append("ASIN (e.g. Amazon's current 'Advertised product ID' column)")
        if missing:
            raise ValueError(
                f"Could not find a {' and '.join(missing)} column in this CSV "
                f"(headers seen: {fieldnames}). Export the Sponsored Products "
                f"'Advertised Product Report' and try again."
            )
        if not cols["spend"]:
            raise ValueError(
                f"Could not find a Spend/Cost column in this CSV (headers seen: {fieldnames})."
            )

        agg = {}   # (asin, date) -> {spend, ad_sales, clicks, orders, sku}
        rows_read = 0
        rows_skipped = 0
        for row in reader:
            rows_read += 1
            date = _parse_ads_date(row.get(cols["date"]))
            asin = (row.get(cols["asin"]) or "").strip()
            if not date or not asin:
                rows_skipped += 1
                continue
            sku = (row.get(cols["sku"]) or "").strip() if cols["sku"] else None
            spend = _parse_money(row.get(cols["spend"]))
            ad_sales = _parse_money(row.get(cols["ad_sales"])) if cols["ad_sales"] else 0.0
            clicks = _parse_int(row.get(cols["clicks"])) if cols["clicks"] else 0
            orders = _parse_int(row.get(cols["orders"])) if cols["orders"] else 0
            # module2_ads_halo: promoted/halo split + variation parent.
            sales_promoted = _parse_money(row.get(cols["sales_promoted"])) if cols["sales_promoted"] else 0.0
            sales_halo = _parse_money(row.get(cols["sales_halo"])) if cols["sales_halo"] else 0.0
            units_promoted = _parse_int(row.get(cols["units_promoted"])) if cols["units_promoted"] else 0
            units_halo = _parse_int(row.get(cols["units_halo"])) if cols["units_halo"] else 0
            parent_asin = (row.get(cols["parent_asin"]) or "").strip() if cols["parent_asin"] else None

            key = (asin, date)
            entry = agg.setdefault(key, dict(spend=0.0, ad_sales=0.0, clicks=0, orders=0, sku=None,
                                             sales_promoted=0.0, sales_halo=0.0,
                                             units_promoted=0, units_halo=0, parent_asin=None))
            entry["spend"] += spend
            entry["ad_sales"] += ad_sales
            entry["clicks"] += clicks
            entry["orders"] += orders
            entry["sales_promoted"] += sales_promoted
            entry["sales_halo"] += sales_halo
            entry["units_promoted"] += units_promoted
            entry["units_halo"] += units_halo
            if sku:
                entry["sku"] = sku
            if parent_asin:
                entry["parent_asin"] = parent_asin
    finally:
        f.close()

    n_upserted = _upsert_ad_spend_rows(account_id, agg, source=source, db_path=db_path)

    dates = [d for (_a, d) in agg.keys()]
    asins = {a for (a, _d) in agg.keys()}
    total_spend = sum(e["spend"] for e in agg.values())
    report = dict(
        account_id=account_id,
        rows_read=rows_read,
        rows_skipped=rows_skipped,
        rows_upserted=n_upserted,
        distinct_asins=len(asins),
        asins=sorted(asins),
        date_min=min(dates) if dates else None,
        date_max=max(dates) if dates else None,
        total_spend=total_spend,
    )
    log.info(f"Ads CSV import ({account_id}): {rows_read} row(s) read, {rows_skipped} skipped, "
             f"{len(asins)} distinct ASIN(s), {report['date_min']}..{report['date_max']}, "
             f"total spend £{total_spend:.2f}.")
    return report


def _upsert_ad_spend_rows(account_id, agg, source="csv", db_path=DB_PATH):
    """Shared by the CSV path and (later) the Ads API path -- takes an
    already-aggregated {(asin, date): {spend, ad_sales, clicks, orders, sku}}
    dict and upserts every entry. INSERT ... ON CONFLICT DO UPDATE (replace,
    not add) is what makes a re-upload of an overlapping date range safe --
    see the module docstring's Idempotency section."""
    conn = get_db(db_path)
    now = _now()
    for (asin, date), e in agg.items():
        conn.execute("""
            INSERT INTO ad_spend (account_id, asin, sku, date, spend, ad_sales, clicks, orders,
                                  sales_promoted, sales_halo, units_promoted, units_halo, parent_asin,
                                  source, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(account_id, asin, date) DO UPDATE SET
                sku=COALESCE(excluded.sku, ad_spend.sku),
                spend=excluded.spend, ad_sales=excluded.ad_sales,
                clicks=excluded.clicks, orders=excluded.orders,
                sales_promoted=excluded.sales_promoted, sales_halo=excluded.sales_halo,
                units_promoted=excluded.units_promoted, units_halo=excluded.units_halo,
                parent_asin=COALESCE(excluded.parent_asin, ad_spend.parent_asin),
                source=excluded.source, updated_at=excluded.updated_at
        """, (account_id, asin, e.get("sku"), date, e["spend"], e["ad_sales"],
              e["clicks"], e["orders"], e.get("sales_promoted", 0.0), e.get("sales_halo", 0.0),
              e.get("units_promoted", 0), e.get("units_halo", 0), e.get("parent_asin"),
              source, now))
    conn.commit()
    conn.close()
    return len(agg)


# ─────────────────────────────────────────────────────────────────────────────
# FUTURE API PATH — thin interface, not implemented yet (Ads API application
# still pending). Kept here so the eventual real implementation slots
# straight into _upsert_ad_spend_rows with no change to storage/join/display.
# ─────────────────────────────────────────────────────────────────────────────

def fetch_from_ads_api(account_id, profile_id, start_date, end_date, db_path=DB_PATH):
    """NOT YET IMPLEMENTED — placeholder for when Ads API credentials land.
    `profile_id` is the Ads API's own per-account scoping header (distinct
    from SP-API's refresh-token-per-account model used everywhere else in
    this app) -- the account_id -> profile_id mapping will need a small
    config/table addition at that point. Once implemented, this should build
    the same {(asin, date): {...}} shape `_upsert_ad_spend_rows` expects and
    call it directly, exactly like `import_advertised_product_csv` does."""
    raise NotImplementedError(
        "Ads API application is still pending -- use import_advertised_product_csv() "
        "(the /pl/ads CSV upload page) until Ads API credentials are available."
    )


# ─────────────────────────────────────────────────────────────────────────────
# COVERAGE — the date range for which ad data actually exists, so the
# dashboard can warn plainly when the viewed range extends beyond it (Phase
# 1's coverage-indicator requirement).
# ─────────────────────────────────────────────────────────────────────────────

def get_ad_data_coverage(account_id=None, db_path=DB_PATH):
    """Returns (min_date, max_date) across every ad_spend row (optionally
    filtered to one account), or (None, None) if nothing has been uploaded
    yet at all."""
    conn = get_db(db_path)
    if account_id and account_id != "all":
        row = conn.execute(
            "SELECT MIN(date) AS mn, MAX(date) AS mx FROM ad_spend WHERE account_id=?",
            (account_id,)
        ).fetchone()
    else:
        row = conn.execute("SELECT MIN(date) AS mn, MAX(date) AS mx FROM ad_spend").fetchone()
    conn.close()
    return row["mn"], row["mx"]


def get_upload_history(account_id=None, db_path=DB_PATH, limit=20):
    """Small per-account/date summary for the /pl/ads page -- shows what's
    actually loaded, grouped by source, so a seller can see at a glance
    whether last week's CSV made it in."""
    conn = get_db(db_path)
    where = "WHERE account_id=?" if (account_id and account_id != "all") else ""
    params = (account_id,) if where else ()
    rows = conn.execute(f"""
        SELECT account_id, source, MIN(date) AS date_min, MAX(date) AS date_max,
               COUNT(DISTINCT asin) AS distinct_asins, SUM(spend) AS total_spend,
               COUNT(*) AS n_rows
        FROM ad_spend {where}
        GROUP BY account_id, source
        ORDER BY account_id, source
    """, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# ROLLUP JOIN — attaches ad spend to an already-built canonical/ASIN rollup
# (from pl_db.get_canonical_rollup), at the ASIN level, over the SAME date
# range the rollup itself was filtered to. Never touches pl_line_items.
# ─────────────────────────────────────────────────────────────────────────────

def get_ad_spend_by_asin(account_id=None, start_date=None, end_date=None, db_path=DB_PATH):
    """{asin: {spend, ad_sales, clicks, orders}} summed over [start_date,
    end_date] (both optional, inclusive ISO date strings; None on either
    side = unbounded on that side, matching pl_db._build_rollup_where's own
    convention so the two filters stay in sync)."""
    conn = get_db(db_path)
    clauses = []
    params = []
    if account_id and account_id != "all":
        clauses.append("account_id=?")
        params.append(account_id)
    if start_date:
        clauses.append("date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("date <= ?")
        params.append(end_date)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(f"""
        SELECT asin, SUM(spend) AS spend, SUM(ad_sales) AS ad_sales,
               SUM(clicks) AS clicks, SUM(orders) AS orders,
               SUM(sales_promoted) AS sales_promoted, SUM(sales_halo) AS sales_halo
        FROM ad_spend {where}
        GROUP BY asin
    """, params).fetchall()
    conn.close()
    return {r["asin"]: dict(spend=r["spend"] or 0.0, ad_sales=r["ad_sales"] or 0.0,
                             clicks=r["clicks"] or 0, orders=r["orders"] or 0,
                             sales_promoted=r["sales_promoted"] or 0.0, sales_halo=r["sales_halo"] or 0.0)
            for r in rows}


def get_parent_asin_rollup(account_id=None, start_date=None, end_date=None, db_path=DB_PATH):
    """Ad economics grouped by Amazon's variation PARENT ASIN (the report's
    'Advertised product parent ID').

    *** THIS IS A THIRD, SEPARATE GROUPING -- NOT the COGS pricing family. ***
    An Amazon parent (e.g. B0H2NP5547) links every size AND colour Amazon sells
    as one variation family (80140 / 5080 / 100200 ...), typically dozens of
    SKUs; a COGS family (e.g. TOWEL-80140) is size-specific and much narrower.
    Never conflate them -- doing so corrupts the COGS path. This view exists
    because HALO sales land on siblings within the Amazon parent, so a family
    can be healthy (good ROAS) even when half its individual ASINs read as
    ad-losses on their own sales -- exactly the "is this family's ad spend
    working?" question per-ASIN numbers can't answer.

    Returns rows sorted by spend desc. Per parent: spend, ad_sales (own+halo),
    promoted, halo, clicks, orders, n_asins (distinct variations advertised),
    own_loss_asins (of those, how many have own promoted sales < their spend --
    i.e. look like a loss individually), roas (ad_sales/spend), acos.
    """
    conn = get_db(db_path)
    clauses = ["parent_asin IS NOT NULL", "parent_asin != ''"]
    params = []
    if account_id and account_id != "all":
        clauses.append("account_id = ?"); params.append(account_id)
    if start_date:
        clauses.append("date >= ?"); params.append(start_date)
    if end_date:
        clauses.append("date <= ?"); params.append(end_date)
    where = "WHERE " + " AND ".join(clauses)

    totals = conn.execute(f"""
        SELECT parent_asin, SUM(spend) AS spend, SUM(ad_sales) AS ad_sales,
               SUM(sales_promoted) AS promoted, SUM(sales_halo) AS halo,
               SUM(clicks) AS clicks, SUM(orders) AS orders,
               COUNT(DISTINCT asin) AS n_asins
        FROM ad_spend {where}
        GROUP BY parent_asin ORDER BY spend DESC
    """, params).fetchall()

    # per-(parent, asin) to count members that look like an individual ad-loss
    per = conn.execute(f"""
        SELECT parent_asin, asin, SUM(spend) AS sp, SUM(sales_promoted) AS own
        FROM ad_spend {where} GROUP BY parent_asin, asin
    """, params).fetchall()
    conn.close()

    adv, loss = {}, {}
    for p in per:
        sp = p["sp"] or 0.0
        if sp > 0:                              # was actually advertised
            adv[p["parent_asin"]] = adv.get(p["parent_asin"], 0) + 1
            if (p["own"] or 0.0) < sp:          # own sales don't cover own spend
                loss[p["parent_asin"]] = loss.get(p["parent_asin"], 0) + 1

    out = []
    for r in totals:
        spend = r["spend"] or 0.0
        sales = r["ad_sales"] or 0.0
        pa = r["parent_asin"]
        out.append(dict(
            parent_asin=pa, spend=spend, ad_sales=sales,
            promoted=r["promoted"] or 0.0, halo=r["halo"] or 0.0,
            clicks=r["clicks"] or 0, orders=r["orders"] or 0,
            n_asins=r["n_asins"] or 0,
            advertised_asins=adv.get(pa, 0), own_loss_asins=loss.get(pa, 0),
            roas=(sales / spend) if spend else None,
            acos=(spend / sales) if sales else None))
    return out


def get_ad_spend_period_series(account_id=None, period="day", start_date=None, end_date=None,
                                asins=None, db_path=DB_PATH):
    """module2_dashboard_fixes D1: {period_key: spend}, bucketed the SAME
    way (day/week/month strftime format) as pl_db.get_period_rollup /
    get_combined_period_rollup / get_sku_period_rollup -- lets the "over
    time" chart's Ad spend / TACOS options plot a real time series instead
    of the single all-range total get_ad_spend_by_asin/attach_ad_spend_to_
    rollup give the per-product table.

    Needed because pl_line_items.ad_spend is always NULL (reserved for
    Module 3 -- see pl_db.py's schema comment); real ad spend only ever
    lives in THIS module's own ad_spend table, joined by ASIN, never by
    period -- so a period-bucketed series has to be built here, then merged
    onto period-rollup rows by matching period key (same strftime format =>
    identical keys) rather than being present on those rows already.

    asins: optional list -- scopes to one canonical product's ASIN(s), for
    the SKU detail page's trend chart. None = every ASIN for the account,
    matching /pl chart1's account-wide scope."""
    fmt = {"day": "%Y-%m-%d", "week": "%Y-W%W", "month": "%Y-%m"}[period]
    conn = get_db(db_path)
    clauses = []
    params = []
    if account_id and account_id != "all":
        clauses.append("account_id=?")
        params.append(account_id)
    if start_date:
        clauses.append("date >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("date <= ?")
        params.append(end_date)
    if asins:
        placeholders = ",".join("?" for _ in asins)
        clauses.append(f"asin IN ({placeholders})")
        params.extend(asins)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(f"""
        SELECT strftime('{fmt}', date) AS period, SUM(spend) AS spend
        FROM ad_spend {where}
        GROUP BY period
        ORDER BY period ASC
    """, params).fetchall()
    conn.close()
    return {r["period"]: (r["spend"] or 0.0) for r in rows}


def attach_ad_spend_to_rollup(rows, account_id=None, start_date=None, end_date=None, db_path=DB_PATH):
    """Enriches an already-built canonical/ASIN rollup (each row must
    already carry an `asins` list -- see pl_db.get_canonical_rollup) with:
      ad_spend               total ad spend for this row's ASIN(s) in range
      ad_sales                total Amazon-attributed ad sales (reference only)
      net_profit_after_ads   row's existing net_profit minus ad_spend
      tacos                  ad_spend / gross_sales_exvat (TOTAL sales, not just
                              ad-attributed sales -- the seller explicitly wants
                              TACOS, not blended ACOS, since ACOS against only
                              ad-attributed sales structurally understates true
                              ad cost as a share of the business)

    Also returns `orphans`: ad_spend ASINs with real spend in range that
    don't appear on ANY row here (i.e. no matching orders in range at all,
    per this rollup's own date filter) -- spend with no matching sales is
    exactly the case the spec says must be surfaced, not hidden.

    Where a rollup row's canonical product covers more than one ASIN (an
    ASIN-consolidated product), ad spend for ALL of those ASINs is summed
    onto that one row -- ASIN is already the join key `pl_cogs`'s own
    consolidation uses.

    Rows are mutated in place AND returned, for caller convenience."""
    ad_map = get_ad_spend_by_asin(account_id=account_id, start_date=start_date,
                                    end_date=end_date, db_path=db_path)
    covered_asins = set()
    for r in rows:
        asins = r.get("asins") or []
        spend = ad_sales = 0.0
        clicks = orders = 0
        ad_sales_promoted = ad_sales_halo = 0.0
        for a in asins:
            e = ad_map.get(a)
            if not e:
                continue
            covered_asins.add(a)
            spend += e["spend"]
            ad_sales += e["ad_sales"]
            clicks += e["clicks"]
            orders += e["orders"]
            # module2_ads_halo: own (promoted) vs sibling (halo) attributed sales
            ad_sales_promoted += e.get("sales_promoted", 0.0)
            ad_sales_halo += e.get("sales_halo", 0.0)
        r["ad_spend"] = spend
        r["ad_sales"] = ad_sales
        r["ad_sales_promoted"] = ad_sales_promoted
        r["ad_sales_halo"] = ad_sales_halo
        r["ad_clicks"] = clicks
        r["ad_orders"] = orders
        base_profit = r.get("net_profit") or 0
        r["net_profit_after_ads"] = base_profit - spend
        gross = r.get("gross_sales_exvat") or 0
        # None (not float('inf')) when gross sales are zero -- Jinja templates
        # don't have Python's float() builtin in scope, so "no sales to divide
        # by" is represented as None and the template shows "∞" only when
        # ad_spend is also non-zero (spend with literally no sales at all),
        # else "—" (no spend, no sales -- nothing to report).
        #
        # module2_tacos_two_clocks (D3): TACOS deliberately divides a CLICK-DATED
        # numerator (ad spend) by a SETTLEMENT-DATED denominator (gross_sales_
        # exvat). These are two different measurement clocks and CANNOT be made
        # to match: ad-report figures credit the click date and keep rising as
        # late conversions land (re-pulling the same window later shows higher ad
        # sales -- proven Jun15-Jul14: £6,589.96 -> £6,649.90), while gross counts
        # at Amazon settlement. This is NOT a bug. Do NOT "reconcile" it, convert
        # bases, or add a "corrected TACOS" -- doing so breaks one basis to flatter
        # the other and reintroduces a multi-day investigation. The drift is
        # expected; the UI labels it as such (search "two clocks").
        r["tacos"] = (spend / gross) if gross else None

    orphans = []
    for asin, e in ad_map.items():
        if asin in covered_asins:
            continue
        orphans.append(dict(asin=asin, **e))
    orphans.sort(key=lambda o: -o["spend"])
    return rows, orphans


def get_coverage_warning(account_id=None, viewed_start=None, viewed_end=None, db_path=DB_PATH):
    """Plain-language coverage check for the dashboard banner. `viewed_start`/
    `viewed_end` are the currently-selected filter's own bounds (ISO date
    strings; viewed_end=None means 'up to today'). Returns:
      None                     — no gap: either no filter is narrower than
                                  the uploaded data, or nothing to warn about
      dict(has_data=False)     — no ad data uploaded for this account at all
      dict(has_data=True, ...) — a real gap: uploaded coverage doesn't reach
                                  one or both ends of the viewed range
    """
    cov_min, cov_max = get_ad_data_coverage(account_id=account_id, db_path=db_path)
    if not cov_min:
        return dict(has_data=False)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    v_end = viewed_end or today

    # Amazon's ad reports lag ~1-2 days (today's and often yesterday's spend
    # isn't final yet), so ad coverage will almost always end a day or two before
    # a range that runs "up to today". Don't cry "incomplete" over that normal
    # trailing lag — only warn when ad data ends MORE than LAG_DAYS before the
    # range end (a genuine gap worth uploading for).
    LAG_DAYS = 3
    try:
        end_cutoff = (datetime.strptime(v_end, "%Y-%m-%d") - timedelta(days=LAG_DAYS)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        end_cutoff = v_end
    gap_start = bool(viewed_start) and viewed_start < cov_min
    gap_end = cov_max < end_cutoff
    if not gap_start and not gap_end:
        return None
    return dict(has_data=True, cov_min=cov_min, cov_max=cov_max,
                viewed_start=viewed_start, viewed_end=v_end,
                gap_start=gap_start, gap_end=gap_end)
