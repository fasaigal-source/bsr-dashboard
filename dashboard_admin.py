"""dashboard_admin.py — one-time maintenance triggers that MUST run on Railway's INTERNAL
network (fast, stable) rather than over the external proxy.

Right now it hosts one action: a full canonical reprocess of pl_line_items from the stored
pl_raw_events ledger — the step that re-maps every row's canonical after an alias change
(e.g. the sku_map_MASTER import). Over the external proxy this took 7.5h and still dropped;
in-app on the internal network it finishes in a couple of minutes.

  · Runs in a BACKGROUND THREAD, so it isn't bound to gunicorn's request timeout.
  · Status is persisted to a 1-row table (admin_reprocess_status) so it's consistent
    across BOTH gunicorn workers (a global wouldn't be).
  · Gated by ADMIN_TOKEN if that env var is set; if it isn't, the route is open (the action
    is idempotent and non-destructive, and the dashboard URL is private) — set ADMIN_TOKEN
    in Railway variables to lock it down.
"""
import os
import json
import threading
import logging
from datetime import datetime, timezone

from flask import request, Response

from dashboard_app import app
import db
import pl_tracker

log = logging.getLogger(__name__)


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_admin_schema(db_path=None):
    conn = db.connect()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS admin_reprocess_status (
            id          INTEGER PRIMARY KEY,
            state       TEXT,
            started_at  TEXT,
            finished_at TEXT,
            before_ct   INTEGER,
            after_ct    INTEGER,
            rows        INTEGER,
            error       TEXT,
            updated_at  TEXT
        )
    """)
    conn.execute("INSERT INTO admin_reprocess_status (id, state, updated_at) "
                 "VALUES (1, 'idle', ?) ON CONFLICT(id) DO NOTHING", (_now(),))
    conn.commit()
    conn.close()


def _set_status(**kw):
    kw["updated_at"] = _now()
    conn = db.connect()
    conn.execute(f"UPDATE admin_reprocess_status SET {', '.join(k + '=?' for k in kw)} WHERE id=1",
                 tuple(kw.values()))
    conn.commit()
    conn.close()


def _get_status():
    conn = db.connect()
    r = conn.execute("SELECT * FROM admin_reprocess_status WHERE id=1").fetchone()
    conn.close()
    return dict(r) if r else {"state": "idle"}


def _distinct_canonicals():
    conn = db.connect()
    n = conn.execute("SELECT COUNT(DISTINCT canonical_sku) AS c FROM pl_line_items").fetchone()["c"]
    conn.close()
    return n


def _run_reprocess():
    try:
        before = _distinct_canonicals()
        _set_status(state="running", started_at=_now(), before_ct=before,
                    finished_at=None, after_ct=None, rows=None, error=None)
        n = pl_tracker.reprocess_from_stored_events()          # no network; internal DB
        after = _distinct_canonicals()
        _set_status(state="done", finished_at=_now(), after_ct=after, rows=n)
        log.info("admin reprocess done: %s rows recomputed, distinct canonicals %s -> %s",
                 n, before, after)
    except Exception as e:
        log.exception("admin reprocess failed")
        _set_status(state="error", finished_at=_now(), error=str(e)[:500])


def _authed():
    tok = os.environ.get("ADMIN_TOKEN")
    return (not tok) or (request.args.get("token") == tok)


def _status_html(st, msg):
    running = st.get("state") == "running"
    refresh = '<meta http-equiv="refresh" content="5">' if running else ""
    err = f'<br><span class="k">error:</span> <span style="color:#a32d2d">{st.get("error")}</span>' if st.get("error") else ""
    tail = ("<p>Auto-refreshing every 5s while it runs…</p>" if running
            else "<p><b>Done.</b> Reload <code>/pl</code> — merged rows, colours intact.</p>"
                 if st.get("state") == "done" else "")
    return Response(f"""<!DOCTYPE html><html><head><meta charset="utf-8">{refresh}
<title>Canonical reprocess</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:640px;
margin:40px auto;padding:0 20px;color:#12211b;line-height:1.5}} .k{{color:#7d8b83}}
code{{background:#f2f4f2;padding:1px 6px;border-radius:4px}}</style></head><body>
<h2>Canonical reprocess <span class="k" style="font-weight:400">— internal network</span></h2>
<p>{msg}</p>
<p><span class="k">state:</span> <b>{st.get('state')}</b><br>
<span class="k">distinct canonicals before:</span> {st.get('before_ct')}<br>
<span class="k">after:</span> {st.get('after_ct')}<br>
<span class="k">rows reprocessed:</span> {st.get('rows')}<br>
<span class="k">started:</span> {st.get('started_at')}<br>
<span class="k">finished:</span> {st.get('finished_at')}{err}</p>
{tail}</body></html>""", mimetype="text/html")


@app.route("/admin/reprocess-canonical")
def admin_reprocess():
    if not _authed():
        return Response("forbidden — set ADMIN_TOKEN in Railway variables and pass ?token=…",
                        status=403)
    init_admin_schema()
    st = _get_status()
    if st.get("state") == "running":
        return _status_html(st, "A reprocess is already running.")
    threading.Thread(target=_run_reprocess, daemon=True).start()
    return _status_html(_get_status(),
                        "Started on the internal network — should finish in a couple of minutes. "
                        "This page auto-refreshes.")


@app.route("/admin/reprocess-status")
def admin_reprocess_status():
    if not _authed():
        return Response("forbidden", status=403)
    return Response(json.dumps(_get_status(), indent=2, default=str), mimetype="application/json")
