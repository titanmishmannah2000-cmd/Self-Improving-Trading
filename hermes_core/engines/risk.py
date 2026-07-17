"""Risk engine (Session 6 / Phase 6) — position sizing, RR guard, ATR floor.

Pure, no I/O. Shared by the live loop, backtester, and reflection deploy gate
(discipline 1.5). Position size is always clamped to (0, 0.5] (roadmap S6 DO-NOT:
must never exceed 0.5). The RR guard rejects any reward:risk < 1.0. The ATR stop
is never tighter than the configured floor.

Guards:
  L40  — param-range hard gate (stop 0.5-10, target 0.5-20, position_size_r 0.05-1)
  RR guard  — reject R:R < 1.0
  ATR floor — stop distance >= floor_pct
"""

from __future__ import annotations

MAX_POSITION_SIZE = 0.5          # hard cap (roadmap S6 DO-NOT)
BULL_MULT = 1.0
NEUTRAL_MULT = 0.6
BEAR_MULT = 0.3
OPEN_BULLISH_PENALTY = 0.15      # per open bullish position beyond the first
RR_GUARD_MIN = 1.0               # reward:risk below this is rejected


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
