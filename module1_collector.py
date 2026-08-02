"""
module1_collector.py — SP-API data collection for Module 1

Collects:
  1. Rank snapshot via getCatalogItem (salesRanks node)
  2. Trailing 7-day order velocity via Orders API

v2: root-rank detection no longer blindly labels the first displayGroupRank as
"root". It only sets root_rank when a node's title actually matches the configured
root_category (e.g. "Home & Kitchen"). If the API doesn't return the root — a known
SP-API quirk — root_rank stays None and the decision logic falls back to sub_rank +
velocity. The full salesRanks payload is always logged so the first run tells us,
empirically, what comes back for each ASIN.
"""

import json
import time
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

try:
    from sp_api.api import CatalogItems, Orders, Reports, Products
    from sp_api.base import Marketplaces, SellingApiException
except ImportError:
    raise SystemExit("Missing library — run: pip install python-amazon-sp-api")


# ─────────────────────────────────────────────────────────────────────────────
# CREDENTIALS
# ─────────────────────────────────────────────────────────────────────────────

def make_credentials(cfg, account):
    """Build credential dict for python-amazon-sp-api (LWA-only, no AWS)."""
    return {
        "refresh_token":     account["refresh_token"],
        "lwa_app_id":        cfg["credentials"]["lwa_app_id"],
        "lwa_client_secret": cfg["credentials"]["lwa_client_secret"],
    }


def get_marketplace(marketplace_id):
    mapping = {m.value: m for m in Marketplaces}
    return mapping.get(marketplace_id, Marketplaces.UK)


# ─────────────────────────────────────────────────────────────────────────────
# 1. RANK — getCatalogItem
# ─────────────────────────────────────────────────────────────────────────────

def fetch_ranks(asin, root_category, credentials, marketplace):
    """
    Call getCatalogItem and parse salesRanks.

    Returns dict:
        root_rank, root_category  — only set if a node title matches root_category
        sub_rank, sub_category    — best (lowest) classification rank that isn't the root
        raw_json                  — full salesRanks payload (always logged)
    """
    result = {"root_rank": None, "root_category": None,
              "sub_rank": None, "sub_category": None, "raw_json": None}

    try:
        client   = CatalogItems(credentials=credentials, marketplace=marketplace)
        response = client.get_catalog_item(asin=asin, includedData=["salesRanks", "summaries"])
        payload     = response.payload or {}
        sales_ranks = payload.get("salesRanks", [])
        result["raw_json"] = sales_ranks

        log.info(f"  [{asin}] Raw salesRanks: {json.dumps(sales_ranks)}")

        # Flatten every rank node we got, tagging where it came from.
        # Handles BOTH payload shapes:
        #   2020-12-01 catalog version:  {"ranks": [{"title","rank",...}]}
        #   2022-04-01 catalog version:  classificationRanks / displayGroupRanks
        nodes = []  # (title, rank, kind)  kind = 'class' | 'group' | 'rank'
        for sr in sales_ranks:
            for r in sr.get("ranks", []):
                if r.get("rank") is not None:
                    nodes.append((r.get("title", ""), r["rank"], "rank"))
            for r in sr.get("classificationRanks", []):
                if r.get("rank") is not None:
                    nodes.append((r.get("title", ""), r["rank"], "class"))
            for r in sr.get("displayGroupRanks", []):
                if r.get("rank") is not None:
                    nodes.append((r.get("title", ""), r["rank"], "group"))

        # Root: ONLY when a title matches the configured root category.
        if root_category:
            rc = root_category.lower()
            for title, rank, _ in nodes:
                if rc in (title or "").lower():
                    result["root_rank"]     = rank
                    result["root_category"] = title
                    break

        # Sub: lowest-numbered classification rank that isn't the chosen root.
        subs = [(t, rk) for (t, rk, k) in nodes
                if k in ("class", "rank") and t != result["root_category"]]
        if subs:
            t, rk = min(subs, key=lambda x: x[1])
            result["sub_rank"], result["sub_category"] = rk, t

        if result["root_rank"] is None:
            log.warning(f"  [{asin}] Root category '{root_category}' NOT in API response "
                        f"— using sub_rank {result['sub_rank']} ({result['sub_category']}).")
        else:
            log.info(f"  [{asin}] root={result['root_rank']:,} ({result['root_category']}) | "
                     f"sub={result['sub_rank']} ({result['sub_category']})")

    except SellingApiException as e:
        log.error(f"  [{asin}] SP-API error fetching ranks: {e}")
    except Exception as e:
        log.error(f"  [{asin}] Unexpected error fetching ranks: {e}", exc_info=True)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# 2. VELOCITY — trailing 7-day units from Orders API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_velocity(asin, marketplace_id, credentials, marketplace, days=7):
    """
    Count units of `asin` sold in the last `days` days via the Orders API.
    Returns (units, window_start_iso, window_end_iso).
    Orders API is heavily rate-limited, so we pace the calls.
    """
    window_end   = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=days)
    units = 0

    try:
        client = Orders(credentials=credentials, marketplace=marketplace)
        kwargs = dict(
            CreatedAfter   = window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            MarketplaceIds = [marketplace_id],
            OrderStatuses  = ["Shipped", "Unshipped", "PartiallyShipped"],
        )
        next_token = None
        while True:
            resp = client.get_orders(NextToken=next_token) if next_token else client.get_orders(**kwargs)
            payload = resp.payload or {}
            for order in payload.get("Orders", []):
                order_id = order.get("AmazonOrderId")
                if not order_id:
                    continue
                # getOrderItems allows ~0.5 req/sec (burst 30): pace at 2.1s
                # and retry on 429 so throttled orders are never silently skipped
                # (a skipped order = undercounted velocity = wrong decision input).
                fetched = False
                for attempt in range(5):
                    try:
                        time.sleep(2.1)
                        items_resp = client.get_order_items(order_id)
                        for item in (items_resp.payload or {}).get("OrderItems", []):
                            if item.get("ASIN") == asin:
                                units += int(item.get("QuantityOrdered", 0))
                        fetched = True
                        break
                    except SellingApiException as e:
                        if "QuotaExceeded" in str(e) or "429" in str(e):
                            wait = 5 * (attempt + 1)
                            log.info(f"  Throttled on {order_id}; retrying in {wait}s "
                                     f"(attempt {attempt + 1}/5)")
                            time.sleep(wait)
                        else:
                            log.warning(f"  Could not fetch items for order {order_id}: {e}")
                            break
                    except Exception as e:
                        log.warning(f"  Could not fetch items for order {order_id}: {e}")
                        break
                if not fetched:
                    log.warning(f"  GAVE UP on order {order_id} after retries — "
                                f"velocity may be undercounted this run.")
            next_token = payload.get("NextToken")
            if not next_token:
                break
            time.sleep(1)

        log.info(f"  [{asin}] Velocity: {units} units in last {days} days")

    except SellingApiException as e:
        log.error(f"  [{asin}] SP-API error fetching velocity: {e}")
    except Exception as e:
        log.error(f"  [{asin}] Unexpected error fetching velocity: {e}", exc_info=True)

    return units, window_start.isoformat(), window_end.isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# 2b. VELOCITY VIA ORDERS REPORT — one request per ACCOUNT, all ASINs at once
# ─────────────────────────────────────────────────────────────────────────────

def fetch_velocities_report(marketplace_id, credentials, marketplace, days=14):
    """
    Request GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL for the trailing
    window, wait for Amazon to generate it, download it, and count units per ASIN
    PER DAY (bucketed by purchase-date), so velocity has daily shape instead of
    a single 7-day blob.

    Returns ({asin: {"YYYY-MM-DD": units}} or None on failure,
             window_start_iso, window_end_iso).

    Why: the Orders API only returns order headers; discovering which orders
    contain a given ASIN needs one getOrderItems call per order at ~0.5 req/s.
    The report is one file per account with asin + quantity on every line —
    covers every ASIN in the account, no throttling, minutes not hours.
    """
    import gzip
    import requests as _rq

    window_end   = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=days)
    w_start_iso, w_end_iso = window_start.isoformat(), window_end.isoformat()

    try:
        client = Reports(credentials=credentials, marketplace=marketplace)

        res = client.create_report(
            reportType="GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL",
            dataStartTime=window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            marketplaceIds=[marketplace_id],
        )
        report_id = res.payload["reportId"]
        log.info(f"  Orders report requested (id {report_id}); waiting for Amazon "
                 f"to generate it — typically 1-5 minutes...")

        status_payload = None
        for attempt in range(60):                     # up to ~20 min
            time.sleep(20)
            r = client.get_report(report_id)
            status_payload = r.payload or {}
            status = status_payload.get("processingStatus")
            if status == "DONE":
                log.info(f"  Report ready after ~{(attempt + 1) * 20}s.")
                break
            if status in ("FATAL", "CANCELLED"):
                log.error(f"  Report failed with status {status}.")
                return None, w_start_iso, w_end_iso
        else:
            log.error("  Report not ready after 20 minutes — giving up this run.")
            return None, w_start_iso, w_end_iso

        doc_id = status_payload["reportDocumentId"]
        doc    = client.get_report_document(doc_id)
        dp     = doc.payload or {}
        url    = dp.get("url")
        if not url:
            log.error("  Report document had no download URL.")
            return None, w_start_iso, w_end_iso

        raw = _rq.get(url, timeout=180).content
        if dp.get("compressionAlgorithm") == "GZIP":
            raw = gzip.decompress(raw)
        text  = raw.decode("cp1252", errors="replace")   # Amazon flat-file encoding
        lines = text.splitlines()
        if not lines:
            log.warning("  Report was empty.")
            return {}, w_start_iso, w_end_iso

        header = [h.strip().lower() for h in lines[0].split("\t")]
        idx    = {h: i for i, h in enumerate(header)}
        a_i, q_i, s_i = idx.get("asin"), idx.get("quantity"), idx.get("item-status")
        d_i = idx.get("purchase-date")
        if a_i is None or q_i is None or d_i is None:
            log.error(f"  Report missing asin/quantity/purchase-date. Header: {header}")
            return None, w_start_iso, w_end_iso

        daily = {}   # {asin: {"YYYY-MM-DD": units}}
        for line in lines[1:]:
            cols = line.split("\t")
            if len(cols) <= max(a_i, q_i, d_i):
                continue
            if s_i is not None and len(cols) > s_i and \
                    cols[s_i].strip().lower() == "cancelled":
                continue                                  # don't count cancellations
            asin = cols[a_i].strip()
            day  = cols[d_i].strip()[:10]                 # ISO date part of purchase-date
            try:
                qty = int(float(cols[q_i].strip() or 0))
            except ValueError:
                qty = 0
            if asin and day:
                daily.setdefault(asin, {})
                daily[asin][day] = daily[asin].get(day, 0) + qty

        log.info(f"  Report parsed: {len(lines) - 1} order lines, "
                 f"{len(daily)} distinct ASIN(s), daily buckets over {days} days.")
        return daily, w_start_iso, w_end_iso

    except SellingApiException as e:
        log.error(f"  SP-API error fetching orders report: {e}")
    except Exception as e:
        log.error(f"  Unexpected error fetching orders report: {e}", exc_info=True)
    return None, w_start_iso, w_end_iso


# ─────────────────────────────────────────────────────────────────────────────
# 2c. SKU<->ASIN CROSSWALK VIA ORDERS REPORT (module2_ux_and_merge_tool)
#
# Same report type/pattern as fetch_velocities_report (GET_FLAT_FILE_ALL_
# ORDERS_DATA_BY_ORDER_DATE_GENERAL) but for a caller-supplied [window_start,
# window_end) date window (not "trailing N days"), and extracting sku+asin
# per order line instead of asin+quantity+day -- this is what lets Module 2
# discover which SKU strings (including old, renamed/closed listings) map to
# which ASIN, so orders under a renamed SKU can be consolidated with the
# current SKU automatically (see pl_cogs.run_asin_consolidation).
#
# IMPORTANT: Amazon caps this report type at a 30-day window per request
# (confirmed against SP-API docs, July 2026) and ~2 years of retrievable
# history. A full historical backfill therefore means one call per 30-day
# window, walked backwards from today -- see asin_sync.py for the
# resumable orchestrator that drives this function repeatedly.
# ─────────────────────────────────────────────────────────────────────────────

def fetch_order_sku_asin_window(marketplace_id, credentials, marketplace,
                                 window_start, window_end):
    """
    Request the flat-file orders report for exactly [window_start, window_end)
    (both timezone-aware datetimes; window_end - window_start must be <= 30
    days per Amazon's limit for this report type) and return a list of
    dicts: [{"order_id":..., "sku":..., "asin":...}, ...] -- one per order
    line in the window (cancelled lines skipped, same as
    fetch_velocities_report). Returns None on failure (caller should treat
    the window as not-yet-completed and retry later, NOT mark it done).
    """
    import gzip
    import requests as _rq

    w_start_iso = window_start.strftime("%Y-%m-%dT%H:%M:%SZ")
    w_end_iso   = window_end.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        client = Reports(credentials=credentials, marketplace=marketplace)

        res = client.create_report(
            reportType="GET_FLAT_FILE_ALL_ORDERS_DATA_BY_ORDER_DATE_GENERAL",
            dataStartTime=w_start_iso,
            dataEndTime=w_end_iso,
            marketplaceIds=[marketplace_id],
        )
        report_id = res.payload["reportId"]
        log.info(f"  ASIN-map report requested for {w_start_iso[:10]}..{w_end_iso[:10]} "
                 f"(id {report_id}); waiting for Amazon to generate it...")

        status_payload = None
        for attempt in range(60):                     # up to ~20 min
            time.sleep(20)
            r = client.get_report(report_id)
            status_payload = r.payload or {}
            status = status_payload.get("processingStatus")
            if status == "DONE":
                log.info(f"  Report ready after ~{(attempt + 1) * 20}s.")
                break
            if status in ("FATAL", "CANCELLED"):
                log.error(f"  Report failed with status {status}.")
                return None
        else:
            log.error("  Report not ready after 20 minutes — giving up this window "
                       "(safe to rerun later; window not marked done).")
            return None

        doc_id = status_payload["reportDocumentId"]
        doc    = client.get_report_document(doc_id)
        dp     = doc.payload or {}
        url    = dp.get("url")
        if not url:
            log.error("  Report document had no download URL.")
            return None

        raw = _rq.get(url, timeout=180).content
        if dp.get("compressionAlgorithm") == "GZIP":
            raw = gzip.decompress(raw)
        text  = raw.decode("cp1252", errors="replace")
        lines = text.splitlines()
        if not lines:
            log.warning("  Report was empty for this window.")
            return []

        header = [h.strip().lower() for h in lines[0].split("\t")]
        idx    = {h: i for i, h in enumerate(header)}
        oid_i  = idx.get("order-id") or idx.get("amazon-order-id")
        sku_i  = idx.get("sku")
        asin_i = idx.get("asin")
        s_i    = idx.get("item-status")
        if oid_i is None or sku_i is None or asin_i is None:
            log.error(f"  Report missing order-id/sku/asin column. Header: {header}")
            return None

        out = []
        need = max(oid_i, sku_i, asin_i)
        for line in lines[1:]:
            cols = line.split("\t")
            if len(cols) <= need:
                continue
            if s_i is not None and len(cols) > s_i and cols[s_i].strip().lower() == "cancelled":
                continue
            order_id = cols[oid_i].strip()
            sku      = cols[sku_i].strip()
            asin     = cols[asin_i].strip()
            if order_id and sku and asin:
                out.append({"order_id": order_id, "sku": sku, "asin": asin})

        log.info(f"  ASIN-map report parsed: {len(out)} order line(s) with sku+asin "
                 f"in this window.")
        return out

    except SellingApiException as e:
        log.error(f"  SP-API error fetching ASIN-map report: {e}")
    except Exception as e:
        log.error(f"  Unexpected error fetching ASIN-map report: {e}", exc_info=True)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# IDENTITY LOOKUP — brand / title / category from an ASIN (for the Add form)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_identity(asin, credentials, marketplace):
    """Return {brand, title, root_category} from getCatalogItem summaries.
    Best-effort: returns blanks on failure so the form still works."""
    out = {"brand": "", "title": "", "root_category": ""}
    try:
        client = CatalogItems(credentials=credentials, marketplace=marketplace)
        resp = client.get_catalog_item(asin=asin, includedData=["summaries", "salesRanks"])
        payload = resp.payload or {}
        summaries = payload.get("summaries", [])
        if summaries:
            s = summaries[0]
            out["brand"] = s.get("brand", "") or s.get("manufacturer", "")
            out["title"] = s.get("itemName", "")
        # display category from salesRanks (first title with a rank)
        for sr in payload.get("salesRanks", []):
            for r in sr.get("ranks", []) + sr.get("displayGroupRanks", []):
                if r.get("title"):
                    out["root_category"] = r["title"]
                    break
            if out["root_category"]:
                break
    except Exception as e:
        log.warning(f"  identity lookup failed for {asin}: {e}")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# LIVE PRICE — our current listing price from SP-API Product Pricing
# ─────────────────────────────────────────────────────────────────────────────

def fetch_live_price(asin, marketplace_id, credentials, marketplace):
    """
    Return our current listing price (float) from SP-API, or None on failure.
    Tries getCompetitivePricing (has our own listing price under CompetitivePrices),
    then falls back to getItemOffers. Best-effort: None means 'could not read,
    keep whatever we had'.
    """
    try:
        client = Products(credentials=credentials, marketplace=marketplace)
        # getCompetitivePricing for our ASIN — includes our own price
        resp = client.get_competitive_pricing_for_asins(
            [asin], MarketplaceId=marketplace_id)
        payload = resp.payload or []
        for entry in payload:
            product = entry.get("Product", {}) if isinstance(entry, dict) else {}
            cp = (product.get("CompetitivePricing", {}) or {}).get("CompetitivePrices", [])
            for price_block in cp:
                amt = (price_block.get("Price", {}) or {}).get("ListingPrice", {}) or {}
                val = amt.get("Amount")
                if val is not None:
                    return round(float(val), 2)
        log.info(f"  [{asin}] Live price: not present in pricing payload.")
    except SellingApiException as e:
        log.warning(f"  [{asin}] Live price fetch failed (pricing): {e}")
    except Exception as e:
        log.warning(f"  [{asin}] Live price fetch error: {e}")
    return None
