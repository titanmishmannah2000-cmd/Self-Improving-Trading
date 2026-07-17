"""Exit engine (Session 5 / Phase 5) — pure exit/stop evaluator.

Single implementation shared by the live loop, backtester, and dashboard export
(discipline 1.5 + roadmap S5). NO I/O, NO network, NO hidden state: given a
trade's parameters, the current price, and a price history (for ATR trailing),
it returns either an ``Exit`` describing the action or ``None``.

Exactly ONE exit reason is ever returned per evaluation — never zero-or-many
(roadmap S5 DO-NOT). The five blueprint Phase-5 reasons:

  stop_loss      price <= entry*(1 - sl/100)
  profit_target  price >= entry*(1 + tp/100)  [hard close, no partial feature]
  partial_close  price >= entry*(1 + tp/100)  [partial feature ON -> 50% off at
                  FULL target, stop moved to breakeven for the remainder]  [GUARD L27]
  time_exit      held_cycles >= time_exit_cycles
  breakeven      unrealised >= tp*0.5  -> move stop to entry               [GUARD L26]
  trailing       ATR-based trailing stop tightening (raises the stop only)

Also exposes the [GUARD L24] circuit-breaker predicate.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes_core.indicators import compute_atr

CIRCUIT_MAX_CONSECUTIVE_FAILURES = 5  # [GUARD L24]


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


def evaluate_exit(
    trade: dict, current_price: float, prices: list[float] | None = None
) -> Exit | None:
    """Evaluate one trade for an exit/stop action. Pure + deterministic.

    ``trade`` keys: entry_price (req), stop_loss_pct, profit_target_pct,
    time_exit_cycles, held_cycles, unrealised_pct (opt), breakeven_set (bool),
    partial_done (bool), partial_enabled (bool), trailing_atr_mult (opt),
    current_stop (opt).
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

    # 1) hard stop-loss
    if sl is not None and current_price <= entry * (1 - sl / 100.0):
        return Exit("stop_loss", current_price)

    # 2) target / partial-close (both triggered at the FULL target)
    if tp is not None and current_price >= entry * (1 + tp / 100.0):
        if partial_enabled and not partial_done:
            # [GUARD L27] 50% off at full target, remainder trailed at breakeven
            return Exit("partial_close", current_price, new_stop=entry, partial_close_fraction=0.5)
        return Exit("profit_target", current_price)

    # 3) time-based exit
    if te is not None and held >= te:
        return Exit("time_exit", current_price)

    # 4) [GUARD L26] breakeven — move stop to entry once past half target
    if tp is not None and not breakeven_set and unreal >= tp * 0.5:
        return Exit("breakeven", current_price, new_stop=entry)

    # 5) ATR-based trailing stop (raises the stop only)
    mult = trade.get("trailing_atr_mult")
    if mult is not None and unreal > 0 and prices:
        atr = compute_atr(prices)
        if atr > 0:
            trail_stop = current_price - atr * mult
            cur = trade.get("current_stop")
            if cur is None or trail_stop > cur:
                return Exit("trailing", current_price, new_stop=trail_stop)

    return None
