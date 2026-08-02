"""
pl_cogs.py — COGS-by-canonical-SKU integration for Module 2 (P&L Tracker)

module2_cogs_integration (Wave 1): attaches real cost-of-goods to every P&L
line item so net_profit reflects true per-product profit instead of the
COGS=0 placeholder. Read-only against Amazon; all data here is local.

=== Resolution rule (CRITICAL, per the account owner) ===
For each order's SellerSKU (SKU is present on ~100% of real orders; ASIN is
NOT usable as a join key here — see the SKU/ASIN coverage diagnostic, ASIN is
populated on well under 1% of pl_line_items rows because it's only ever
resolved via a manual Module 1 product-catalog entry, not returned by Amazon
at the line-item level at all):

  1. If the SKU IS in the alias table (`cogs_aliases`, seeded from
     sku_aliases.csv) -> resolve to its canonical_sku.
  2. If the SKU is NOT in the alias table -> it is ALREADY a clean/proper
     SKU -> it IS its own canonical. This is NOT "unmapped" — a SKU missing
     from the alias table means "already canonical," never "unknown."

This kills the old "(unmapped SKU)" bucket: noise SKUs get redirected to
their canonical via the alias table, clean SKUs pass through as themselves.
"Unpriced" (no COGS number yet) is a completely separate, orthogonal concept
from "unmapped" — see get_missing_prices_worklist().

=== Product classification + COGS derivation ===
Every canonical SKU is classified into one of four product types by NAME
PATTERN (confirmed by the account owner), which determines both its pricing
family (so same-family SKUs share one editable price) and how that family's
entered price turns into this SKU's actual COGS:

  cushion  — name contains <prefix>x<digit(s)> at the very end (e.g. "1220x4",
             "18X4", "D-18x1"). pack_qty = the trailing digits. price_basis =
             'single' (price is cost of ONE item in the pack).
             COGS = family_price * pack_qty.
  pillow   — name contains "-P<digit(s)>" (e.g. "6337-P2", "BD-6337-P1",
             "6023-P2-Nbl" — trailing suffix after the pack digits, like a
             colour code, is ignored for family/pack purposes).
             -P1            -> single-sold.  price_basis='single'. COGS = price * 1.
             -P2/-P4/-P6    -> pair-sold.    price_basis='pair'.   COGS = price * (pack_qty/2).
             Family = base code with "BD" prefix and the "-P#..." suffix
             stripped — so a family can hold BOTH a pair price and a single
             price as two SEPARATE family entries (e.g. PILLOW-6337-pair and
             PILLOW-6337-single).
             HF exception (names can't be parsed for qty — hardcoded):
               HF-P2P -> pack 2, HF-P2Px2 -> pack 4, HF-P2Px3 -> pack 6, all pair-sold.
  towel    — has a bare "P<digit(s)>" (digits immediately followed by P then
             digits, e.g. "PUR3030P1", "LAT80140P4") with NO "x" and NO
             hyphen before the P (that's what distinguishes it from a
             pillow). pack_qty = trailing digits. price_basis='single'.
             COGS = family_price * pack_qty.
             Family = the SIZE only (leading colour letters stripped) — e.g.
             CHB3030P2/PUR3030P1 both live under family TOWEL-3030, because
             colour never affects cost. Known sizes: 3030, 5080, 80140, 100200.
  other    — everything else (duvets, dog beds, covers, toppers, ...).
             pack_qty=1, price_basis='single'. COGS = family_price * 1.
             Family = the SKU itself (BD- stripped) — each "other" SKU is
             normally its own 1-SKU family unless the seller later merges it.

Every family price entered by the seller is EX-VAT (net) — COGS is therefore
ex-VAT throughout, consistent with the P&L's existing ex-VAT ("true profit")
headline view. VAT is never added to COGS anywhere in this module.

The classifier (`classify_sku`) was validated to reproduce ALL 42
seller-confirmed families in price_families.csv (51 canonical SKUs derived
from them) with ZERO mismatches before being wired in here — see the
classify_sku docstring. It is used for two things: (1) nothing, for the 42
seeded families (their family/pack/basis come directly, verbatim, from
price_families.csv — the seller's own confirmed values, not re-derived); and
(2) auto-suggesting family/pack/basis for any canonical SKU NOT in that seed
list, both when it's first encountered during a P&L recompute (so it gets a
sensible family grouping immediately, price NULL until the seller fills it
in) and on the missing-prices worklist.

=== Design note: why a separate module from pl_db.py ===
pl_db.py (the working, already-verified P&L ledger/aggregate engine) is
deliberately NOT touched beyond the minimum: a couple of new columns on
pl_line_items (canonical_sku, cogs_priced) and one new call inside
recompute_line_item() to get_cogs_for_sku() below, replacing the old
`managed_asins.cogs` lookup (which was always 0 for this account — no
product had COGS entered against it). Keeping all the alias/family/
classification/worklist logic in its own module keeps this new, more complex
surface area (and its own small schema) isolated and independently testable,
without risking the already-verified pl_db.py internals.
"""

import csv
import html
import io
import os
import re
import sqlite3
import db
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)
DB_PATH = "bsr_history.db"   # same DB file as Module 1 + the rest of Module 2

# module2_debug_fix_pass FIX 2: seed_from_csvs() used to default to the bare
# relative paths "sku_aliases.csv" / "price_families.csv", which only
# resolve correctly if the process's CURRENT WORKING DIRECTORY happens to be
# this file's directory. Any launcher, shortcut, or scheduler that starts
# python from somewhere else silently hits FileNotFoundError -- caught and
# logged as a warning, easy to miss in a scrolling console -- and the seed
# quietly does nothing (0 aliases loaded, no crash, no obvious symptom until
# someone notices unmerged SKU variants much later). Anchoring to this
# module's own directory makes the seed cwd-independent.
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ALIASES_CSV = os.path.join(_MODULE_DIR, "sku_aliases.csv")
DEFAULT_FAMILIES_CSV = os.path.join(_MODULE_DIR, "price_families.csv")


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
CREATE TABLE IF NOT EXISTS cogs_aliases (
    variant_sku    TEXT PRIMARY KEY,
    canonical_sku  TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'seed_csv',   -- 'seed_csv' | 'manual' | 'csv_upload'
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cogs_families (
    family             TEXT PRIMARY KEY,
    product_type       TEXT NOT NULL,    -- cushion | pillow | towel | other
    price_basis        TEXT NOT NULL,    -- single | pair
    unit_price_exvat   REAL,             -- NULL = not priced yet (Wave-2 worklist item)
    source             TEXT NOT NULL DEFAULT 'seed_csv',  -- 'seed_csv' | 'manual' | 'csv_upload' | 'auto_classified'
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cogs_canonical (
    canonical_sku   TEXT PRIMARY KEY,
    family          TEXT NOT NULL,
    product_type    TEXT NOT NULL,
    pack_qty        INTEGER NOT NULL DEFAULT 1,
    price_basis     TEXT NOT NULL,
    source          TEXT NOT NULL DEFAULT 'seed_csv',   -- 'seed_csv' | 'auto_classified' | 'manual'
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cogs_overheads (
    account_id            TEXT PRIMARY KEY,
    monthly_amount_exvat  REAL NOT NULL DEFAULT 0,
    updated_at            TEXT NOT NULL
);

-- module2_sku_map: per-CANONICAL COGS override, populated from Faraz's
-- sku_map_MASTER.xlsx workbook (the @ x multiplier model). This is the ex-VAT
-- cost of ONE order line's worth of packs BEFORE multiplying by the order's
-- quantity -- i.e. exactly the sheet's 'COGS = @ x multiplier' cell.
-- get_cogs_for_sku() checks this FIRST and, when present, uses it instead of
-- family_price x cogs_multiplier. It exists because the workbook groups by
-- purchasing base unit, which does not map 1:1 onto the old pricing families;
-- rather than reshape every family, a canonical simply carries its own cost.
-- Additive: absent rows fall straight through to the family-price path, so
-- nothing already priced changes unless the sheet gives that SKU a cost.
CREATE TABLE IF NOT EXISTS cogs_canonical_cost (
    canonical_sku   TEXT PRIMARY KEY,
    cogs_per_line   REAL NOT NULL,      -- ex-VAT cost for one line's packs (pre x quantity)
    source          TEXT NOT NULL DEFAULT 'sku_map',
    updated_at      TEXT NOT NULL
);

-- module2_ux_and_merge_tool: SKU -> ASIN crosswalk. This is the raw
-- observation table ("this SKU string was seen on this ASIN"), populated
-- from (a) a small hardcoded seed of seller-confirmed cases and (b) the
-- live Orders-flat-file backfill (see asin_sync.py). It is NOT itself the
-- resolution path -- run_asin_consolidation() reads this table and writes
-- its conclusions into cogs_aliases (source='asin_auto'), so the actual
-- per-order SKU->canonical lookup in resolve_to_canonical() stays exactly
-- the one fast, already-indexed cogs_aliases read it always was.
CREATE TABLE IF NOT EXISTS cogs_sku_asin (
    variant_sku    TEXT PRIMARY KEY,
    asin           TEXT NOT NULL,
    source         TEXT NOT NULL DEFAULT 'orders_report',  -- 'orders_report' | 'confirmed_seed' | 'manual'
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cogs_sku_asin_asin ON cogs_sku_asin(asin);

-- Tracks which 30-day windows of the Orders flat-file report have already
-- been fetched for a given account, so the multi-hour backfill (Amazon
-- caps this report type at 30 days/request) is safely resumable/rerunnable
-- without re-fetching windows that already succeeded.
CREATE TABLE IF NOT EXISTS cogs_asin_sync_state (
    account_id     TEXT NOT NULL,
    window_start   TEXT NOT NULL,   -- ISO date, inclusive
    window_end     TEXT NOT NULL,   -- ISO date, exclusive
    status         TEXT NOT NULL,   -- 'done' | 'failed'
    rows_parsed    INTEGER NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (account_id, window_start)
);
"""


# module2_dashboard_fixes A1: VAT rate is recorded METADATA + a display/
# cross-check field ONLY -- it is never read by pl_db.recompute_line_item or
# any part of the profit formula. The validated formula anchors on Amazon's
# balance_change, which is already ex-VAT; nothing here divides it by a VAT
# factor (that was the exact module2_debug_fix_pass bug this must not
# reintroduce). VAT rate lives on cogs_families (belongs with the family,
# same as price) -- default 0.20 (standard) since most of the catalogue is
# standard-rated; 0.0 (zero-rated) and 0.05 (reduced) are the other two
# valid settings.
_FAMILY_MIGRATIONS = {
    "vat_rate": "REAL NOT NULL DEFAULT 0.20",
}


def init_cogs_schema(db_path=DB_PATH):
    conn = get_db(db_path)
    if not db.is_postgres():          # on Postgres the schema is owned by migrate.py
        conn.executescript(SCHEMA)
    if db.table_exists(conn, "cogs_families"):
        cols = db.table_columns(conn, "cogs_families")
        for col, decl in _FAMILY_MIGRATIONS.items():
            if col not in cols:
                conn.execute(f"ALTER TABLE cogs_families ADD COLUMN {col} {decl}")
                log.info(f"Migrated cogs_families: added {col} column.")
    conn.commit()
    conn.close()
    log.info("Module 2 COGS schema initialised.")


# ─────────────────────────────────────────────────────────────────────────────
# CLASSIFICATION — pure function, no DB access. Validated to reproduce every
# one of the 42 seller-confirmed families in price_families.csv exactly
# (51 canonical SKUs, 0 mismatches) before being wired into the pipeline.
# ─────────────────────────────────────────────────────────────────────────────

_BD_PREFIX_RE = re.compile(r'^BD[-_=]?', re.IGNORECASE)
_CUSHION_RE   = re.compile(r'^(.+)[xX](\d+)$')
_PILLOW_RE    = re.compile(r'^(.*)-[Pp](\d+)(.*)$')
_TOWEL_RE     = re.compile(r'^[A-Za-z]*(\d+)[Pp](\d+)$')

# Names that can't be parsed for pack quantity (per the account owner) —
# hardcoded rather than guessed.
_HF_HARDCODE = {
    "HF-P2P":   dict(pack_qty=2, family="PILLOW-HF-pair"),
    "HF-P2Px2": dict(pack_qty=4, family="PILLOW-HF-pair"),
    "HF-P2Px3": dict(pack_qty=6, family="PILLOW-HF-pair"),
}


def strip_bd(sku):
    """'BD-' / 'BD_' / 'BD=' / bare 'BD' prefix is a category label only —
    stripped before family-matching, per the account owner. Never applied to
    the STORED canonical_sku value itself, only to the string used for
    classification."""
    return _BD_PREFIX_RE.sub('', sku or '')


_SEP_VARIANTS_RE = re.compile(r"[=_]+")
_DOT_SEP_RE = re.compile(r"(?<!\d)\.|\.(?!\d)")   # a '.' NOT between digits
_DASH_RUN_RE = re.compile(r"-{2,}")


def normalize_separators(sku):
    """'=' , '_' , '.' and spaces are all separator variants of '-' in this
    catalogue's naming ('BD-6372=P4', 'BD_6378_P4', 'HF.P2P' are the same
    shapes as their '-' spelling). Collapsing them makes classification
    separator-agnostic, which is what stops one product line fragmenting into
    parallel families (6372 was sitting in FOUR: PILLOW-6372-pair,
    PILLOW-6372=-pair, OTHER-6372=P1, OTHER-6372=P2).

    Runs of dashes are collapsed too, otherwise '6372=-P4' would normalise to
    '6372--P4' and produce yet another family ('PILLOW-6372--pair') instead of
    joining PILLOW-6372-pair."""
    s = _SEP_VARIANTS_RE.sub("-", (sku or "").strip())
    # '.' is only a separator when it ISN'T a decimal point: 'HF.P2P' -> 'HF-P2P',
    # but '10.5' / '4.6ft' keep their decimal (an earlier version turned those
    # into '10-5' / '4-6ft', destroying real sizes). Spaces are left alone for
    # the same reason -- they appear inside genuine names ('6026-Gy-45455 x 2').
    s = _DOT_SEP_RE.sub("-", s)
    s = _DASH_RUN_RE.sub("-", s)
    return s.strip("-")


def classify_sku(canonical_sku):
    """Returns dict(product_type, pack_qty, price_basis, family) for a
    canonical SKU, by name pattern. See module docstring for the full rule
    set and validation note.

    Separator variants are normalised first (see normalize_separators), so a
    punctuation quirk can't spawn a parallel family or drop a pillow to
    'other'/single (which silently applied the wrong COGS multiplier). NOTE:
    this only affects how a canonical is classified the FIRST time it's seen --
    ensure_canonical never reclassifies an existing row, so no stored
    canonical (least of all a source='manual' override) is touched by this."""
    s = (canonical_sku or '').strip()
    norm = normalize_separators(s)

    hard = s if s in _HF_HARDCODE else (norm if norm in _HF_HARDCODE else None)
    if hard:
        h = _HF_HARDCODE[hard]
        return dict(product_type="pillow", pack_qty=h["pack_qty"],
                    price_basis="pair", family=h["family"])

    stripped = strip_bd(norm)

    m = _CUSHION_RE.match(stripped)
    if m:
        prefix, qty = m.group(1), int(m.group(2))
        return dict(product_type="cushion", pack_qty=qty, price_basis="single",
                    family=f"CUSHION-{prefix}")

    m = _PILLOW_RE.match(stripped)
    if m:
        base, qty, _suffix = m.group(1), int(m.group(2)), m.group(3)
        pair = qty in (2, 4, 6)
        basis = "pair" if pair else "single"
        return dict(product_type="pillow", pack_qty=qty, price_basis=basis,
                    family=f"PILLOW-{base}-{basis}")

    m = _TOWEL_RE.match(stripped)
    if m:
        size, qty = m.group(1), int(m.group(2))
        return dict(product_type="towel", pack_qty=qty, price_basis="single",
                    family=f"TOWEL-{size}")

    return dict(product_type="other", pack_qty=1, price_basis="single",
                family=f"OTHER-{stripped}")


def cogs_multiplier(product_type, price_basis, pack_qty):
    """How many 'family price units' one sold pack of this canonical SKU
    costs. cushion/towel: price is per single item, multiply by pack_qty.

    PILLOWS come in two commercial shapes and the basis says which:
      * price_basis='pair'   -- the family price is the price of TWO units, so
        a pack of N costs N/2 price-units (a 4-pack = 2 pairs). Lines that only
        ever ship multipacks: 6372, HF.
      * price_basis='single' -- the family price is the price of ONE unit, so a
        pack of N costs N price-units, exactly like towels. Lines that sell P1
        alongside P2/P4 at a linear price: 6337, 6378, 6379.

    'single' previously returned a hard 1.0 ('the price IS the per-pack cost').
    That was safe only because every pillow/single canonical had pack_qty=1, so
    1.0 and pack_qty were the same number -- but it meant a per-unit line could
    not be modelled at all: a P4 would have cost the same as a P1. Changed to
    pack_qty (verified zero-impact at the time: 0 rows with pack_qty != 1).

    The two bases are mirror-image traps, so be exact about which number goes in
    the family price: enter a PER-PAIR price on a 'single' family and every pack
    doubles; enter a PER-UNIT price on a 'pair' family and every pack halves."""
    if product_type == "cushion":
        return pack_qty
    if product_type == "pillow":
        return (pack_qty / 2.0) if price_basis == "pair" else pack_qty
    if product_type == "towel":
        return pack_qty
    return 1.0  # other


# ─────────────────────────────────────────────────────────────────────────────
# RESOLUTION — SKU -> canonical -> family -> price -> COGS
# ─────────────────────────────────────────────────────────────────────────────

def resolve_to_canonical(raw_sku, db_path=DB_PATH, conn=None):
    """Resolution rule: alias table hit -> its canonical_sku. No hit -> the
    SKU is already its own canonical (NOT 'unmapped'). None/empty in -> None
    out (nothing to resolve).

    Real-data quirk (found against the live 33,720-row DB): ~8,300 order
    SKUs are stored with literal HTML entities (e.g. "18&quot;X4" instead of
    the real 'inch mark' character 18"X4) while sku_aliases.csv / the
    seller's own naming uses the real character. Un-escaping here before
    lookup lets those orders match the alias table (and each other) as
    intended -- this is purely a matching normalisation, it never changes
    what's stored in pl_line_items.sku (the raw value) or in cogs_aliases
    (the seed data).

    Pre-existing bug fix (found while verifying module2_postage_bugfix,
    unrelated to the postage fix itself): the body below already shared a
    passed-in `conn` (owns_conn = conn is None), but `conn` was never
    declared as a parameter here at all -- get_cogs_for_sku calls this with
    conn=conn on every single line item, which raised TypeError before this
    fix and was blocking test_pl_tracker.py from completing a full run."""
    if not raw_sku:
        return None
    normalized = html.unescape(raw_sku).strip()
    owns_conn = conn is None
    if owns_conn:
        conn = get_db(db_path)
    row = conn.execute(
        "SELECT canonical_sku FROM cogs_aliases WHERE variant_sku=?", (normalized,)
    ).fetchone()
    if owns_conn:
        conn.close()
    return row["canonical_sku"] if row else normalized


def ensure_canonical(canonical_sku, db_path=DB_PATH, conn=None):
    """Returns the cogs_canonical row for this canonical SKU, creating it
    (via classify_sku) — and its family, if the family itself doesn't exist
    yet, with unit_price_exvat=NULL — the first time it's ever seen. Every
    subsequent call for the same canonical_sku is a cheap read. Never
    overwrites a family/canonical row that already exists (seed data or a
    manual edit is never clobbered by auto-classification).

    module2_debug_fix_pass FIX 3 perf note: pass an already-open `conn` (as
    pl_db.recompute_line_item now does, via get_cogs_for_sku) to avoid
    opening a fresh sqlite3 connection for every single line item — with a
    default db_path connection per call, a full 33,720-row reprocess opened
    100,000+ short-lived connections (3 per row across resolve_to_canonical/
    ensure_canonical/get_cogs_for_sku), which was slow enough on a real
    machine to make Merge/Define POST requests (which trigger a synchronous
    reprocess) look like they'd silently failed. If a conn IS passed in,
    this function's internal commit() only flushes what's been written on
    it SO FAR (safe with recompute_line_item's call order: get_cogs_for_sku
    runs before that function's own INSERT/commit) — do not pass a shared
    conn from a caller with earlier uncommitted writes you don't want
    flushed yet."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db(db_path)
    row = conn.execute(
        "SELECT * FROM cogs_canonical WHERE canonical_sku=?", (canonical_sku,)
    ).fetchone()
    if row:
        if owns_conn:
            conn.close()
        return dict(row)

    cls = classify_sku(canonical_sku)
    now = _now()
    conn.execute("""
        INSERT INTO cogs_families (family, product_type, price_basis, unit_price_exvat, source, updated_at)
        VALUES (?,?,?,NULL,'auto_classified',?)
        ON CONFLICT(family) DO NOTHING
    """, (cls["family"], cls["product_type"], cls["price_basis"], now))
    conn.execute("""
        INSERT INTO cogs_canonical (canonical_sku, family, product_type, pack_qty, price_basis, source, updated_at)
        VALUES (?,?,?,?,?,'auto_classified',?)
        ON CONFLICT(canonical_sku) DO NOTHING
    """, (canonical_sku, cls["family"], cls["product_type"], cls["pack_qty"], cls["price_basis"], now))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM cogs_canonical WHERE canonical_sku=?", (canonical_sku,)
    ).fetchone()
    if owns_conn:
        conn.close()
    return dict(row)


def get_cogs_for_sku(raw_sku, quantity, db_path=DB_PATH, conn=None):
    """The one function pl_db.recompute_line_item() calls. Returns:
      canonical_sku   — resolved canonical (None if raw_sku itself was empty)
      family          — its pricing family
      product_type    — cushion|pillow|towel|other
      priced          — bool: does the family have a unit_price_exvat yet?
      cogs_total      — priced ? unit_price_exvat * multiplier * quantity : 0.0
    quantity is the number of LISTING units (packs) sold on this order line
    (Amazon's QuantityShipped) — cogs_total is the cost for the WHOLE line,
    already multiplied by quantity, ready to store directly in
    pl_line_items.cogs.

    Pass an already-open `conn` (recompute_line_item does) to share one
    connection across resolve/ensure/this function's own family-price
    lookup instead of opening three fresh ones per line item — see
    ensure_canonical's perf note."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db(db_path)

    canonical = resolve_to_canonical(raw_sku, db_path=db_path, conn=conn)
    if not canonical:
        if owns_conn:
            conn.close()
        return dict(canonical_sku=None, family=None, product_type=None,
                    priced=False, cogs_total=0.0)

    row = ensure_canonical(canonical, db_path=db_path, conn=conn)

    # PER-CANONICAL OVERRIDE (sku_map workbook) takes precedence over the
    # family-price path. cogs_per_line is already the cost of one line's packs,
    # so it's just multiplied by quantity -- NOT by cogs_multiplier, which is
    # baked into the sheet's number.
    ovr = conn.execute(
        "SELECT cogs_per_line FROM cogs_canonical_cost WHERE canonical_sku=?", (canonical,)
    ).fetchone()
    if ovr and ovr["cogs_per_line"] is not None:
        if owns_conn:
            conn.close()
        return dict(canonical_sku=canonical, family=row["family"],
                    product_type=row["product_type"], priced=True,
                    cogs_total=ovr["cogs_per_line"] * (quantity or 0))

    fam = conn.execute(
        "SELECT unit_price_exvat FROM cogs_families WHERE family=?", (row["family"],)
    ).fetchone()
    if owns_conn:
        conn.close()
    price = fam["unit_price_exvat"] if fam else None

    if price is None:
        return dict(canonical_sku=canonical, family=row["family"],
                    product_type=row["product_type"], priced=False, cogs_total=0.0)

    mult = cogs_multiplier(row["product_type"], row["price_basis"], row["pack_qty"])
    units = quantity or 0
    return dict(canonical_sku=canonical, family=row["family"],
                product_type=row["product_type"], priced=True,
                cogs_total=price * mult * units)


# ─────────────────────────────────────────────────────────────────────────────
# SEEDING — from sku_aliases.csv / price_families.csv. Idempotent: only ever
# INSERTs rows that don't already exist, so re-running this on every app
# startup never clobbers a price the seller has since edited on the
# dashboard, or a manual alias correction.
# ─────────────────────────────────────────────────────────────────────────────

_DERIVES_RE = re.compile(r'^(.+?)\s*\(pack\s*(\d+)\)$')


def _parse_derives(derives_str):
    """'1220x2 (pack 2); 1220x4 (pack 4)' -> [('1220x2', 2), ('1220x4', 4)]."""
    out = []
    for part in (derives_str or "").split(";"):
        part = part.strip()
        if not part:
            continue
        m = _DERIVES_RE.match(part)
        if m:
            out.append((m.group(1).strip(), int(m.group(2))))
        else:
            log.warning(f"cogs seed: could not parse derives_these_skus entry: {part!r}")
    return out


def seed_from_csvs(aliases_path=None, families_path=None, db_path=DB_PATH):
    """One-time (but always-safe-to-repeat) seed from the two provided CSVs.

    module2_debug_fix_pass FIX 2: paths now default to files sitting next to
    THIS module (DEFAULT_ALIASES_CSV / DEFAULT_FAMILIES_CSV), not a bare
    relative filename -- see the module-level comment for why the old
    relative default could silently seed zero rows depending on the
    process's working directory. Every outcome (found+parsed, found+empty,
    or genuinely not found) is now logged at INFO so a mismatch is visible
    in the server console immediately at startup, not discovered later via
    unmerged SKUs. Use get_alias_count()/get_family_count() to check the
    result programmatically (the /pl/cogs page surfaces these)."""
    aliases_path = aliases_path or DEFAULT_ALIASES_CSV
    families_path = families_path or DEFAULT_FAMILIES_CSV
    init_cogs_schema(db_path=db_path)
    n_aliases = n_families = n_canonical = 0
    aliases_parsed = families_parsed = 0
    now = _now()
    conn = get_db(db_path)

    log.info(f"COGS seed: reading aliases from {aliases_path!r} "
             f"(exists={os.path.isfile(aliases_path)}).")
    try:
        with open(aliases_path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                variant = (row.get("variant_sku") or "").strip()
                canon = (row.get("canonical_sku") or "").strip()
                if not variant or not canon:
                    continue
                aliases_parsed += 1
                cur = conn.execute("""
                    INSERT INTO cogs_aliases (variant_sku, canonical_sku, source, updated_at)
                    VALUES (?,?,'seed_csv',?)
                    ON CONFLICT DO NOTHING
                """, (variant, canon, now))
                if cur.rowcount:
                    n_aliases += 1
        log.info(f"COGS seed: aliases — {aliases_parsed} row(s) parsed from {aliases_path!r}, "
                 f"{n_aliases} newly inserted (rest already present — normal on a repeat run).")
    except FileNotFoundError:
        log.warning(f"COGS seed: {aliases_path!r} NOT FOUND — alias seed skipped entirely "
                    f"(0 rows). If sku_aliases.csv exists elsewhere, pass its path explicitly "
                    f"to seed_from_csvs().")

    log.info(f"COGS seed: reading families from {families_path!r} "
             f"(exists={os.path.isfile(families_path)}).")
    try:
        with open(families_path, "r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                family = (row.get("family") or "").strip()
                ptype = (row.get("type") or "").strip()
                basis = (row.get("price_basis") or "").strip()
                if not family:
                    continue
                families_parsed += 1
                try:
                    price = float(row.get("your_price")) if row.get("your_price") not in (None, "") else None
                except ValueError:
                    price = None
                cur = conn.execute("""
                    INSERT INTO cogs_families
                        (family, product_type, price_basis, unit_price_exvat, source, updated_at)
                    VALUES (?,?,?,?,'seed_csv',?)
                    ON CONFLICT DO NOTHING
                """, (family, ptype, basis, price, now))
                if cur.rowcount:
                    n_families += 1

                for sku, pack_qty in _parse_derives(row.get("derives_these_skus")):
                    cur2 = conn.execute("""
                        INSERT INTO cogs_canonical
                            (canonical_sku, family, product_type, pack_qty, price_basis, source, updated_at)
                        VALUES (?,?,?,?,?,'seed_csv',?)
                        ON CONFLICT DO NOTHING
                    """, (sku, family, ptype, pack_qty, basis, now))
                    if cur2.rowcount:
                        n_canonical += 1
        log.info(f"COGS seed: families — {families_parsed} row(s) parsed from {families_path!r}, "
                 f"{n_families} newly inserted.")
    except FileNotFoundError:
        log.warning(f"COGS seed: {families_path!r} NOT FOUND — family seed skipped entirely "
                    f"(0 rows).")

    conn.commit()
    conn.close()
    log.info(f"COGS seed: {n_aliases} new alias(es), {n_families} new family(ies), "
             f"{n_canonical} new canonical SKU mapping(s).")
    return dict(aliases=n_aliases, families=n_families, canonical=n_canonical,
                aliases_parsed=aliases_parsed, families_parsed=families_parsed)


def get_alias_count(db_path=DB_PATH):
    """Total rows currently in cogs_aliases, by source -- the /pl/cogs page
    shows this as 'Alias table: N mappings loaded' (module2_debug_fix_pass
    FIX 2) so a seeding failure (0 or far below the CSV's row count) is
    visible to the seller immediately, not just in a server log they may
    never look at."""
    conn = get_db(db_path)
    total = conn.execute("SELECT COUNT(*) AS n FROM cogs_aliases").fetchone()["n"]
    by_source = {r["source"]: r["n"] for r in conn.execute(
        "SELECT source, COUNT(*) AS n FROM cogs_aliases GROUP BY source"
    ).fetchall()}
    conn.close()
    return dict(total=total, by_source=by_source)


# ─────────────────────────────────────────────────────────────────────────────
# ASIN-BASED AUTO-CONSOLIDATION (module2_ux_and_merge_tool)
#
# Real-world problem: the seller renames SKUs on live listings over time
# (old orders carry the old SKU, new orders carry the new SKU) and some SKU
# strings don't match ANY of the cushion/pillow/towel naming patterns at
# all (e.g. "19" x 29" x,2"). The SKU string alone is unreliable; the ASIN
# is the stable join key because it's the same physical listing.
#
# Resolution order (per the account owner):
#   1. alias-table hit -> its canonical_sku                    (existing)
#   2. no alias hit, but shares an ASIN with a SKU that already
#      has an established canonical -> adopt that canonical    (NEW, here)
#   3. no alias, no ASIN match -> SKU is its own canonical      (existing)
#
# Implementation choice: rather than adding a second lookup table to the hot
# resolve_to_canonical() path (called once per order line on every
# reprocess), run_asin_consolidation() runs as a batch/maintenance pass that
# WRITES its conclusions straight into cogs_aliases (source='asin_auto').
# Rule 1 above then already covers rule 2 automatically, so
# resolve_to_canonical() itself needed zero changes. Idempotent: only ever
# INSERT OR IGNOREs, so a manual alias (source='manual'/'seed_csv') the
# seller already made is never overwritten, and reruns after new ASIN data
# arrives just fill in newly-discovered pairs.
# ─────────────────────────────────────────────────────────────────────────────

# Seller-confirmed real cases (module2_ux_and_merge_tool prompt), seeded
# immediately so the mechanism is provably correct before the live Orders
# report backfill (asin_sync.py) has been run. The live backfill will
# rediscover these same pairs (source='orders_report') and everything else
# besides -- this seed is just enough to verify the mechanism, not a
# substitute for the full sync.
_CONFIRMED_ASIN_PAIRS = [
    # (variant_sku, asin) -- both the closed-listing old SKU and the
    # currently-active new SKU map to the SAME asin (one physical listing).
    ('19" x 29" x,2', "B08D1QXRP1"),
    ("HF-P-2P",       "B08D1QXRP1"),
    ('19" x 29" x,4', "B08D1NXZSW"),
    ("HF-P-4P",       "B08D1NXZSW"),
    ('19" x 29" x,6', "B08D1XF3F9"),
    ("HF-P-6P",       "B08D1XF3F9"),
]


def seed_confirmed_asin_pairs(db_path=DB_PATH):
    """Idempotent -- INSERT OR IGNORE only, never overwrites a pair the live
    sync (or a manual edit) has since established."""
    init_cogs_schema(db_path=db_path)
    now = _now()
    conn = get_db(db_path)
    n = 0
    for variant, asin in _CONFIRMED_ASIN_PAIRS:
        cur = conn.execute("""
            INSERT INTO cogs_sku_asin (variant_sku, asin, source, updated_at)
            VALUES (?,?,'confirmed_seed',?)
            ON CONFLICT DO NOTHING
        """, (variant, asin, now))
        if cur.rowcount:
            n += 1
    conn.commit()
    conn.close()
    log.info(f"ASIN confirmed-pairs seed: {n} new pair(s).")
    return n


def upsert_sku_asin(variant_sku, asin, source="orders_report", db_path=DB_PATH):
    """Used by the live backfill sync (and any future manual entry) to
    record one SKU->ASIN observation. Overwrites on conflict (unlike the
    aliases table) because this is a raw observation, not a seller
    decision -- if a SKU string is later reused on a different ASIN, the
    most recent observation should win. updated_at lets a caller see when
    a mapping was last (re)confirmed."""
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO cogs_sku_asin (variant_sku, asin, source, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(variant_sku) DO UPDATE SET
            asin=excluded.asin, source=excluded.source, updated_at=excluded.updated_at
    """, (variant_sku, asin, source, _now()))
    conn.commit()
    conn.close()


def _pick_canonical_for_asin_group(conn, skus):
    """Given 2+ SKU strings confirmed to share one ASIN, pick ONE canonical
    to represent them all, in priority order:
      1. any member already has a manual/seed_csv alias -> use ITS canonical
         (an explicit seller decision always wins).
      2. any member IS ALREADY a canonical some other alias points to
         (i.e. it's already established downstream) -> use that member.
      3. any member has an existing cogs_canonical row with a PRICED family
         -> use that member (don't discard a price the seller already set);
         ties broken by real order count (most representative), not
         alphabetically.
      4. any member is an EXACT match in the classifier's seller-confirmed
         hardcode table (_HF_HARDCODE) -> prefer it. This is a genuinely
         non-guessing signal (an exact string the account owner explicitly
         confirmed), distinct from rule 5's generic pattern match, which a
         noise variant can satisfy by accident (module2_debug_fix_pass FIX
         4 finding: "HF--P2P" matches the pillow regex too, just with a
         messier family name -- without this rule, alphabetical tiebreaking
         inside the old rule 4 could pick the NOISY variant as canonical
         instead of the clean, seller-confirmed one).
      5. fall back to whichever member has the most real orders in
         pl_line_items (most representative / most likely the current,
         actively-sold listing) -- checked BEFORE the generic "recognised
         type" rule, because order volume is real seller data, not a
         punctuation-driven coincidence.
      6. any member has an existing cogs_canonical row at all (auto-
         classified, unpriced, zero orders so far) with a recognised type
         (not 'other') -> prefer that one.
      7. absolute last resort: alphabetical, for determinism only.
    Returns the chosen canonical SKU string (unchanged, not reclassified)."""
    # 1. explicit manual/seed_csv alias
    for sku in skus:
        row = conn.execute(
            "SELECT canonical_sku FROM cogs_aliases WHERE variant_sku=? AND source IN ('manual','seed_csv')",
            (sku,)
        ).fetchone()
        if row:
            return row["canonical_sku"]

    # 2. member is itself an existing alias TARGET (other SKUs already point to it)
    for sku in skus:
        row = conn.execute(
            "SELECT 1 FROM cogs_aliases WHERE canonical_sku=? LIMIT 1", (sku,)
        ).fetchone()
        if row:
            return sku

    def _order_count(sku):
        row = conn.execute("SELECT COUNT(*) AS n FROM pl_line_items WHERE sku=?", (sku,)).fetchone()
        return row["n"] or 0

    # 3. member has a priced family already -- most-orders tiebreak
    priced_candidates = []
    for sku in skus:
        row = conn.execute("""
            SELECT cc.canonical_sku FROM cogs_canonical cc
            JOIN cogs_families cf ON cf.family = cc.family
            WHERE cc.canonical_sku=? AND cf.unit_price_exvat IS NOT NULL
        """, (sku,)).fetchone()
        if row:
            priced_candidates.append(sku)
    if priced_candidates:
        priced_candidates.sort(key=lambda s: (-_order_count(s), s))
        return priced_candidates[0]

    # 4. exact seller-confirmed hardcode match (e.g. "HF-P2P", not a noisy
    # variant that merely happens to satisfy the same regex)
    hardcoded = [s for s in skus if s in _HF_HARDCODE]
    if hardcoded:
        hardcoded.sort(key=lambda s: (-_order_count(s), s))
        return hardcoded[0]

    # 5. most real orders (any member with orders > 0)
    counts = [(sku, _order_count(sku)) for sku in skus]
    with_orders = [sku for sku, n in counts if n > 0]
    if with_orders:
        with_orders.sort(key=lambda s: (-dict(counts)[s], s))
        return with_orders[0]

    # 6. recognised (non-'other') classification, zero orders so far
    typed_candidates = []
    for sku in skus:
        row = conn.execute(
            "SELECT product_type FROM cogs_canonical WHERE canonical_sku=? AND product_type != 'other'",
            (sku,)
        ).fetchone()
        if row:
            typed_candidates.append(sku)
    if typed_candidates:
        return sorted(typed_candidates)[0]

    # 7. last resort
    return sorted(skus)[0]


def seed_asin_from_managed_asins(db_path=DB_PATH):
    """module2_debug_fix_pass FIX 4: a second, IMMEDIATELY-available ASIN
    source alongside the confirmed-pairs seed and the (multi-hour, manual)
    asin_sync.py backfill -- Module 1's own managed_asins product catalog
    already carries real, seller-verified sku->asin pairs for every product
    it tracks (however few or many that is right now). Feeding these into
    cogs_sku_asin means run_asin_consolidation() has more real data to work
    with from the very next app restart, with zero waiting on the Orders
    report. Idempotent (upsert_sku_asin's own ON CONFLICT semantics apply)."""
    try:
        conn = get_db(db_path)
        # managed_asins is a Module 1 (collector) table — it does not exist in
        # Module 2's own Postgres, so guard rather than let the SELECT raise.
        if not db.table_exists(conn, "managed_asins"):
            conn.close()
            return 0
        rows = conn.execute(
            "SELECT sku, asin FROM managed_asins WHERE sku IS NOT NULL AND sku != '' "
            "AND asin IS NOT NULL AND asin != ''"
        ).fetchall()
        conn.close()
    except Exception as e:
        log.warning(f"seed_asin_from_managed_asins: could not read managed_asins ({e}) -- skipping.")
        return 0
    n = 0
    for r in rows:
        upsert_sku_asin(r["sku"], r["asin"], source="managed_asins", db_path=db_path)
        n += 1
    log.info(f"ASIN seed from managed_asins: {n} sku->asin pair(s) loaded from Module 1's catalog.")
    return n


def run_asin_consolidation(db_path=DB_PATH):
    """Groups cogs_sku_asin by ASIN, picks one canonical per group (2+
    distinct SKUs only -- a group of 1 has nothing to consolidate), and
    writes cogs_aliases rows (source='asin_auto') for every OTHER SKU in
    the group. Safe to call repeatedly (e.g. every app startup, and after
    every backfill window) -- INSERT OR IGNORE means it only ever fills
    gaps. Returns dict(groups_considered, aliases_added).

    module2_debug_fix_pass FIX 4: this is the ONLY place ASIN grouping
    decisions get made, and it writes straight into cogs_aliases -- the
    exact same table resolve_to_canonical() checks FIRST for every single
    SKU resolution, everywhere (recompute_line_item's per-order lookup,
    get_canonical_rollup's /pl grouping, search_canonicals' merge dropdown,
    the missing-prices worklist). There is no separate 'display-only'
    ASIN-consolidation path -- once a pair lands in cogs_sku_asin and this
    function has run, the grouping is real everywhere on the very next
    reprocess, not just a worklist-row label. If /pl or the merge dropdown
    still show fragmented SKU-string-driven groups, the cause is that
    cogs_sku_asin doesn't yet KNOW those SKUs share an ASIN (no seed/sync
    data for them yet) -- not that the mechanism only applies at display
    time."""
    init_cogs_schema(db_path=db_path)
    conn = get_db(db_path)
    rows = conn.execute("SELECT variant_sku, asin FROM cogs_sku_asin").fetchall()
    by_asin = {}
    for r in rows:
        by_asin.setdefault(r["asin"], []).append(r["variant_sku"])

    groups_considered = 0
    aliases_added = 0
    now = _now()
    for asin, skus in by_asin.items():
        skus = sorted(set(skus))
        if len(skus) < 2:
            continue
        groups_considered += 1
        canonical = _pick_canonical_for_asin_group(conn, skus)
        for sku in skus:
            if sku == canonical:
                continue
            cur = conn.execute("""
                INSERT INTO cogs_aliases (variant_sku, canonical_sku, source, updated_at)
                VALUES (?,?,'asin_auto',?)
                ON CONFLICT DO NOTHING
            """, (sku, canonical, now))
            if cur.rowcount:
                aliases_added += 1

    conn.commit()
    conn.close()
    log.info(f"ASIN consolidation: {groups_considered} multi-SKU ASIN group(s) considered, "
             f"{aliases_added} new alias(es) added.")
    return dict(groups_considered=groups_considered, aliases_added=aliases_added)


def get_member_skus_for_canonicals(db_path=DB_PATH):
    """Returns {canonical_sku: [variant_sku, ...]} — every raw/variant SKU
    that resolves to a given canonical, via cogs_aliases, PLUS the canonical
    SKU itself in its own list (a canonical with zero aliases still "has" at
    least itself as a matchable SKU string). Read-only, one grouped query —
    built for module2_search_filters (the /pl rollup search box), so typing
    ANY family-member SKU (a renamed/duplicate listing's SKU, not just the
    canonical one shown in the table) finds the right row."""
    conn = get_db(db_path)
    rows = conn.execute("SELECT variant_sku, canonical_sku FROM cogs_aliases").fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["canonical_sku"], []).append(r["variant_sku"])
    return out


def get_sku_identity(canonical_sku, db_path=DB_PATH):
    """module2_sku_detail: the full identity graph for ONE canonical SKU --
    "everything about this product" for the /pl/sku/<canonical_sku> detail
    page's Identity & relationships block. Read-only, small scale (a handful
    of rows per canonical), safe to call once per page load.

    Returns:
      canonical_sku, family, product_type, price_basis, pack_qty
      family_priced (bool), family_price (float or None)
      member_skus: [{"sku", "source", "updated_at"}] -- every raw/variant SKU
        that resolves to this canonical via cogs_aliases, PLUS the canonical
        itself tagged source="native" (it "links to itself" with no alias
        row needed) -- so the seller can see WHY things are grouped (alias
        source: seed_csv/manual/csv_upload/asin_auto) and spot a wrong merge.
      asins: [{"asin", "via_sku", "source"}] -- every ASIN observed for any
        SKU in member_skus (via cogs_sku_asin), de-duplicated by ASIN (first
        source seen wins if the same ASIN was seen via two different SKUs).
      variation_siblings: [{"canonical_sku", "pack_qty", "price_basis"}] --
        every OTHER canonical in the same family (pack-2/4/6, single/pair
        variants etc.), for the "hop between pack sizes" links.
    """
    conn = get_db(db_path)
    cls_row = conn.execute(
        "SELECT * FROM cogs_canonical WHERE canonical_sku=?", (canonical_sku,)
    ).fetchone()
    cls = dict(cls_row) if cls_row else classify_sku(canonical_sku)
    family = cls.get("family")

    fam_row = None
    if family:
        fr = conn.execute("SELECT * FROM cogs_families WHERE family=?", (family,)).fetchone()
        fam_row = dict(fr) if fr else None

    alias_rows = conn.execute(
        "SELECT variant_sku, source, updated_at FROM cogs_aliases WHERE canonical_sku=?",
        (canonical_sku,)
    ).fetchall()
    member_skus = [{"sku": canonical_sku, "source": "native", "updated_at": None}]
    member_skus += [{"sku": r["variant_sku"], "source": r["source"], "updated_at": r["updated_at"]}
                     for r in alias_rows]
    all_skus = [m["sku"] for m in member_skus]

    placeholders = ",".join("?" * len(all_skus))
    asin_rows = conn.execute(
        f"SELECT variant_sku, asin, source FROM cogs_sku_asin WHERE variant_sku IN ({placeholders})",
        all_skus
    ).fetchall()
    asins_by_asin = {}
    for r in asin_rows:
        asins_by_asin.setdefault(r["asin"], {"asin": r["asin"], "via_sku": r["variant_sku"], "source": r["source"]})
    asins = sorted(asins_by_asin.values(), key=lambda a: a["asin"])

    variation_siblings = []
    if family:
        sib_rows = conn.execute(
            "SELECT canonical_sku, pack_qty, price_basis FROM cogs_canonical "
            "WHERE family=? AND canonical_sku != ? ORDER BY pack_qty",
            (family, canonical_sku)
        ).fetchall()
        variation_siblings = [dict(r) for r in sib_rows]

    conn.close()
    return {
        "canonical_sku": canonical_sku,
        "family": family,
        "product_type": cls.get("product_type"),
        "price_basis": cls.get("price_basis"),
        "pack_qty": cls.get("pack_qty"),
        "family_priced": bool(fam_row and fam_row.get("unit_price_exvat") is not None),
        "family_price": fam_row.get("unit_price_exvat") if fam_row else None,
        # module2_dashboard_fixes A1: recorded VAT rate is family-level
        # metadata, defaults to 0.20 (standard) the moment a family row
        # exists at all (see _FAMILY_MIGRATIONS) -- None only if the family
        # itself has never been materialised (classify_sku fallback, no DB
        # row yet), in which case the UI should show "not set" rather than
        # assume standard.
        "vat_rate": fam_row.get("vat_rate") if fam_row else None,
        "member_skus": member_skus,
        "asins": asins,
        "variation_siblings": variation_siblings,
    }


def get_asin_map_for_canonicals(db_path=DB_PATH):
    """Returns {canonical_sku: [asin, ...]} for every canonical SKU that has
    ANY associated ASIN observation, by joining every real order's raw SKU
    (pl_line_items.sku) through cogs_sku_asin. One grouped query -- no N+1,
    matches the /pl/cogs performance lesson (see get_missing_prices_worklist
    perf note)."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT DISTINCT li.canonical_sku AS canonical_sku, sam.asin AS asin
        FROM pl_line_items li
        JOIN cogs_sku_asin sam ON sam.variant_sku = li.sku
        WHERE li.canonical_sku IS NOT NULL AND li.canonical_sku != ''
    """).fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault(r["canonical_sku"], []).append(r["asin"])
    for k in out:
        out[k] = sorted(set(out[k]))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# FAMILY PRICE EDITING — dashboard-driven
# ─────────────────────────────────────────────────────────────────────────────

def upsert_family_price(family, unit_price_exvat, source="manual", db_path=DB_PATH):
    """Set/replace ONE family's price (dashboard inline edit). All canonical
    SKUs under this family pick up the new price on the NEXT recompute/
    reprocess — this function itself does not touch pl_line_items (the
    caller is responsible for triggering a reprocess if immediate
    recalculation is wanted)."""
    conn = get_db(db_path)
    conn.execute("""
        UPDATE cogs_families SET unit_price_exvat=?, source=?, updated_at=? WHERE family=?
    """, (unit_price_exvat, source, _now(), family))
    conn.commit()
    conn.close()


_VALID_VAT_RATES = (0.0, 0.05, 0.20)


def upsert_family_vat_rate(family, vat_rate, source="manual", db_path=DB_PATH):
    """module2_dashboard_fixes A1: seller-recorded VAT rate for a family --
    metadata + display + cross-check ONLY (see the _FAMILY_MIGRATIONS
    comment above). Same write pattern as upsert_family_price: one row,
    every canonical SKU in the family shares it, no reprocess needed since
    nothing in pl_line_items reads this value. Rounds to the nearest valid
    rate rather than silently accepting an arbitrary number, since 0/5/20%
    are the only three real UK VAT treatments this seller's catalogue uses."""
    if vat_rate not in _VALID_VAT_RATES:
        raise ValueError(f"vat_rate must be one of {_VALID_VAT_RATES}, got {vat_rate!r}")
    conn = get_db(db_path)
    conn.execute("""
        UPDATE cogs_families SET vat_rate=?, source=?, updated_at=? WHERE family=?
    """, (vat_rate, source, _now(), family))
    conn.commit()
    conn.close()


def bulk_upsert_families_csv(file_path_or_obj, db_path=DB_PATH):
    """Bulk price add/update from an uploaded CSV in the SAME shape as
    price_families.csv (family,type,price_basis,enter_price_for,your_price,
    derives_these_skus). Unlike seed_from_csvs (which never overwrites),
    an explicit upload IS allowed to update an existing family's price —
    this is the seller intentionally re-entering/correcting prices, not a
    passive app-startup reseed. Also creates any brand-new family/canonical
    rows the upload introduces (e.g. a Wave-2 worklist CSV filled in and
    re-uploaded). Returns dict(updated=n, created=n)."""
    if hasattr(file_path_or_obj, "read"):
        text = file_path_or_obj.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8-sig")
        f = io.StringIO(text)
    else:
        f = open(file_path_or_obj, "r", encoding="utf-8-sig", newline="")

    updated = created = 0
    now = _now()
    conn = get_db(db_path)
    try:
        for row in csv.DictReader(f):
            family = (row.get("family") or "").strip()
            ptype = (row.get("type") or "").strip()
            basis = (row.get("price_basis") or "").strip()
            price_raw = (row.get("your_price") or "").strip()
            if not family or price_raw == "":
                continue   # a blank price = still unpriced, nothing to apply
            try:
                price = float(price_raw)
            except ValueError:
                continue

            existing = conn.execute(
                "SELECT 1 FROM cogs_families WHERE family=?", (family,)
            ).fetchone()
            if existing:
                conn.execute("""
                    UPDATE cogs_families SET unit_price_exvat=?, source='csv_upload', updated_at=?
                    WHERE family=?
                """, (price, now, family))
                updated += 1
            else:
                conn.execute("""
                    INSERT INTO cogs_families (family, product_type, price_basis, unit_price_exvat, source, updated_at)
                    VALUES (?,?,?,?,'csv_upload',?)
                """, (family, ptype or "other", basis or "single", price, now))
                created += 1

            for sku, pack_qty in _parse_derives(row.get("derives_these_skus")):
                conn.execute("""
                    INSERT INTO cogs_canonical
                        (canonical_sku, family, product_type, pack_qty, price_basis, source, updated_at)
                    VALUES (?,?,?,?,?,'csv_upload',?)
                    ON CONFLICT(canonical_sku) DO UPDATE SET
                        family=excluded.family, product_type=excluded.product_type,
                        pack_qty=excluded.pack_qty, price_basis=excluded.price_basis,
                        source='csv_upload', updated_at=excluded.updated_at
                """, (sku, family, ptype or "other", pack_qty, basis or "single", now))
    finally:
        f.close()
        conn.commit()
        conn.close()
    log.info(f"COGS bulk upload: {updated} family price(s) updated, {created} new family(ies) created.")
    return dict(updated=updated, created=created)


def get_all_families(db_path=DB_PATH):
    """Every family with its price + how many canonical SKUs derive from it
    — for the editable dashboard table."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT f.family, f.product_type, f.price_basis, f.unit_price_exvat, f.source, f.updated_at,
               f.vat_rate,
               COUNT(c.canonical_sku) AS n_canonical_skus
        FROM cogs_families f
        LEFT JOIN cogs_canonical c ON c.family = f.family
        GROUP BY f.family
        ORDER BY (f.unit_price_exvat IS NULL) DESC, f.product_type, f.family
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# FIX / MERGE TOOL (module2_ux_and_merge_tool) — manual overrides for the
# residual cross-ASIN cases ASIN auto-consolidation can't reach on its own
# (e.g. towel sizes spanning many ASINs, or a seller decision to treat
# several ASINs as one price family). This is the permanent in-app
# replacement for the Railway Postgres alias-table workflow — cogs_aliases
# (this SQLite DB) is now the single source of truth going forward.
# ─────────────────────────────────────────────────────────────────────────────

def manual_merge_sku(variant_sku, target_canonical, db_path=DB_PATH, require_existing=True):
    """Seller-driven: 'this SKU is actually the same product as
    target_canonical.' Writes cogs_aliases (source='manual') — an explicit
    seller action, so (unlike seed/asin_auto, which only ever fill gaps)
    this DOES overwrite any existing alias for variant_sku, including a
    previous manual or asin_auto decision; _pick_canonical_for_asin_group's
    priority order means a manual alias also always wins over any future
    ASIN auto-consolidation pass. Returns the target's cogs_canonical row.

    module2_debug_fix_pass FIX 3: the Fix/Merge UI's option A is explicitly
    'merge into an EXISTING canonical' (option B, manual_define_family, is
    the one for brand-new products) — but ensure_canonical() will happily
    AUTO-CREATE a canonical for whatever string it's given. If the seller's
    typed/selected target_canonical doesn't exactly match a real existing
    one (a stray character, autocomplete not actually selected, a typo),
    the old behaviour silently created a phantom new canonical instead of
    merging into the intended product — which looks exactly like 'merging
    doesn't do anything' from the seller's side (the row just gets replaced
    by an equally-unpriced new one). require_existing=True (the default)
    now rejects that case with a clear error instead of silently doing the
    wrong thing; pass False only for programmatic/internal callers that
    intentionally want auto-create-on-merge."""
    if not variant_sku or not target_canonical:
        raise ValueError("variant_sku and target_canonical are both required.")
    if variant_sku == target_canonical:
        raise ValueError("Cannot merge a SKU into itself.")
    conn = get_db(db_path)
    if require_existing:
        existing = conn.execute(
            "SELECT 1 FROM cogs_canonical WHERE canonical_sku=?", (target_canonical,)
        ).fetchone()
        if not existing:
            conn.close()
            raise ValueError(
                f"'{target_canonical}' is not an existing canonical product — pick one from "
                f"the search list, or use 'Define as new family' instead if it's genuinely new."
            )
    conn.execute("""
        INSERT INTO cogs_aliases (variant_sku, canonical_sku, source, updated_at)
        VALUES (?,?,'manual',?)
        ON CONFLICT(variant_sku) DO UPDATE SET
            canonical_sku=excluded.canonical_sku, source='manual', updated_at=excluded.updated_at
    """, (variant_sku, target_canonical, _now()))
    conn.commit()
    result = ensure_canonical(target_canonical, db_path=db_path, conn=conn)
    conn.close()
    return result


def manual_define_family(canonical_sku, product_type, price_basis, pack_qty=1,
                          unit_price_exvat=None, db_path=DB_PATH):
    """Seller-driven: 'this SKU is its own new product, here's its type/
    basis/price' — bypasses classify_sku's name-pattern guess entirely (for
    a SKU the pattern-matcher got wrong, or one the seller wants priced
    immediately without waiting on a guessed grouping). Creates (or
    explicitly overwrites — this is a deliberate seller decision, like
    manual_merge_sku) a cogs_canonical row with source='manual' and a
    matching cogs_families row (family = the canonical SKU itself; other
    SKUs can be folded into it later via manual_merge_sku)."""
    if product_type not in ("cushion", "pillow", "towel", "other"):
        raise ValueError(f"Unknown product_type: {product_type!r}")
    if price_basis not in ("single", "pair"):
        raise ValueError(f"Unknown price_basis: {price_basis!r}")
    if not canonical_sku:
        raise ValueError("canonical_sku is required.")
    family = canonical_sku
    now = _now()
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO cogs_families (family, product_type, price_basis, unit_price_exvat, source, updated_at)
        VALUES (?,?,?,?,'manual',?)
        ON CONFLICT(family) DO UPDATE SET
            product_type=excluded.product_type, price_basis=excluded.price_basis,
            unit_price_exvat=excluded.unit_price_exvat, source='manual', updated_at=excluded.updated_at
    """, (family, product_type, price_basis, unit_price_exvat, now))
    conn.execute("""
        INSERT INTO cogs_canonical (canonical_sku, family, product_type, pack_qty, price_basis, source, updated_at)
        VALUES (?,?,?,?,?,'manual',?)
        ON CONFLICT(canonical_sku) DO UPDATE SET
            family=excluded.family, product_type=excluded.product_type, pack_qty=excluded.pack_qty,
            price_basis=excluded.price_basis, source='manual', updated_at=excluded.updated_at
    """, (canonical_sku, family, product_type, pack_qty, price_basis, now))
    conn.commit()
    conn.close()
    return dict(canonical_sku=canonical_sku, family=family, product_type=product_type,
                price_basis=price_basis, pack_qty=pack_qty, unit_price_exvat=unit_price_exvat)


_PUNCT_FIX_RE = re.compile(r"[._ ]")


def describe_pack(product_type, price_basis, pack_qty):
    """Unambiguous human label. Pack SIZE and price BASIS are two DIFFERENT
    things and must never be collapsed into one '×N': a pillow '-P4' is a pack
    of 4 PILLOWS, which is 2 PAIRS. Writing 'pair ×4' reads as 4 pairs (8
    pillows) -- exactly the confusion this spells out, and the same class of
    display bug as the old '£2.65 / unit × pack of 4' one. The stored numbers
    (pack_qty=4, basis=pair, multiplier 4/2=2) are unaffected; this is wording
    only."""
    q = int(pack_qty or 1)
    if product_type == "pillow" and price_basis == "pair":
        pairs = q / 2.0
        pairs_txt = str(int(pairs)) if pairs == int(pairs) else f"{pairs:g}"
        return (f"pack of {q} pillow{'s' if q != 1 else ''} "
                f"= {pairs_txt} pair{'s' if pairs != 1 else ''} · price is per PAIR")
    if product_type == "pillow":
        return f"pack of {q} pillow{'s' if q != 1 else ''} · price is per pillow"
    if product_type == "cushion":
        return f"pack of {q} cushion{'s' if q != 1 else ''} · price is per cushion"
    if product_type == "towel":
        return f"pack of {q} towel{'s' if q != 1 else ''} · price is per towel"
    return f"pack of {q} · price is per pack"


def manual_override_type_basis(canonical_sku, product_type, price_basis, pack_qty=1,
                                target_family=None, db_path=DB_PATH):
    """File F: force type/basis/pack_qty on a SKU the name-pattern matcher got
    wrong (punctuation-broken names like 'HF.P2P' or 'BD_6378_P4' fall to
    'other'/single and get the wrong COGS multiplier), AND optionally fold it
    into an EXISTING family so it inherits that family's single price -- no
    one-SKU family to keep in sync, one number per real family.

    target_family must already exist (never invented here); pass None to keep
    the old 'its own family' behaviour. Written with source='manual', which is
    what makes it durable: seed_from_csvs uses INSERT OR IGNORE (fills gaps
    only) and ensure_canonical never reclassifies an existing canonical, so
    neither a CSV re-seed nor a reprocess can silently revert it."""
    if product_type not in ("cushion", "pillow", "towel", "other"):
        raise ValueError(f"Unknown product_type: {product_type!r}")
    if price_basis not in ("single", "pair"):
        raise ValueError(f"Unknown price_basis: {price_basis!r}")
    if not canonical_sku:
        raise ValueError("canonical_sku is required.")
    now = _now()
    conn = get_db(db_path)
    if target_family:
        if not conn.execute("SELECT 1 FROM cogs_families WHERE family=?", (target_family,)).fetchone():
            conn.close()
            raise ValueError(f"'{target_family}' is not an existing family — pick one from the list.")
        family = target_family
    else:
        family = canonical_sku
        conn.execute("""
            INSERT INTO cogs_families (family, product_type, price_basis, unit_price_exvat, source, updated_at)
            VALUES (?,?,?,NULL,'manual',?)
            ON CONFLICT(family) DO UPDATE SET product_type=excluded.product_type,
                price_basis=excluded.price_basis, source='manual', updated_at=excluded.updated_at
        """, (family, product_type, price_basis, now))
    conn.execute("""
        INSERT INTO cogs_canonical (canonical_sku, family, product_type, pack_qty, price_basis, source, updated_at)
        VALUES (?,?,?,?,?,'manual',?)
        ON CONFLICT(canonical_sku) DO UPDATE SET family=excluded.family,
            product_type=excluded.product_type, pack_qty=excluded.pack_qty,
            price_basis=excluded.price_basis, source='manual', updated_at=excluded.updated_at
    """, (canonical_sku, family, product_type, pack_qty, price_basis, now))
    conn.commit()
    conn.close()
    return dict(canonical_sku=canonical_sku, family=family, product_type=product_type,
                price_basis=price_basis, pack_qty=pack_qty)


def find_punctuation_misclassified(db_path=DB_PATH):
    """Canonicals sitting in 'other' ONLY because punctuation ('.', '_', space)
    stands in for the '-P#' / 'x' separator classify_sku expects -- e.g.
    'BD_6378_P4' and 'HF.P2P', which lose the pillow PAIR basis and so get the
    wrong COGS multiplier. Returns what each SHOULD be (classify_sku on a
    punctuation-normalised copy) plus its order volume so the impact is
    visible. READ-ONLY -- suggests, never fixes."""
    conn = get_db(db_path)
    rows = conn.execute(
        "SELECT canonical_sku, family FROM cogs_canonical WHERE product_type='other'").fetchall()
    out = []
    for r in rows:
        s = r["canonical_sku"]
        if classify_sku(s)["product_type"] != "other":
            continue
        alt = classify_sku(_PUNCT_FIX_RE.sub("-", s or ""))
        if alt["product_type"] == "other":
            continue
        v = conn.execute("""SELECT COUNT(*) AS lines, COALESCE(SUM(quantity),0) AS units,
                                   ROUND(COALESCE(SUM(sale_price_exvat),0),2) AS revenue
                            FROM pl_line_items WHERE canonical_sku=?""", (s,)).fetchone()
        exists = conn.execute("SELECT 1 FROM cogs_families WHERE family=?",
                              (alt["family"],)).fetchone() is not None
        out.append(dict(canonical_sku=s, current_family=r["family"],
                        suggested_type=alt["product_type"], suggested_basis=alt["price_basis"],
                        suggested_pack_qty=alt["pack_qty"], suggested_family=alt["family"],
                        suggested_family_exists=exists,
                        suggested_describe=describe_pack(alt["product_type"], alt["price_basis"], alt["pack_qty"]),
                        lines=v["lines"], units=v["units"], revenue=v["revenue"] or 0.0))
    conn.close()
    out.sort(key=lambda d: -(d["revenue"] or 0))
    return out


def get_merge_workbench(db_path=DB_PATH, limit=500):
    """Every canonical with the chip data needed to judge a merge:
    SKU · ASIN(s) · order lines · units · revenue, plus family/price/priced.
    Revenue-desc so the SKUs that actually matter sort to the top."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT cc.canonical_sku, cc.family, cc.product_type, cc.price_basis, cc.pack_qty, cc.source,
               (SELECT unit_price_exvat FROM cogs_families f WHERE f.family=cc.family) AS unit_price,
               (SELECT COUNT(*) FROM pl_line_items li WHERE li.canonical_sku=cc.canonical_sku) AS lines,
               (SELECT COALESCE(SUM(quantity),0) FROM pl_line_items li WHERE li.canonical_sku=cc.canonical_sku) AS units,
               (SELECT ROUND(COALESCE(SUM(sale_price_exvat),0),2) FROM pl_line_items li WHERE li.canonical_sku=cc.canonical_sku) AS revenue
        FROM cogs_canonical cc ORDER BY revenue DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    asins = get_asin_map_for_canonicals(db_path=db_path)
    out = []
    for r in rows:
        d = dict(r)
        d["asins"] = asins.get(r["canonical_sku"], [])
        d["describe"] = describe_pack(d.get("product_type"), d.get("price_basis"), d.get("pack_qty"))
        out.append(d)
    return out


def undo_merge(variant_sku, db_path=DB_PATH):
    """Reversible merge: drop the alias so the SKU resolves to itself again.
    Only ever removes a source='manual' alias -- a seed_csv/asin_auto mapping
    is never silently deleted by an undo."""
    conn = get_db(db_path)
    cur = conn.execute("DELETE FROM cogs_aliases WHERE variant_sku=? AND source='manual'",
                       (variant_sku,))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def get_recent_manual_merges(limit=60, db_path=DB_PATH):
    """The undo list: manual merges, newest first."""
    conn = get_db(db_path)
    rows = conn.execute("""SELECT variant_sku, canonical_sku, updated_at FROM cogs_aliases
                           WHERE source='manual' ORDER BY updated_at DESC LIMIT ?""",
                        (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def search_canonicals(query, db_path=DB_PATH, limit=25):
    """Powers the Fix/Merge 'search for an existing canonical' box — matches
    on canonical SKU or family substring, case-insensitive. Small dataset
    (~hundreds of canonicals at most), a LIKE scan is plenty fast.

    module2_debug_fix_pass FIX 4: excludes any canonical_sku that has ITSELF
    since been merged away (i.e. it now appears as a variant_sku in
    cogs_aliases, pointing somewhere else — via a manual merge, an ASIN
    auto-consolidation, or a CSV alias). Without this, a SKU processed
    before its alias/ASIN grouping was known left a permanent, stale
    cogs_canonical row that kept showing up as a selectable 'existing
    canonical' in the merge dropdown forever after — exactly the 'five
    variants instead of one' symptom. This filter doesn't delete anything
    (the row and its history stay intact), it just stops offering a
    defunct, merged-away canonical as a fresh merge target."""
    conn = get_db(db_path)
    q = f"%{query}%"
    rows = conn.execute("""
        SELECT canonical_sku, family, product_type, price_basis
        FROM cogs_canonical
        WHERE (canonical_sku LIKE ? OR family LIKE ?)
          AND canonical_sku NOT IN (SELECT variant_sku FROM cogs_aliases)
        ORDER BY canonical_sku
        LIMIT ?
    """, (q, q, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# MISSING-PRICES WORKLIST — every canonical SKU that has actually appeared in
# real orders (pl_line_items) but whose family still has no price. This is
# the seller's Wave-2 worklist: fill in one number per family, the
# derivation handles the rest.
# ─────────────────────────────────────────────────────────────────────────────

def get_missing_prices_worklist(db_path=DB_PATH, start_date=None, end_date=None):
    """Returns a list of dicts, one per UNPRICED FAMILY actually seen in real
    orders (within [start_date, end_date] if given, both inclusive ISO date
    strings compared against posted_date -- 'All time' is start_date=None,
    end_date=None), each with: family, product_type, price_basis,
    canonical_skus (list of {sku, pack_qty}), order_count, units,
    revenue_exvat (so the seller can prioritise by real order volume, not
    just by SKU count).

    Perf note: this used to run one full table scan of pl_line_items PER
    DISTINCT canonical SKU (no usable index on canonical_sku alone -- the
    only index was composite (account_id, canonical_sku), and this query
    doesn't filter by account_id). On the real 33,720-row table that made
    /pl/cogs take a very long time to load. Now a SINGLE grouped query does
    the per-SKU aggregation in one table scan; the per-canonical lookups
    that remain (cogs_canonical / cogs_families) are cheap indexed
    primary-key reads."""
    conn = get_db(db_path)
    where = "WHERE canonical_sku IS NOT NULL AND canonical_sku != ''"
    params = []
    if start_date:
        where += " AND posted_date >= ?"
        params.append(start_date)
    if end_date:
        where += " AND posted_date <= ?"
        params.append(end_date)
    agg_rows = conn.execute(f"""
        SELECT canonical_sku,
               COUNT(DISTINCT order_id) AS orders,
               SUM(quantity) AS units,
               SUM(sale_price_exvat) AS revenue_exvat
        FROM pl_line_items
        {where}
        GROUP BY canonical_sku
    """, params).fetchall()

    by_family = {}
    for agg in agg_rows:
        canonical = agg["canonical_sku"]
        row = conn.execute(
            "SELECT * FROM cogs_canonical WHERE canonical_sku=?", (canonical,)
        ).fetchone()
        if not row:
            # Shouldn't normally happen (recompute always calls ensure_canonical
            # first) but classify on the fly defensively so the worklist is
            # never silently incomplete.
            cls = classify_sku(canonical)
            row = dict(canonical_sku=canonical, **cls)
        else:
            row = dict(row)
        fam = conn.execute(
            "SELECT unit_price_exvat FROM cogs_families WHERE family=?", (row["family"],)
        ).fetchone()
        priced = bool(fam and fam["unit_price_exvat"] is not None)
        if priced:
            continue
        entry = by_family.setdefault(row["family"], dict(
            family=row["family"], product_type=row["product_type"],
            price_basis=row["price_basis"], canonical_skus=[],
            order_count=0, units=0, revenue_exvat=0.0,
        ))
        entry["canonical_skus"].append(dict(sku=canonical, pack_qty=row["pack_qty"]))
        entry["order_count"] += agg["orders"] or 0
        entry["units"] += agg["units"] or 0
        entry["revenue_exvat"] += agg["revenue_exvat"] or 0.0

    # module2_ux_and_merge_tool: annotate each canonical with how many OTHER
    # SKUs were folded into it via ASIN auto-consolidation, so the dashboard
    # can show "consolidated from N SKUs via ASIN" per the spec's UX note.
    asin_merge_counts = {
        r["canonical_sku"]: r["n"] for r in conn.execute(
            "SELECT canonical_sku, COUNT(*) AS n FROM cogs_aliases WHERE source='asin_auto' GROUP BY canonical_sku"
        ).fetchall()
    }
    for entry in by_family.values():
        for skuinfo in entry["canonical_skus"]:
            skuinfo["asin_merged_count"] = asin_merge_counts.get(skuinfo["sku"], 0)

    conn.close()
    worklist = sorted(by_family.values(), key=lambda e: -e["revenue_exvat"])
    return worklist


def resolve_worklist_date_range(db_path=DB_PATH, start_date=None, end_date=None):
    """The ACTUAL earliest/latest posted_date among orders covered by the
    given filter (for display: 'Showing orders from 10 Apr 2026 - 08 Jul
    2026') -- distinct from the requested filter bounds, since e.g. a
    'Last 90 days' filter on an account with only 40 days of history should
    show the real 40-day span, not a misleading 90-day claim. Returns
    (min_date, max_date) as ISO date strings (date part only), or
    (None, None) if no orders fall in range."""
    conn = get_db(db_path)
    where = "WHERE canonical_sku IS NOT NULL AND canonical_sku != ''"
    params = []
    if start_date:
        where += " AND posted_date >= ?"
        params.append(start_date)
    if end_date:
        where += " AND posted_date <= ?"
        params.append(end_date)
    row = conn.execute(
        f"SELECT MIN(posted_date) AS mn, MAX(posted_date) AS mx FROM pl_line_items {where}",
        params
    ).fetchone()
    conn.close()
    mn = (row["mn"] or "")[:10] or None
    mx = (row["mx"] or "")[:10] or None
    return mn, mx


def export_missing_prices_csv_rows(db_path=DB_PATH, start_date=None, end_date=None):
    """Same shape as price_families.csv, your_price left BLANK — download,
    fill in, re-upload via bulk_upsert_families_csv()."""
    worklist = get_missing_prices_worklist(db_path=db_path, start_date=start_date, end_date=end_date)
    rows = [["family", "type", "price_basis", "enter_price_for", "your_price", "derives_these_skus"]]
    for entry in worklist:
        skus_sorted = sorted(entry["canonical_skus"], key=lambda s: s["pack_qty"])
        enter_for = skus_sorted[0]["sku"] if skus_sorted else ""
        derives = "; ".join(f"{s['sku']} (pack {s['pack_qty']})" for s in skus_sorted)
        rows.append([entry["family"], entry["product_type"], entry["price_basis"],
                     enter_for, "", derives])
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# MONTHLY OVERHEADS — fixed costs, deliberately NOT allocated into per-order
# COGS. Subtracted as a single monthly line at the rollup level only.
# ─────────────────────────────────────────────────────────────────────────────

def get_overheads(account_id, db_path=DB_PATH):
    conn = get_db(db_path)
    row = conn.execute(
        "SELECT monthly_amount_exvat FROM cogs_overheads WHERE account_id=?", (account_id,)
    ).fetchone()
    conn.close()
    return row["monthly_amount_exvat"] if row else 0.0


def set_overheads(account_id, monthly_amount_exvat, db_path=DB_PATH):
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO cogs_overheads (account_id, monthly_amount_exvat, updated_at)
        VALUES (?,?,?)
        ON CONFLICT(account_id) DO UPDATE SET
            monthly_amount_exvat=excluded.monthly_amount_exvat, updated_at=excluded.updated_at
    """, (account_id, monthly_amount_exvat, _now()))
    conn.commit()
    conn.close()
