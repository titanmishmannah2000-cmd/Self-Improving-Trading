"""HIF Layer C lite — exit intelligence (pair-tuned trail / BE / partial).

When ``EXIT_INTEL=1``, stamp exit knobs from cortex edge stats onto the open
position only. Does not change entry fills, size, SL%, or TP%.

When cortex also has MFE/MAE giveback memory (``MFE_TRACKING`` closes), high
average giveback pulls BE earlier / trail tighter.

Thin evidence / errors → passthrough YAML defaults (fail-open).
Flag off → identical to legacy exit behaviour.
"""

from __future__ import annotations

from hermes_core.env import get_env

EVIDENCE_MIN = 5
EXCURSION_MIN = 3
BE_DEFAULT = 0.5
BE_STRONG = 0.65
BE_WEAK = 0.35
TRAIL_STRONG = 1.8
TRAIL_WEAK = 1.1
WR_STRONG = 0.58
WR_WEAK = 0.42
GIVEBACK_HIGH = 0.40   # avg fraction of peak given back → protect earlier
GIVEBACK_LOW = 0.15


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
        "avg_giveback_frac": None,
    }
    if not enabled:
        return base

    reasons: list[str] = []
    edge = {"wins": 0, "losses": 0, "n": 0, "avg_win": None, "avg_loss": None}
    exc = {
        "n": 0, "avg_mfe": None, "avg_mae": None,
        "avg_giveback": None, "avg_giveback_frac": None,
    }
    try:
        if cortex is not None:
            edge = cortex.edge_stats(pair, entry_type) or edge
            with_exc = getattr(cortex, "excursion_stats", None)
            if callable(with_exc):
                exc = with_exc(pair, entry_type) or exc
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

    # Excursion overlay: high giveback → protect earlier even if WR looked fine.
    gf = exc.get("avg_giveback_frac")
    n_exc = int(exc.get("n") or 0)
    if gf is not None and n_exc >= EXCURSION_MIN:
        try:
            gf_f = float(gf)
        except (TypeError, ValueError):
            gf_f = None
        if gf_f is not None:
            if gf_f >= GIVEBACK_HIGH:
                be = min(be, BE_WEAK)
                trail = TRAIL_WEAK if trail is None else min(trail, TRAIL_WEAK)
                partial = True
                reasons.append(f"high_giveback={gf_f:.2f}")
            elif gf_f <= GIVEBACK_LOW and wr >= WR_STRONG:
                be = max(be, BE_STRONG)
                trail = TRAIL_STRONG if trail is None else max(trail, 1.5)
                reasons.append(f"low_giveback={gf_f:.2f}")

    return {
        "exit_intel_mode": "soft",
        "honor_current_stop": True,
        "be_trigger_frac": round(be, 4),
        "trailing_atr_mult": trail,
        "partial_enabled": bool(partial),
        "exit_intel_n": n,
        "exit_intel_reasons": reasons or ["soft"],
        "wr": round(wr, 4),
        "avg_giveback_frac": (
            round(float(gf), 4) if gf is not None else None
        ),
        "excursion_n": n_exc,
    }
