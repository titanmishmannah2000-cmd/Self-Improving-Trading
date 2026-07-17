"""Session 2 / Phase 2 acceptance tests for the price adapter.

Mirrors the blueprint Phase 2 block (test_fetch_keys_non_none,
test_stale_candle_guard, test_seed_history_count, test_yfinance_timeout_returns_none)
plus a golden-master for L01: a repeated candle_ts always yields None on the
second call, permanently.

All network calls are mocked, so the suite is deterministic and offline. A
separate live probe (run in the terminal) provides real-feed evidence; the unit
suite is the binding gate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from hermes_core.adapters import price


def _fake_df(rows: int = 5) -> pd.DataFrame:
    """Build a yfinance-like single-ticker DataFrame with ``rows`` candles."""
    idx = pd.date_range("2024-01-01", periods=rows, freq="5min")
    data = {
        "Open": [1.0] * rows,
        "High": [1.2] * rows,
        "Low": [0.9] * rows,
        "Close": [1.1] * rows,
        "Adj Close": [1.1] * rows,
        "Volume": [100] * rows,
    }
    return pd.DataFrame(data, index=idx)


@pytest.fixture(autouse=True)
def _reset_guard():
    price._last_candle_ts.clear()
    yield
    price._last_candle_ts.clear()


async def test_fetch_keys_non_none():
    with patch("yfinance.download", return_value=_fake_df(rows=5)):
        r = await price.fetch("EUR/USD")
    assert r is not None
    for k in ("price", "high", "low", "candle_ts", "ts"):
        assert r.get(k) is not None


async def test_stale_candle_guard():
    # [GUARD L01] With the guard in the adapter: the first delivery returns a
    # candle; the second, identical candle_ts within the window, returns None
    # (permanently). This is the behavior that prevents the 198 fake-trade bug —
    # the blueprint's literal `if a and b and a["candle_ts"]==b["candle_ts"]`
    # test is internally contradictory once b is correctly None, so we assert
    # the correct outcome directly.
    df = _fake_df(rows=5)
    with patch("yfinance.download", return_value=df):
        a = await price.fetch("EUR/USD")
        b = await price.fetch("EUR/USD")
    assert a is not None
    assert b is None


async def test_seed_history_count():
    with patch("yfinance.download", return_value=_fake_df(rows=350)):
        h = await price.seed_history("BTC-USD")
    assert len(h) == 300  # blueprint cap: up to 300 candles


async def test_yfinance_timeout_returns_none():
    # speed up: collapse the retry backoff so the test doesn't sleep ~14s
    with patch("yfinance.download", side_effect=TimeoutError), patch(
        "hermes_core.adapters.price.asyncio.sleep", new=AsyncMock()
    ):
        r = await price.fetch("EUR/USD")
    assert r is None  # graceful, no exception


async def test_stale_guard_repeat_permanently_none():
    df = _fake_df(rows=3)
    with patch("yfinance.download", return_value=df):
        first = await price.fetch("EUR/USD")
        second = await price.fetch("EUR/USD")
        third = await price.fetch("EUR/USD")
    assert first is not None
    assert second is None
    assert third is None  # permanently, until a newer candle_ts arrives
