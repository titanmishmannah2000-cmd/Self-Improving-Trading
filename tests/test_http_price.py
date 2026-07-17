"""Tests for the async REST price adapter (httpx). Network-free.

Uses httpx.MockTransport to inject canned responses — no socket is ever opened,
so the suite stays deterministic and CI-safe (matching the S0-S18 discipline).
"""

from __future__ import annotations

import httpx

from hermes_core.adapters import http_price


def _transport(handler):
    return httpx.MockTransport(handler)


def _ok_price(symbol, price=1.10, candle_ts=1000.0):
    def handler(req):
        return httpx.Response(
            200,
            json={"symbol": symbol, "price": price, "candle_ts": candle_ts},
        )
    return handler


def _ok_history(symbol, prices=(1.0, 1.05, 1.02, 1.08)):
    def handler(req):
        candles = [
            {"symbol": symbol, "price": p, "candle_ts": 1000.0 + i}
            for i, p in enumerate(prices)
        ]
        return httpx.Response(200, json={"candles": candles})
    return handler


def _err_handler(req):
    return httpx.Response(500, json={"error": "boom"})


def _raise_handler(req):
    raise httpx.ConnectError("down")


async def test_fetch_returns_candle():
    c = http_price.HttpPriceClient(
        base_url="https://fake.test", transport=_transport(_ok_price("EURUSD"))
    )
    candle = await c.fetch("EUR/USD")
    await c.aclose()
    assert candle is not None
    assert candle["price"] == 1.10
    assert candle["candle_ts"] == 1000.0


async def test_fetch_stale_guard_l01():
    """[GUARD L01] identical candle_ts -> None (no repeat delivered)."""
    c = http_price.HttpPriceClient(
        base_url="https://fake.test",
        transport=_transport(_ok_price("EURUSD", candle_ts=2000.0)),
    )
    first = await c.fetch("EUR/USD")
    second = await c.fetch("EUR/USD")  # same candle_ts -> stale
    await c.aclose()
    assert first is not None
    assert second is None  # L01 caught the repeat at the source


async def test_fetch_force_bypasses_stale():
    c = http_price.HttpPriceClient(
        base_url="https://fake.test",
        transport=_transport(_ok_price("EURUSD", candle_ts=2000.0)),
    )
    await c.fetch("EUR/USD")
    forced = await c.fetch("EUR/USD", force=True)
    await c.aclose()
    assert forced is not None  # force re-delivers


async def test_fetch_fail_soft_on_500():
    c = http_price.HttpPriceClient(
        base_url="https://fake.test", transport=_transport(_err_handler)
    )
    candle = await c.fetch("EUR/USD")
    await c.aclose()
    assert candle is None  # fail-soft, no raise


async def test_fetch_fail_soft_on_connect_error():
    c = http_price.HttpPriceClient(
        base_url="https://fake.test", transport=_transport(_raise_handler)
    )
    candle = await c.fetch("EUR/USD")
    await c.aclose()
    assert candle is None  # never raises


async def test_fetch_no_base_url_is_none():
    c = http_price.HttpPriceClient(base_url="")  # no env -> no source
    candle = await c.fetch("EUR/USD")
    await c.aclose()
    assert candle is None


async def test_seed_history_returns_candles():
    c = http_price.HttpPriceClient(
        base_url="https://fake.test", transport=_transport(_ok_history("EURUSD"))
    )
    rows = await c.seed_history("EUR/USD")
    await c.aclose()
    assert len(rows) == 4
    assert rows[0]["price"] == 1.0
    assert rows[-1]["price"] == 1.08


async def test_seed_history_fail_soft_on_error():
    c = http_price.HttpPriceClient(
        base_url="https://fake.test", transport=_transport(_err_handler)
    )
    rows = await c.seed_history("EUR/USD")
    await c.aclose()
    assert rows == []  # fail-soft


async def test_symbol_mapping_strips_slash():
    captured = {}

    def handler(req):
        captured["symbol"] = dict(req.url.params).get("symbol")
        return httpx.Response(200, json={"price": 1.1, "candle_ts": 1.0})

    c = http_price.HttpPriceClient(
        base_url="https://fake.test", transport=_transport(handler)
    )
    await c.fetch("EUR/USD")
    await c.aclose()
    assert captured["symbol"] == "EURUSD"  # mapped, no slash
