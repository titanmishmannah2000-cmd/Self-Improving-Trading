"""Exit engine (Session 5 / Phase 5) — pure exit/stop evaluator.

Single implementation shared by the live loop, backtester, and dashboard export
(discipline 1.5 + roadmap S5). NO I/O, NO network, NO hidden state: given a
trade's parameters, the current price, and a price history (for ATR trailing),
it returns either an ``Exit`` describing the action or ``None``.

Exactly ONE exit reason is ever returned per evaluation — never zero-or-many
(roadmap S5 DO-NOT). The Phase-5 reasons:

  stop_loss      price <= entry*(1 - sl/100)
  profit_target  price >= entry*(1 + tp/100)  [hard close, no partial feature]
  partial_close  price >= entry*(1 + tp/100)  [partial feature ON -> 50% off at
                  FULL target, stop moved to breakeven for the remainder]  [GUARD L27]
  mfe_giveback   peak MFE >= min and giveback_frac >= thresh (lock winners)
  breakeven      unrealised >= tp*be_trigger_frac  -> move stop to entry [GUARD L26]
  trailing       ATR-based trailing stop tightening (raises the stop only)
  time_exit      held_cycles >= time_exit_cycles (last resort — after protectors)

Also exposes the [GUARD L24] circuit-breaker predicate.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes_core.indicators import compute_atr

CIRCUIT_MAX_CONSECUTIVE_FAILURES = 5  # [GUARD L24]

# Defaults when strategy / position omit knobs (≈2.5h at 60s cycles).
DEFAULT_TIME_EXIT_CYCLES = 150
DEFAULT_MFE_GIVEBACK_MIN_PCT = 0.4   # need real peak before locking
DEFAULT_MFE_GIVEBACK_FRAC = 0.5      # exit after giving back ≥50% of MFE


@dataclass
class Exit:
    reason: str                      # one of the reasons above
    price: float                     # price at evaluation
    new_stop: float | None = None   # stop adjustment (breakeven/trailing)
    partial_close_fraction: float | None = None  # 0.5 when partial-closing


def _unrealised_pct(trade: dict, current_price: float) -> float:
    if trade.get("unrealised_pct") is not None:
        return float(trade["unrealised_pct"])
    entry = trade["entry_price"]
    if entry == 0:
        return 0.0
    return (current_price - entry) / entry * 100.0


def should_circuit_break(
    consecutive_failures: int,
    max_consecutive: int = CIRCUIT_MAX_CONSECUTIVE_FAILURES,
) -> bool:
    """[GUARD L24] Halt fetching/sleep when consecutive failures hit the cap."""
    return consecutive_failures >= max_consecutive


def _mfe_giveback_hit(trade: dict, unreal: float) -> bool:
    """True when peak MFE is meaningful and enough of it has been given back.

    Opt-out: ``mfe_giveback_enabled: false`` on the position/strategy stamp.
    """
    if trade.get("mfe_giveback_enabled", True) is False:
        return False
    try:
        peak = float(trade.get("peak_mfe_pct") or 0.0)
        min_mfe = float(trade.get("mfe_giveback_min_pct", DEFAULT_MFE_GIVEBACK_MIN_PCT))
        thresh = float(trade.get("mfe_giveback_frac", DEFAULT_MFE_GIVEBACK_FRAC))
    except (TypeError, ValueError):
        return False
    if peak < min_mfe or peak <= 1e-9 or thresh <= 0:
        return False
    giveback = max(0.0, peak - float(unreal))
    return (giveback / peak) >= thresh


def evaluate_exit(
    trade: dict, current_price: float, prices: list[float] | None = None
) -> Exit | None:
    """Evaluate one trade for an exit/stop action. Pure + deterministic.

    ``trade`` keys: entry_price (req), stop_loss_pct, profit_target_pct,
    time_exit_cycles, held_cycles, unrealised_pct (opt), breakeven_set (bool),
    partial_done (bool), partial_enabled (bool), trailing_atr_mult (opt),
    current_stop (opt), peak_mfe_pct (opt), mfe_giveback_* (opt).
    """
    if not trade or "entry_price" not in trade:
        return None

    entry = trade["entry_price"]
    sl = trade.get("stop_loss_pct")
    tp = trade.get("profit_target_pct")
    held = trade.get("held_cycles", 0)
    te = trade.get("time_exit_cycles")
    unreal = _unrealised_pct(trade, current_price)
    partial_enabled = trade.get("partial_enabled", False)
    partial_done = trade.get("partial_done", False)
    breakeven_set = trade.get("breakeven_set", False)

    # 1) Armed current_stop hit (EXIT_INTEL / trail / BE) — before %-SL
    if trade.get("honor_current_stop") and trade.get("current_stop") is not None:
        try:
            if current_price <= float(trade["current_stop"]):
                return Exit("stop_loss", current_price)
        except (TypeError, ValueError):
            pass

    # 2) hard stop-loss
    if sl is not None and current_price <= entry * (1 - sl / 100.0):
        return Exit("stop_loss", current_price)

    # 3) target / partial-close (both triggered at the FULL target)
    if tp is not None and current_price >= entry * (1 + tp / 100.0):
        if partial_enabled and not partial_done:
            # [GUARD L27] 50% off at full target, remainder trailed at breakeven
            return Exit("partial_close", current_price, new_stop=entry, partial_close_fraction=0.5)
        return Exit("profit_target", current_price)

    # 4) MFE giveback — lock winners before the clock donates them back
    if _mfe_giveback_hit(trade, unreal):
        return Exit("mfe_giveback", current_price)

    # 5) [GUARD L26] breakeven — move stop to entry past be_trigger_frac * TP
    #    (before time_exit so protectors can arm during the hold window)
    be_frac = 0.5
    try:
        if trade.get("be_trigger_frac") is not None:
            be_frac = max(0.15, min(0.9, float(trade["be_trigger_frac"])))
    except (TypeError, ValueError):
        be_frac = 0.5
    if tp is not None and not breakeven_set and unreal >= tp * be_frac:
        return Exit("breakeven", current_price, new_stop=entry)

    # 6) ATR-based trailing stop (raises the stop only) — before time_exit
    mult = trade.get("trailing_atr_mult")
    if mult is not None and unreal > 0 and prices:
        atr = compute_atr(prices)
        if atr > 0:
            trail_stop = current_price - atr * mult
            cur = trade.get("current_stop")
            if cur is None or trail_stop > cur:
                return Exit("trailing", current_price, new_stop=trail_stop)

    # 7) time-based exit — last resort after TP / giveback / BE / trail
    if te is not None and held >= te:
        return Exit("time_exit", current_price)

    return None
