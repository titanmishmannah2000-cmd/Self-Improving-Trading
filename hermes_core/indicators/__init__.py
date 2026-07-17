"""Indicator engine (Session 3 / Phase 3) — pure, deterministic math.

Every function here is a REFACTORING-RESISTANT pure function: no file I/O, no
network, no global mutation. The live loop, the backtester, and the dashboard
export all share THESE implementations (discipline 1.5 + S3 DO-NOT). If an
indicator needs High/Low/Close, the contract passes a list of closes; we recover
the Intrabar range with the consecutive-close step (a bounded, documented
approximation) so the function stays close-only and still satisfies the
no-I/O purity rule.
"""

from __future__ import annotations

import statistics


def compute_rsi(prices: list[float], period: int = 14) -> float:
    """Wilder RSI. Returns 50.0 (neutral) when fewer than ``period+1`` closes."""
    if len(prices) <= period:
        return 50.0
    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(changes)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def compute_atr(prices: list[float], period: int = 14) -> float:
    """Average True Range from a close-only series.

    True Range needs H/L/C; with closes only we use the step between consecutive
    closes as the realized range (bounded, monotonic in volatility). Flat series
    -> all steps 0 -> ATR 0 (the blueprint's test_atr_flat_zero gate).
    """
    if len(prices) < 2:
        return 0.0
    steps = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    if len(steps) < period:
        return sum(steps) / len(steps) if steps else 0.0
    atr = sum(steps[:period]) / period
    for v in steps[period:]:
        atr = (atr * (period - 1) + v) / period
    return atr


def compute_adx(prices: list[float], period: int = 14) -> float:
    """Wilder ADX (trend strength), bounded to [0, 100].

    Close-only: directional movement is recovered from consecutive-close steps.
    Returns 0.0 when there is not enough data to form a smoothed DX series.
    """
    n = len(prices)
    if n < period * 2:
        return 0.0

    tr = [0.0] * n
    pdm = [0.0] * n
    mdm = [0.0] * n
    for i in range(1, n):
        up = prices[i] - prices[i - 1]
        tr[i] = abs(up)
        pdm[i] = max(up, 0.0)
        mdm[i] = max(-up, 0.0)

    def smooth(arr: list[float]) -> list[float]:
        out = [0.0] * n
        out[1] = arr[1]
        for i in range(2, n):
            out[i] = (out[i - 1] * (period - 1) + arr[i]) / period
        return out

    ATR, PDM, MDM = smooth(tr), smooth(pdm), smooth(mdm)
    pdi = [0.0] * n
    mdi = [0.0] * n
    dx = [0.0] * n
    for i in range(1, n):
        if ATR[i] > 1e-9:
            pdi[i] = 100.0 * PDM[i] / ATR[i]
            mdi[i] = 100.0 * MDM[i] / ATR[i]
            denom = pdi[i] + mdi[i]
            dx[i] = 100.0 * abs(pdi[i] - mdi[i]) / denom if denom > 1e-9 else 0.0

    valid = [dx[i] for i in range(period, n)]
    if not valid:
        return 0.0
    adx = sum(valid[:period]) / period
    for v in valid[period:]:
        adx = (adx * (period - 1) + v) / period
    return max(0.0, min(100.0, adx))


def compute_bb(prices: list[float], period: int = 20, mult: float = 1.5) -> dict[str, float]:
    """Bollinger Bands. Uses the trailing ``period`` closes (all if shorter)."""
    window = prices[-period:] if len(prices) >= period else prices
    if not window:
        return {"lower": 0.0, "middle": 0.0, "upper": 0.0}
    mid = statistics.mean(window)
    sd = statistics.pstdev(window)
    return {"lower": mid - mult * sd, "middle": mid, "upper": mid + mult * sd}


def compute_roc(prices: list[float], period: int = 20) -> float:
    """Rate of Change (percent) over ``period`` closes."""
    if len(prices) <= period:
        return 0.0
    prev = prices[-1 - period]
    last = prices[-1]
    if prev == 0.0:
        return 0.0
    return (last - prev) / prev * 100.0


def _detect_divergence(prices: list[float], rsi: float) -> str:
    """Heuristic RSI/price divergence over the recent window.

    Returns 'bullish' / 'bearish' / 'none'. Pure and bounded; does not affect
    entry/exit on its own (downstream engines decide), it is a context signal.
    """
    if len(prices) < 10:
        return "none"
    rsi_prev = compute_rsi(prices[:-3]) if len(prices) > 17 else rsi
    price_up = prices[-1] > prices[-5]
    rsi_up = rsi > rsi_prev
    if price_up and not rsi_up:
        return "bearish"
    if not price_up and rsi_up:
        return "bullish"
    return "none"


def compute_all(prices: list[float]) -> dict:
    """IndicatorEngine contract (Section 6): one dict, eight keys.

    {rsi, atr, adx, bb, roc, regime, fast_regime, divergence}
    ``regime`` = 'trend' if ADX>=25 else 'range'; ``fast_regime`` is the short
    ROC sign ('up'/'down'/'flat').
    """
    rsi = compute_rsi(prices)
    atr = compute_atr(prices)
    adx = compute_adx(prices)
    bb = compute_bb(prices)
    roc = compute_roc(prices)
    fast = compute_roc(prices, 5)
    regime = "trend" if adx >= 25 else "range"
    fast_regime = "up" if fast > 0 else ("down" if fast < 0 else "flat")
    divergence = _detect_divergence(prices, rsi)
    return {
        "rsi": rsi,
        "atr": atr,
        "adx": adx,
        "bb": bb,
        "roc": roc,
        "regime": regime,
        "fast_regime": fast_regime,
        "divergence": divergence,
    }
