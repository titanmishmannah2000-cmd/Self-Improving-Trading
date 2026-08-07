"""Canonical size_mode / size_reason for open positions and dashboard pushes.

Keeps loop stamping, runner backfill, and tests aligned so probe/chart
haircuts never show as ``full``.
"""

from __future__ import annotations


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
