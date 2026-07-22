"""HIF Phase-2 — soft expert weights (Layer A meta-allocator lite).

When ``SOFT_WEIGHTS=1``, former hard L35 suppressions become size multipliers
instead of skips. Under-sampled (pair, entry_type) pairs keep an explore floor
so reflection/cortex still get samples. When the flag is off, callers keep
today's hard ``policy.is_suppressed`` skip path unchanged.

Pure helpers — no I/O. The live loop and policy engine call these.
"""

from __future__ import annotations

# Types the meta-allocator knows about (momentum included for dashboard).
EXPERT_TYPES = ("mean_reversion", "rsi_momentum", "gp_ensemble")

SOFT_SUPPRESS_MULT = 0.25   # L35 "bench" → 25% size, still allow entry
EXPLORE_FLOOR = 0.40        # thin-evidence experts stay at least this weight
EXPLORE_MIN_N = 5           # closed outcomes before explore floor lifts
MIN_WEIGHT = 0.05           # absolute floor — never zero (no hard ban in soft mode)


def expert_weight(
    *,
    enabled: bool,
    suppressed: bool,
    evidence_n: int | None = None,
    wr: float | None = None,
    soft_suppress_mult: float = SOFT_SUPPRESS_MULT,
    explore_floor: float = EXPLORE_FLOOR,
    explore_min_n: int = EXPLORE_MIN_N,
) -> dict:
    """Compute a single expert's size weight in (MIN_WEIGHT, 1.0].

    ``enabled=False`` → weight 1.0 (legacy full size; hard suppress handled elsewhere).
    """
    if not enabled:
        return {
            "weight": 1.0,
            "mode": "disabled",
            "suppressed_soft": False,
            "evidence_n": evidence_n,
            "wr": wr,
            "reasons": ["disabled"],
        }

    reasons: list[str] = []
    w = 1.0
    if wr is not None:
        try:
            wr_f = float(wr)
        except (TypeError, ValueError):
            wr_f = None
        else:
            # 0% WR → 0.35, 50% → ~0.675, 100% → 1.0
            w = max(0.35, min(1.0, 0.35 + wr_f * 0.65))
            reasons.append(f"wr={wr_f:.2f}")
            wr = wr_f

    soft = False
    if suppressed:
        w *= float(soft_suppress_mult)
        soft = True
        reasons.append("soft_suppress")

    if evidence_n is not None:
        try:
            n = int(evidence_n)
        except (TypeError, ValueError):
            n = None
        else:
            evidence_n = n
            if n < int(explore_min_n) and w < float(explore_floor):
                w = float(explore_floor)
                reasons.append("explore_floor")

    w = max(float(MIN_WEIGHT), min(1.0, float(w)))
    if not reasons:
        reasons.append("neutral")
    return {
        "weight": round(w, 4),
        "mode": "soft",
        "suppressed_soft": soft,
        "evidence_n": evidence_n,
        "wr": wr,
        "reasons": reasons,
    }


def apply_expert_weight(base_size: float, weight_info: dict) -> dict:
    """Scale ``base_size`` by expert weight; return size + metadata for logs/UI."""
    base = float(base_size)
    w = float(weight_info.get("weight", 1.0) or 1.0)
    sized = max(0.0, base * w)
    return {
        "size": sized,
        "base_size": base,
        "expert_weight": w,
        "expert_mode": weight_info.get("mode", "disabled"),
        "suppressed_soft": bool(weight_info.get("suppressed_soft")),
        "expert_reasons": list(weight_info.get("reasons") or []),
        "evidence_n": weight_info.get("evidence_n"),
        "wr": weight_info.get("wr"),
    }


def pair_expert_weights(
    pair: str,
    cortex,
    suppressed_types: set[str] | list[str] | None,
    *,
    enabled: bool,
) -> dict[str, dict]:
    """Per-entry-type weight map for one pair (dashboard + policy allocation)."""
    suppressed = set(suppressed_types or ())
    out: dict[str, dict] = {}
    for etype in EXPERT_TYPES:
        n = None
        wr = None
        if cortex is not None:
            try:
                n = int(cortex.evidence_n(pair, etype))
            except Exception:  # noqa: BLE001 — fail-soft
                n = None
            try:
                wr = cortex.entry_type_wr(etype, pair=pair)
            except Exception:  # noqa: BLE001
                wr = None
        out[etype] = expert_weight(
            enabled=enabled,
            suppressed=etype in suppressed,
            evidence_n=n,
            wr=wr,
        )
    return out
