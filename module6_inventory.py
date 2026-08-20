"""module6_inventory.py — Module 6 data layer: live Amazon inventory snapshot.

Pulls the merchant listings report (GET_MERCHANT_LISTINGS_ALL_DATA) from SP-API and
caches it in a table so the /inventory page loads instantly, with a "Refresh from
Amazon" button that re-pulls in the background (a report can take 30s–a few minutes,
longer than a web request should block).

Flow (per account):
  1. create_report(GET_MERCHANT_LISTINGS_ALL_DATA)      -> reportId
  2. poll get_report(reportId) until processingStatus DONE -> reportDocumentId
  3. get_report_document(docId, download=True)          -> decoded TSV text
  4. parse the TSV, resolve each seller-sku to its canonical, upsert a snapshot row

Dual-backend (Postgres on Railway / SQLite locally) via db.py, same conventions as the
rest of the project. Credentials come from module5_orders.load_spapi_config (DB settings
first, then Railway env vars) — nothing secret lives here.
"""
import csv
import io
import time
import logging
import threading
from datetime import datetime, timezone

import db

log = logging.getLogger(__name__)
DB_PATH = "bsr_history.db"
MARKETPLACE_ID = "A1F83G8C2ARO7P"   # Amazon UK
REPORT_TYPE = "GET_MERCHANT_LISTINGS_ALL_DATA"


def _now():
    return datetime.now(timezone.utc).isoformat()


def get_db(db_path=DB_PATH):
    conn = db.connect(db_path)
    if not db.is_postgres():
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── schema ───────────────────────────────────────────────────────────────────

def init_inventory_schema(db_path=DB_PATH):
    conn = get_db(db_path)
    pg = db.is_postgres()
    MONEY = "NUMERIC(14,4)" if pg else "REAL"
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS inventory_snapshot (
            account_id    TEXT NOT NULL,
            seller_sku    TEXT NOT NULL,
            canonical_sku TEXT,
            asin          TEXT,
            title         TEXT,
            price         {MONEY},
            quantity      INTEGER,
            channel       TEXT,
            status        TEXT,
            fetched_at    TEXT,
            PRIMARY KEY (account_id, seller_sku)
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_inv_canon ON inventory_snapshot(canonical_sku)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_inv_asin ON inventory_snapshot(asin)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inventory_refresh_status (
            account_id  TEXT PRIMARY KEY,
            state       TEXT,
            message     TEXT,
            rows        INTEGER,
            started_at  TEXT,
            finished_at TEXT
        )""")
    if hasattr(conn, "commit"):
        conn.commit()
    log.info("Module 6 inventory schema initialised.")


# ── status ───────────────────────────────────────────────────────────────────

def _set_status(account_id, **kw):
    conn = get_db()
    cols = ["account_id"] + list(kw.keys())
    vals = [account_id] + list(kw.values())
    ph = ",".join("?" for _ in cols)
    upd = ",".join(f"{k}=?" for k in kw.keys())
    conn.execute(
        f"INSERT INTO inventory_refresh_status ({','.join(cols)}) VALUES ({ph}) "
        f"ON CONFLICT(account_id) DO UPDATE SET {upd}",
        vals + list(kw.values()))
    if hasattr(conn, "commit"):
        conn.commit()


def get_status(account_id=None):
    conn = get_db()
    if account_id:
        rows = conn.execute("SELECT * FROM inventory_refresh_status WHERE account_id=?",
                            (account_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM inventory_refresh_status").fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        out[d["account_id"]] = d
    return out


def is_running():
    return any((s.get("state") == "running") for s in get_status().values())


# ── SP-API report pull ───────────────────────────────────────────────────────

def _reports_client(cfg, account):
    from sp_api.api import Reports
    import module1_collector as m1
    creds = m1.make_credentials(cfg, account)
    mid = account.get("marketplace_id") or MARKETPLACE_ID
    return Reports(credentials=creds, marketplace=m1.get_marketplace(mid)), mid


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return None


def parse_listings_tsv(text):
    """GET_MERCHANT_LISTINGS_ALL_DATA is a tab-separated report with a header row.
    We keep seller-sku, asin1, item-name, price, quantity, fulfillment-channel, status."""
    out = []
    if not text:
        return out
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    for row in reader:
        # header names are lower-case-hyphenated; be tolerant of minor variants
        g = {(k or "").strip().lower(): v for k, v in row.items()}
        sku = (g.get("seller-sku") or g.get("sku") or "").strip()
        if not sku:
            continue
        out.append({
            "seller_sku": sku,
            "asin": (g.get("asin1") or g.get("asin") or "").strip(),
            "title": (g.get("item-name") or "").strip(),
            "price": _num(g.get("price")),
            "quantity": _int(g.get("quantity")),
            "channel": (g.get("fulfillment-channel") or "").strip() or "DEFAULT",
            "status": (g.get("status") or "").strip(),
        })
    return out


def fetch_listings(cfg, account, poll_timeout=240, interval=5):
    """Run the merchant-listings report end to end and return parsed rows.
    Raises on report FATAL/CANCELLED or timeout."""
    client, mid = _reports_client(cfg, account)
    created = client.create_report(reportType=REPORT_TYPE, marketplaceIds=[mid])
    report_id = created.payload.get("reportId")
    if not report_id:
        raise RuntimeError(f"create_report returned no reportId: {created.payload}")
    doc_id = None
    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        g = client.get_report(report_id).payload or {}
        st = g.get("processingStatus")
        if st == "DONE":
            doc_id = g.get("reportDocumentId")
            break
        if st in ("CANCELLED", "FATAL"):
            raise RuntimeError(f"report {report_id} ended {st}")
        time.sleep(interval)
    if not doc_id:
        raise RuntimeError(f"report {report_id} did not finish within {poll_timeout}s")
    doc = client.get_report_document(doc_id, download=True)
    return parse_listings_tsv(doc.payload.get("document", ""))


# ── refresh + upsert ─────────────────────────────────────────────────────────

def _resolve_canonical(sku):
    try:
        import pl_cogs
        return pl_cogs.resolve_to_canonical(sku)
    except Exception:
        return sku


def upsert_rows(account_id, rows):
    """Replace this account's snapshot with the freshly pulled rows."""
    conn = get_db()
    now = _now()
    conn.execute("DELETE FROM inventory_snapshot WHERE account_id=?", (account_id,))
    for r in rows:
        conn.execute(
            "INSERT INTO inventory_snapshot "
            "(account_id, seller_sku, canonical_sku, asin, title, price, quantity, channel, status, fetched_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(account_id, seller_sku) DO UPDATE SET "
            "canonical_sku=excluded.canonical_sku, asin=excluded.asin, title=excluded.title, "
            "price=excluded.price, quantity=excluded.quantity, channel=excluded.channel, "
            "status=excluded.status, fetched_at=excluded.fetched_at",
            (account_id, r["seller_sku"], _resolve_canonical(r["seller_sku"]), r.get("asin"),
             r.get("title"), r.get("price"), r.get("quantity"), r.get("channel"),
             r.get("status"), now))
    if hasattr(conn, "commit"):
        conn.commit()
    return len(rows)


def refresh_account(cfg, account):
    """Blocking single-account refresh. Updates status as it goes."""
    aid = account.get("account_id") or "account"
    _set_status(aid, state="running", message="requesting report…", started_at=_now(),
                finished_at=None)
    try:
        rows = fetch_listings(cfg, account)
        n = upsert_rows(aid, rows)
        _set_status(aid, state="done", message=f"{n} listings", rows=n, finished_at=_now())
        return n
    except Exception as e:
        log.warning("inventory refresh failed for %s: %s", aid, e)
        _set_status(aid, state="error", message=str(e)[:300], finished_at=_now())
        raise


def _refresh_all_worker():
    try:
        import module5_orders
        cfg, accounts = module5_orders.load_spapi_config()
    except Exception as e:
        log.warning("inventory refresh: cannot load SP-API config: %s", e)
        _set_status("account", state="error", message=f"no SP-API config: {e}", finished_at=_now())
        return
    if not accounts:
        _set_status("account", state="error", message="no configured SP-API account", finished_at=_now())
        return
    for account in accounts:
        try:
            refresh_account(cfg, account)
        except Exception:
            pass   # status already recorded per account


def start_refresh_async():
    """Kick off a background refresh over every configured account. No-op if one is
    already running. Returns True if started."""
    if is_running():
        return False
    threading.Thread(target=_refresh_all_worker, daemon=True).start()
    return True


# ── reads for the page ───────────────────────────────────────────────────────

def list_inventory(q=None, limit=2000):
    conn = get_db()
    sql = ("SELECT account_id, seller_sku, canonical_sku, asin, title, price, quantity, "
           "channel, status, fetched_at FROM inventory_snapshot")
    params = []
    if q:
        like = f"%{q.strip()}%"
        sql += " WHERE seller_sku LIKE ? OR canonical_sku LIKE ? OR asin LIKE ? OR title LIKE ?"
        params = [like, like, like, like]
    sql += " ORDER BY (quantity IS NULL), quantity ASC, seller_sku ASC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def summary():
    conn = get_db()
    r = conn.execute(
        "SELECT COUNT(*) AS skus, COALESCE(SUM(quantity),0) AS units, "
        "SUM(CASE WHEN quantity=0 OR quantity IS NULL THEN 1 ELSE 0 END) AS oos, "
        "MAX(fetched_at) AS updated FROM inventory_snapshot").fetchone()
    return dict(r) if r else {"skus": 0, "units": 0, "oos": 0, "updated": None}


# ── sales velocity / prediction ──────────────────────────────────────────────

def velocity_by_asin(windows=(7, 14, 30)):
    """Units sold per ASIN over trailing N-day windows, from the P&L line-item ledger.
    Returns {asin: {7: n, 14: n, 30: n}}. Only counts positive-quantity sale lines
    (refund lines are excluded). posted_date is ISO text, so lexical >= is a valid
    date comparison."""
    from datetime import datetime, timezone, timedelta
    conn = get_db()
    now = datetime.now(timezone.utc)
    wmax = max(windows)
    cutoffs = {w: (now - timedelta(days=w)).strftime("%Y-%m-%dT%H:%M:%SZ") for w in windows}
    cut_max = (now - timedelta(days=wmax)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sel = ", ".join(
        f"SUM(CASE WHEN posted_date >= ? THEN quantity ELSE 0 END) AS w{w}" for w in windows)
    params = [cutoffs[w] for w in windows] + [cut_max]
    rows = conn.execute(
        f"SELECT asin, {sel} FROM pl_line_items "
        f"WHERE asin IS NOT NULL AND asin <> '' AND quantity > 0 AND posted_date >= ? "
        f"GROUP BY asin", params).fetchall()
    out = {}
    for r in rows:
        d = dict(r)
        out[d["asin"]] = {w: int(d.get(f"w{w}") or 0) for w in windows}
    return out


def _cover_status(qty, daily_rate):
    """Days of cover + a status bucket from stock on hand and a daily sales rate."""
    if qty is None:
        return None, "unknown"
    if qty <= 0:
        return 0.0, "out"
    if not daily_rate or daily_rate <= 0:
        return None, "idle"          # no recent sales — can't predict
    cover = qty / daily_rate
    if cover < 7:
        return cover, "reorder"
    if cover < 14:
        return cover, "low"
    return cover, "ok"


def enrich_with_prediction(rows, windows=(7, 14, 30)):
    """Attach u7/u14/u30, daily rate, days-of-cover and a status bucket to each
    inventory row (in place) using ASIN-keyed velocity. Daily rate uses the longest
    window available with sales (30d preferred) for a stable estimate."""
    vel = velocity_by_asin(windows)
    reorder = 0
    for r in rows:
        v = vel.get(r.get("asin")) or {}
        for w in windows:
            r[f"u{w}"] = v.get(w, 0)
        # daily rate = the FASTEST pace across the windows, so a recent surge drives
        # the reorder flag instead of being diluted by a long, quiet 30-day average.
        rates = [v[w] / float(w) for w in windows if v.get(w)]
        daily = max(rates) if rates else 0.0
        r["daily_rate"] = round(daily, 2)
        cover, status = _cover_status(r.get("quantity"), daily)
        r["days_cover"] = cover
        r["stock_status"] = status
        if status == "reorder" or status == "out":
            reorder += 1
    return reorder
