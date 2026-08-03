"""HIF — MFE/MAE peak tracking + exit-bar stall counters (layered exits)."""

from __future__ import annotations

from hermes_core.env import get_env

DEFAULT_PEAK_EPSILON_PCT = 0.05


def mfe_tracking_enabled() -> bool:
    return get_env("MFE_TRACKING", "1") == "1"


def update_position_excursions(
    pos: dict,
    unrealised_pct: float,
    *,
    tick: bool = True,
    exit_bar_id: str | None = None,
    peak_epsilon_pct: float | None = None,
) -> dict:
    """Mutate peak MFE / MAE and TF-bar stall counters.

    ``tick=False`` skips stall/hold counters (weekend recycled quotes).
    Meaningful peak only when unreal >= peak + epsilon.
    """
    try:
        u = float(unrealised_pct)
    except (TypeError, ValueError):
        return {
            "peak_mfe_pct": pos.get("peak_mfe_pct"),
            "trough_mae_pct": pos.get("trough_mae_pct"),
            "exit_bars_since_peak": pos.get("exit_bars_since_peak"),
        }
    try:
        eps = (
            float(peak_epsilon_pct)
            if peak_epsilon_pct is not None
            else float(pos.get("peak_epsilon_pct") or DEFAULT_PEAK_EPSILON_PCT)
        )
    except (TypeError, ValueError):
        eps = DEFAULT_PEAK_EPSILON_PCT

    meaningful = False
    try:
        peak = pos.get("peak_mfe_pct")
        peak_f = float(peak) if peak is not None else 0.0
        if u >= peak_f + eps or (peak is None and u > 0):
            if u > peak_f:
                pos["peak_mfe_pct"] = round(u, 4)
                meaningful = True
        elif "peak_mfe_pct" not in pos:
            pos["peak_mfe_pct"] = round(max(0.0, u), 4)

        trough = pos.get("trough_mae_pct")
        trough_f = float(trough) if trough is not None else 0.0
        if u < trough_f:
            pos["trough_mae_pct"] = round(u, 4)
        elif "trough_mae_pct" not in pos:
            pos["trough_mae_pct"] = round(min(0.0, u), 4)
    except Exception:  # noqa: BLE001
        pass

    if tick:
        if meaningful:
            pos["exit_bars_since_peak"] = 0
            pos["cycles_since_peak_mfe"] = 0
        else:
            pos["cycles_since_peak_mfe"] = int(pos.get("cycles_since_peak_mfe") or 0) + 1

        if exit_bar_id is not None:
            prev = pos.get("exit_bar_id")
            if prev is not None and str(prev) != str(exit_bar_id):
                pos["exit_bars_held"] = int(pos.get("exit_bars_held") or 0) + 1
                if not meaningful:
                    pos["exit_bars_since_peak"] = int(pos.get("exit_bars_since_peak") or 0) + 1
                # record peak snapshot per bar for dual-slope
                hist = list(pos.get("mfe_bar_peaks") or [])
                hist.append(float(pos.get("peak_mfe_pct") or 0.0))
                pos["mfe_bar_peaks"] = hist[-8:]
            elif prev is None:
                pos["exit_bars_held"] = int(pos.get("exit_bars_held") or 0)
                pos.setdefault("exit_bars_since_peak", 0)
            pos["exit_bar_id"] = str(exit_bar_id)
        else:
            pos.setdefault("exit_bars_since_peak", int(pos.get("exit_bars_since_peak") or 0))
            pos.setdefault("exit_bars_held", int(pos.get("exit_bars_held") or 0))

    return {
        "peak_mfe_pct": pos.get("peak_mfe_pct"),
        "trough_mae_pct": pos.get("trough_mae_pct"),
        "exit_bars_since_peak": pos.get("exit_bars_since_peak"),
        "exit_bars_held": pos.get("exit_bars_held"),
    }


def excursion_from_position(pos: dict, final_pnl: float | None = None) -> dict:
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
    capture = (float(pnl) / mfe) if mfe > 1e-9 else None
    return {
        "mfe_pct": round(mfe, 4),
        "mae_pct": round(mae, 4),
        "giveback_pct": round(giveback, 4),
        "giveback_frac": round(giveback_frac, 4) if giveback_frac is not None else None,
        "mfe_capture": round(capture, 4) if capture is not None else None,
    }


def append_mfe_path_point(pos: dict, extra: dict | None = None) -> dict:
    """Snapshot one path point for counterfactual training (L3)."""
    pt = {
        "held_cycles": pos.get("held_cycles"),
        "unreal": pos.get("unrealised_pct"),
        "peak": pos.get("peak_mfe_pct"),
        "mae": pos.get("trough_mae_pct"),
        "exit_bar_id": pos.get("exit_bar_id"),
        "regime": pos.get("live_regime") or pos.get("entry_regime"),
        "d1": pos.get("live_d1") or pos.get("d1"),
    }
    if extra:
        pt.update(extra)
    path = list(pos.get("mfe_path") or [])
    path.append(pt)
    pos["mfe_path"] = path[-500:]
    return pt
