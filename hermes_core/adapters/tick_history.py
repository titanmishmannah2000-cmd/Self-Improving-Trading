"""Shared rolling tick-buffer helpers (FX consensus + crypto WS).

Only sample a new bar when price moves enough or enough wall time has passed.
Otherwise identical WS/consensus ticks collapse to one tip and indicators
degenerate into ``flat_price:unchanged`` / ``bb_bandwidth:0``.
"""

from __future__ import annotations

import time

TICK_HISTORY_MAX = 300
TICK_MOVE_MIN_PCT = 1e-5   # 0.001%
TICK_SAMPLE_MIN_S = 45.0
FLAT_TAIL_BARS = 5


def append_bucketed_tick(
    hist: list[dict],
    candle: dict,
    *,
    move_min_pct: float = TICK_MOVE_MIN_PCT,
    sample_min_s: float = TICK_SAMPLE_MIN_S,
    max_len: int = TICK_HISTORY_MAX,
) -> None:
    """Append ``candle`` to ``hist`` in-place with move/time bucketing."""
    try:
        price = float(candle.get("price") or 0)
    except (TypeError, ValueError):
        return
    if price <= 0:
        return
    try:
        ts = float(candle.get("ts") or time.time())
    except (TypeError, ValueError):
        ts = time.time()

    if hist:
        prev = hist[-1]
        try:
            prev_price = float(prev.get("price") or 0)
            prev_ts = float(prev.get("ts") or 0)
        except (TypeError, ValueError):
            prev_price, prev_ts = 0.0, 0.0
        moved = (
            prev_price > 0
            and abs(price - prev_price) / prev_price >= float(move_min_pct)
        )
        aged = (ts - prev_ts) >= float(sample_min_s)
        if not moved and not aged:
            # Refresh the tip without growing identical bars.
            hist[-1] = dict(candle)
            return

    hist.append(dict(candle))
    if len(hist) > int(max_len):
        del hist[: len(hist) - int(max_len)]


def series_is_flat(
    candles: list[dict] | list[float],
    *,
    tail: int = FLAT_TAIL_BARS,
) -> bool:
    """True when the series has no usable variation (identical closes).

    Matches the loop's ``flat_price:unchanged`` idea so crypto WS buffers full
    of duplicate ticks are rejected in favour of Yahoo / external history.
    """
    if not candles:
        return True
    prices: list[float] = []
    for c in candles:
        try:
            if isinstance(c, (int, float)):
                prices.append(float(c))
            else:
                prices.append(float(c.get("price") or 0))
        except (TypeError, ValueError, AttributeError):
            continue
    prices = [p for p in prices if p > 0]
    if len(prices) < 2:
        return True
    # Distinct prices: a real series needs more than one unique close.
    if len({round(p, 10) for p in prices}) < 2:
        return True
    n = min(int(tail), len(prices))
    if n >= FLAT_TAIL_BARS:
        tip = prices[-n:]
        if all(abs(p - tip[0]) < 1e-12 for p in tip):
            return True
    return False
