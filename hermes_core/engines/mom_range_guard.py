"""HIF — momentum range / confluence guard (Jul 23 gold lesson).

When enabled, ``rsi_momentum`` entries:

* **range + no confluence/GP** → bench (skip traditional; GP path untouched)
* **trend (or any) + no confluence/GP** → cap at probe fraction (size > probe
  needs both-metals oversold **or** GP bullish agree)
* **confirmed** → passthrough size

Flag ``MOM_RANGE_GUARD``: ``1`` on, ``0`` off. Unset → on for ``gold`` only.
Never raises; never blocks non-momentum entry types.
"""

from __future__ import annotations

from hermes_core.engines.risk import PROBE_SIZE_FRACTION
from hermes_core.env import get_env

MIN_CONFLUENCE = 2


def mom_range_guard_enabled(*, bot: str | None = None) -> bool:
    raw = (get_env("MOM_RANGE_GUARD", "") or "").strip()
    if raw == "1":
        return True
    if raw == "0":
        return False
    # Unset: auto-enable for gold (XAU/XAG chop lesson); other bots unchanged.
    return (bot or "").strip().lower() == "gold"


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
    if confirmed:
        reasons.append("confirmed")

    if reg == "range" and not confirmed:
        return {
            **meta,
            "size": 0.0,
            "mom_guard_mode": "soft",
            "mom_guard_action": "bench",
            "mom_guard_confirmed": False,
            "mom_guard_reasons": reasons + ["range_bench"],
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
