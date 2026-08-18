"""module5_orders.py — Module 5a SP-API order reads for the ready-to-ship queue.

Read-only: lists unshipped FBM (MFN) orders and their line items via the Orders API, reusing
the same auth pattern as Module 1 (module1_collector.make_credentials / get_marketplace).
No PII is requested here (no buyer name/address — those need a Restricted Data Token and are
only pulled later for the packing slip), so the queue works without RDT.

Nothing is written and nothing is purchased here — this is the "what do I need to ship" read.
"""
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)
MARKETPLACE_ID = "A1F83G8C2ARO7P"   # Amazon UK


def load_spapi_config():
    """(cfg, accounts) for SP-API, working both locally and on Railway.

    Local dev: config.json (via pl_tracker) — used if it carries LWA creds + a token.
    Railway (no config.json): built from environment variables —
        SPAPI_LWA_APP_ID          your LWA app id
        SPAPI_LWA_CLIENT_SECRET   your LWA client secret
        SPAPI_ACCOUNTS            JSON list, e.g.
            [{"account_id":"M4Mart_UK","refresh_token":"Atzr|...","marketplace_id":"A1F83G8C2ARO7P"}]
    Returns ({"credentials":{lwa_app_id,lwa_client_secret}, "accounts":[...]}, accounts-with-token).
    Secrets live only in Railway variables — never in the repo."""
    import os
    import json
    import pl_tracker
    cfg = pl_tracker.load_config()
    if cfg.get("credentials", {}).get("lwa_app_id"):
        accts = [a for a in pl_tracker.get_effective_accounts(cfg) if a.get("refresh_token")]
        if accts:
            return cfg, accts
    app_id = os.environ.get("SPAPI_LWA_APP_ID")
    secret = os.environ.get("SPAPI_LWA_CLIENT_SECRET")
    raw = os.environ.get("SPAPI_ACCOUNTS")
    if app_id and secret and raw:
        try:
            accounts = json.loads(raw)
        except Exception as e:
            log.warning("SPAPI_ACCOUNTS is not valid JSON: %s", e)
            accounts = []
        for a in accounts:
            a.setdefault("marketplace_id", MARKETPLACE_ID)
        cfg = {"credentials": {"lwa_app_id": app_id, "lwa_client_secret": secret}, "accounts": accounts}
        return cfg, [a for a in accounts if a.get("refresh_token")]
    return cfg, []


def _client(cfg, account):
    from sp_api.api import Orders
    import module1_collector as m1
    creds = m1.make_credentials(cfg, account)
    mid = account.get("marketplace_id") or MARKETPLACE_ID
    return Orders(credentials=creds, marketplace=m1.get_marketplace(mid)), mid


def list_unshipped(cfg, account, days=30, limit=150):
    """Unshipped / partially-shipped MFN orders in the last `days`. One get_orders call
    (paginated), no per-item calls — fast enough for a page load. Returns order summaries;
    line items are fetched separately (order_items) only when preparing one order."""
    client, mid = _client(cfg, account)
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kwargs = dict(CreatedAfter=start, MarketplaceIds=[mid],
                  OrderStatuses=["Unshipped", "PartiallyShipped"],
                  FulfillmentChannels=["MFN"])
    out, token = [], None
    while True:
        resp = client.get_orders(NextToken=token) if token else client.get_orders(**kwargs)
        p = resp.payload or {}
        for o in p.get("Orders", []):
            tot = o.get("OrderTotal") or {}
            out.append(dict(
                order_id=o.get("AmazonOrderId"),
                purchase_date=(o.get("PurchaseDate") or "")[:10],
                status=o.get("OrderStatus"),
                ship_by=(o.get("LatestShipDate") or "")[:10],
                deliver_by=(o.get("LatestDeliveryDate") or "")[:10],
                n_unshipped=o.get("NumberOfItemsUnshipped"),
                service=o.get("ShipmentServiceLevelCategory") or "",
                prime=bool(o.get("IsPrime")),
                total=tot.get("Amount"),
                currency=tot.get("CurrencyCode") or "GBP",
            ))
            if len(out) >= limit:
                return out
        token = p.get("NextToken")
        if not token:
            break
    return out


def order_items(cfg, account, order_id):
    """Line items for one order: [{sku, asin, qty, title}]. One get_order_items call."""
    client, _mid = _client(cfg, account)
    resp = client.get_order_items(order_id)
    items = []
    for it in (resp.payload or {}).get("OrderItems", []):
        items.append(dict(
            sku=it.get("SellerSKU") or "",
            asin=it.get("ASIN") or "",
            qty=int(it.get("QuantityOrdered") or 0),
            title=it.get("Title") or "",
        ))
    return items
