"""
pl_tracker.py — Module 2: P&L Tracker job runner

Pulls settled financials from SP-API's Finances API (`listFinancialEvents`) for
every account in config.json / the accounts table, computes true per-line-item
profit, and upserts into `pl_line_items`. READ-ONLY: this module makes no SP-API
write calls of any kind.

RUN:
  python pl_tracker.py                    full run (incremental after first backfill)
  python pl_tracker.py --since 7           only walk the last 7 days (fast iteration/testing)
  python pl_tracker.py --reprocess         recompute pl_line_items from ALREADY-STORED
                                           pl_raw_events -- no network call at all. Use this
                                           to apply a formula fix to historical data.

If config.json → pl_tracker.job_time is set, also starts a daily scheduler (same
pattern as module1_job.py). If not set, run this again manually or wire it into
your own scheduler — re-running is always safe; it upserts.

=== v3: anchor-based net_profit ===
net_profit = balance_change (sum of EVERY charge/fee line Amazon sends, generic,
             never a hand-picked subset) − label_cost (real settled shipping-label
             cost, its own financial event, netted onto the order by order_id/line
             across window boundaries) − cogs.
Both a cash (inc-VAT) and an ex-VAT view are always computed and stored
(`net_profit_cash` / `net_profit_exvat`); `pl_tracker.vat_treatment` in config.json
only controls which one the dashboard shows as the headline. See pl_db.py's
module docstring and categorize_item() for the full reasoning.

Key design points (see pl_db.py docstring for the ledger/aggregate split):
  * Every account's history is walked in date windows via PostedAfter/PostedBefore,
    following NextToken within each window, with backoff on throttling.
  * First run per account: backfills from pl_tracker.history_start_date (or the
    account's own `pl_history_start` override) up to now.
  * Later runs: only re-walk a trailing "recheck window" (pl_tracker.recheck_days)
    plus anything new since the last run — this is what lets `pending` orders
    settle in, late-arriving shipping labels net on, and late refunds get picked
    up without re-walking all history.
  * Settlement status is read from the real FinancialEventGroup ProcessingStatus
    (Open/Closed) where available, not guessed.
  * SellerSKU -> ASIN mapping comes from managed_asins (Finances events carry
    SellerSKU, not ASIN). A SKU that isn't in managed_asins still gets a row but
    asin/cogs will be NULL/0 and a warning is logged — nothing is silently dropped.
  * Postage has three states, detected per order (module2_true_profit Phase 3
    retired the old flat-per-SKU-default fallback): if Amazon bought the
    shipping label (the seller's normal case, ~80-90% of orders), its real
    cost is used (`postage_source="exact"`). If not (an off-Amazon courier —
    InPost/Evri/Royal Mail direct, ~10-20%), the order needs a seller-entered
    real amount (see pl_postage.py / the /pl/postage worklist) — once
    entered it's `postage_source="manual"` (treated as exact everywhere);
    until then it's `postage_source="missing"` (cost £0, never a guessed
    default) and the order stays on the worklist.

=== module2_postage_bugfix: real label costs, per-order lookup ===
Amazon's real Buy-Shipping label charge for this account posts via
AdjustmentEventList (AdjustmentType 'PostageBilling_*'/'PostageRefund_*'),
NEVER as a fee-only ShipmentEvent item (that was always a dead end — see the
retired `has_principal`-based "label" detection in parse_event, which still
exists for accounts where Amazon DOES use that mechanism, but finds nothing
for this one). Confirmed via a real per-order diagnostic pull
(module2_postage_bugfix): those AdjustmentEventList entries carry no
order-linking field at all, so the ONLY reliable way to attach one to an
order is SP-API's PER-ORDER Finances endpoint (get_financial_events_for_order
— see fetch_label_events_for_order below), which Amazon itself scopes
server-side. The previous approach (windowed bulk pull + nearest-in-time
heuristic matching, see pl_db.py's now-superseded find_nearest_unlabeled_
shipment) is retired — real data proved it broken on three independent
counts, most damningly that this account's postage-billing events are
frequently posted BEFORE the order's own ShipmentEvent by a week or more,
which is backwards from what the heuristic's own filter assumed, and is
the actual reason the live dashboard showed ~0% exact.
"""

import argparse
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

import pl_db
import pl_cogs
# Read accounts + managed_asins from Module 2's OWN database (Postgres on Railway,
# SQLite locally) via pl_db — NOT module1_db, which is SQLite-only and has neither
# table on Railway (that broke every dashboard reprocess: COGS/postage edits).
from pl_db import get_accounts, get_managed_asins
from module1_collector import make_credentials, get_marketplace

try:
    from sp_api.api import Finances
    from sp_api.base import SellingApiException
except ImportError:
    raise SystemExit("Missing library — run: pip install python-amazon-sp-api")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.FileHandler("pl_tracker.log", encoding="utf-8"), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

CONFIG_PATH = "config.json"
DEFAULT_HISTORY_DAYS_BACK = 730   # ~2 years — matches Amazon's own retrieval limit for event groups
HTTP_TIMEOUT_SECONDS = 60   # httpx's own default (~5s total) is too tight for Amazon's
                            # occasional slow Finances responses on a long multi-window
                            # backfill -- this is layered UNDER the retry-on-timeout logic
                            # in _call_with_backoff: a generous per-call timeout first,
                            # a bounded number of retries second.


def load_config():
    # config.json holds SP-API creds + settings for the LOCAL sync. On Railway the
    # dashboard has no config.json (it's gitignored), but a network-free reprocess
    # doesn't need it — accounts come from the DB. Return {} rather than crashing.
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def get_effective_accounts(cfg):
    """Same DB-first-then-config merge Module 1 uses, kept local so this module
    never has to modify module1_job.py."""
    db_accounts = get_accounts()
    cfg_accounts = cfg.get("accounts", [])
    if db_accounts:
        cfg_by_id = {a["account_id"]: a for a in cfg_accounts}
        accounts = []
        for a in db_accounts:
            merged = dict(cfg_by_id.get(a["account_id"], {}))
            merged.update({k: v for k, v in a.items() if v is not None})
            accounts.append(merged)
        for a in cfg_accounts:
            if a["account_id"] not in {x["account_id"] for x in db_accounts}:
                accounts.append(a)
        return accounts
    return cfg_accounts


# ─────────────────────────────────────────────────────────────────────────────
# WINDOW PLANNING
# ─────────────────────────────────────────────────────────────────────────────

MAX_WINDOW_DAYS = 180   # Amazon's Finances API hard limit: a single PostedAfter/
                        # PostedBefore (or FinancialEventGroupStartedAfter/Before)
                        # span may not exceed 180 days -- SellingApiBadRequestException
                        # ('Cannot span more than 180 days') otherwise. Every window
                        # this module ever requests must be clamped to this, no matter
                        # which code path built it.

POSTED_BEFORE_SAFETY_BUFFER = timedelta(minutes=5)   # Amazon rejects PostedBefore /
    # FinancialEventGroupStartedBefore if it's "later than 2 minutes from now" AT THE
    # MOMENT THE REQUEST LANDS -- not when we computed it. A literal datetime.now() used
    # as a window's upper bound can trip this from ordinary request latency alone, and
    # will trip it reliably if the local machine's clock runs even a couple of minutes
    # ahead of true UTC (module2_pl_dashboard_bugfix round 5: hit exactly this on the
    # real account's --since run and the read-only inspect_finances_shape.py diagnostic).
    # Every "now" used as an upper date bound sent to Amazon is backed off by this much;
    # 5 minutes safely covers Amazon's 2-minute tolerance plus request latency and any
    # ordinary clock drift.

LABEL_MATCH_LOOKBACK_DAYS = None   # SUPERSEDED (module2_postage_bugfix) -- only still
    # referenced by pl_db.find_nearest_unlabeled_shipment, which the live pipeline no
    # longer calls (see fetch_label_events_for_order). Left in place only so nothing
    # importing this constant breaks; see pl_db.py's LABEL-ADJUSTMENT MATCHING section
    # for why the heuristic it configured was retired.

LATE_CHARGE_ALERT_DAYS = 14   # module2_pl_dashboard_bugfix round 6: per the account owner --
    # "whenever you get any charge to the order, notify me so I know what is actually going
    # on" -- whenever ANY new raw event (a matched label, a late refund, a late fee
    # correction, anything) lands on an order more than this many days after that order's
    # EARLIEST known event, it's logged as a prominent, individually-listed ALERT (not just
    # folded silently into the aggregate numbers) so nothing unusual is buried in normal run
    # output. This is independent of LABEL_MATCH_LOOKBACK_DAYS -- it fires no matter how the
    # late charge was found (heuristic label match or a normal, exactly-keyed refund/shipment
    # correction).


def _parse_iso_utc(s):
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _parse_amazon_date(s):
    """Parses Amazon's own PostedDate format (e.g. '2026-06-23T18:45:14Z')
    safely across Python versions -- datetime.fromisoformat() only accepts a
    bare 'Z' suffix from Python 3.11 onward, so this strips it defensively."""
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _alert_late_charge(summary, account_id, order_id, order_item_id, event_type,
                        gap_days, posted_date, note=""):
    """module2_pl_dashboard_bugfix round 6: per the account owner, ANY new
    charge landing on an order more than LATE_CHARGE_ALERT_DAYS after that
    order's earliest known event must be surfaced individually -- not just
    folded silently into the aggregate P&L numbers -- so nothing unusual is
    buried in a long run's output. Fires regardless of event source (a
    heuristically-matched label, a late refund, a late fee correction)."""
    alert = dict(account_id=account_id, order_id=order_id, order_item_id=order_item_id,
                 event_type=event_type, gap_days=round(gap_days, 1), posted_date=posted_date, note=note)
    summary.setdefault("late_charge_alerts", []).append(alert)
    log.warning(
        f"  [LATE CHARGE ALERT] {account_id} / order {order_id} / item {order_item_id}: "
        f"a new '{event_type}' event posted {posted_date} -- {gap_days:.1f} day(s) after this "
        f"order's earliest known event (over the {LATE_CHARGE_ALERT_DAYS}-day alert threshold)."
        + (f" {note}" if note else ""))


def plan_windows(account_id, account, cfg, since_days=None):
    """Always returns a series of consecutive, chunked (start, end) windows --
    never a single oversized one. `since_days`, if given, only changes the
    START date (walk the last N days instead of the normal backfill/recheck
    logic); it must NOT collapse that span into one request, since Amazon
    rejects any single window >180 days regardless of how it was chosen.
    (Fix for module2_pl_dashboard_bugfix round 3: the previous `since_days`
    branch returned `[(start, now)]` as one window, which worked fine against
    a sandbox that doesn't enforce Amazon's 180-day cap, but failed instantly
    on the real API for any since_days > ~180.)

    `--since` runs are also resumable (module2_pl_dashboard_bugfix round 4): if
    pl_sync_state shows a PRIOR --since run already made progress covering (at
    least) the requested range and hasn't yet reached `now`, this resumes from
    that high-water mark instead of re-walking from scratch -- so a crash
    partway through a multi-hour backfill doesn't cost re-fetching everything
    already-completed. It only resumes when the earlier walk's own start was
    AT OR BEFORE the newly-requested start (i.e. the new request isn't asking
    for MORE/older history than the in-progress walk already committed to);
    otherwise it correctly restarts from the new, wider start date.

    `now` here is deliberately backed off by POSTED_BEFORE_SAFETY_BUFFER (see its
    docstring) -- every window's upper bound derived from it is what actually gets
    sent to Amazon as PostedBefore/FinancialEventGroupStartedBefore, so it must
    never be literal wall-clock now()."""
    now = datetime.now(timezone.utc) - POSTED_BEFORE_SAFETY_BUFFER
    pl_cfg = cfg.get("pl_tracker", {})
    window_days = min(pl_cfg.get("window_days", 30), MAX_WINDOW_DAYS)

    if since_days is not None:
        requested_start = now - timedelta(days=since_days)
        state = pl_db.get_sync_state(account_id)
        resumed = None
        if state and state.get("latest_synced") and state.get("earliest_synced"):
            earliest = _parse_iso_utc(state["earliest_synced"])
            latest = _parse_iso_utc(state["latest_synced"])
            if requested_start >= earliest and latest < now:
                resumed = latest
        if resumed is not None:
            start = resumed
            log.info(f"  {account_id}: --since {since_days} — resuming from {start.date()} "
                     f"(a prior run already walked from {state['earliest_synced'][:10]} up to here; "
                     f"not re-walking already-completed history).")
        else:
            start = requested_start
            log.info(f"  {account_id}: --since override — walking the last {since_days} day(s) "
                     f"in {window_days}-day chunks (Amazon rejects any single window >{MAX_WINDOW_DAYS} days).")
    else:
        recheck_days = pl_cfg.get("recheck_days", 45)
        history_start_str = account.get("pl_history_start") or pl_cfg.get("history_start_date")

        state = pl_db.get_sync_state(account_id)
        if not state or not state.get("latest_synced"):
            if history_start_str:
                start = datetime.fromisoformat(history_start_str).replace(tzinfo=timezone.utc)
            else:
                start = now - timedelta(days=DEFAULT_HISTORY_DAYS_BACK)
            log.info(f"  {account_id}: no prior sync state — full backfill from {start.date()}.")
        else:
            latest = datetime.fromisoformat(state["latest_synced"])
            if latest.tzinfo is None:
                latest = latest.replace(tzinfo=timezone.utc)
            start = latest - timedelta(days=recheck_days)
            if history_start_str:
                floor = datetime.fromisoformat(history_start_str).replace(tzinfo=timezone.utc)
                start = max(start, floor)
            log.info(f"  {account_id}: incremental — re-walking from {start.date()} "
                     f"(recheck window catches late settlements/refunds/labels).")

    windows = []
    cur = start
    while cur < now:
        nxt = min(cur + timedelta(days=window_days), now)
        windows.append((cur, nxt))
        cur = nxt
    return windows, start


# ─────────────────────────────────────────────────────────────────────────────
# SP-API CALLS WITH BACKOFF
# ─────────────────────────────────────────────────────────────────────────────

def _call_with_backoff(fn, *args, max_attempts=6, **kwargs):
    """Retries on both SP-API throttling AND transient network/timeout errors --
    a single slow response among hundreds of paced calls across a multi-hour,
    multi-window backfill must not kill the whole run (module2_pl_dashboard_bugfix
    round 4: an unhandled httpx.ReadTimeout crashed a real ~24-window, 2-year
    backfill near the end, discarding all in-flight progress on that call)."""
    for attempt in range(max_attempts):
        try:
            return fn(*args, **kwargs)
        except SellingApiException as e:
            if "QuotaExceeded" in str(e) or "429" in str(e):
                wait = 5 * (attempt + 1)
                log.info(f"    Throttled; retrying in {wait}s (attempt {attempt + 1}/{max_attempts})")
                time.sleep(wait)
            else:
                raise
        except (httpx.TimeoutException, httpx.TransportError) as e:
            if attempt == max_attempts - 1:
                raise
            wait = 2 * (2 ** attempt)   # 2s, 4s, 8s, 16s, 32s...
            log.warning(f"    Network/timeout error ({type(e).__name__}: {e}); "
                        f"retrying in {wait}s (attempt {attempt + 1}/{max_attempts})")
            time.sleep(wait)
    raise RuntimeError(f"Gave up after {max_attempts} attempts (throttling/timeouts) calling {fn}")


def fetch_events_in_window(credentials, marketplace, posted_after, posted_before):
    """All pages' `FinancialEvents` dicts for the window, following NextToken.

    Pages are fetched BACK-TO-BACK and buffered here, BEFORE the caller does any
    per-event work. This matters against a REMOTE database: the Finances NextToken
    has a short TTL, and the old generator yielded each page so the caller's slow
    per-event DB writes (now round-trips to Railway Postgres over the proxy) ran
    *between* paginated calls — the token could expire mid-walk ('Time to live
    (TTL) exceeded of next token'). Buffering keeps each NextToken used ~2.1s after
    it was issued (pacing only), never blocked behind DB writes. Returns a list, so
    the existing `for fe in fetch_events_in_window(...)` loop is unchanged."""
    client = Finances(credentials=credentials, marketplace=marketplace, timeout=HTTP_TIMEOUT_SECONDS)
    pages = []
    next_token = None
    while True:
        kwargs = {"NextToken": next_token} if next_token else {
            "PostedAfter": posted_after, "PostedBefore": posted_before, "MaxResultsPerPage": 100,
        }
        resp = _call_with_backoff(client.list_financial_events, **kwargs)
        payload = resp.payload or {}
        pages.append(payload.get("FinancialEvents", {}) or {})
        next_token = payload.get("NextToken")
        if not next_token:
            break
        time.sleep(2.1)   # Finances API pacing (0.5 req/s); no DB work in between now
    return pages


def fetch_event_group_status_map(credentials, marketplace, posted_after, posted_before):
    """{FinancialEventGroupId: ProcessingStatus} for the window — the authoritative
    signal for whether a group (and therefore the line items in it) is settled."""
    client = Finances(credentials=credentials, marketplace=marketplace, timeout=HTTP_TIMEOUT_SECONDS)
    status_map = {}
    next_token = None
    while True:
        kwargs = {"NextToken": next_token} if next_token else {
            "FinancialEventGroupStartedAfter": posted_after,
            "FinancialEventGroupStartedBefore": posted_before,
            "MaxResultsPerPage": 100,
        }
        try:
            resp = _call_with_backoff(client.list_financial_event_groups, **kwargs)
        except Exception as e:
            log.warning(f"    Could not fetch financial event groups for window: {e}")
            break
        payload = resp.payload or {}
        for g in payload.get("FinancialEventGroupList", []) or []:
            gid = g.get("FinancialEventGroupId")
            if gid:
                status_map[gid] = g.get("ProcessingStatus")
        next_token = payload.get("NextToken")
        time.sleep(2.1)
        if not next_token:
            break
    return status_map


# ─────────────────────────────────────────────────────────────────────────────
# PER-ORDER LABEL LOOKUP (module2_postage_bugfix) -- the real fix. See the
# module docstring's "real label costs, per-order lookup" section for why
# this replaces the windowed-timestamp-heuristic approach entirely.
# ─────────────────────────────────────────────────────────────────────────────

def fetch_label_events_for_order(credentials, marketplace, order_id):
    """Calls SP-API's per-order Finances endpoint (GET /finances/2024-06-19/
    orders/{orderId}/financialEvents, SDK: get_financial_events_for_order —
    order_id is POSITIONAL, not a kwarg) for ONE order, and pulls every real
    Amazon Buy-Shipping label charge out of its AdjustmentEventList. Amazon
    scopes this response to the order server-side, so every AdjustmentEventList
    entry it returns is guaranteed to belong to this order -- no matching,
    no heuristic, no ambiguity.

    Groups PostageBilling_*/PostageRefund_* entries by their own shared
    PostedDate (every cost component of ONE real label purchase or refund —
    Postage, VAT, FuelSurcharge, DeliveryAreaSurcharge, OversizeSurcharge,
    DeliveryConfirmation, etc. -- shares one timestamp; confirmed via a real
    diagnostic pull). PostageRefund_* entries are included in the same sum as
    PostageBilling_* (a genuine label refund should net the charge down, not
    be ignored) — if a distinct-AdjustmentType refund ever appears with its
    OWN separate PostedDate rather than netting within the same group, it
    naturally becomes its own group instead, which resolve_effective_label_
    group's "latest wins" policy then treats as superseding the original
    charge (a cancelled-and-refunded label reads as "no current label cost"
    once its refund group is the most recent one, rather than double-counting
    both the original charge AND its later refund).

    Returns a list of groups, NEWEST first:
        [{"posted_date": ..., "vat": <signed sum of *_VAT>,
          "base": <signed sum of every other component>,
          "currency": ..., "components": {AdjustmentType: amount}}, ...]
    More than one group = more than one label-purchase event was found for
    this order (a replacement/reprinted label, or separate charge+refund
    groups) -- see resolve_effective_label_group for the policy on that.
    Empty list = no Buy-Shipping label event exists for this order at all —
    the genuine off-Amazon-courier case; postage stays 'estimated'."""
    client = Finances(credentials=credentials, marketplace=marketplace, timeout=HTTP_TIMEOUT_SECONDS)
    groups = {}
    next_token = None
    while True:
        kwargs = {"NextToken": next_token} if next_token else {"MaxResultsPerPage": 100}
        resp = _call_with_backoff(client.get_financial_events_for_order, order_id, **kwargs)
        payload = resp.payload or {}
        fe = payload.get("FinancialEvents", {}) or {}
        _accumulate_postage_adjustments(fe.get("AdjustmentEventList", []) or [], groups)
        next_token = payload.get("NextToken")
        time.sleep(2.1)   # Finances API: pace well under the burst limit
        if not next_token:
            break
    return sorted(groups.values(), key=lambda g: g["posted_date"], reverse=True)


def _accumulate_postage_adjustments(adjustment_event_list, groups):
    """Pure parsing step factored out of fetch_label_events_for_order so it's
    unit-testable with a hand-built AdjustmentEventList (no network call) --
    see test_pl_tracker.py's per-order label parsing checks. Mutates `groups`
    (a {posted_date: group} dict) in place; the caller sorts/returns."""
    for adj_event in adjustment_event_list:
        atype = adj_event.get("AdjustmentType") or ""
        pdate = adj_event.get("PostedDate")
        amt_obj = adj_event.get("AdjustmentAmount") or {}
        try:
            amt = float(amt_obj.get("CurrencyAmount") or 0)
        except (TypeError, ValueError):
            amt = 0.0
        cur = amt_obj.get("CurrencyCode")
        if not pdate or not (atype.startswith("PostageBilling_") or atype.startswith("PostageRefund_")):
            continue
        group = groups.setdefault(pdate, {"posted_date": pdate, "vat": 0.0, "base": 0.0,
                                            "currency": None, "components": {}})
        if atype.endswith("_VAT"):
            group["vat"] += amt
        else:
            group["base"] += amt
        group["components"][atype] = group["components"].get(atype, 0.0) + amt
        if cur:
            group["currency"] = cur
    return groups


def resolve_effective_label_group(groups):
    """Policy for orders with more than one label-purchase group (module2_
    postage_bugfix Step 2: 'decide and document whether to sum them or take
    the latest; flag any order with more than one'). Decision: take the
    LATEST group as the order's effective, currently-billed label cost —
    earlier groups are treated as superseded, not summed on top, since
    summing would double-charge a straightforward reprint (the common case).
    Returns (effective_group, multiple) where multiple=True flags the order
    for the seller to spot-check. Returns (None, False) for an empty list."""
    if not groups:
        return None, False
    return groups[0], len(groups) > 1


# ─────────────────────────────────────────────────────────────────────────────
# PARSING — ShipmentEvent and RefundEvent share the same item schema. A
# fee-only item (no "Principal" charge) is very likely a shipping-label /
# Buy-Shipping purchase -- its own financial event, tagged event_type="label"
# so it nets onto the order as a cost rather than being read as a sale.
# ─────────────────────────────────────────────────────────────────────────────

def parse_event(event, source):
    """source: 'shipment' | 'refund' (which list this event came from).
    Returns one dict per line item, event_type overridden to 'label' when the
    item carries no Principal charge (a fee-only / shipping-label event)."""
    order_id = event.get("AmazonOrderId")
    posted_date = event.get("PostedDate")
    fegid = event.get("FinancialEventGroupId")
    out = []

    for item in event.get("ShipmentItemList", []) or []:
        cat = pl_db.categorize_item(item)
        order_item_id = cat["order_item_id"]
        if not order_item_id:
            log.warning(f"  Skipping a {source} line on order {order_id} with no OrderItemId "
                        f"— cannot key it into pl_line_items.")
            continue

        event_type = source if cat["has_principal"] else "label"

        out.append(dict(
            order_id=order_id, order_item_id=order_item_id, sku=cat["sku"],
            quantity=cat["quantity"], event_type=event_type, posted_date=posted_date,
            financial_event_group_id=fegid, currency=cat["currency"],
            principal=cat["principal"], charge_tax=cat["charge_tax"],
            other_charges=cat["other_charges"], other_charge_types=cat["other_charge_types"],
            referral_fee=cat["referral_fee"], fee_tax=cat["fee_tax"],
            other_fees=cat["other_fees"], fee_types=cat["fee_types"],
            promotion_total=cat["promotion_total"], raw_json=item,
        ))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────────────────────

def _record_line(ln, account_id, summary, db_path=pl_db.DB_PATH):
    """Ingest one parsed line into the raw ledger. Per the fix's 'aggregate
    warnings, not per-line' rule: unusual charge/fee type names are counted
    into `summary` and logged ONCE at the end of the run, not on every line —
    Tax / ShippingCharge / ShippingHB / DigitalServicesFee (and any other new
    type) are all already captured correctly inside other_charges/other_fees,
    so per-line noise would just be alarming without being actionable.

    Late-charge alerting (module2_pl_dashboard_bugfix round 6) is the
    exception to that "aggregate, don't spam" rule -- checked BEFORE the
    insert (so `prior_earliest` reflects the ledger as it stood before this
    event) and, if this event lands more than LATE_CHARGE_ALERT_DAYS after
    the order's earliest known event, logged individually via
    _alert_late_charge regardless of how routine the event type itself is."""
    prior_earliest = pl_db.get_earliest_posted_date(
        account_id, ln["order_id"], ln["order_item_id"], db_path=db_path)

    inserted = pl_db.insert_raw_event(
        account_id=account_id, order_id=ln["order_id"], order_item_id=ln["order_item_id"],
        event_type=ln["event_type"], posted_date=ln["posted_date"], sku=ln["sku"],
        quantity=ln["quantity"], principal=ln["principal"], charge_tax=ln["charge_tax"],
        other_charges=ln["other_charges"], referral_fee=ln["referral_fee"],
        fee_tax=ln["fee_tax"], other_fees=ln["other_fees"],
        promotion_total=ln["promotion_total"], currency=ln["currency"],
        financial_event_group_id=ln["financial_event_group_id"],
        other_charge_types=ln["other_charge_types"], fee_types=ln["fee_types"],
        raw_json=ln["raw_json"], db_path=db_path,
    )
    if ln["event_type"] == "label":
        summary["label_events_seen"] = summary.get("label_events_seen", 0) + 1
    for ctype, amt in ln["other_charge_types"].items():
        bucket = summary["other_charge_types"].setdefault(ctype, {"count": 0, "total": 0.0})
        bucket["count"] += 1
        bucket["total"] += amt
    for ftype, amt in (ln["fee_types"] or {}).items():
        bucket = summary["fee_types_seen"].setdefault(ftype, {"count": 0, "total": 0.0})
        bucket["count"] += 1
        bucket["total"] += amt

    if inserted and prior_earliest and ln["posted_date"]:
        try:
            gap_days = (_parse_amazon_date(ln["posted_date"])
                        - _parse_amazon_date(prior_earliest)).total_seconds() / 86400
        except (ValueError, TypeError):
            gap_days = None
        if gap_days is not None and gap_days > LATE_CHARGE_ALERT_DAYS:
            _alert_late_charge(summary, account_id, ln["order_id"], ln["order_item_id"],
                                ln["event_type"], gap_days, ln["posted_date"],
                                note=f"(order's earliest known event was {prior_earliest})")


def _match_adjustment_group(account_id, pdate, group, summary, db_path=pl_db.DB_PATH):
    """SUPERSEDED (module2_postage_bugfix) -- run_pl_job no longer calls this.
    Real data proved the nearest-in-time heuristic this function implements
    unreliable on three counts (no order-linking field, colliding timestamps
    across unrelated orders, and this account's postage-billing events
    frequently posting BEFORE the order's own ShipmentEvent -- see the module
    docstring and pl_db.py's LABEL-ADJUSTMENT MATCHING section). Real label
    costs are now fetched per-order via fetch_label_events_for_order instead.
    Left in place (and still covered by test_pl_tracker.run_label_adjustment_
    matching_check) only so nothing referencing it breaks; do not wire this
    back into run_pl_job.

    Match ONE label-purchase adjustment group (see run_pl_job's per-page
    AdjustmentEventList extraction -- all cost components sharing one real
    label purchase's PostedDate, pre-summed into group['vat']/group['base'])
    to the nearest not-yet-labeled shipment, insert the resulting 'label' raw
    event, and record the match so a later recheck-window re-walk of the same
    AdjustmentEventList entries never reassigns it. Also fires the late-charge
    alert when the match gap exceeds LATE_CHARGE_ALERT_DAYS.

    Returns the (order_id, order_item_id) touched if matched, else None (a
    group already matched in an earlier run, or one with no candidate
    shipment at all, both return None -- the caller distinguishes them via
    pl_db.is_adjustment_matched if it needs to)."""
    if pl_db.is_adjustment_matched(account_id, pdate, db_path=db_path):
        return None
    match = pl_db.find_nearest_unlabeled_shipment(
        account_id, pdate, max_lookback_days=LABEL_MATCH_LOOKBACK_DAYS, db_path=db_path)
    if not match:
        summary["label_adjustments_unmatched"] += 1
        log.warning(f"    [UNMATCHED LABEL COST] {account_id}: a real PostageBilling adjustment "
                    f"posted {pdate} (total £{abs(group['vat'] + group['base']):.2f}) could not be "
                    f"matched to any not-yet-labeled shipment in the ledger -- will retry on a "
                    f"later run once/if a matching shipment is ingested.")
        return None
    gap_days = (_parse_amazon_date(pdate) - _parse_amazon_date(match["posted_date"])).total_seconds() / 86400
    pl_db.insert_raw_event(
        account_id=account_id, order_id=match["order_id"], order_item_id=match["order_item_id"],
        event_type="label", posted_date=pdate, sku=None, quantity=None,
        principal=0, charge_tax=0, other_charges=0, referral_fee=0,
        fee_tax=group["vat"], other_fees=group["base"], promotion_total=0,
        currency=group["currency"], financial_event_group_id=None,
        other_charge_types=None, fee_types=group["components"],
        raw_json={"matched_via": "timestamp_heuristic (nearest not-yet-labeled shipment, "
                                  "unbounded lookback)",
                  "gap_days": round(gap_days, 2),
                  "matched_shipment_posted_date": match["posted_date"],
                  "components": group["components"]},
        db_path=db_path,
    )
    pl_db.record_label_adjustment_match(account_id, pdate, match["order_id"],
                                         match["order_item_id"], gap_days, db_path=db_path)
    summary["label_adjustments_matched"] += 1
    summary["label_adjustments_matched_cost_total"] += abs(group["vat"] + group["base"])
    if gap_days > LATE_CHARGE_ALERT_DAYS:
        _alert_late_charge(summary, account_id, match["order_id"], match["order_item_id"],
                            "label (matched)", gap_days, pdate,
                            note=f"(matched shipment posted {match['posted_date']}; "
                                 f"heuristic timestamp match, not a guaranteed order ID join)")
    return (match["order_id"], match["order_item_id"])


def _log_reconciliation_sample(row):
    log.info("  ┌─ Reconciliation sample (one settled order — eyeball against Seller Central) ─")
    log.info(f"  │ order_id={row['order_id']}  order_item_id={row['order_item_id']}  "
             f"asin={row.get('asin')}  sku={row.get('sku')}  qty={row['quantity']}")
    log.info(f"  │ sale_price_incvat (Sales Proceeds) = {row['sale_price_incvat']:.2f} {row.get('currency') or ''}")
    log.info(f"  │ sale_price_exvat                   = {row['sale_price_exvat']:.2f}")
    log.info(f"  │ referral_fee                       = {row['referral_fee']:.2f}")
    log.info(f"  │ other_amazon_fees                  = {row['other_amazon_fees']:.2f}")
    log.info(f"  │ promotion_total                    = {row['promotion_total']:.2f}")
    log.info(f"  │ balance_change (Amazon's own total, anchor) = {row['balance_change']:.2f}")
    log.info(f"  │ label_cost (postage, {row['postage_source']}) = {row['label_cost']:.2f} "
             f"(ex-VAT {row['label_cost_exvat']:.2f})")
    log.info(f"  │ cogs                               = {row['cogs']:.2f}")
    log.info(f"  │ output_vat                         = {row['output_vat']:.2f}")
    log.info(f"  │ input_vat_reclaimed                = {row['input_vat_reclaimed']:.2f}")
    log.info(f"  │ net_profit_cash  (before ex-VAT adj)= {row['net_profit_cash']:.2f}")
    log.info(f"  │ net_profit_exvat (headline default)= {row['net_profit_exvat']:.2f}")
    log.info("  └───────────────────────────────────────────────────────────────────────────")


def run_pl_job(since_days=None):
    log.info("=" * 70)
    log.info(f"  Module 2 — P&L Tracker run — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    log.info("=" * 70)

    cfg = load_config()
    accounts = get_effective_accounts(cfg)

    summary = {
        "accounts_processed": 0, "lines_inserted": 0, "lines_updated": 0,
        "label_events_seen": 0, "other_charge_types": {}, "fee_types_seen": {},
        "other_event_types_seen": {}, "other_adjustment_types_seen": {},
        "label_orders_matched": 0, "label_orders_off_amazon": 0,
        "label_orders_matched_cost_total": 0.0, "multi_label_orders": [],
        "late_charge_alerts": [],
    }

    if not accounts:
        log.error("No accounts found (config.json + accounts table both empty). Nothing to do.")
        return summary

    for account in accounts:
        account_id = account["account_id"]
        if not account.get("refresh_token"):
            log.warning(f"Account {account_id} has no refresh_token — skipping.")
            continue
        marketplace = get_marketplace(account["marketplace_id"])
        credentials = make_credentials(cfg, account)
        products = get_managed_asins(account_id)
        sku_map = {p["sku"]: p for p in products if p.get("sku")}

        def asin_lookup(sku, _m=sku_map):
            if not sku:
                return None
            hit = _m.get(sku)
            if hit is None:
                log.warning(f"    SKU '{sku}' has no matching managed_asins row for {account_id} — "
                            f"storing the line with account_id/sku but no ASIN/COGS/default-postage; "
                            f"add it on the Products page to get accurate net_profit.")
            return hit

        windows, walked_from = plan_windows(account_id, account, cfg, since_days=since_days)
        if not windows:
            log.info(f"── Account: {account_id} — up to date, nothing to walk ──")
            continue
        log.info(f"\n── Account: {account_id} — {len(windows)} window(s), "
                 f"{len(products)} known SKU(s) ──")

        touched = set()
        group_status_map = {}
        sample_logged = False
        for (w_start, w_end) in windows:
            posted_after = w_start.strftime("%Y-%m-%dT%H:%M:%SZ")
            posted_before = w_end.strftime("%Y-%m-%dT%H:%M:%SZ")
            log.info(f"  Window {w_start.date()} → {w_end.date()}")

            group_status_map.update(
                fetch_event_group_status_map(credentials, marketplace, posted_after, posted_before))

            window_touched = set()
            page_count = 0
            for fe in fetch_events_in_window(credentials, marketplace, posted_after, posted_before):
                page_count += 1
                for key, val in fe.items():
                    if key not in ("ShipmentEventList", "RefundEventList") and val:
                        summary["other_event_types_seen"][key] = \
                            summary["other_event_types_seen"].get(key, 0) + len(val)

                # module2_postage_bugfix: label costs are no longer extracted from
                # the windowed bulk pull at all -- AdjustmentEventList entries here
                # carry no order-linking field, and this account's postage-billing
                # events are frequently posted BEFORE the order's own ShipmentEvent,
                # which is why the old nearest-in-time heuristic (retired, see
                # pl_db.py) matched essentially nothing. Real label costs are now
                # fetched per-order via fetch_label_events_for_order, below, once
                # we know which orders exist. Still tallied here (visibility only,
                # never dropped, no matching attempted).
                for adj_event in fe.get("AdjustmentEventList", []) or []:
                    atype = adj_event.get("AdjustmentType") or "(none)"
                    summary["other_adjustment_types_seen"][atype] = \
                        summary["other_adjustment_types_seen"].get(atype, 0) + 1

                for event in fe.get("ShipmentEventList", []) or []:
                    for ln in parse_event(event, "shipment"):
                        _record_line(ln, account_id, summary)
                        window_touched.add((ln["order_id"], ln["order_item_id"]))
                for event in fe.get("RefundEventList", []) or []:
                    for ln in parse_event(event, "refund"):
                        _record_line(ln, account_id, summary)
                        window_touched.add((ln["order_id"], ln["order_item_id"]))
            log.info(f"    {page_count} page(s) fetched.")

            # Recompute P&L for everything touched in THIS window right away
            # (not just once at the very end), and advance pl_sync_state's
            # high-water mark to this window's end (module2_pl_dashboard_bugfix
            # round 4). Raw events were already committed per-line by
            # _record_line/insert_raw_event; this is what makes pl_line_items
            # (what the dashboard reads) and the resume point durable too, so
            # a timeout/crash in a LATER window never discards a window
            # that's already fully fetched -- a re-run of the same --since
            # command resumes from here (see plan_windows) instead of
            # re-walking hours of already-completed history.
            for (order_id, order_item_id) in window_touched:
                kind, row = pl_db.recompute_line_item(
                    account_id, order_id, order_item_id, asin_lookup,
                    group_status=lambda g: group_status_map.get(g))
                if kind == "inserted":
                    summary["lines_inserted"] += 1
                elif kind == "updated":
                    summary["lines_updated"] += 1
                if row and row["settlement_status"] == "settled" and not sample_logged:
                    _log_reconciliation_sample(row)
                    sample_logged = True
            pl_db.set_sync_state(account_id, walked_from.isoformat(), w_end.isoformat())
            touched |= window_touched

        # module2_postage_bugfix: fetch the REAL Amazon label cost for every
        # order this run touched that doesn't already have one, via the
        # per-order Finances endpoint. Guaranteed-correct order linkage --
        # see fetch_label_events_for_order's docstring for why this replaces
        # the retired windowed-timestamp-heuristic matching pass.
        order_ids_touched = sorted({oid for (oid, _item) in touched})
        orders_needing_label = pl_db.get_order_ids_without_label(account_id, order_ids_touched)
        if orders_needing_label:
            log.info(f"  Looking up real label cost for {len(orders_needing_label)} order(s) "
                     f"without one yet (per-order Finances call, ~2.1s each)...")
        for order_id in orders_needing_label:
            groups = fetch_label_events_for_order(credentials, marketplace, order_id)
            if not groups:
                summary["label_orders_off_amazon"] = summary.get("label_orders_off_amazon", 0) + 1
                continue
            effective, multiple = resolve_effective_label_group(groups)
            if multiple:
                summary.setdefault("multi_label_orders", []).append(
                    {"order_id": order_id, "label_count": len(groups),
                     "posted_dates": [g["posted_date"] for g in groups]})
                log.warning(f"    [MULTIPLE LABELS] {account_id} order {order_id}: {len(groups)} separate "
                            f"label-purchase events found -- using the latest ({effective['posted_date']}) "
                            f"as the current cost, treating earlier one(s) as superseded "
                            f"(reprint/replacement, or a refund-then-rebuy). Worth a spot-check.")
            item_ids = pl_db.get_order_item_ids(account_id, order_id)
            if not item_ids:
                continue
            if len(item_ids) > 1:
                log.info(f"    {account_id} order {order_id}: multi-item order ({len(item_ids)} items) -- "
                         f"label cost split evenly across items (no reliable per-item allocation signal "
                         f"in the postage-billing event itself).")
            per_item_vat = effective["vat"] / len(item_ids)
            per_item_base = effective["base"] / len(item_ids)
            for order_item_id in item_ids:
                pl_db.insert_raw_event(
                    account_id=account_id, order_id=order_id, order_item_id=order_item_id,
                    event_type="label", posted_date=effective["posted_date"], sku=None, quantity=None,
                    principal=0, charge_tax=0, other_charges=0, referral_fee=0,
                    fee_tax=per_item_vat, other_fees=per_item_base, promotion_total=0,
                    currency=effective["currency"], financial_event_group_id=None,
                    other_charge_types=None, fee_types=effective["components"],
                    raw_json={"matched_via": "per_order_finances_api "
                                              "(order-scoped by Amazon server-side, no heuristic)",
                              "label_groups_seen": len(groups), "components": effective["components"]},
                )
                touched.add((order_id, order_item_id))
            summary["label_orders_matched"] = summary.get("label_orders_matched", 0) + 1
            summary["label_orders_matched_cost_total"] = summary.get("label_orders_matched_cost_total", 0.0) \
                + abs(effective["vat"] + effective["base"])

        # Final pass: re-recompute everything touched this run using the now
        # FULLY-merged group_status_map (a group's ProcessingStatus can arrive
        # in a later window's fetch_event_group_status_map call than the one
        # that fetched its line items) -- corrects any settlement_status that
        # was provisionally "pending" during the incremental per-window pass
        # above. Cheap and idempotent; the per-window pass already made
        # everything durable, this just finalises accuracy.
        for (order_id, order_item_id) in touched:
            pl_db.recompute_line_item(
                account_id, order_id, order_item_id, asin_lookup,
                group_status=lambda g: group_status_map.get(g))
        log.info(f"  {len(touched)} line item(s) touched this run (recomputed incrementally per window, "
                 f"then finalised with the fully-merged settlement-status map).")

        pl_db.set_sync_state(account_id, walked_from.isoformat(), datetime.now(timezone.utc).isoformat())
        summary["accounts_processed"] += 1

    summary["pending_total"] = pl_db.get_pending_count()

    log.info("\n" + "─" * 70)
    log.info("  RUN SUMMARY")
    log.info(f"    Accounts processed:      {summary['accounts_processed']}")
    log.info(f"    Line items inserted:     {summary['lines_inserted']}")
    log.info(f"    Line items updated:      {summary['lines_updated']}")
    log.info(f"    Label/shipping-purchase events seen (fee-only ShipmentItem mechanism, "
             f"not used by this account): {summary['label_events_seen']}")
    log.info(f"    Real Amazon label cost found via per-order Finances lookup: "
             f"{summary['label_orders_matched']} order(s)  "
             f"(total £{summary['label_orders_matched_cost_total']:.2f})")
    log.info(f"    Orders confirmed off-Amazon (no label event at all -- genuine estimate case): "
             f"{summary['label_orders_off_amazon']}")
    if summary["multi_label_orders"]:
        log.warning(f"    Orders with MORE THAN ONE label-purchase event (using latest, earlier "
                    f"treated as superseded -- worth a spot-check): {len(summary['multi_label_orders'])}")
        for m in summary["multi_label_orders"]:
            log.warning(f"      order {m['order_id']}: {m['label_count']} label events "
                        f"({', '.join(m['posted_dates'])})")
    log.info(f"    Still pending (all-time):{summary['pending_total']}")
    if summary["other_event_types_seen"]:
        log.info(f"    Other event types seen (not modelled by this module, nothing dropped, "
                 f"just not in scope for Module 2's per-line P&L): {summary['other_event_types_seen']}")
    if summary["other_adjustment_types_seen"]:
        log.info(f"    Other AdjustmentType(s) seen but NOT matched to an order (out of scope for this "
                 f"fix -- e.g. return postage, reserve movements -- never dropped, just not yet allocated): "
                 f"{summary['other_adjustment_types_seen']}")
    if summary["other_charge_types"]:
        log.info(f"    Non-Principal/Tax charge types seen (folded into other_charges, "
                 f"included in balance_change, never dropped): {summary['other_charge_types']}")
    if summary["fee_types_seen"]:
        log.info(f"    Non-Commission/Tax fee types seen (folded into other_fees, "
                 f"included in balance_change, never dropped): {summary['fee_types_seen']}")
    if summary["late_charge_alerts"]:
        log.warning(f"\n    ── LATE CHARGE ALERTS ({len(summary['late_charge_alerts'])}) -- a charge "
                     f"landed on an order more than {LATE_CHARGE_ALERT_DAYS} day(s) after that order's "
                     f"earliest known event. Review each of these; heuristic label matches in particular "
                     f"are not a guaranteed order-ID join. ──")
        for a in summary["late_charge_alerts"]:
            log.warning(f"      order {a['order_id']} / item {a['order_item_id']}: {a['event_type']} "
                        f"posted {a['posted_date']}, {a['gap_days']:.1f} day(s) late. {a['note']}")
    log.info("─" * 70 + "\n")
    return summary


def reprocess_orders(account_id, order_ids):
    """Network-free recompute of ONLY the given orders' line items (e.g. after a
    manual-postage edit). Far cheaper than reprocess_from_stored_events(family=None),
    which rebuilds all ~34k rows and would time out a web request over the DB."""
    if not order_ids:
        return 0
    products = get_managed_asins(account_id)
    sku_map = {p["sku"]: p for p in products if p.get("sku")}

    def asin_lookup(sku, _m=sku_map):
        return _m.get(sku) if sku else None

    keys = pl_db.get_keys_for_orders(account_id, order_ids)
    return pl_db.reprocess_all_from_raw_events(
        account_id=account_id, asin_lookup=asin_lookup, keys=keys)


def reprocess_from_stored_events(account_id=None, family=None):
    """No network call at all — rebuilds pl_line_items from the already-ingested
    pl_raw_events ledger using the current (fixed) formula. This is how a
    formula correction gets applied to historical data without re-pulling
    from Amazon.

    module2_save_scope_fix: pass `family` to recompute ONLY the line items in
    that price family (a single inline/COGS-page price edit) instead of all
    ~34k rows — turns a price save from a ~15s full rebuild into a near-instant
    one. Leave `family=None` for a full reprocess (--reprocess, or a
    merge/define edit that changes which family a SKU belongs to)."""
    scope = f"family '{family}'" if family else "ALL rows"
    log.info(f"Reprocessing pl_line_items from stored pl_raw_events ({scope}, no network call)...")
    cfg = load_config()
    accounts = get_effective_accounts(cfg)
    lookups = {}
    for account in accounts:
        acct_id = account["account_id"]
        products = get_managed_asins(acct_id)
        sku_map = {p["sku"]: p for p in products if p.get("sku")}
        lookups[acct_id] = sku_map

    def asin_lookup(sku, acct=None):
        if not sku or not acct:
            return None
        return lookups.get(acct, {}).get(sku)

    if family:
        # Only the keys whose resolved canonical is in this family. They can
        # span accounts, so group by account and reprocess each subset with
        # that account's own product lookup.
        fam_keys = pl_db.get_keys_for_family(family, account_id=account_id)
        keys_by_acct = {}
        for k in fam_keys:
            keys_by_acct.setdefault(k[0], []).append(k)
        n = 0
        for acct_id, keys in keys_by_acct.items():
            n += pl_db.reprocess_all_from_raw_events(
                account_id=acct_id,
                asin_lookup=lambda sku, _a=acct_id: lookups.get(_a, {}).get(sku),
                keys=keys)
        log.info(f"Reprocess complete: {n} line item(s) recomputed for {scope}.")
        return n

    if account_id:
        n = pl_db.reprocess_all_from_raw_events(
            account_id=account_id,
            asin_lookup=lambda sku: lookups.get(account_id, {}).get(sku))
    else:
        n = 0
        for acct_id in {k[0] for k in pl_db.get_all_keys()}:
            n += pl_db.reprocess_all_from_raw_events(
                account_id=acct_id,
                asin_lookup=lambda sku, _a=acct_id: lookups.get(_a, {}).get(sku))
    log.info(f"Reprocess complete: {n} line item(s) recomputed.")
    return n


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Module 2 — P&L Tracker")
    parser.add_argument("--since", dest="since_days", type=int, default=None,
                        help="Only walk the last N days (fast validation) instead of the full "
                             "backfill/recheck-window logic.")
    parser.add_argument("--reprocess", action="store_true",
                        help="Recompute pl_line_items from already-stored pl_raw_events, no "
                             "network call. Use after a formula fix to correct historical data.")
    parser.add_argument("--account", dest="account_id", default=None,
                        help="Limit to a single account_id (used with --reprocess or --since).")
    args = parser.parse_args()

    pl_db.init_pl_schema()
    # module2_cogs_integration: idempotent (INSERT OR IGNORE-only) seed from
    # the seller's sku_aliases.csv / price_families.csv, if present alongside
    # this script — safe to call on every run, never overwrites a price the
    # seller has since edited on the dashboard.
    pl_cogs.seed_from_csvs()
    pl_cogs.seed_confirmed_asin_pairs()   # module2_ux_and_merge_tool: 3 seller-confirmed ASIN cases
    pl_cogs.seed_asin_from_managed_asins()  # module2_debug_fix_pass FIX 4: Module 1's own catalog
    pl_cogs.run_asin_consolidation()      # cheap, idempotent -- rerun any time new ASIN data lands

    if args.reprocess:
        reprocess_from_stored_events(account_id=args.account_id)
    else:
        print("\n" + "=" * 55)
        print("  Module 2 — P&L Tracker")
        print("=" * 55)
        print("  Read-only. No SP-API writes. Running one pass now...")
        print("=" * 55 + "\n")

        run_pl_job(since_days=args.since_days)

        cfg = load_config()
        job_time = cfg.get("pl_tracker", {}).get("job_time")
        if job_time and args.since_days is None:
            from apscheduler.schedulers.blocking import BlockingScheduler
            hour, minute = map(int, job_time.split(":"))
            print(f"\npl_tracker.job_time is set — scheduling daily runs at {job_time} UTC. Ctrl+C to stop.\n")
            scheduler = BlockingScheduler(timezone="UTC")
            scheduler.add_job(run_pl_job, "cron", hour=hour, minute=minute)
            try:
                scheduler.start()
            except (KeyboardInterrupt, SystemExit):
                log.info("Scheduler stopped.")
        elif args.since_days is None:
            print("\nNo pl_tracker.job_time in config.json — this was a one-off run. Re-run "
                  "`python pl_tracker.py` periodically (or set pl_tracker.job_time, or use Windows "
                  "Task Scheduler) so pending settlements, late-arriving shipping labels, and late "
                  "refunds keep getting picked up.\n")
