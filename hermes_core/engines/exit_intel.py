"""HIF Layer C lite — exit intelligence (pair-tuned trail / BE / partial).

When ``EXIT_INTEL=1``, stamp exit knobs from cortex edge stats onto the open
position only. Does not change entry fills, size, SL%, or TP%.

Thin evidence / errors → passthrough YAML defaults (fail-open).
Flag off → identical to legacy exit behaviour.
"""

from __future__ import annotations

from hermes_core.env import get_env

EVIDENCE_MIN = 5
BE_DEFAULT = 0.5
BE_STRONG = 0.65
BE_WEAK = 0.35
TRAIL_STRONG = 1.8
TRAIL_WEAK = 1.1
WR_STRONG = 0.58
WR_WEAK = 0.42


def exit_intel_enabled() -> bool:
    return get_env("EXIT_INTEL", "0") == "1"


def apply_exit_intel(
    *,
    enabled: bool,
    pair: str,
    entry_type: str,
    strategy: dict | None,
    cortex=None,
) -> dict:
    """Return exit knobs + dashboard metadata for stamping onto a position."""
    strategy = strategy or {}
    yaml_partial = bool(strategy.get("partial_enabled", False))
    base = {
        "exit_intel_mode": "disabled",
        "honor_current_stop": False,
        "be_trigger_frac": BE_DEFAULT,
        "trailing_atr_mult": None,
        "partial_enabled": yaml_partial,
        "exit_intel_n": None,
        "exit_intel_reasons": [],
    }
    if not enabled:
        return base

    reasons: list[str] = []
    edge = {"wins": 0, "losses": 0, "n": 0, "avg_win": None, "avg_loss": None}
    try:
        if cortex is not None:
            edge = cortex.edge_stats(pair, entry_type) or edge
    except Exception:  # noqa: BLE001 — fail-open passthrough
        return {
            **base,
            "exit_intel_mode": "passthrough",
            "exit_intel_reasons": ["cortex_error"],
        }

    n = int(edge.get("n") or 0)
    wins = int(edge.get("wins") or 0)
    losses = int(edge.get("losses") or 0)
    if n < EVIDENCE_MIN:
        return {
            **base,
            "exit_intel_mode": "passthrough",
            "exit_intel_n": n,
            "exit_intel_reasons": ["thin_evidence"],
        }

    wr = wins / max(n, 1)
    avg_win = edge.get("avg_win")
    avg_loss = edge.get("avg_loss")
    fat_win = False
    try:
        if avg_win is not None and avg_loss is not None and float(avg_loss) > 0:
            fat_win = float(avg_win) >= float(avg_loss) * 1.15
        elif avg_win is not None:
            fat_win = float(avg_win) > 0
    except (TypeError, ValueError):
        fat_win = False

    be = BE_DEFAULT
    trail = None
    partial = yaml_partial

    if wr >= WR_STRONG and (fat_win or wr >= 0.65):
        be = BE_STRONG
        trail = TRAIL_STRONG
        partial = True
        reasons.append("strong_edge")
    elif wr <= WR_WEAK or (avg_win is not None and avg_loss is not None
                           and float(avg_loss or 0) > 0
                           and float(avg_win or 0) < float(avg_loss) * 0.85):
        be = BE_WEAK
        trail = TRAIL_WEAK
        partial = False
        reasons.append("weak_edge")
    else:
        be = BE_DEFAULT
        trail = 1.4
        reasons.append("neutral_edge")

    return {
        "exit_intel_mode": "soft",
        "honor_current_stop": True,
        "be_trigger_frac": round(be, 4),
        "trailing_atr_mult": trail,
        "partial_enabled": bool(partial),
        "exit_intel_n": n,
        "exit_intel_reasons": reasons or ["soft"],
        "wr": round(wr, 4),
    }
