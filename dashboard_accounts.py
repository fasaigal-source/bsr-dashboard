"""dashboard_accounts.py — login + user management ("/login", "/logout", "/accounts").

Opt-in auth: a before_request guard leaves the whole app OPEN until the first master
account is created, so deploying this cannot lock you out. Once an active user exists,
login is required; non-master users are blocked from admin areas (/accounts, /settings,
/admin). Set a strong SECRET_KEY env var in Railway for durable sessions.
"""
import os
import logging

from flask import request, redirect, session, g, Response, render_template_string

from dashboard_app import app
import module8_accounts as m8

log = logging.getLogger(__name__)

# Durable session signing key in production (Railway env). Falls back to the existing
# dev key locally so nothing breaks in local runs.
if os.environ.get("SECRET_KEY"):
    app.secret_key = os.environ["SECRET_KEY"]

_OPEN_PATHS = ("/login", "/logout", "/favicon.ico", "/healthz")


@app.before_request
def _auth_guard():
    p = request.path or "/"
    if p.startswith("/static") or p in _OPEN_PATHS:
        return
    if not m8.any_active_users():
        return                       # OPEN until the first master account exists
    uid = session.get("uid")
    if not uid:
        return redirect("/login?next=" + p)
    user = m8.get_user(uid)
    if not user or not user.get("active"):
        session.clear()
        return redirect("/login")
    g.user = user
    if m8.is_admin_path(p) and user.get("role") != "master":
        return Response("Forbidden — master access required.", status=403)


@app.context_processor
def _inject_user():
    return {"current_user": getattr(g, "user", None)}


_STYLE = """
<style>
 body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef1f4;color:#12161c;margin:0}
 .wrap{max-width:900px;margin:20px auto;padding:0 20px} a{color:#0e5c5b}
 .mid{max-width:380px;margin:8vh auto}
 .card{background:#fff;border-radius:12px;padding:22px 24px;box-shadow:0 2px 8px rgba(0,0,0,.06);margin-bottom:16px}
 h2{margin:0 0 6px;font-size:19px} .muted{color:#8a94a2;font-size:12.5px}
 label{display:block;font-size:12px;color:#5a6472;margin:10px 0 3px}
 input,select{width:100%;padding:8px 10px;border:1px solid #dfe4e9;border-radius:8px;font-size:14px;box-sizing:border-box}
 .btn{background:#0e5c5b;color:#fff;border:none;border-radius:8px;padding:10px 16px;font-size:14px;cursor:pointer;margin-top:14px}
 .btn.sec{background:#eef1f4;color:#0e5c5b;padding:6px 12px;font-size:12.5px}
 .err{background:#fdecec;color:#c0392b;padding:8px 12px;border-radius:8px;font-size:13px;margin-top:10px}
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{padding:7px 9px;text-align:left;border-bottom:1px solid #eef1f4}
 th{color:#5a6472;font-weight:600;font-size:11px;text-transform:uppercase}
 .pill{font-size:11px;font-weight:700;padding:2px 9px;border-radius:20px}
 .r-master{background:#e8eefc;color:#3355bb}.r-user{background:#eef1f4;color:#5a6472}
 .on{color:#1f7a45;font-weight:600}.off{color:#c0392b;font-weight:600}
 .grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px}
</style>
"""

LOGIN = _STYLE + """
<div class="mid"><div class="card">
  <h2>Sign in</h2><div class="muted">BSR Repricer dashboard</div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST" action="/login">
    <input type="hidden" name="next" value="{{ next or '/' }}">
    <label>Username</label><input name="username" autofocus>
    <label>Password</label><input name="password" type="password">
    <button class="btn" type="submit" style="width:100%">Sign in</button>
  </form>
</div></div>
"""

SETUP = _STYLE + """
<div class="mid"><div class="card">
  <h2>Create master account</h2>
  <div class="muted">No accounts exist yet, so the app is currently open. Create the first
  master account to switch on login. Keep these credentials safe.</div>
  {% if error %}<div class="err">{{ error }}</div>{% endif %}
  <form method="POST" action="/accounts/create">
    <input type="hidden" name="role" value="master">
    <label>Username</label><input name="username" autofocus>
    <label>Password</label><input name="password" type="password">
    <button class="btn" type="submit" style="width:100%">Create master &amp; enable login</button>
  </form>
</div></div>
"""

MANAGE = _STYLE + """
{{ nav|safe }}
<div class="wrap">
  <div class="card"><h2>Accounts</h2>
    <div class="muted">Signed in as <b>{{ current_user.username if current_user else '—' }}</b>.
      Master accounts have full access; users are blocked from Settings and Accounts.
      <a href="/logout">Log out</a></div>
  </div>
  <div class="card">
    <h2 style="font-size:15px">Users</h2>
    <table>
      <thead><tr><th>Username</th><th>Role</th><th>Status</th><th>Last login</th><th></th></tr></thead>
      <tbody>
      {% for u in users %}
        <tr>
          <td>{{ u.username }}</td>
          <td><span class="pill r-{{ u.role }}">{{ u.role }}</span></td>
          <td><span class="{{ 'on' if u.active else 'off' }}">{{ 'active' if u.active else 'disabled' }}</span></td>
          <td class="muted">{{ (u.last_login or '')[:16].replace('T',' ') or '—' }}</td>
          <td>
            <form method="POST" action="/accounts/{{ u.id }}/toggle" style="display:inline">
              <button class="btn sec" type="submit">{{ 'Disable' if u.active else 'Enable' }}</button></form>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  <div class="card">
    <h2 style="font-size:15px">Add user</h2>
    {% if error %}<div class="err">{{ error }}</div>{% endif %}
    <form method="POST" action="/accounts/create">
      <div class="grid">
        <div><label>Username</label><input name="username"></div>
        <div><label>Password</label><input name="password" type="password"></div>
        <div><label>Role</label><select name="role"><option value="user">User</option><option value="master">Master</option></select></div>
      </div>
      <button class="btn" type="submit">Add user</button>
    </form>
  </div>
</div>
"""


@app.route("/login", methods=["GET", "POST"])
def login_page():
    nxt = request.values.get("next") or "/"
    if request.method == "POST":
        u = m8.verify(request.form.get("username"), request.form.get("password"))
        if u:
            session.clear()
            session["uid"] = u["id"]
            return redirect(nxt if nxt.startswith("/") else "/")
        return render_template_string(LOGIN, error="Wrong username or password.", next=nxt)
    if not m8.any_active_users():
        return redirect("/accounts")     # nothing to log into yet → setup
    return render_template_string(LOGIN, error=None, next=nxt)


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/accounts")
def accounts_page():
    if not m8.any_active_users():
        return render_template_string(SETUP, error=None)   # first-master setup (open)
    return render_template_string(MANAGE, users=m8.list_users(), error=None)


@app.route("/accounts/create", methods=["POST"])
def accounts_create():
    first = not m8.any_active_users()
    role = request.form.get("role") or "user"
    if first:
        role = "master"                 # the very first account is always master
    try:
        m8.create_user(request.form.get("username"), request.form.get("password"),
                       role=role)
    except Exception as e:
        tpl = SETUP if first else MANAGE
        return render_template_string(tpl, error=str(e), users=m8.list_users() if not first else None)
    if first:
        # auto-login the brand-new master so they aren't bounced to /login
        u = m8.verify(request.form.get("username"), request.form.get("password"))
        if u:
            session.clear()
            session["uid"] = u["id"]
    return redirect("/accounts")


@app.route("/accounts/<int:uid>/toggle", methods=["POST"])
def accounts_toggle(uid):
    u = m8.get_user(uid)
    if u:
        m8.set_active(uid, not u.get("active"))
    return redirect("/accounts")
