"""Classify trade outcomes so soft banks cannot poison full-edge learning."""

from __future__ import annotations

SOFT_REASONS = frozenset({"profit_bank", "soft_bank"})
FAILED_REASONS = frozenset({"failed_breakout"})
FULL_WIN_REASONS = frozenset(
    {
        "profit_target",
        "tp",
        "take_profit",
        "mfe_giveback",
        "trailing",
        "partial_close",
    }
)


def _reason(rec: dict | None) -> str:
    if not rec:
        return ""
    return str(rec.get("exit_reason") or rec.get("reason") or "").strip().lower()


def exit_class_for(rec: dict | None) -> str:
    """Return soft_capture | failed_breakout | full."""
    if rec and rec.get("exit_class"):
        return str(rec["exit_class"])
    r = _reason(rec)
    if r in SOFT_REASONS or rec and rec.get("soft_bank"):
        return "soft_capture"
    if r in FAILED_REASONS:
        return "failed_breakout"
    return "full"


def is_soft_capture(rec: dict | None) -> bool:
    return exit_class_for(rec) == "soft_capture"


def counts_for_full_edge(rec: dict | None) -> bool:
    """True when the close should drive Kelly / WR / TP pathology at full weight."""
    return exit_class_for(rec) == "full"


def edge_weight(rec: dict | None) -> float:
    cls = exit_class_for(rec)
    if cls == "soft_capture":
        return 0.25
    if cls == "failed_breakout":
        return 0.0
    return 1.0


def stamp_exit_class(reason: str, *, soft_bank: bool = False) -> str:
    r = str(reason or "").strip().lower()
    if soft_bank or r in SOFT_REASONS:
        return "soft_capture"
    if r in FAILED_REASONS:
        return "failed_breakout"
    return "full"
