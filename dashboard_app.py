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

# Unified menu, Veeqo-style grouped dropdowns. Each entry is either a single link
# ("href","label") or a group {"label", "items":[(href,label), ...]} that opens on
# hover. Keeps the top bar to a handful of headings instead of ~15 flat links.
_NAV_GROUPS = [
    ("/", "Home"),
    {"label": "Orders", "items": [
        ("/ship", "Ship labels"),
        ("/packages", "Package dims"),
        ("/pl/postage", "Missing postage"),
    ]},
    ("/inventory", "Inventory"),
    ("/purchase-orders", "Purchasing"),
    ("/products", "Products"),
    {"label": "Analytics", "items": [
        ("/pl", "P&amp;L"),
        ("/pl/cogs", "COGS &amp; pricing"),
        ("/pl/ads", "Ad spend"),
        ("/pl/expenses", "Expenses"),
        ("/ppc", "PPC"),
        ("/advertising", "Advertising"),
        ("/calculator", "Scale-up calculator"),
        ("/baseline", "Market baseline"),
        ("/channels", "Channels report"),
    ]},
    {"label": "Settings", "items": [
        ("/settings", "Channels &amp; dispatch"),
        ("/mirakl/test", "Mirakl connection test"),
    ]},
]

_NAV_STYLE = (
    "<style>"
    ".unav{background:#0e5c5b;color:#eafcfb;padding:0 22px;display:flex;flex-wrap:wrap;"
    "align-items:center;gap:2px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}"
    ".unav .brand{font-weight:800;font-size:15px;margin-right:16px;color:#fff;padding:12px 0}"
    ".unav a{color:#bfe9e7;text-decoration:none;font-size:13px;white-space:nowrap}"
    ".unav>a,.unav .grp>.top{display:block;padding:14px 12px;border-radius:0}"
    ".unav>a:hover,.unav .grp:hover>.top{background:rgba(255,255,255,.12);color:#fff;cursor:pointer}"
    ".unav>a.active,.unav .grp.active>.top{color:#fff;box-shadow:inset 0 -3px 0 #eafcfb;font-weight:700}"
    ".unav .grp{position:relative}"
    ".unav .grp>.top .car{font-size:9px;opacity:.7;margin-left:4px}"
    ".unav .menu{display:none;position:absolute;top:100%;left:0;min-width:190px;background:#0e5c5b;"
    "border-radius:0 0 9px 9px;box-shadow:0 10px 24px rgba(0,0,0,.25);padding:6px;z-index:60}"
    ".unav .grp:hover .menu{display:block}"
    ".unav .menu a{display:block;padding:9px 12px;border-radius:6px;font-size:13px}"
    ".unav .menu a:hover{background:rgba(255,255,255,.14);color:#fff}"
    ".unav .menu a.active{background:#eafcfb;color:#0e5c5b;font-weight:700}"
    ".unav .spacer{flex:1}"
    ".unav a.ext{border:1px solid #59a3a1;color:#d7efee;padding:7px 11px;border-radius:6px;margin:6px 0}"
    ".unav a.ext:hover{background:#0b4a49;color:#fff}"
    "</style>"
)


def _is_active(href, path):
    """Does this leaf href own the current path?"""
    if href == "/":
        return path == "/"
    if href == "/products":
        return path.startswith("/products") or path.startswith("/product/")
    return path == href or path.startswith(href + "/")


def _leaf_hrefs():
    for g in _NAV_GROUPS:
        if isinstance(g, tuple):
            yield g[0]
        else:
            for href, _ in g["items"]:
                yield href


def _active_href(path):
    """Longest matching leaf href -> the active page (used to light up its group)."""
    best = ""
    for href in _leaf_hrefs():
        if _is_active(href, path) and len(href) >= len(best):
            best = href
    return best


def _nav_html():
    active = _active_href(request.path)
    parts = [_NAV_STYLE, '<div class="unav"><span class="brand">BSR Repricer</span>']
    for g in _NAV_GROUPS:
        if isinstance(g, tuple):
            href, label = g
            cls = ' class="active"' if href == active else ""
            parts.append(f'<a href="{href}"{cls}>{label}</a>')
        else:
            group_active = any(href == active for href, _ in g["items"])
            gcls = " active" if group_active else ""
            parts.append(f'<div class="grp{gcls}"><span class="top">{g["label"]}'
                         f'<span class="car">&#9660;</span></span><div class="menu">')
            for href, label in g["items"]:
                acls = ' class="active"' if href == active else ""
                parts.append(f'<a href="{href}"{acls}>{label}</a>')
            parts.append("</div></div>")
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
