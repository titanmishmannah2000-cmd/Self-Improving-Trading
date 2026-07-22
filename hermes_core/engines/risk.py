"""Risk engine (Session 6 / Phase 6) — position sizing, RR guard, ATR floor.

Pure, no I/O. Shared by the live loop, backtester, and reflection deploy gate
(discipline 1.5). Position size is always clamped to (0, 0.5] (roadmap S6 DO-NOT:
must never exceed 0.5). The RR guard rejects any reward:risk < 1.0. The ATR stop
is never tighter than the configured floor.

Guards:
  L40  — param-range hard gate (stop 0.5-10, target 0.5-20, position_size_r 0.05-1)
  RR guard  — reject R:R < 1.0
  ATR floor — stop distance >= floor_pct

HIF Phase 1 — Probe sizing (L30/L33-lite):
  When PROBE_SIZING=1 and cortex evidence for (pair, entry_type) is thin
  (< PROBE_EVIDENCE_MIN closed outcomes), shrink size to PROBE_SIZE_FRACTION.
  Never blocks a trade. Fail-open to full size when evidence is unknown.
"""

from __future__ import annotations

MAX_POSITION_SIZE = 0.5          # hard cap (roadmap S6 DO-NOT)
BULL_MULT = 1.0
NEUTRAL_MULT = 0.6
BEAR_MULT = 0.3
OPEN_BULLISH_PENALTY = 0.15      # per open bullish position beyond the first
RR_GUARD_MIN = 1.0               # reward:risk below this is rejected

# HIF Phase 1 — probe sizing (matches policy_engine.PROBE_CORTEX_THRESHOLD)
PROBE_EVIDENCE_MIN = 5           # closed outcomes before full size
PROBE_SIZE_FRACTION = 0.25       # 25% of computed size while evidence is thin


def _raw_size(base: float, regime: str, open_bullish: int) -> float:
    if regime == "BULL":
        s = base * BULL_MULT
    elif regime == "BEAR":
        s = base * BEAR_MULT
    else:  # NEUTRAL or unknown -> conservative
        s = base * NEUTRAL_MULT
        if open_bullish > 0:
            s *= (1 - OPEN_BULLISH_PENALTY * open_bullish)
    s = max(0.0, s)
    return min(s, MAX_POSITION_SIZE)


def compute_position_size(regime, vol, open_bullish_count, config) -> float:
    """Blueprint-exact Phase-6 signature. Returns position size in (0, 0.5]."""
    base = float(config.get("position_size_r", 0.15))
    return _raw_size(base, regime, int(open_bullish_count))


def evidence_state_for(
    evidence_n: int | None,
    *,
    enabled: bool,
    evidence_min: int = PROBE_EVIDENCE_MIN,
) -> str:
    """Dashboard label: disabled | unknown | thin | ok."""
    if not enabled:
        return "disabled"
    if evidence_n is None:
        return "unknown"
    return "thin" if int(evidence_n) < int(evidence_min) else "ok"


def apply_probe_sizing(
    base_size: float,
    *,
    enabled: bool,
    evidence_n: int | None,
    evidence_min: int = PROBE_EVIDENCE_MIN,
    probe_fraction: float = PROBE_SIZE_FRACTION,
) -> dict:
    """Apply HIF Phase-1 probe sizing. Never blocks; may only shrink size.

    ``evidence_n is None`` means cortex missing / unread → fail-open to **full**
    (same capital as today). Thin evidence (< evidence_min) → probe fraction.
    """
    base = float(base_size)
    base_clamped = min(max(0.0, base), MAX_POSITION_SIZE)
    state = evidence_state_for(
        evidence_n, enabled=enabled, evidence_min=evidence_min,
    )
    if state in ("disabled", "unknown", "ok"):
        return {
            "size": base_clamped,
            "base_size": base_clamped,
            "size_mode": "full",
            "evidence_n": None if evidence_n is None else int(evidence_n),
            "evidence_state": state,
            "probe_fraction": float(probe_fraction),
        }
    # thin → probe
    sized = min(MAX_POSITION_SIZE, max(0.0, base_clamped * float(probe_fraction)))
    return {
        "size": sized,
        "base_size": base_clamped,
        "size_mode": "probe",
        "evidence_n": int(evidence_n),
        "evidence_state": "thin",
        "probe_fraction": float(probe_fraction),
    }


def size(strategy, regime, vol, gp_state) -> float:
    """Roadmap S6 API. ``strategy`` carries position_size_r; ``gp_state`` carries
    open_bullish_count. Delegates to the shared sizing core."""
    base = float(strategy.get("position_size_r", 0.15))
    open_bullish = int((gp_state or {}).get("open_bullish_count", 0))
    return _raw_size(base, regime, open_bullish)


def check_rr_guard(stop_pct, target_pct) -> bool:
    """Return True only if reward:risk >= 1.0 (target/stop >= RR_GUARD_MIN)."""
    if stop_pct is None or target_pct is None or stop_pct <= 0:
        return False
    return (float(target_pct) / float(stop_pct)) >= RR_GUARD_MIN


def compute_atr_stop(entry, atr, mult, floor_pct) -> float:
    """ATR-based stop price; stop distance is never tighter than floor_pct.

    distance = max(atr*mult, floor_pct); returns entry - distance (long stop).
    """
    distance = max(float(atr) * float(mult), float(floor_pct))
    return entry - distance


def param_range_gate(strategy) -> tuple[bool, str | None]:
    """[GUARD L40] Hard gate: reject any param outside STRATEGY_PARAM_RANGES.

    Returns (ok, reason). Mirrors hermes_core.config.schema.STRATEGY_PARAM_RANGES.
    """
    ranges = {
        "stop_loss_pct": (0.5, 10.0),
        "profit_target_pct": (0.5, 20.0),
        "trailing_stop_pct": (0.0, 5.0),
        "position_size_r": (0.05, 1.0),
        "time_exit_cycles": (60, 2880),
        "entry_threshold": (5, 95),
    }
    for key, (lo, hi) in ranges.items():
        v = strategy.get(key)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            return False, f"{key} not numeric: {v!r}"
        if not (lo <= v <= hi):
            return False, f"{key}={v} outside [{lo},{hi}]"
    return True, None
