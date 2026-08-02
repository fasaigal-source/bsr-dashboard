"""
module1_db.py — Database schema and helpers for Module 1 (Decision-Support Repricer)

v2 changes vs. first Cowork build:
  • managed_asins gains a `current_price` column (the seed price / source-of-truth
    fallback) so day-one recommendations aren't computed off the floor.
  • get_current_price falls back to managed_asins.current_price, not the floor.
  • get_net_delta() computes OUR month-over-month velocity change MINUS the market's,
    which is what the decision function actually wants (spec §5). The old
    get_baseline_delta only returned the market's own change.
  • import_baseline_csv() ingests the competitor seasonal CSV (utf-8-sig + num()).
  • init_schema() migrates existing DBs (ALTER TABLE) so nothing has to be deleted.
"""

import sqlite3
import json
import csv
import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)
DB_PATH = "bsr_history.db"


def get_db(db_path=DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ─────────────────────────────────────────────────────────────────────────────
# SCHEMA
# ─────────────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id      TEXT PRIMARY KEY,
    seller_id       TEXT,
    marketplace_id  TEXT NOT NULL DEFAULT 'A1F83G8C2ARO7P',
    refresh_token   TEXT,
    active          INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT
);

CREATE TABLE IF NOT EXISTS managed_asins (
    account_id        TEXT NOT NULL,
    asin              TEXT NOT NULL,
    sku               TEXT,
    brand             TEXT,
    title             TEXT,
    root_category     TEXT,
    current_price     REAL,
    cogs              REAL,
    postage           REAL,
    vat_rate          REAL DEFAULT 0.20,
    target_bsr        INTEGER,
    target_acos       REAL,
    floor_price       REAL NOT NULL DEFAULT 11.75,
    ceiling_price     REAL NOT NULL DEFAULT 16.99,
    step_pct          REAL NOT NULL DEFAULT 0.05,
    raise_below       INTEGER NOT NULL DEFAULT 13500,
    lower_above       INTEGER NOT NULL DEFAULT 22000,
    hard_lower_above  INTEGER NOT NULL DEFAULT 28000,
    min_confirm_days  INTEGER NOT NULL DEFAULT 2,
    cooldown_days     INTEGER NOT NULL DEFAULT 3,
    active            INTEGER NOT NULL DEFAULT 1,
    notes             TEXT,
    PRIMARY KEY (account_id, asin)
);

CREATE TABLE IF NOT EXISTS rank_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL,
    asin          TEXT NOT NULL,
    captured_at   TEXT NOT NULL,
    root_rank     INTEGER,
    root_category TEXT,
    sub_rank      INTEGER,
    sub_category  TEXT,
    source        TEXT NOT NULL DEFAULT 'sp-api',
    raw_json      TEXT
);

CREATE TABLE IF NOT EXISTS rank_history_import (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL,
    asin          TEXT NOT NULL,
    day           TEXT NOT NULL,
    root_rank     INTEGER,
    price         REAL,
    source        TEXT NOT NULL DEFAULT 'trellis',
    UNIQUE(account_id, asin, day, source)
);

CREATE TABLE IF NOT EXISTS velocity_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   TEXT NOT NULL,
    asin         TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end   TEXT NOT NULL,
    units        INTEGER NOT NULL,
    captured_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_baseline (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    category           TEXT NOT NULL,
    month              TEXT NOT NULL,
    avg_rank           REAL,
    avg_monthly_units  REAL,
    source_note        TEXT,
    imported_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id            TEXT NOT NULL,
    asin                  TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    current_price         REAL,
    current_root_rank     INTEGER,
    current_sub_rank      INTEGER,
    current_velocity      INTEGER,
    baseline_delta_pct    REAL,
    signal_state          TEXT NOT NULL,
    recommended_action    TEXT NOT NULL,
    recommended_price     REAL,
    reasoning             TEXT NOT NULL,
    status                TEXT NOT NULL DEFAULT 'pending',
    decided_at            TEXT,
    decided_price         REAL,
    decided_note          TEXT
);

CREATE TABLE IF NOT EXISTS price_changes (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id               TEXT NOT NULL,
    asin                     TEXT NOT NULL,
    changed_at               TEXT NOT NULL,
    old_price                REAL,
    new_price                REAL,
    source_recommendation_id INTEGER,
    applied_via              TEXT DEFAULT 'manual'
);
"""


def init_schema(db_path=DB_PATH):
    """Create all tables if missing, and migrate older DBs in place."""
    conn = get_db(db_path)
    for statement in SCHEMA.strip().split(";"):
        s = statement.strip()
        if s:
            conn.execute(s)
    # ── Migration: add current_price to managed_asins if an older DB lacks it ──
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(managed_asins)").fetchall()]
    add_cols = {
        "current_price": "REAL", "cogs": "REAL", "postage": "REAL",
        "vat_rate": "REAL DEFAULT 0.20", "target_bsr": "INTEGER",
        "target_acos": "REAL", "title": "TEXT",
    }
    for col, decl in add_cols.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE managed_asins ADD COLUMN {col} {decl}")
            log.info(f"Migrated managed_asins: added {col} column.")
    conn.commit()
    conn.close()
    log.info("Module 1 schema initialised.")


# ─────────────────────────────────────────────────────────────────────────────
# MANAGED ASINs — seed / upsert
# ─────────────────────────────────────────────────────────────────────────────

def upsert_managed_asin(account_id, asin, sku=None, brand=None, title=None,
                         root_category=None, current_price=None, cogs=None,
                         postage=None, vat_rate=0.20, target_bsr=None, target_acos=None,
                         floor_price=11.75, ceiling_price=16.99,
                         step_pct=0.05, raise_below=13500, lower_above=22000,
                         hard_lower_above=28000, min_confirm_days=2, cooldown_days=3,
                         notes=None, db_path=DB_PATH):
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO managed_asins
            (account_id, asin, sku, brand, title, root_category, current_price,
             cogs, postage, vat_rate, target_bsr, target_acos, floor_price,
             ceiling_price, step_pct, raise_below, lower_above, hard_lower_above,
             min_confirm_days, cooldown_days, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(account_id, asin) DO UPDATE SET
            sku=COALESCE(excluded.sku, managed_asins.sku),
            brand=COALESCE(excluded.brand, managed_asins.brand),
            title=COALESCE(excluded.title, managed_asins.title),
            root_category=COALESCE(excluded.root_category, managed_asins.root_category),
            current_price=COALESCE(excluded.current_price, managed_asins.current_price),
            cogs=COALESCE(excluded.cogs, managed_asins.cogs),
            postage=COALESCE(excluded.postage, managed_asins.postage),
            vat_rate=COALESCE(excluded.vat_rate, managed_asins.vat_rate),
            target_bsr=COALESCE(excluded.target_bsr, managed_asins.target_bsr),
            target_acos=COALESCE(excluded.target_acos, managed_asins.target_acos),
            floor_price=excluded.floor_price,
            ceiling_price=excluded.ceiling_price,
            step_pct=excluded.step_pct,
            raise_below=excluded.raise_below,
            lower_above=excluded.lower_above,
            hard_lower_above=excluded.hard_lower_above,
            min_confirm_days=excluded.min_confirm_days,
            cooldown_days=excluded.cooldown_days,
            notes=COALESCE(excluded.notes, managed_asins.notes)
    """, (account_id, asin, sku, brand, title, root_category, current_price,
          cogs, postage, vat_rate, target_bsr, target_acos, floor_price,
          ceiling_price, step_pct, raise_below, lower_above, hard_lower_above,
          min_confirm_days, cooldown_days, notes))
    conn.commit()
    conn.close()
    log.info(f"Upserted managed ASIN {asin} for account {account_id}")


def set_current_price(account_id, asin, price, db_path=DB_PATH):
    """Manually correct the stored current price for an ASIN (no price_changes row)."""
    conn = get_db(db_path)
    conn.execute("UPDATE managed_asins SET current_price=? WHERE account_id=? AND asin=?",
                 (price, account_id, asin))
    conn.commit()
    conn.close()


def get_managed_asins(account_id=None, db_path=DB_PATH):
    conn = get_db(db_path)
    if account_id:
        rows = conn.execute(
            "SELECT * FROM managed_asins WHERE account_id=? AND active=1", (account_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM managed_asins WHERE active=1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# RANK HISTORY
# ─────────────────────────────────────────────────────────────────────────────

def save_rank(account_id, asin, root_rank, root_category, sub_rank, sub_category,
              raw_json, db_path=DB_PATH):
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO rank_history
            (account_id, asin, captured_at, root_rank, root_category,
             sub_rank, sub_category, raw_json)
        VALUES (?,?,?,?,?,?,?,?)
    """, (account_id, asin, datetime.utcnow().isoformat(),
          root_rank, root_category, sub_rank, sub_category,
          json.dumps(raw_json) if raw_json else None))
    conn.commit()
    conn.close()


def get_rank_history(account_id, asin, days=7, db_path=DB_PATH):
    since = (datetime.utcnow() - timedelta(days=days)).isoformat()
    conn  = get_db(db_path)
    rows  = conn.execute("""
        SELECT * FROM rank_history
        WHERE account_id=? AND asin=? AND captured_at>=?
        ORDER BY captured_at ASC
    """, (account_id, asin, since)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_signal_days(account_id, asin, direction, raise_below, lower_above, db_path=DB_PATH):
    """Count consecutive recent snapshots the rank has held its direction ('up'|'down')."""
    rows = get_rank_history(account_id, asin, days=14, db_path=db_path)
    if not rows:
        return 0
    count = 0
    for row in reversed(rows):
        rank = row.get("root_rank") or row.get("sub_rank")
        if rank is None:
            break
        if direction == "up" and rank < raise_below:
            count += 1
        elif direction == "down" and rank > lower_above:
            count += 1
        else:
            break
    return count


# ─────────────────────────────────────────────────────────────────────────────
# VELOCITY HISTORY
# ─────────────────────────────────────────────────────────────────────────────

def save_velocity(account_id, asin, window_start, window_end, units, db_path=DB_PATH):
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO velocity_history
            (account_id, asin, window_start, window_end, units, captured_at)
        VALUES (?,?,?,?,?,?)
    """, (account_id, asin, window_start, window_end, units,
          datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def save_daily_velocity(account_id, asin, day, units, db_path=DB_PATH):
    """Upsert ONE day's unit count (window_start == window_end == the day).
    Re-running the same day replaces it, so late orders correct the figure."""
    conn = get_db(db_path)
    conn.execute("""
        DELETE FROM velocity_history
        WHERE account_id=? AND asin=? AND window_start=? AND window_end=?
    """, (account_id, asin, day, day))
    conn.execute("""
        INSERT INTO velocity_history
            (account_id, asin, window_start, window_end, units, captured_at)
        VALUES (?,?,?,?,?,?)
    """, (account_id, asin, day, day, units, datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()


def get_daily_units(account_id, asin, days=14, db_path=DB_PATH):
    """{day: units} for the last N days from the daily rows (window_start==window_end)."""
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    conn  = get_db(db_path)
    rows  = conn.execute("""
        SELECT window_start AS day, units FROM velocity_history
        WHERE account_id=? AND asin=? AND window_start=window_end AND window_start>=?
        ORDER BY window_start ASC
    """, (account_id, asin, since)).fetchall()
    conn.close()
    return {r["day"]: r["units"] for r in rows}


def get_velocity_windows(account_id, asin, db_path=DB_PATH):
    """
    Compare NON-OVERLAPPING recent windows from daily buckets:
      velocity_now  = units in the last 3 full days (yesterday and the 2 before)
      velocity_prev = units in the 3 days before those
    Today is excluded — it's a partial day and would always read as a slump.
    Falls back to legacy trailing-window rows if no daily buckets exist yet.
    """
    daily = get_daily_units(account_id, asin, days=8, db_path=db_path)
    if daily:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        days_sorted = [(datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
                       for i in range(1, 7)]           # yesterday .. 6 days ago
        now_days, prev_days = days_sorted[:3], days_sorted[3:]
        velocity_now  = sum(daily.get(d, 0) for d in now_days)
        velocity_prev = sum(daily.get(d, 0) for d in prev_days)
        return velocity_now, velocity_prev

    # Legacy fallback (pre-daily rows)
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT units FROM velocity_history
        WHERE account_id=? AND asin=?
        ORDER BY captured_at DESC LIMIT 2
    """, (account_id, asin)).fetchall()
    conn.close()
    if len(rows) >= 2:
        return rows[0]["units"], rows[1]["units"]
    elif len(rows) == 1:
        return rows[0]["units"], 0
    return 0, 0


def get_last7_units(account_id, asin, db_path=DB_PATH):
    """Sum of the last 7 full days' daily buckets — for display."""
    daily = get_daily_units(account_id, asin, days=8, db_path=db_path)
    days7 = [(datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
             for i in range(1, 8)]
    return sum(daily.get(d, 0) for d in days7)


def _our_month_avg(account_id, asin, month, db_path=DB_PATH):
    """Average trailing-window units for a calendar month ('YYYY-MM'). Needs >=2 samples."""
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT AVG(units) AS a, COUNT(*) AS n FROM velocity_history
        WHERE account_id=? AND asin=? AND substr(captured_at,1,7)=?
    """, (account_id, asin, month)).fetchone()
    conn.close()
    if row and row["n"] and row["n"] >= 2 and row["a"] is not None:
        return row["a"]
    return None


# ─────────────────────────────────────────────────────────────────────────────
# MARKET BASELINE  +  NET DELTA (ours minus market)
# ─────────────────────────────────────────────────────────────────────────────

def _num(v):
    """Strip £, commas, %, spaces → float. Returns None on blank/garbage."""
    if v is None:
        return None
    s = str(v).replace("£", "").replace(",", "").replace("%", "").strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def import_baseline_csv(path):
    """
    Load competitor seasonal baseline. Idempotent: re-importing a (category, month)
    replaces it. Expected headers (case-insensitive):
        category, month, avg_rank, avg_monthly_units, source_note
    `month` must be 'YYYY-MM'. Returns number of rows imported.
    """
    imported = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        norm = {k: (k or "").strip().lower() for k in (reader.fieldnames or [])}
        conn = get_db()
        for raw in reader:
            row = { norm.get(k, k): v for k, v in raw.items() }
            category = (row.get("category") or "").strip()
            month    = (row.get("month") or "").strip()
            if not category or not month:
                continue
            conn.execute("DELETE FROM market_baseline WHERE category=? AND month=?",
                         (category, month))
            conn.execute("""
                INSERT INTO market_baseline
                    (category, month, avg_rank, avg_monthly_units, source_note, imported_at)
                VALUES (?,?,?,?,?,?)
            """, (category, month, _num(row.get("avg_rank")),
                  _num(row.get("avg_monthly_units")),
                  (row.get("source_note") or "").strip(),
                  datetime.utcnow().isoformat()))
            imported += 1
        conn.commit()
        conn.close()
    log.info(f"Imported {imported} baseline rows from {path}")
    return imported


def _market_mom(category, db_path=DB_PATH):
    """Market's month-over-month % change in avg_monthly_units. None if not enough data."""
    now        = datetime.utcnow()
    this_month = now.strftime("%Y-%m")
    prev_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    conn = get_db(db_path)
    this = conn.execute("SELECT avg_monthly_units FROM market_baseline WHERE category=? AND month=?",
                        (category, this_month)).fetchone()
    prev = conn.execute("SELECT avg_monthly_units FROM market_baseline WHERE category=? AND month=?",
                        (category, prev_month)).fetchone()
    conn.close()
    if this and prev and prev["avg_monthly_units"] and prev["avg_monthly_units"] > 0:
        return (this["avg_monthly_units"] - prev["avg_monthly_units"]) / prev["avg_monthly_units"] * 100
    return None


def get_net_delta(account_id, asin, category, db_path=DB_PATH):
    """
    net_delta = OUR month-over-month velocity change (%) − MARKET's (%).
    Positive => we are outpacing the seasonal market tide.
    Returns None until we have both our own history (>=2 samples in each of this and
    last calendar month) AND a baseline for both months. Dormant, and safe, until then.
    """
    market = _market_mom(category or "", db_path=db_path)
    if market is None:
        return None
    now        = datetime.utcnow()
    this_month = now.strftime("%Y-%m")
    prev_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    ours_this = _our_month_avg(account_id, asin, this_month, db_path=db_path)
    ours_prev = _our_month_avg(account_id, asin, prev_month, db_path=db_path)
    if ours_this is None or ours_prev is None or ours_prev <= 0:
        return None
    our_mom = (ours_this - ours_prev) / ours_prev * 100
    return our_mom - market


# ─────────────────────────────────────────────────────────────────────────────
# RECOMMENDATIONS
# ─────────────────────────────────────────────────────────────────────────────

def save_recommendation(account_id, asin, current_price, root_rank, sub_rank,
                         velocity, baseline_delta_pct, signal_state, action,
                         recommended_price, reasoning, db_path=DB_PATH):
    conn = get_db(db_path)
    cur  = conn.execute("""
        INSERT INTO recommendations
            (account_id, asin, created_at, current_price, current_root_rank,
             current_sub_rank, current_velocity, baseline_delta_pct,
             signal_state, recommended_action, recommended_price, reasoning)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
    """, (account_id, asin, datetime.utcnow().isoformat(), current_price,
          root_rank, sub_rank, velocity, baseline_delta_pct,
          signal_state, action, recommended_price, reasoning))
    rec_id = cur.lastrowid
    conn.commit()
    conn.close()
    return rec_id


def decide_recommendation(rec_id, status, decided_price, decided_note,
                           account_id, asin, old_price, db_path=DB_PATH):
    """Apply approve / override / reject. Writes price_changes + updates current_price."""
    conn = get_db(db_path)
    conn.execute("""
        UPDATE recommendations
        SET status=?, decided_at=?, decided_price=?, decided_note=?
        WHERE id=?
    """, (status, datetime.utcnow().isoformat(), decided_price, decided_note, rec_id))

    if status in ("approved", "overridden") and decided_price:
        conn.execute("""
            INSERT INTO price_changes
                (account_id, asin, changed_at, old_price, new_price,
                 source_recommendation_id, applied_via)
            VALUES (?,?,?,?,?,?,'manual')
        """, (account_id, asin, datetime.utcnow().isoformat(),
              old_price, decided_price, rec_id))
        # Keep managed_asins.current_price in step with the approved price.
        conn.execute("UPDATE managed_asins SET current_price=? WHERE account_id=? AND asin=?",
                     (decided_price, account_id, asin))

    conn.commit()
    conn.close()


def get_pending_recommendations(db_path=DB_PATH):
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT r.*, m.sku, m.brand, m.floor_price, m.ceiling_price
        FROM recommendations r
        LEFT JOIN managed_asins m ON r.account_id=m.account_id AND r.asin=m.asin
        WHERE r.status='pending'
        ORDER BY r.created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recommendation(rec_id, db_path=DB_PATH):
    conn = get_db(db_path)
    row  = conn.execute("SELECT * FROM recommendations WHERE id=?", (rec_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ─────────────────────────────────────────────────────────────────────────────
# PRICE — our own source of truth
# ─────────────────────────────────────────────────────────────────────────────

def get_current_price(account_id, asin, fallback=None, db_path=DB_PATH):
    """
    Priority: last recorded price_change > managed_asins.current_price > fallback.
    Never reads a third-party crawl — price truth is what WE set.
    """
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT new_price FROM price_changes
        WHERE account_id=? AND asin=?
        ORDER BY changed_at DESC LIMIT 1
    """, (account_id, asin)).fetchone()
    if row and row["new_price"] is not None:
        conn.close()
        return row["new_price"]
    seed = conn.execute(
        "SELECT current_price FROM managed_asins WHERE account_id=? AND asin=?",
        (account_id, asin)).fetchone()
    conn.close()
    if seed and seed["current_price"] is not None:
        return seed["current_price"]
    return fallback


def days_since_last_change(account_id, asin, db_path=DB_PATH):
    conn = get_db(db_path)
    row  = conn.execute("""
        SELECT changed_at FROM price_changes
        WHERE account_id=? AND asin=?
        ORDER BY changed_at DESC LIMIT 1
    """, (account_id, asin)).fetchone()
    conn.close()
    if not row:
        return 9999
    last = datetime.fromisoformat(row["changed_at"])
    return (datetime.utcnow() - last).days


# ─────────────────────────────────────────────────────────────────────────────
# SEED — run once to populate managed_asins
# ─────────────────────────────────────────────────────────────────────────────

def seed_default_asins():
    """
    Seed known ASINs ONLY IF they don't already exist. This will NOT overwrite
    prices or settings you've edited in the dashboard — the database is the
    source of truth once a product exists. Safe to re-run any time.
    """
    _c = get_db()
    _exists = _c.execute(
        "SELECT 1 FROM managed_asins WHERE account_id=? AND asin=? LIMIT 1",
        ("M4Mart_UK", "B08VSBQWDZ")).fetchone()
    _c.close()
    if _exists:
        log.info("Managed ASINs already exist — skipping seed (dashboard is source of truth).")
        return
    upsert_managed_asin(
        account_id      = "M4Mart_UK",
        asin            = "B08VSBQWDZ",
        sku             = "BD-6372=P4",
        brand           = "Nod Off",
        root_category   = "Home & Kitchen",
        current_price   = 12.99,          # ← set to the live price before first run
        floor_price     = 11.75,
        ceiling_price   = 16.99,
        step_pct        = 0.05,
        raise_below     = 13500,
        lower_above     = 22000,
        hard_lower_above= 28000,
        min_confirm_days= 2,
        cooldown_days   = 3,
        notes           = "Standard pillow 4-pack — primary UK listing"
    )
    # Add more ASINs / the second account here as upsert_managed_asin(...) calls.
    log.info("Default ASINs seeded.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    init_schema()
    seed_default_asins()
    print("Database initialised and ASINs seeded.")


# ─────────────────────────────────────────────────────────────────────────────
# ACCOUNTS (managed from the front end; refresh_token pasted by the user)
# ─────────────────────────────────────────────────────────────────────────────

def upsert_account(account_id, seller_id, marketplace_id, refresh_token,
                   db_path=DB_PATH):
    conn = get_db(db_path)
    conn.execute("""
        INSERT INTO accounts (account_id, seller_id, marketplace_id, refresh_token, created_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(account_id) DO UPDATE SET
            seller_id=excluded.seller_id,
            marketplace_id=excluded.marketplace_id,
            refresh_token=COALESCE(excluded.refresh_token, accounts.refresh_token)
    """, (account_id, seller_id, marketplace_id, refresh_token,
          datetime.utcnow().isoformat()))
    conn.commit(); conn.close()


def get_accounts(db_path=DB_PATH):
    conn = get_db(db_path)
    rows = conn.execute("SELECT * FROM accounts WHERE active=1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_asin_active(account_id, asin, active, db_path=DB_PATH):
    conn = get_db(db_path)
    conn.execute("UPDATE managed_asins SET active=? WHERE account_id=? AND asin=?",
                 (1 if active else 0, account_id, asin))
    conn.commit(); conn.close()


def get_asin(account_id, asin, db_path=DB_PATH):
    conn = get_db(db_path)
    row = conn.execute("SELECT * FROM managed_asins WHERE account_id=? AND asin=?",
                       (account_id, asin)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_price_changes(account_id, asin, limit=50, db_path=DB_PATH):
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT * FROM price_changes WHERE account_id=? AND asin=?
        ORDER BY changed_at DESC LIMIT ?
    """, (account_id, asin, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_recommendation_history(account_id, asin, limit=50, db_path=DB_PATH):
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT * FROM recommendations WHERE account_id=? AND asin=?
        ORDER BY created_at DESC LIMIT ?
    """, (account_id, asin, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def has_open_duplicate(account_id, asin, action, signal_state, db_path=DB_PATH):
    """True if an identical recommendation is already pending (avoid stacking HOLDs)."""
    conn = get_db(db_path)
    row = conn.execute("""
        SELECT 1 FROM recommendations
        WHERE account_id=? AND asin=? AND status='pending'
          AND recommended_action=? AND signal_state=?
        LIMIT 1
    """, (account_id, asin, action, signal_state)).fetchone()
    conn.close()
    return row is not None


def supersede_pending_holds(account_id, asin, db_path=DB_PATH):
    """Auto-close stale pending HOLD/NEUTRAL recs when a real signal arrives."""
    conn = get_db(db_path)
    conn.execute("""
        UPDATE recommendations
        SET status='superseded', decided_at=?, decided_note='auto-closed by newer signal'
        WHERE account_id=? AND asin=? AND status='pending' AND recommended_action='HOLD'
    """, (datetime.utcnow().isoformat(), account_id, asin))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# HISTORIC BSR IMPORT (Trellis/Helium10 CSV — reference layer, not live data)
# ─────────────────────────────────────────────────────────────────────────────

def import_bsr_history_csv(account_id, asin, path, source="trellis"):
    """
    Import a historic BSR export with columns: Date, Best Seller Rank, Price.
    Dates DD/MM/YYYY. Placeholder ranks ('-', '-1') are skipped. Idempotent
    (re-import replaces same day). Stored separately from live sp-api readings.
    Returns number of usable rows imported.
    """
    import csv as _csv
    from datetime import datetime as _dt
    n = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = _csv.DictReader(f)
        norm = {(k or "").strip().lower(): k for k in (reader.fieldnames or [])}
        d_key = norm.get("date")
        r_key = norm.get("best seller rank") or norm.get("bsr") or norm.get("rank")
        p_key = norm.get("price")
        conn = get_db()
        for row in reader:
            raw_d = (row.get(d_key) or "").strip()
            raw_r = (row.get(r_key) or "").strip() if r_key else ""
            raw_p = (row.get(p_key) or "").strip() if p_key else ""
            if not raw_d:
                continue
            # parse date DD/MM/YYYY -> YYYY-MM-DD
            try:
                day = _dt.strptime(raw_d, "%d/%m/%Y").strftime("%Y-%m-%d")
            except ValueError:
                continue
            # rank: skip placeholders
            rank = None
            rc = raw_r.replace(",", "").strip()
            if rc and rc not in ("-", "-1"):
                try:
                    rank = int(float(rc))
                    if rank <= 0:
                        rank = None
                except ValueError:
                    rank = None
            price = _num(raw_p)
            if rank is None and price is None:
                continue
            conn.execute("""
                INSERT INTO rank_history_import (account_id, asin, day, root_rank, price, source)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(account_id, asin, day, source)
                DO UPDATE SET root_rank=excluded.root_rank, price=excluded.price
            """, (account_id, asin, day, rank, price, source))
            n += 1
        conn.commit()
        conn.close()
    log.info(f"Imported {n} historic BSR rows for {asin} from {path}")
    return n


def get_bsr_history_import(account_id, asin, db_path=DB_PATH):
    """{day: root_rank} for imported historic BSR (chart reference layer)."""
    conn = get_db(db_path)
    rows = conn.execute("""
        SELECT day, root_rank FROM rank_history_import
        WHERE account_id=? AND asin=? AND root_rank IS NOT NULL
        ORDER BY day ASC
    """, (account_id, asin)).fetchall()
    conn.close()
    return {r["day"]: r["root_rank"] for r in rows}
