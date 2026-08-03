"""ppc_scheduler.py — Module 3 Phase C hourly day-parting reconciler.

Run once per hour (Railway cron: `python ppc_scheduler.py`). For every campaign the
user has marked active, it works out the desired state for the CURRENT hour (in the
seller's local timezone) from its schedule, and if that differs from the state it
last applied, it calls the Ads API to enable/pause it. Idempotent: if a campaign is
already in the desired state it makes no API call. Every attempt is logged.

Requires DATABASE_URL (the Module 2 Postgres) so it reads the same schedules the
dashboard writes. Ads writes are dry-run unless the ADS_* creds are set (see
ppc_ads_api.py). Timezone via ADS_TZ (default Europe/London).

  DATABASE_URL   Module 2 Postgres (internal URL on Railway)
  ADS_TZ         IANA tz for the schedule hours (default Europe/London)
"""
import os
import sys
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ppc_scheduler")


def _local_hour():
    tzname = os.environ.get("ADS_TZ", "Europe/London")
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tzname)).hour, tzname
    except Exception as e:
        log.warning("tz %s unavailable (%s) — falling back to UTC", tzname, e)
        return datetime.now(timezone.utc).hour, "UTC"


def reconcile(account_id=None, force=False, now_hour=None):
    """Bring every active campaign to its scheduled state for `now_hour`.
    Returns (checked, changed)."""
    import pl_ppc
    import ppc_ads_api
    if now_hour is None:
        now_hour, tzname = _local_hour()
    else:
        tzname = "override"
    scheds = pl_ppc.get_schedules(account_id, active_only=True)
    log.info("reconcile: hour=%02d:00 %s — %d active campaign(s)", now_hour, tzname, len(scheds))
    checked = changed = 0
    for s in scheds:
        checked += 1
        desired = pl_ppc.desired_state_for_hour(s, now_hour)
        if not force and s.get("last_desired_state") == desired:
            continue   # already applied this state — no API call, idempotent
        res = ppc_ads_api.set_campaign_state(
            s["account_id"], s["campaign_id"], desired, s.get("campaign_name"))
        result = res.get("result")
        if result in ("ok", "dry_run"):
            pl_ppc.set_last_desired_state(s["account_id"], s["campaign_id"], desired)
            changed += 1
            log.info("  %s [%s] -> %s (%s)", s.get("campaign_name") or s["campaign_id"],
                     s["account_id"], desired, result)
        else:
            log.warning("  %s [%s] -> %s FAILED: %s", s.get("campaign_name") or s["campaign_id"],
                        s["account_id"], desired, res.get("detail"))
    log.info("reconcile done: checked=%d changed=%d", checked, changed)
    return checked, changed


if __name__ == "__main__":
    if not os.environ.get("DATABASE_URL"):
        sys.exit("DATABASE_URL not set — the scheduler needs the Module 2 Postgres.")
    import pl_ppc
    pl_ppc.init_ppc_schema()   # ensure tables/indexes exist
    force = "--force" in sys.argv        # re-apply even if unchanged (first run / manual)
    checked, changed = reconcile(force=force)
    print(f"PPC scheduler: checked {checked}, changed {changed}.")
