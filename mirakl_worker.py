"""mirakl_worker.py — Module 5 scheduled jobs (APScheduler, Europe/London).

Run as its own Railway worker: `python mirakl_worker.py`. Matches the existing
BlockingScheduler convention (module1_job.py / pl_tracker.py).

Jobs:
  1. Order pull        — every MIRAKL_PULL_MINUTES (default 30), upsert into pl_line_items.
  2. Financial sync    — daily (MIRAKL_FIN_HOUR, default 06:00), prior 2 days → mirakl_transactions.
  3. Auto-accept       — WAITING_ACCEPTANCE → ACCEPTED. OFF unless MIRAKL_AUTO_ACCEPT=1 AND
                         writes enabled (MIRAKL_DRY_RUN=0). Default: not scheduled (flagged §6).
  4. Tracking write-back — a stub, NOT scheduled: there's no tracking-number source wired
                         up yet (carrier/label module is out of scope). See run_tracking_writeback().

Only runs for accounts that are actually configured in config.json (creds present),
so it boots and idles safely with no creds. All order writes are dry-run unless
MIRAKL_DRY_RUN=0.

Env:
  MIRAKL_PULL_MINUTES=30     order-pull cadence
  MIRAKL_FIN_HOUR=6          hour (London) for the daily financial sync
  MIRAKL_AUTO_ACCEPT=0       set 1 to schedule auto-accept (only meaningful with writes enabled)
  MIRAKL_DRY_RUN=1           writes are logged-only unless this is 0
"""
import os
import sys
import logging
from datetime import datetime, timedelta, timezone

import mirakl_client
import mirakl_sync
import mirakl_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mirakl_worker")

TZ = "Europe/London"


def _configured_accounts():
    return [a for a in mirakl_db.MIRAKL_ACCOUNTS if mirakl_client.creds_for(a) is not None]


def run_order_pull():
    accts = _configured_accounts()
    if not accts:
        log.info("order pull: no configured Mirakl accounts — skipping")
        return
    # pull orders updated in the last ~2 days (cheap + covers any missed run)
    since = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for a in accts:
        try:
            log.info("order pull %s: %s", a, mirakl_sync.pull_orders(a, since=since))
        except Exception as e:
            log.warning("order pull %s FAILED: %s", a, e)


def run_financial_sync():
    accts = _configured_accounts()
    if not accts:
        log.info("financial sync: no configured Mirakl accounts — skipping")
        return
    today = datetime.now(timezone.utc).date()
    d_from = (today - timedelta(days=2)).isoformat()
    d_to = today.isoformat()
    for a in accts:
        try:
            log.info("financial sync %s: %s", a, mirakl_sync.sync_transactions(a, d_from, d_to))
        except Exception as e:
            log.warning("financial sync %s FAILED: %s", a, e)


def run_auto_accept():
    for a in _configured_accounts():
        try:
            log.info("auto-accept %s: %s", a, mirakl_sync.auto_accept(a))
        except Exception as e:
            log.warning("auto-accept %s FAILED: %s", a, e)


def run_tracking_writeback(account, order_id, carrier_code, tracking_number, tracking_url=None):
    """STUB — ready to call once a tracking-number source exists (carrier/label module,
    out of scope). Sets tracking then confirms shipment. Dry-run gated in the client."""
    r1 = mirakl_client.set_tracking(account, order_id, carrier_code, tracking_number, tracking_url)
    r2 = mirakl_client.confirm_shipment(account, order_id) if r1.get("result") in ("ok", "dry_run") else None
    return {"tracking": r1, "ship": r2}


def main():
    from apscheduler.schedulers.blocking import BlockingScheduler
    mirakl_db.init_mirakl_schema()
    pull_min = int(os.environ.get("MIRAKL_PULL_MINUTES", "30"))
    fin_hour = int(os.environ.get("MIRAKL_FIN_HOUR", "6"))
    auto = os.environ.get("MIRAKL_AUTO_ACCEPT") == "1" and not mirakl_client.is_dry_run()

    log.info("Mirakl worker starting (tz=%s). Configured accounts: %s. Dry-run=%s. Auto-accept=%s.",
             TZ, _configured_accounts() or "none", mirakl_client.is_dry_run(), auto)

    sched = BlockingScheduler(timezone=TZ)
    sched.add_job(run_order_pull, "interval", minutes=pull_min, id="order_pull")
    sched.add_job(run_financial_sync, "cron", hour=fin_hour, minute=0, id="financial_sync")
    if auto:
        sched.add_job(run_auto_accept, "interval", minutes=pull_min, id="auto_accept")
        log.info("auto-accept scheduled (MIRAKL_AUTO_ACCEPT=1, writes enabled).")
    else:
        log.info("auto-accept NOT scheduled — set MIRAKL_AUTO_ACCEPT=1 and MIRAKL_DRY_RUN=0 to enable "
                 "(confirm the auto-accept policy first, §6).")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        log.info("Mirakl worker stopped.")


if __name__ == "__main__":
    if "--once" in sys.argv:          # manual single pass (still dry-run for writes)
        mirakl_db.init_mirakl_schema()
        run_order_pull()
        run_financial_sync()
    else:
        main()
