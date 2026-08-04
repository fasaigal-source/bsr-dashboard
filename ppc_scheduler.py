"""ppc_scheduler.py — Module 3 Phase C day-parting + temporary-pause reconciler.

Run on a Railway cron (`python ppc_scheduler.py`). For every campaign that is either
(a) on the day-parting schedule (active=1) or (b) under a temporary pause (snooze), it
works out the desired state RIGHT NOW and, if that differs from the state it last
applied, calls the Ads API to enable/pause it. Idempotent: no change → no API call.
Every attempt is logged.

Two things it enforces:
  * Day-parting: active campaigns follow their [on_start_hour, on_end_hour) window
    (seller-local time, ADS_TZ).
  * Temporary pause ("Pause 15/30 min"): while paused_until is in the future the
    campaign is forced paused; once it passes, the campaign is resumed and the snooze
    cleared automatically.

CADENCE: the auto-resume happens on the FIRST run after paused_until, so the cron
interval is the resume precision. For 15-minute pauses to feel like ~15 minutes, run
every 5 minutes (`*/5 * * * *`). Hourly is fine if you only day-part.

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


def reconcile(account_id=None, force=False, now_slot=None, now_dt=None):
    """Bring every active OR snoozed campaign to the state it should be in right now.
    now_slot = the current 15-min slot (0..95) in the scheduling tz. Returns
    (checked, changed)."""
    import pl_ppc
    import ppc_ads_api
    if now_dt is None:
        now_dt = datetime.now(timezone.utc)
    if now_slot is None:
        now_slot = pl_ppc.current_slot()
    tzname = os.environ.get("ADS_TZ", "Europe/London")
    hh, mm = (now_slot * pl_ppc.SLOT_MINUTES) // 60, (now_slot * pl_ppc.SLOT_MINUTES) % 60
    # every schedule row; act on those that are active OR currently/recently snoozed.
    scheds = pl_ppc.get_schedules(account_id, active_only=False)
    todo = [s for s in scheds if s.get("active") or s.get("paused_until")]
    log.info("reconcile: slot=%02d:%02d %s — %d campaign(s) to check", hh, mm, tzname, len(todo))
    checked = changed = 0
    for s in todo:
        checked += 1
        snoozed = pl_ppc.is_snoozed(s, now_dt)
        desired = pl_ppc.desired_state_now(s, now_slot, now_dt)
        # snooze just expired (paused_until set but no longer in the future) → resume + clear it
        if s.get("paused_until") and not snoozed:
            pl_ppc.clear_snooze(s["account_id"], s["campaign_id"])
        if not force and s.get("last_desired_state") == desired:
            continue   # already applied this state — no API call, idempotent
        res = ppc_ads_api.set_campaign_state(
            s["account_id"], s["campaign_id"], desired, s.get("campaign_name"))
        result = res.get("result")
        if result in ("ok", "dry_run"):
            pl_ppc.set_last_desired_state(s["account_id"], s["campaign_id"], desired)
            changed += 1
            log.info("  %s [%s] -> %s%s (%s)", s.get("campaign_name") or s["campaign_id"],
                     s["account_id"], desired, " (snoozed)" if snoozed else "", result)
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
