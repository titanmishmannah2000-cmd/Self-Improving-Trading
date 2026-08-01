"""BTC/USDT Focus Phase 2 — daily regime gate.

Higher-TF market type for crypto spot v1:
  * trend_up   → allow long momentum / GP
  * trend_down → hard flat (no shorts in v1)
  * chop       → hard flat (no MR sleeve)

Labels use D1 SMA50/SMA200 + ADX≥25. Persist last label to
``{bot}/state/regime.jsonl`` and expose via ``classify_btc_regime``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from hermes_core.adapters.pair_aliases import is_crypto_pair
from hermes_core.state.paths import bot_state_dir

TREND_UP = "trend_up"
TREND_DOWN = "trend_down"
CHOP = "chop"
ADX_TREND = 25.0


def _sma(xs: list[float], n: int) -> float | None:
    if len(xs) < n or n <= 0:
        return None
    window = xs[-n:]
    return sum(window) / float(n)


def _adx_approx(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> float:
    """Lightweight ADX approximation from OHLC lists (fail-soft → 0)."""
    if len(closes) < n + 2:
        return 0.0
    try:
        trs: list[float] = []
        plus_dm: list[float] = []
        minus_dm: list[float] = []
        for i in range(1, len(closes)):
            h, l, c_prev = highs[i], lows[i], closes[i - 1]
            tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
            up = highs[i] - highs[i - 1]
            dn = lows[i - 1] - lows[i]
            plus_dm.append(up if up > dn and up > 0 else 0.0)
            minus_dm.append(dn if dn > up and dn > 0 else 0.0)
            trs.append(tr)
        if len(trs) < n:
            return 0.0
        atr = sum(trs[-n:]) / n
        if atr <= 0:
            return 0.0
        pdi = 100.0 * (sum(plus_dm[-n:]) / n) / atr
        mdi = 100.0 * (sum(minus_dm[-n:]) / n) / atr
        dx = 100.0 * abs(pdi - mdi) / max(pdi + mdi, 1e-9)
        return float(dx)
    except Exception:  # noqa: BLE001
        return 0.0


def classify_from_closes(
    closes: list[float],
    *,
    highs: list[float] | None = None,
    lows: list[float] | None = None,
) -> dict[str, Any]:
    """Classify regime from daily closes (and optional high/low for ADX)."""
    xs = [float(x) for x in closes if x is not None]
    if len(xs) < 50:
        return {
            "label": CHOP,
            "reason": "insufficient_bars",
            "sma50": None,
            "sma200": None,
            "adx": 0.0,
            "last": xs[-1] if xs else None,
        }
    last = xs[-1]
    s50 = _sma(xs, 50)
    s200 = _sma(xs, 200) if len(xs) >= 200 else _sma(xs, min(200, len(xs)))
    hs = highs if highs is not None else xs
    ls = lows if lows is not None else xs
    adx = _adx_approx(
        [float(h) for h in hs[-max(60, len(xs)) :]],
        [float(l) for l in ls[-max(60, len(xs)) :]],
        xs[-max(60, len(xs)) :],
    )
    label = CHOP
    reason = "range_or_weak_adx"
    if s50 is not None and s200 is not None and adx >= ADX_TREND:
        if last > s50 > s200:
            label = TREND_UP
            reason = "price_gt_sma50_gt_sma200_adx"
        elif last < s50 < s200:
            label = TREND_DOWN
            reason = "price_lt_sma50_lt_sma200_adx"
        else:
            reason = "mixed_sma_stack"
    elif s50 is not None and s200 is not None:
        if last > s50 > s200:
            # Weak ADX but stacked bullish — still chop for v1 (hard selectivity).
            reason = "bullish_stack_weak_adx"
        elif last < s50 < s200:
            reason = "bearish_stack_weak_adx"
    return {
        "label": label,
        "reason": reason,
        "sma50": s50,
        "sma200": s200,
        "adx": adx,
        "last": last,
    }


def fetch_daily_closes(pair: str, *, max_candles: int = 260) -> list[dict]:
    """Daily OHLC candles for ``pair`` (fail-soft → [])."""
    try:
        from hermes_core.adapters.price import seed_history_interval_sync

        return (
            seed_history_interval_sync(
                pair, interval="1d", period="2y", max_candles=max_candles
            )
            or []
        )
    except Exception:  # noqa: BLE001
        return []


_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL_S = 3600.0


def classify_btc_regime(pair: str, *, force: bool = False) -> dict[str, Any]:
    """Fetch D1 history and classify. Non-crypto pairs return passthrough chop skip."""
    now = time.time()
    if not force:
        hit = _CACHE.get(pair)
        if hit and (now - hit[0]) < _CACHE_TTL_S:
            return dict(hit[1])
    out: dict[str, Any] = {
        "pair": pair,
        "label": CHOP,
        "reason": "not_crypto",
        "ts": now,
    }
    if not is_crypto_pair(pair) and not str(pair).upper().startswith("BTC/"):
        out["reason"] = "not_btc_focus"
        _CACHE[pair] = (now, out)
        return out
    bars = fetch_daily_closes(pair)
    if not bars:
        out["reason"] = "no_daily_history"
        _CACHE[pair] = (now, out)
        return out
    closes = [float(b["price"]) for b in bars if b.get("price") is not None]
    highs = [float(b.get("high", b["price"])) for b in bars if b.get("price") is not None]
    lows = [float(b.get("low", b["price"])) for b in bars if b.get("price") is not None]
    cls = classify_from_closes(closes, highs=highs, lows=lows)
    out.update(cls)
    out["pair"] = pair
    out["ts"] = now
    out["n_bars"] = len(closes)
    _CACHE[pair] = (now, out)
    return out


def allows_long(label: str) -> bool:
    return (label or "").strip().lower() == TREND_UP


def hard_blocks_entry(label: str) -> bool:
    """True when spot v1 must skip (chop or downtrend)."""
    return (label or "").strip().lower() != TREND_UP


def append_regime_log(bot: str, rec: dict) -> None:
    """Append one regime snapshot to ``regime.jsonl`` (fail-soft)."""
    try:
        path: Path = bot_state_dir(bot) / "regime.jsonl"
        line = json.dumps(rec, default=str) + "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001
        pass
