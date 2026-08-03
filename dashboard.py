"""dashboard.py — entry point for the DECOUPLED Module 1 + Module 2 web UI.

Routes now live in two INDEPENDENT files that share one Flask app:
  * dashboard_module1.py — Module 1 (BSR / repricing)
  * dashboard_module2.py — Module 2 (P&L)
  * dashboard_app.py      — the shared Flask `app`

Importing the two route modules below registers their routes on the shared app.
Because they are separate files, a Module-1 edit can no longer overwrite a
Module-2 route (or vice-versa). Gunicorn entrypoint stays `dashboard:app`.

Run:   python dashboard.py       →  http://localhost:5000  (local, SQLite)
Serve: gunicorn dashboard:app    →  Railway (Module 2 Postgres + read-only collector)
"""
import os
import logging

from dashboard_app import app
import dashboard_module1   # noqa: F401 -- registers Module 1 routes on `app`
import dashboard_module2   # noqa: F401 -- registers Module 2 routes on `app`
import dashboard_module3   # noqa: F401 -- registers Module 3 (PPC) routes on `app`

# startup schema init + idempotent seeds
from module1_db import init_schema
import pl_db
import pl_cogs
import pl_ads
import pl_price
import pl_amazon
import pl_ppc

log = logging.getLogger(__name__)


def _bootstrap():
    """Idempotent schema init + seeds. Runs at IMPORT time so it executes under
    gunicorn (`dashboard:app`) too, not only under `python dashboard.py`.

    Why it matters on Railway: migrate_to_railway.py loads the ROWS into the
    Module 2 Postgres but not the app's INDEXES. init_pl_schema() (and the other
    init_*_schema calls) issue `CREATE INDEX IF NOT EXISTS`, so this is what gives
    /pl/cogs its indexes on Postgres — without it those lookups full-scan 34k rows.
    On Postgres the CREATE TABLEs are skipped (migration owns the schema); on SQLite
    everything is created as before. Each step is guarded so one failure can never
    stop the web service from booting.
    """
    steps = []
    # Module 1's own SQLite schema is only for local/standalone mode. On Railway
    # Module 1 is READ-ONLY against the collector (COLLECTOR_RO_URL is set), so
    # don't create a stray, unused local SQLite there.
    if not os.environ.get("COLLECTOR_RO_URL"):
        steps.append(("module1 schema", init_schema))
    steps += [
        ("module2 schema (+indexes)", pl_db.init_pl_schema),
        ("ads schema",               pl_ads.init_ads_schema),
        ("price schema",             pl_price.init_price_schema),
        ("amazon schema",            pl_amazon.init_amazon_schema),
        ("ppc schema",               pl_ppc.init_ppc_schema),
        ("seed csvs",                pl_cogs.seed_from_csvs),
        ("seed confirmed asins",     pl_cogs.seed_confirmed_asin_pairs),
        ("seed asin from managed",   pl_cogs.seed_asin_from_managed_asins),
        ("asin consolidation",       pl_cogs.run_asin_consolidation),
    ]
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            log.warning("bootstrap step %r failed (continuing): %s", name, e)


# Run once at import (works under gunicorn --preload and `python dashboard.py`).
_bootstrap()


if __name__ == "__main__":
    print("\n" + "=" * 55)
    print("  BSR Repricer — dashboard")
    print("  →  http://localhost:5000  (Module 1 home)")
    print("  →  http://localhost:5000/pl  (Module 2 — P&L)")
    print("  →  http://localhost:5000/pl/ads  (Ad spend CSV upload)")
    print("  →  http://localhost:5000/pl/postage  (Missing-postage worklist)")
    print("  →  http://localhost:5000/pl/sku/<canonical_sku>  (Per-SKU detail page)")
    print("=" * 55 + "\n")
    # Debug/reloader is OFF unless FLASK_DEBUG=1 is set in the environment.
    # Local dev exports FLASK_DEBUG=1 to get auto-reload; Railway (and any
    # other deploy) sets nothing and stays debug=False. Deliberately env-gated
    # rather than hardcoded True: debug=True serves the Werkzeug interactive
    # debugger, which exposes a Python console on any error page -- unacceptable
    # on a host holding SP-API refresh tokens. Safe by default, not by memory.
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=5000)
