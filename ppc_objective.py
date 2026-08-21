"""ppc_objective.py — Module 3 Phase 2: the optimiser's OBJECTIVE + per-SKU target ACOS.

The optimiser needs a target to aim at. Three selectable modes (stored in app_settings
under "ppc_objective"):

  profit  — (default, the real edge) derive each SKU's target ACOS from its own margin:
            spend on ads only up to the point that still leaves `target_margin` of the
            price as profit. Uses your live P&L numbers (COGS + Amazon fees + postage).
  acos    — a single flat target ACOS you set (e.g. keep ACOS under 25%).
  sales   — grow: spend right up to break-even ACOS (zero margin) within a daily budget.

Key numbers per canonical SKU (from pl_db.get_canonical_rollup):
  contribution_ex = ex-VAT sales − COGS − referral fees − other fees − postage
  breakeven_acos  = contribution_ex ÷ inc-VAT ad-sales   (max ACOS before a per-unit loss)
  target_acos     = mode-dependent (see above), clamped to (0, breakeven_acos].

No network here. Phase 3 (rules) maps keywords/campaigns to SKUs and applies these targets.
"""
import logging

log = logging.getLogger(__name__)

_KEY = "ppc_objective"
DEFAULTS = {"mode": "profit", "target_acos": 0.25, "target_margin": 0.10, "daily_budget": 0.0}
MODES = ("profit", "acos", "sales")
_ACOS_FLOOR, _ACOS_CEIL = 0.02, 0.90


def get_objective():
    try:
        import module5_labels_db as m5
        o = m5.get_setting(_KEY) or {}
    except Exception as e:
        log.warning("ppc_objective read failed: %s", e)
        o = {}
    out = dict(DEFAULTS)
    out.update({k: v for k, v in o.items() if v is not None})
    if out["mode"] not in MODES:
        out["mode"] = "profit"
    return out


def set_objective(mode=None, target_acos=None, target_margin=None, daily_budget=None):
    o = get_objective()
    if mode in MODES:
        o["mode"] = mode
    if target_acos is not None:
        o["target_acos"] = max(_ACOS_FLOOR, min(_ACOS_CEIL, float(target_acos)))
    if target_margin is not None:
        o["target_margin"] = max(0.0, min(0.9, float(target_margin)))
    if daily_budget is not None:
        o["daily_budget"] = max(0.0, float(daily_budget))
    import module5_labels_db as m5
    m5.set_setting(_KEY, o)
    return o


def _clamp(x, lo=_ACOS_FLOOR, hi=_ACOS_CEIL):
    if x is None:
        return None
    return max(lo, min(hi, x))


def _row_econ(r):
    """(rev_ex, rev_inc, varcost, contribution_ex, breakeven_acos) for a rollup row,
    or None if there isn't enough to compute (no sales)."""
    rev_ex = float(r.get("gross_sales_exvat") or 0)
    rev_inc = float(r.get("gross_sales_incvat") or 0)
    units = r.get("units") or 0
    if rev_inc <= 0 or units <= 0:
        return None
    varcost = (float(r.get("cogs") or 0) + float(r.get("referral_fees") or 0)
               + float(r.get("other_fees") or 0) + float(r.get("postage") or 0))
    contribution = rev_ex - varcost
    be_acos = contribution / rev_inc
    return rev_ex, rev_inc, varcost, contribution, be_acos


def target_for_econ(econ, objective):
    """target ACOS (fraction) for one SKU's economics under the objective."""
    rev_ex, rev_inc, varcost, contribution, be_acos = econ
    mode = objective["mode"]
    if mode == "acos":
        return _clamp(objective["target_acos"])
    if mode == "sales":
        return _clamp(be_acos)                       # spend up to break-even
    # profit: leave target_margin of price as profit
    allowable_ad = contribution - objective["target_margin"] * rev_ex
    return _clamp(allowable_ad / rev_inc)


def sku_targets(account_id=None, start_date=None, end_date=None):
    """{canonical_sku: {breakeven_acos, target_acos, rev, units}} using live P&L rollup.
    Falls back to an empty dict if the rollup can't be read."""
    try:
        import pl_db
        rows = pl_db.get_canonical_rollup(account_id=account_id, vat_treatment="ex_vat",
                                          start_date=start_date, end_date=end_date)
    except Exception as e:
        log.warning("ppc_objective: rollup read failed: %s", e)
        return {}
    obj = get_objective()
    out = {}
    for r in rows:
        econ = _row_econ(r)
        if not econ:
            continue
        sku = r.get("canonical_sku")
        out[sku] = {
            "breakeven_acos": _clamp(econ[4], lo=0.0),
            "target_acos": target_for_econ(econ, obj),
            "rev": econ[1],
            "units": r.get("units") or 0,
        }
    return out


def account_default_acos(account_id=None, start_date=None, end_date=None):
    """A single fallback target ACOS from the WHOLE account's aggregate economics —
    used for keywords/campaigns we can't map to a specific SKU."""
    try:
        import pl_db
        rows = pl_db.get_canonical_rollup(account_id=account_id, vat_treatment="ex_vat",
                                          start_date=start_date, end_date=end_date)
    except Exception as e:
        log.warning("ppc_objective: default rollup read failed: %s", e)
        rows = []
    rev_ex = rev_inc = varcost = 0.0
    for r in rows:
        e = _row_econ(r)
        if not e:
            continue
        rev_ex += e[0]; rev_inc += e[1]; varcost += e[2]
    obj = get_objective()
    if obj["mode"] == "acos" or rev_inc <= 0:
        return _clamp(obj["target_acos"])
    econ = (rev_ex, rev_inc, varcost, rev_ex - varcost, (rev_ex - varcost) / rev_inc)
    return target_for_econ(econ, obj)
