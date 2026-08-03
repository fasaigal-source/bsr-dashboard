"""Shared Flask `app` object for the decoupled dashboard.

Both dashboard_module1.py and dashboard_module2.py import `app` from here and
register their routes on it. Kept intentionally tiny: the two route modules are
independent files, so editing one can never overwrite the other's routes — the
whole-file-overwrite risk the old combined dashboard.py warned about is gone.

Also hosts the ONE shared navigation bar (via a context processor) so every page
in both modules renders the same menu. `{{ nav|safe }}` is injected everywhere;
`collector_mode` / `collector_url` let templates hide write-only controls and link
out to the collector's own status page when the dashboard is read-only.
"""
import os
import logging
from flask import Flask, request

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
app.secret_key = "module1-local"   # local single-user tool; fine for localhost

# Where watchlist / account management actually lives (the collector's own status
# page). The dashboard is READ-ONLY to the collector, so management controls link
# out here instead of pretending to write.
COLLECTOR_STATUS_URL = "https://web-production-de115.up.railway.app/"

# Unified menu. (href, label). Module 1 sections, then Module 2 sections.
_NAV_ITEMS = [
    ("/", "Home"),
    ("/products", "Products"),
    ("/baseline", "Market baseline"),
    ("__sep__", ""),
    ("/pl", "P&amp;L"),
    ("/pl/cogs", "COGS &amp; pricing"),
    ("/pl/ads", "Ad spend"),
    ("/pl/postage", "Missing postage"),
    ("__sep__", ""),
    ("/ppc", "PPC"),
]

_NAV_STYLE = (
    "<style>"
    ".unav{background:#0e5c5b;color:#eafcfb;padding:11px 22px;display:flex;flex-wrap:wrap;"
    "align-items:center;gap:3px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
    ".unav .brand{font-weight:800;font-size:15px;margin-right:14px;color:#fff}"
    ".unav a{color:#bfe9e7;text-decoration:none;font-size:13px;padding:5px 10px;border-radius:6px;white-space:nowrap}"
    ".unav a:hover{background:rgba(255,255,255,.12);color:#fff}"
    ".unav a.active{background:#eafcfb;color:#0e5c5b;font-weight:700}"
    ".unav .sep{color:#3f807e;margin:0 6px}"
    ".unav .spacer{flex:1}"
    ".unav a.ext{border:1px solid #59a3a1;color:#d7efee}"
    ".unav a.ext:hover{background:#0b4a49;color:#fff}"
    "</style>"
)


def _active_href(path):
    """Longest nav href that prefixes the current path -> the active section.
    Home only matches '/', and /product/<..> detail pages count as Products."""
    if path == "/":
        return "/"
    if path.startswith("/product/") or path.startswith("/products"):
        return "/products"
    best = ""
    for href, _ in _NAV_ITEMS:
        if href in ("/", "__sep__"):
            continue
        if (path == href or path.startswith(href + "/")) and len(href) > len(best):
            best = href
    return best


def _nav_html():
    active = _active_href(request.path)
    parts = [_NAV_STYLE, '<div class="unav"><span class="brand">BSR Repricer</span>']
    for href, label in _NAV_ITEMS:
        if href == "__sep__":
            parts.append('<span class="sep">|</span>')
            continue
        cls = ' class="active"' if href == active else ""
        parts.append(f'<a href="{href}"{cls}>{label}</a>')
    parts.append('<span class="spacer"></span>')
    parts.append(f'<a class="ext" href="{COLLECTOR_STATUS_URL}" target="_blank" '
                 f'rel="noopener">Manage watchlist &amp; accounts &#8599;</a>')
    parts.append("</div>")
    return "".join(parts)


@app.context_processor
def _inject_nav():
    return {
        "nav": _nav_html(),
        "collector_mode": bool(os.environ.get("COLLECTOR_RO_URL")),
        "collector_url": COLLECTOR_STATUS_URL,
    }
