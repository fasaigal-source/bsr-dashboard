"""module5_orders.py — Module 5a SP-API order reads for the ready-to-ship queue.

Read-only: lists unshipped FBM (MFN) orders and their line items via the Orders API, reusing
the same auth pattern as Module 1 (module1_collector.make_credentials / get_marketplace).
No PII is requested here (no buyer name/address — those need a Restricted Data Token and are
only pulled later for the packing slip), so the queue works without RDT.

Nothing is written and nothing is purchased here — this is the "what do I need to ship" read.
"""
import time
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
    """Line items for one order: [{order_item_id, sku, asin, qty, title}]."""
    client, _mid = _client(cfg, account)
    resp = client.get_order_items(order_id)
    items = []
    for it in (resp.payload or {}).get("OrderItems", []):
        items.append(dict(
            order_item_id=it.get("OrderItemId") or "",
            sku=it.get("SellerSKU") or "",
            asin=it.get("ASIN") or "",
            qty=int(it.get("QuantityOrdered") or 0),
            title=it.get("Title") or "",
        ))
    return items


# ── Merchant Fulfillment: rate quote (read-only; NO purchase here) ────────────

def load_ship_from():
    """Seller's dispatch address for Buy-Shipping quotes. From env SHIP_FROM (JSON) —
    Amazon requires Name, AddressLine1, City, PostalCode, CountryCode (Phone/Email help).
    Returns the dict or None if unset."""
    import os
    import json
    raw = os.environ.get("SHIP_FROM")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        log.warning("SHIP_FROM is not valid JSON: %s", e)
        return None


def build_shipment_request(order_id, items, ship_from, weight_g, length_cm, width_cm, height_cm):
    """ShipmentRequestDetails for get_eligible_shipment_services / create_shipment.
    Ship-TO is derived by Amazon from the order id, so no buyer PII is sent here."""
    return {
        "AmazonOrderId": order_id,
        "ItemList": [{"OrderItemId": it["order_item_id"], "Quantity": int(it.get("qty") or 1)}
                     for it in items if it.get("order_item_id")],
        "ShipFromAddress": ship_from,
        "PackageDimensions": {"Length": float(length_cm), "Width": float(width_cm),
                              "Height": float(height_cm), "Unit": "centimeters"},
        "Weight": {"Value": int(weight_g), "Unit": "g"},
        "ShippingServiceOptions": {"DeliveryExperience": "DeliveryConfirmationWithoutSignature",
                                   "CarrierWillPickUp": False, "LabelFormat": "PDF"},
    }


def eligible_services(cfg, account, request_details):
    """Call getEligibleShipmentServices. Returns (services, notes) where services is a list
    of {id, offer_id, carrier, name, amount, currency, earliest, latest, options} and notes
    is a list of unavailable/terms messages. Raises on hard API failure."""
    client, _mid = _client(cfg, account)
    resp = client.get_eligible_shipment_services(request_details)
    p = resp.payload or {}
    services = []
    for s in p.get("ShippingServiceList", []):
        rate = s.get("Rate") or {}
        services.append(dict(
            id=s.get("ShippingServiceId"),
            offer_id=s.get("ShippingServiceOfferId"),
            carrier=s.get("CarrierName"),
            name=s.get("ShippingServiceName"),
            amount=rate.get("Amount"),
            currency=rate.get("CurrencyCode") or "GBP",
            earliest=(s.get("EarliestEstimatedDeliveryDate") or "")[:10],
            latest=(s.get("LatestEstimatedDeliveryDate") or "")[:10],
        ))
    notes = []
    for c in p.get("TemporarilyUnavailableCarrierList", []) or []:
        notes.append(f"temporarily unavailable: {c.get('CarrierName')}")
    for c in p.get("TermsAndConditionsNotAcceptedCarrierList", []) or []:
        notes.append(f"terms not accepted: {c.get('CarrierName')} — accept in Seller Central once")
    return services, notes


def pick_cheapest(services, deliver_by=None):
    """Cheapest service that still meets the delivery deadline (LatestEstimatedDeliveryDate
    <= deliver_by). If none meet it (or no deadline), the cheapest overall, flagged."""
    priced = [s for s in services if s.get("amount") is not None]
    if not priced:
        return None, False
    if deliver_by:
        in_time = [s for s in priced if (s.get("latest") or "9999") <= deliver_by]
        if in_time:
            return min(in_time, key=lambda s: s["amount"]), True
    return min(priced, key=lambda s: s["amount"]), False if deliver_by else True


def sync_orders(cfg, account, days=30):
    """Full fetch for caching: unshipped orders + each order's items. Paced within the
    getOrderItems burst. Returns the order list with an `items` key on each."""
    orders = list_unshipped(cfg, account, days=days)
    for o in orders:
        try:
            o["items"] = order_items(cfg, account, o["order_id"])
        except Exception as e:
            log.warning("items fetch failed for %s: %s", o.get("order_id"), e)
            o["items"] = []
        time.sleep(0.3)
    return orders
