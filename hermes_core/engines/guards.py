"""Market-data validity guards (L02/L03).

Pure helpers shared by the live loop and tests. No I/O.
"""

from __future__ import annotations

FLAT_BARS_MIN = 5
# FX live-tick BB bandwidth often lands ~0.0004–0.0006; the old 0.001 floor
# blocked essentially all EUR/GBP/JPY mean-reversion. 0.0003 still rejects
# degenerate flat bands (bw≈0) while allowing that live FX range.
BB_BW_MIN = 0.0003


def flat_price_guard(indicators: dict, prices: list[float]) -> tuple[bool, str]:
    """[GUARD L02] Skip stale/degenerate data (flat weekend candles).

    Blueprint trigger: rsi < 0.01 and roc == 0 and adx in (None, 0).
    Also catches N consecutive unchanged closes (roadmap flat-price gate).
    """
    rsi = float(indicators.get("rsi", 50))
    roc = float(indicators.get("roc", 0))
    adx = indicators.get("adx", 0)
    if rsi < 0.01 and roc == 0.0 and adx in (None, 0, 0.0):
        return True, "flat_price:degenerate_indicators"
    if len(prices) >= FLAT_BARS_MIN:
        tail = prices[-FLAT_BARS_MIN:]
        if all(abs(p - tail[0]) < 1e-12 for p in tail):
            return True, "flat_price:unchanged"
    return False, ""


# Rolling bw samples for soak ops (measure before retuning BB_BW_MIN).
_BW_SAMPLES: list[float] = []
_BW_SAMPLE_MAX = 500


def bb_bandwidth_samples() -> list[float]:
    return list(_BW_SAMPLES)


def bb_bandwidth_guard(bb: dict, threshold: float = BB_BW_MIN) -> tuple[bool, str]:
    """[GUARD L03] Skip MR when Bollinger bandwidth is too narrow for edge."""
    middle = float(bb.get("middle", 0) or 0)
    if middle <= 0:
        return True, "bb_bandwidth:zero_middle"
    upper = float(bb.get("upper", middle))
    lower = float(bb.get("lower", middle))
    bw = (upper - lower) / middle
    try:
        _BW_SAMPLES.append(float(bw))
        if len(_BW_SAMPLES) > _BW_SAMPLE_MAX:
            del _BW_SAMPLES[: len(_BW_SAMPLES) - _BW_SAMPLE_MAX]
    except Exception:  # noqa: BLE001
        pass
    if bw < threshold:
        return True, f"bb_bandwidth:{bw:.6f}"
    return False, ""
