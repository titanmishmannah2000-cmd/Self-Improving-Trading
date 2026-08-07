"""Canonical size_mode / size_reason + closed-trade truth recovery helpers."""

from __future__ import annotations

from typing import Any


def resolve_size_stamp(
    *,
    size_mode: str | None = None,
    size_reason: str | None = None,
    entry_decision: str | None = None,
    chart_size_mult: float | None = 1.0,
    size: float | None = None,
    base_size: float | None = None,
    probe_fraction: float | None = None,
) -> dict:
    """Return ``size_mode``, ``size_reason``, ``probe_fraction``, ``chart_size_mult``.

    Priority when classifying probe:
    1. ``entry_decision == "probe"`` → sentient_probe
    2. chart soft haircut (``chart_size_mult < 1``) while still ``full`` → chart_soft
    3. existing ``size_mode == "probe"`` (cortex HIF) → keep / cortex_probe
    4. size meaningfully below base with no other stamp → size_haircut
    """
    mode = str(size_mode or "full")
    reason = size_reason
    if mode == "probe" and not reason:
        reason = "cortex_probe"

    try:
        csm = float(chart_size_mult if chart_size_mult is not None else 1.0)
    except (TypeError, ValueError):
        csm = 1.0

    try:
        sz = float(size if size is not None else 0.0)
    except (TypeError, ValueError):
        sz = 0.0
    try:
        base = float(base_size if base_size is not None else 0.0)
    except (TypeError, ValueError):
        base = 0.0

    dec = str(entry_decision or "")
    if dec == "probe":
        mode, reason = "probe", "sentient_probe"
    elif csm < 0.999 and mode == "full":
        mode, reason = "probe", "chart_soft"
    elif mode == "full" and base > 1e-12 and sz / base < 0.99:
        mode = "probe"
        reason = reason or "size_haircut"

    frac = probe_fraction
    if mode == "probe" and base > 1e-12 and frac is None:
        frac = round(sz / base, 4)

    return {
        "size_mode": mode,
        "size_reason": reason or ("full" if mode == "full" else "probe"),
        "probe_fraction": frac,
        "chart_size_mult": csm,
    }


def normalize_open_size_fields(pos: dict) -> dict:
    """Copy ``pos`` with resolved size fields (backfill legacy opens)."""
    if not isinstance(pos, dict):
        return pos
    out = dict(pos)
    stamp = resolve_size_stamp(
        size_mode=out.get("size_mode"),
        size_reason=out.get("size_reason"),
        entry_decision=out.get("entry_decision"),
        chart_size_mult=out.get("chart_size_mult"),
        size=out.get("size"),
        base_size=out.get("base_size"),
        probe_fraction=out.get("probe_fraction"),
    )
    out.update(stamp)
    return out


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def infer_closed_size_fields(
    trade: dict,
    *,
    strategy: dict | None = None,
    pair_max_size: float | None = None,
) -> dict:
    """Infer size_mode for legacy closes. Never invents entry_decision=take."""
    if not isinstance(trade, dict):
        return trade
    out = dict(trade)
    if out.get("size_mode"):
        if not out.get("decision_source") and out.get("entry_decision"):
            out.setdefault("decision_source", "stamped")
        return out

    strat = strategy or {}
    sz = _f(out.get("size"))
    base_candidates = [
        _f(out.get("base_size")),
        _f(strat.get("position_size_r")),
        _f(pair_max_size),
        sz,
    ]
    base = max(base_candidates)
    if base <= 1e-12:
        out["size_mode"] = "full"
        out["size_reason"] = "full"
        out["size_stamp_inferred"] = True
        out.setdefault("decision_source", out.get("decision_source") or "unknown")
        return out

    stamp = resolve_size_stamp(
        size_mode=None,
        entry_decision=out.get("entry_decision"),
        chart_size_mult=out.get("chart_size_mult"),
        size=sz,
        base_size=base,
        probe_fraction=out.get("probe_fraction"),
    )
    out["base_size"] = round(base, 6)
    out["size_mode"] = stamp["size_mode"]
    out["size_reason"] = stamp["size_reason"]
    out["probe_fraction"] = stamp["probe_fraction"]
    out["size_stamp_inferred"] = True
    if not out.get("entry_decision"):
        # Size-only probe: do NOT invent take; leave decision unknown.
        et = str(out.get("entry_type") or "").lower()
        frac = stamp.get("probe_fraction")
        if (
            stamp["size_mode"] == "probe"
            and et in {"pullback", "mean_reversion"}
            and frac is not None
            and 0.4 <= float(frac) <= 0.6
        ):
            out["size_reason"] = "sentient_probe"
        out.setdefault("decision_source", "unknown")
    else:
        out.setdefault("decision_source", "stamped")
    return out


def synthesize_mfe_path(trade: dict) -> list[dict]:
    """Coarse unreal path from mae → mfe → final pnl over hold_cycles."""
    hold = max(3, int(_f(trade.get("hold_cycles"), 3)))
    mae = _f(trade.get("mae_pct") if trade.get("mae_pct") is not None else trade.get("trough_mae_pct"))
    mfe = _f(trade.get("mfe_pct") if trade.get("mfe_pct") is not None else trade.get("peak_mfe_pct"))
    pnl = _f(trade.get("pnl_pct") if trade.get("pnl_pct") is not None else trade.get("net_pnl_pct"))
    # Piecewise: 0 → mae trough → mfe peak → pnl
    n1 = max(1, hold // 3)
    n2 = max(1, hold // 3)
    n3 = max(1, hold - n1 - n2)
    path: list[dict] = []
    peak = 0.0
    for i in range(n1):
        t = (i + 1) / n1
        u = mae * t
        peak = max(peak, u)
        path.append({"unreal": u, "peak": peak})
    for i in range(n2):
        t = (i + 1) / n2
        u = mae + (mfe - mae) * t
        peak = max(peak, u, mfe)
        path.append({"unreal": u, "peak": peak})
    for i in range(n3):
        t = (i + 1) / n3
        start = mfe if mfe != 0 else path[-1]["unreal"]
        u = start + (pnl - start) * t
        peak = max(peak, u)
        path.append({"unreal": u, "peak": peak})
    if path:
        path[-1]["unreal"] = pnl
        path[-1]["peak"] = max(peak, mfe, pnl)
    return path


def ensure_mfe_path(trade: dict) -> dict:
    """Attach mfe_path (real or synthetic) for path-replay prove."""
    out = dict(trade)
    path = out.get("mfe_path")
    if isinstance(path, list) and len(path) >= 3:
        return out
    out["mfe_path"] = synthesize_mfe_path(out)
    out["mfe_path_synthetic"] = True
    return out
