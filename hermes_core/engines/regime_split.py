"""Regime split — MR in ranges, trend-follow in uptrends; chart avoid is sleeve-aware.

Profitability Path: stop using one playbook for every tape.

* **range** → mean reversion (buy dips)
* **trend_up** → trend follow (buy strength)
* **trend_down** → no new longs
* Chart ``avoid`` kills **MR only when the tape is not a confirmed range**;
  in a true range, avoid becomes a soft size tilt (vision false positives).
* Trend sleeve is blocked by avoid+downtrend (wrong direction for longs).
* Cost-aware: require ATR% large enough vs round-trip cost haircut.

Flag ``REGIME_SPLIT``: ``1`` on, ``0`` off. Unset → on for ``forex`` only.
Never raises.
"""

from __future__ import annotations

from hermes_core.engines.chart_vision import hard_block
from hermes_core.env import get_env

_DEFAULT_BOTS = frozenset({"forex"})
ADX_TREND = 25.0
DEFAULT_COST_PCT = 0.05
COST_ATR_MULT = 2.0  # need ATR% >= cost * this


def regime_split_enabled(*, bot: str | None = None, strategy: dict | None = None) -> bool:
    """Env wins; strategy ``regime_split`` / ``entry.regime_split`` can force on/off."""
    if isinstance(strategy, dict):
        entry = strategy.get("entry") if isinstance(strategy.get("entry"), dict) else {}
        raw_s = strategy.get("regime_split", entry.get("regime_split") if entry else None)
        if raw_s is True or str(raw_s).strip().lower() in ("1", "true", "yes", "on"):
            return True
        if raw_s is False or str(raw_s).strip().lower() in ("0", "false", "no", "off"):
            return False
    raw = (get_env("REGIME_SPLIT", "") or "").strip()
    if raw == "1":
        return True
    if raw == "0":
        return False
    return (bot or "").strip().lower() in _DEFAULT_BOTS


def _ctx(context: str | None) -> str:
    return (context or "").lower()


def classify_market(
    *,
    adx: float | None,
    regime: str | None = None,
    context: str | None = None,
    adx_trend: float = ADX_TREND,
) -> str:
    """Return ``range`` | ``trend_up`` | ``trend_down`` | ``unknown``."""
    c = _ctx(context)
    reg = (regime or "").strip().lower()
    try:
        adx_f = float(adx) if adx is not None else 0.0
    except (TypeError, ValueError):
        adx_f = 0.0

    chart_down = "downtrend" in c
    chart_up = "uptrend" in c
    chart_side = "sideways" in c or "range" in c
    avoid = hard_block(c)

    # Strong chart direction wins when ADX confirms a trend.
    if adx_f >= adx_trend:
        if chart_down or avoid and not chart_up:
            return "trend_down"
        if chart_up or "enter long" in c or reg in ("trend", "bull"):
            return "trend_up"
        if reg in ("bear",):
            return "trend_down"
        if reg in ("trend", "bull"):
            return "trend_up"
        # Trending ADX but no chart cue — treat as trend_up only if regime says trend.
        if reg in ("range", "neutral"):
            return "range"
        return "trend_up" if not chart_down else "trend_down"

    # Calm ADX → range unless chart is clearly directional with avoid.
    if chart_side or reg in ("range", "neutral", ""):
        return "range"
    if chart_down and avoid:
        return "trend_down"
    if chart_up:
        return "trend_up"
    if chart_down:
        return "trend_down"
    return "range" if adx_f < adx_trend else "unknown"


def pick_sleeve(market: str) -> str | None:
    """Map market class → entry sleeve. ``None`` = no long entry."""
    if market == "range":
        return "mean_reversion"
    if market == "trend_up":
        return "trend_follow"
    if market == "trend_down":
        return None
    return "mean_reversion"


def chart_blocks_sleeve(
    context: str | None,
    *,
    sleeve: str | None,
    market: str,
) -> bool:
    """Sleeve-aware L14: avoid does not blanket-ban every playbook.

    * MR: blocked on ``trend_down``; blocked on avoid when market is *not* range.
      In a confirmed range, avoid is soft-only (caller applies size tilt).
    * trend_follow: blocked on ``trend_down`` or avoid+downtrend (wrong way long).
    * GP / unknown: keep legacy hard avoid.
    """
    c = _ctx(context)
    avoid = hard_block(c)
    down = "downtrend" in c
    et = (sleeve or "").strip().lower()

    if et in ("mean_reversion", "mr"):
        if market == "trend_down":
            return True
        if avoid and market != "range":
            return True
        return False

    if et in ("trend_follow", "rsi_momentum"):
        if market == "trend_down":
            return True
        if avoid and down:
            return True
        if avoid and "uptrend" not in c and "enter long" not in c:
            return True
        return False

    # gp_ensemble and anything else: legacy capital veto on avoid
    return avoid


def cost_aware_ok(
    atr: float | None,
    last: float | None,
    *,
    cost_pct: float | None = None,
    min_mult: float | None = None,
    pair: str | None = None,
) -> bool:
    """True when ATR% is large enough to clear a round-trip cost haircut.

    FX majors move in small ATR% — requiring 2× cost silenced almost all
    entries. Forex pairs use ``min_mult=1.0`` (and a soft floor of 0.02%);
    other bots keep the stricter 2× default.
    """
    try:
        px = float(last or 0.0)
        a = float(atr or 0.0)
    except (TypeError, ValueError):
        return False
    if px <= 0 or a < 0:
        return False
    if cost_pct is None:
        raw = (get_env("SCORECARD_COST_PCT", "") or "").strip()
        try:
            cost = float(raw) if raw else DEFAULT_COST_PCT
        except ValueError:
            cost = DEFAULT_COST_PCT
    else:
        cost = float(cost_pct)
    cost = max(0.0, cost)
    if cost <= 0:
        return True
    atr_pct = (a / px) * 100.0
    fx = (pair or "").upper() in {"EUR/USD", "GBP/USD", "AUD/USD", "GBP/JPY"}
    if min_mult is None:
        min_mult = 1.0 if fx else COST_ATR_MULT
    need = cost * float(min_mult)
    if fx:
        need = max(need, 0.02)  # ignore noise ticks, not whole FX ATR band
    return atr_pct >= need
