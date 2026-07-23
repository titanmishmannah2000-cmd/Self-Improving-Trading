"""HIF — MFE/MAE peak tracking (excursion memory for exit intel).

When ``MFE_TRACKING=1``, open positions update peak favourable (MFE) and
adverse (MAE) unrealised % each cycle. On close, those values (plus giveback)
are logged and stored in cortex so exit intel can tighten BE/trail when the
book tends to give back peak profit.

Flag off → no peak updates / no excursion fields on closes (legacy).
Fail-open: bad numbers never break the cycle.
"""

from __future__ import annotations

from hermes_core.env import get_env


def mfe_tracking_enabled() -> bool:
    return get_env("MFE_TRACKING", "0") == "1"


def update_position_excursions(pos: dict, unrealised_pct: float) -> dict:
    """Mutate ``pos`` peak MFE / trough MAE from current unrealised %.

    Returns a small dashboard snapshot. Never raises.
    """
    try:
        u = float(unrealised_pct)
    except (TypeError, ValueError):
        return {
            "peak_mfe_pct": pos.get("peak_mfe_pct"),
            "trough_mae_pct": pos.get("trough_mae_pct"),
        }
    try:
        peak = pos.get("peak_mfe_pct")
        peak_f = float(peak) if peak is not None else 0.0
        if u > peak_f:
            pos["peak_mfe_pct"] = round(u, 4)
        elif "peak_mfe_pct" not in pos:
            pos["peak_mfe_pct"] = round(max(0.0, u), 4)

        trough = pos.get("trough_mae_pct")
        trough_f = float(trough) if trough is not None else 0.0
        # MAE stored as negative (or zero); more negative = worse
        if u < trough_f:
            pos["trough_mae_pct"] = round(u, 4)
        elif "trough_mae_pct" not in pos:
            pos["trough_mae_pct"] = round(min(0.0, u), 4)
    except Exception:  # noqa: BLE001
        pass
    return {
        "peak_mfe_pct": pos.get("peak_mfe_pct"),
        "trough_mae_pct": pos.get("trough_mae_pct"),
    }


def excursion_from_position(pos: dict, final_pnl: float | None = None) -> dict:
    """Snapshot MFE/MAE/giveback for a close (or live open)."""
    try:
        mfe = float(pos.get("peak_mfe_pct") or 0.0)
    except (TypeError, ValueError):
        mfe = 0.0
    try:
        mae = float(pos.get("trough_mae_pct") or 0.0)
    except (TypeError, ValueError):
        mae = 0.0
    pnl = final_pnl
    if pnl is None:
        try:
            pnl = float(pos.get("unrealised_pct") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
    giveback = max(0.0, mfe - float(pnl)) if mfe > 0 else 0.0
    giveback_frac = (giveback / mfe) if mfe > 1e-9 else None
    return {
        "mfe_pct": round(mfe, 4),
        "mae_pct": round(mae, 4),
        "giveback_pct": round(giveback, 4),
        "giveback_frac": round(giveback_frac, 4) if giveback_frac is not None else None,
    }
