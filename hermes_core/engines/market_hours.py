"""Market-hours helpers for paper-soak weekend / holiday gating.

FX spot convention (matches dashboard countdown):
  closed from Friday 22:00 UTC through Sunday 22:00 UTC.

Metals (COMEX-style) use the same window for soak — free GoldAPI quotes
also go quiet over the weekend, and treating them as open produced
frozen marks with ``market_closed=false``.
"""

from __future__ import annotations

from datetime import UTC, datetime

# Friday 22:00 UTC → Sunday 22:00 UTC (inclusive of Fri after close).
_FX_CLOSE_WEEKDAY = 4  # Friday
_FX_OPEN_WEEKDAY = 6  # Sunday
_FX_BOUNDARY_HOUR = 22


def is_fx_market_closed(now_ts: float | None = None) -> bool:
    """True during the standard FX weekend gap."""
    import time as _time

    dt = datetime.fromtimestamp(now_ts if now_ts is not None else _time.time(), tz=UTC)
    wd = dt.weekday()
    hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
    if wd == _FX_CLOSE_WEEKDAY and hour >= _FX_BOUNDARY_HOUR:
        return True
    if wd == 5:  # Saturday
        return True
    if wd == _FX_OPEN_WEEKDAY and hour < _FX_BOUNDARY_HOUR:
        return True
    return False


def is_metals_market_closed(now_ts: float | None = None) -> bool:
    """Metals weekend gate — same boundary as FX for soak consistency."""
    return is_fx_market_closed(now_ts)


def is_bot_market_closed(bot: str, now_ts: float | None = None) -> bool:
    """Bot-aware calendar close. Crypto trades 24/7 → never calendar-closed."""
    b = (bot or "").strip().lower()
    if b == "forex":
        return is_fx_market_closed(now_ts)
    if b == "gold":
        return is_metals_market_closed(now_ts)
    return False


def live_book_is_flat(
    price_history: dict[str, list] | None,
    *,
    min_pairs: int = 1,
    flat_tail: int = 5,
    eps: float = 1e-12,
) -> bool:
    """True when every pair with enough history has an identical-price tail.

    Used as a holiday/feed-freeze backup when the calendar says open but the
    live tick buffer has stopped moving (all majors stuck on one print).
    """
    if not price_history:
        return False
    flat_n = 0
    eligible = 0
    for _pair, hist in price_history.items():
        if not isinstance(hist, list) or len(hist) < flat_tail:
            continue
        eligible += 1
        tail = hist[-flat_tail:]
        try:
            nums = [float(x) for x in tail]
        except (TypeError, ValueError):
            continue
        if nums and all(abs(p - nums[0]) < eps for p in nums):
            flat_n += 1
    return eligible >= min_pairs and flat_n == eligible and flat_n > 0
