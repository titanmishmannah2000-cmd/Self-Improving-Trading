"""Market data price adapter (Session 2 / Phase 2).

Single source of live prices. Implements the blueprint Candle contract
(price, high, low, candle_ts, ts) and the **L01 stale-candle guard INSIDE the
adapter at the source** — not only in the loop. The original bug (loop.py:1612)
placed the guard in the loop, so any caller hitting the adapter directly
bypassed it and produced 198 fake weekend trades. Here the guard fires the
moment a candle is read, so no caller can get a stale repeat.

Discipline D3 (fail-soft): fetch and seed_history NEVER raise. On
timeout/error/empty they return None / [] after at most RETRY_ATTEMPTS retries
(base delay RETRY_BASE_DELAY, exponential backoff).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pandas as pd
import yfinance as yf

# [GUARD L60] yfinance persistent cache (S19): direct Yahoo's timezone + cookie
# cache to a project-local dir so yfinance reuses a valid session instead of
# re-handshaking every call. Reduces "feed unavailable" blips and Yahoo IP-bans.
# Fail-soft: a missing/readonly cache dir must never crash the adapter.
try:
    from pathlib import Path

    _yf_cache_dir = Path(__file__).resolve().parents[2] / ".cache" / "py-yfinance"
    _yf_cache_dir.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(_yf_cache_dir))
except Exception:  # noqa: BLE001 — caching is an optimisation, never fatal
    pass

# [GUARD L01] retry envelope — cap retries so a dead feed can't hang the loop.
RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 2.0  # seconds; doubled each attempt

# [GUARD L01] stale-candle window: same candle_ts within this many seconds → None.
STALE_WINDOW_S = 60.0

# In-process guard state, keyed by pair. Lives for the adapter process lifetime
# (the loop's lifetime in production), so the stale check is authoritative here.
_last_candle_ts: dict[str, float] = {}


def _to_symbol(pair: str) -> str:
    """Map a HERMES pair to a yfinance ticker symbol."""
    if "-" in pair:  # crypto reference, e.g. BTC-USD
        return pair
    if pair == "XAU/USD":
        return "GC=F"
    if pair == "XAG/USD":
        return "XAG=F"
    # forex: EUR/USD -> EURUSD=X
    return pair.replace("/", "") + "=X"


def _normalize(df: Any) -> pd.DataFrame | None:
    if df is None or not hasattr(df, "empty") or df.empty:
        return None
    # collapse MultiIndex columns (yfinance can return (field, ticker) tuples)
    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = [c[0] for c in df.columns]
    return df


def _row_to_candle(row: pd.Series, candle_ts: float, fetch_ts: float) -> dict:
    return {
        "price": float(row["Close"]),
        "high": float(row["High"]),
        "low": float(row["Low"]),
        "candle_ts": candle_ts,
        "ts": fetch_ts,
    }


async def _download(symbol: str, period: str, interval: str) -> pd.DataFrame | None:
    """Retry-enveloped yfinance download. [GUARD L01] returns None on failure."""
    for attempt in range(RETRY_ATTEMPTS):
        try:
            df = yf.download(
                symbol,
                period=period,
                interval=interval,
                auto_adjust=False,
                progress=False,
            )
            norm = _normalize(df)
            if norm is not None:
                return norm
            return None
        except Exception:  # noqa: BLE001 — fail-soft contract: swallow + retry
            if attempt < RETRY_ATTEMPTS - 1:
                await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
    # exhausted retries — feed unavailable, fail soft
    return None


async def fetch(pair: str, force: bool = False) -> dict | None:
    """Fetch the latest 5-minute candle for ``pair``.

    Returns a Candle dict {price, high, low, candle_ts, ts}, or None if the feed
    is empty/stale/errored. [GUARD L01] A candle whose candle_ts equals the last
    delivered candle_ts within STALE_WINDOW_S is stale and returns None —
    permanently, until a newer candle_ts arrives. ``force=True`` bypasses the
    stale check (used on explicit re-seed).
    """
    symbol = _to_symbol(pair)
    df = await _download(symbol, period="1d", interval="5m")
    if df is None or len(df) == 0:
        return None

    last = df.iloc[-1]
    candle_ts = float(pd.Timestamp(df.index[-1]).timestamp())
    fetch_ts = time.time()

    # [GUARD L01] stale-candle: identical candle_ts → None (never deliver a repeat).
    prev = _last_candle_ts.get(pair)
    if not force and prev is not None and prev == candle_ts:
        return None

    _last_candle_ts[pair] = candle_ts
    return _row_to_candle(last, candle_ts, fetch_ts)


async def seed_history(pair: str, max_candles: int = 300) -> list:
    """Return the most recent ``max_candles`` (default 300) candles for ``pair``.

    Bootstraps indicator state on cold start (blueprint: up to 300 candles).
    Returns a list of Candle dicts, oldest-first, capped at ``max_candles``.
    Returns [] if the feed is unavailable (fail-soft).
    """
    symbol = _to_symbol(pair)
    df = await _download(symbol, period="60d", interval="5m")
    if df is None or len(df) == 0:
        return []
    tail = df.tail(max_candles)
    out: list[dict] = []
    fetch_ts = time.time()
    for idx, row in tail.iterrows():
        candle_ts = float(pd.Timestamp(idx).timestamp())
        out.append(_row_to_candle(row, candle_ts, fetch_ts))
    return out


def _run(coro):
    """Run a coroutine to completion, fail-soft, from sync code.

    Tolerates an already-running event loop (e.g. pytest-asyncio) by reusing
    the current loop instead of asyncio.run, which would raise. Any feed
    exception inside the coroutine is swallowed -> None (fail-soft contract:
    the loop never crashes on a bad price fetch).
    """
    try:
        return asyncio.run(coro)
    except RuntimeError as exc:
        msg = str(exc)
        if "already running" in msg or "cannot run loop" in msg:
            loop = asyncio.get_event_loop()
            try:
                return loop.run_until_complete(coro)
            except Exception:
                return None
        return None
    except Exception:
        return None


def fetch_sync(pair: str, force: bool = False) -> dict | None:
    """Synchronous wrapper around :func:`fetch` for the sync trade loop.

    The loop calls ``fetch_fn(pair)`` without await, so the default fetch must
    be synchronous. This wraps the async fetch safely (no coroutine leak).
    """
    return _run(fetch(pair, force=force))


def seed_history_sync(pair: str, max_candles: int = 300) -> list:
    """Synchronous wrapper around :func:`seed_history` for the sync trade loop."""
    return _run(seed_history(pair, max_candles=max_candles))
