"""HIF — momentum range / confluence guard (Jul 23 gold lesson).

When enabled:

* ``rsi_momentum``:
  - **range + no confluence/GP** → bench
  - **chart downtrend + no confluence/GP** → bench (stop-churn lesson)
  - **trend + no confluence/GP** → probe size
  - **confirmed** → full size
* ``gp_ensemble`` + chart downtrend → probe size (crypto/gold GP chop)
* other entry types → passthrough

Flag ``MOM_RANGE_GUARD``: ``1`` on, ``0`` off. Unset → on for ``gold`` and
``crypto``. Forex stays off unless explicitly enabled.
Never raises.
"""

from __future__ import annotations

from hermes_core.engines.risk import PROBE_SIZE_FRACTION
from hermes_core.env import get_env

MIN_CONFLUENCE = 2
_MOM_GUARD_DEFAULT_BOTS = frozenset({"gold", "crypto"})


def mom_range_guard_enabled(*, bot: str | None = None) -> bool:
    raw = (get_env("MOM_RANGE_GUARD", "") or "").strip()
    if raw == "1":
        return True
    if raw == "0":
        return False
    # Unset: auto-enable for gold + crypto (momentum chop / stop-churn lessons).
    return (bot or "").strip().lower() in _MOM_GUARD_DEFAULT_BOTS


def gp_agree_bullish(
    ensemble: str | None = None,
    *,
    gp_strength: float | None = None,
) -> bool:
    """True when GP / ensemble leans bullish for a long momentum unlock."""
    try:
        if gp_strength is not None and float(gp_strength) > 0:
            return True
    except (TypeError, ValueError):
        pass
    text = (ensemble or "").strip().lower()
    return "bull" in text  # bullish / bull


def count_oversold(
    pair_rows: list[dict],
    *,
    rsi_key: str = "rsi",
    thr_key: str = "threshold",
) -> int:
    """Count pairs with RSI <= threshold. Each row: {rsi, threshold}."""
    n = 0
    for row in pair_rows or []:
        try:
            rsi = float(row[rsi_key])
            thr = float(row[thr_key])
        except (KeyError, TypeError, ValueError):
            continue
        if rsi <= thr:
            n += 1
    return n


def chart_downtrend_hostile(
    chart_context: str | None = None,
    *,
    chart_soft_reasons: list | None = None,
) -> bool:
    """True when vision labels a downtrend (soft tilt) — hostile to long momentum.

    ``avoid`` is handled by L14 hard-block upstream; this only covers gray-zone
    downtrend that used to leak through as full-size rsi_momentum stop-churn.
    """
    if chart_soft_reasons:
        return any(str(r).strip().lower() == "downtrend" for r in chart_soft_reasons)
    return "downtrend" in (chart_context or "").lower()


def apply_mom_range_guard(
    base_size: float,
    *,
    enabled: bool,
    entry_type: str | None,
    regime: str | None,
    oversold_count: int,
    gp_agree: bool,
    min_confluence: int = MIN_CONFLUENCE,
    probe_fraction: float = PROBE_SIZE_FRACTION,
    chart_context: str | None = None,
    chart_soft_reasons: list | None = None,
) -> dict:
    """Return size + action metadata. ``action``: full | probe | bench | disabled.

    ``bench`` → caller should skip the traditional momentum entry (size may be 0).
    """
    base = float(base_size)
    et = (entry_type or "").strip().lower()
    meta = {
        "size": base,
        "base_size": base,
        "mom_guard_mode": "disabled",
        "mom_guard_action": "disabled",
        "mom_guard_confirmed": False,
        "mom_guard_reasons": [],
        "oversold_count": int(oversold_count or 0),
        "gp_agree": bool(gp_agree),
    }
    if not enabled:
        return meta
    hostile = chart_downtrend_hostile(
        chart_context,
        chart_soft_reasons=chart_soft_reasons,
    )
    # GP longs into chart downtrend: probe-size only (crypto/gold stop-churn).
    if et == "gp_ensemble":
        if hostile:
            frac = max(0.05, min(1.0, float(probe_fraction)))
            return {
                **meta,
                "size": round(base * frac, 6),
                "mom_guard_mode": "soft",
                "mom_guard_action": "probe",
                "mom_guard_confirmed": False,
                "mom_guard_reasons": ["entry_type=gp_ensemble", "chart_downtrend", "gp_downtrend_probe"],
            }
        return {
            **meta,
            "mom_guard_mode": "passthrough",
            "mom_guard_action": "full",
            "mom_guard_reasons": [f"entry_type={et or 'none'}"],
        }
    if et != "rsi_momentum":
        return {
            **meta,
            "mom_guard_mode": "passthrough",
            "mom_guard_action": "full",
            "mom_guard_reasons": [f"entry_type={et or 'none'}"],
        }

    confirmed = int(oversold_count or 0) >= int(min_confluence) or bool(gp_agree)
    reg = (regime or "").strip().lower() or "unknown"
    reasons: list[str] = [f"regime={reg}", f"oversold={int(oversold_count or 0)}"]
    if gp_agree:
        reasons.append("gp_agree")
    if hostile:
        reasons.append("chart_downtrend")
    if confirmed:
        reasons.append("confirmed")

    # Chop / risk-off: unconfirmed momentum longs get benched.
    if (reg == "range" or hostile) and not confirmed:
        tag = "chart_downtrend_bench" if hostile else "range_bench"
        return {
            **meta,
            "size": 0.0,
            "mom_guard_mode": "soft",
            "mom_guard_action": "bench",
            "mom_guard_confirmed": False,
            "mom_guard_reasons": reasons + [tag],
        }

    if not confirmed:
        frac = max(0.05, min(1.0, float(probe_fraction)))
        return {
            **meta,
            "size": round(base * frac, 6),
            "mom_guard_mode": "soft",
            "mom_guard_action": "probe",
            "mom_guard_confirmed": False,
            "mom_guard_reasons": reasons + ["probe_until_confluence"],
        }

    return {
        **meta,
        "mom_guard_mode": "soft",
        "mom_guard_action": "full",
        "mom_guard_confirmed": True,
        "mom_guard_reasons": reasons,
    }
