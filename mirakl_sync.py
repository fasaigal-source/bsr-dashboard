"""mirakl_sync.py — Module 5 sync logic (orders → pl_line_items, ledger → mirakl_transactions).

Pure orchestration over mirakl_client (network) + mirakl_db (storage). Idempotent:
re-running a window inserts zero duplicates.

P&L TREATMENT — deliberately conservative until §6 is confirmed:
  * Sale side: Mirakl's line total is treated as the customer-paid INC-VAT amount;
    sale_price_exvat is derived at the UK standard rate (÷1.2, override per call).
    This is the well-defined part (standard-rated pillows/cushions).
  * Fees / net: COMMISSION and its VAT are NOT wired into net_profit here — that is
    the open question (commission inc/ex-VAT, how it nets against VAT registration).
    Mirakl lines land with cogs/postage/fees/net LEFT NULL → they show as PROVISIONAL
    in the P&L (same mechanism as Amazon orders missing COGS/postage). The raw fee
    rows live in mirakl_transactions for when the formula is confirmed.
Nothing here writes to Amazon or auto-ships; writes go through the dry-run-gated client.
"""
import logging
from datetime import datetime, timezone

import mirakl_db
import mirakl_client

log = logging.getLogger(__name__)
DEFAULT_VAT_RATE = 0.20   # UK standard rate for the sale-side ex-VAT derivation


def _now():
    return datetime.now(timezone.utc).isoformat()


def _g(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return default


def _num(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("£", "").strip())
    except (ValueError, TypeError):
        return None


# ── order → canonical line items ─────────────────────────────────────────────

def map_order_to_line_items(account, order, vat_rate=DEFAULT_VAT_RATE):
    """Turn one Mirakl order dict into pl_line_items row-dicts (one per order line).
    Revenue + sale-side VAT only; cogs/postage/fees/net stay NULL (provisional)."""
    import json
    order_id = str(_g(order, "order_id", "id", default=""))
    posted = _g(order, "created_date", "date_created", "order_date", "acceptance_decision_date")
    currency = _g(order, "currency_iso_code", "currency", default="GBP")
    lines = _g(order, "order_lines", "orderLines", "lines", default=[]) or []
    rows = []
    for ln in lines:
        line_id = str(_g(ln, "order_line_id", "id", "offer_id", default=order_id + "-L"))
        qty = int(_num(_g(ln, "quantity", "qty", default=1)) or 1)
        # per-line total the customer paid (inc VAT) — prefer an explicit total, else price×qty
        inc = _num(_g(ln, "total_price", "price", "line_total"))
        if inc is None:
            unit = _num(_g(ln, "unit_price", "price_unit"))
            inc = round((unit or 0) * qty, 2) if unit is not None else None
        exvat = round(inc / (1 + vat_rate), 2) if inc is not None else None
        out_vat = round(inc - exvat, 2) if inc is not None else None
        sku = _g(ln, "offer_sku", "shop_sku", "product_sku", "sku")
        rows.append({
            "account_id": account, "order_id": order_id, "order_item_id": line_id,
            "posted_date": posted, "asin": None, "sku": sku, "quantity": qty,
            "sale_price_incvat": inc, "sale_price_exvat": exvat, "output_vat": out_vat,
            "currency": currency, "channel": "mirakl", "settlement_status": "pending",
            "raw_event_json": json.dumps({"order_line": ln}, default=str),
        })
    return rows


def _upsert_line_item(conn, r):
    """Insert/refresh a Mirakl line in pl_line_items WITHOUT clobbering cogs/postage/net
    (those are owned by the COGS reprocess / the — deferred — P&L formula)."""
    now = _now()
    conn.execute("""
        INSERT INTO pl_line_items
            (account_id, order_id, order_item_id, posted_date, asin, sku, quantity,
             sale_price_incvat, sale_price_exvat, output_vat, currency, channel,
             settlement_status, raw_event_json, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (account_id, order_id, order_item_id) DO UPDATE SET
            posted_date=excluded.posted_date, sku=excluded.sku, quantity=excluded.quantity,
            sale_price_incvat=excluded.sale_price_incvat, sale_price_exvat=excluded.sale_price_exvat,
            output_vat=excluded.output_vat, currency=excluded.currency, channel=excluded.channel,
            raw_event_json=excluded.raw_event_json, updated_at=excluded.updated_at
    """, (r["account_id"], r["order_id"], r["order_item_id"], r["posted_date"], r["asin"],
          r["sku"], r["quantity"], r["sale_price_incvat"], r["sale_price_exvat"],
          r["output_vat"], r["currency"], r["channel"], r["settlement_status"],
          r["raw_event_json"], now, now))


def pull_orders(account, since=None, vat_rate=DEFAULT_VAT_RATE, db_path=mirakl_db.DB_PATH):
    """Fetch new/updated orders and upsert them into pl_line_items + mirakl_order_state.
    Returns {orders, line_items}. Idempotent."""
    orders = mirakl_client.get_new_orders(account, since=since)
    conn = mirakl_db.get_db(db_path)
    n_lines = 0
    for o in orders:
        order_id = str(_g(o, "order_id", "id", default=""))
        state = _g(o, "order_state", "state")
        mirakl_db.upsert_order_state(account, order_id, state=state, raw=o, db_path=db_path)
        for r in map_order_to_line_items(account, o, vat_rate=vat_rate):
            if not r["order_id"]:
                continue
            _upsert_line_item(conn, r)
            n_lines += 1
    conn.commit()
    conn.close()
    log.info("mirakl pull_orders(%s): %d order(s), %d line(s)", account, len(orders), n_lines)
    return {"orders": len(orders), "line_items": n_lines}


def sync_transactions(account, date_from, date_to, db_path=mirakl_db.DB_PATH):
    """Pull the financial ledger for the window and upsert into mirakl_transactions.
    Returns {pulled, written, duplicate}. Idempotent (dedup on re-run)."""
    txns = mirakl_client.get_transactions(account, date_from, date_to)
    written = dup = 0
    for t in txns:
        if mirakl_db.upsert_transaction(account, t, db_path=db_path):
            written += 1
        else:
            dup += 1
    log.info("mirakl sync_transactions(%s %s→%s): pulled=%d written=%d dup=%d",
             account, date_from, date_to, len(txns), written, dup)
    return {"pulled": len(txns), "written": written, "duplicate": dup}


def auto_accept(account, db_path=mirakl_db.DB_PATH):
    """Accept every order sitting in WAITING_ACCEPTANCE. DEFAULT ASSUMPTION is full
    auto-accept (always-in-stock model) — but this only advances state on a real 'ok';
    in dry-run it logs intent and leaves the order in WAITING. Do NOT enable live until
    the 'is full auto-accept correct / any manual-review gate?' question is answered.
    Returns {checked, accepted, dry_run}."""
    waiting = mirakl_db.get_orders_in_state(account, mirakl_db.STATE_WAITING, db_path=db_path)
    accepted = dry = 0
    for o in waiting:
        res = mirakl_client.accept_order(account, o["order_id"])
        if res.get("result") == "ok":
            mirakl_db.upsert_order_state(account, o["order_id"],
                                         state=mirakl_db.STATE_ACCEPTED, accepted=True, db_path=db_path)
            accepted += 1
        elif res.get("result") == "dry_run":
            dry += 1
    log.info("mirakl auto_accept(%s): checked=%d accepted=%d dry_run=%d",
             account, len(waiting), accepted, dry)
    return {"checked": len(waiting), "accepted": accepted, "dry_run": dry}
