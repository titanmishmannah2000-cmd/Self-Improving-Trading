"""Wiring tests for #1b: the sync loop can use the price adapters without await.

Network-free. Proves the latent coroutine-leak (loop called the async fetch
without await) is fixed and that BOTH backends are usable as a sync fetch_fn.
"""

from __future__ import annotations

import inspect

import httpx

from hermes_core.adapters import fetch, make_default_fetch
from hermes_core.adapters.http_price import HttpPriceClient
from hermes_core.engines.loop import run_cycle


# ── default fetch is synchronous (coroutine-leak fixed) ──────────────────────
def test_default_fetch_is_sync_and_returns_dict(monkeypatch):
    """The loop calls ``fetch_fn(pair)`` without await -> default must be sync."""

    async def fake_async(pair, force=False):
        return {"price": 1.1, "high": 1.1, "low": 1.1, "candle_ts": 1.0, "ts": 1.0}

    monkeypatch.setattr("hermes_core.adapters.price.fetch", fake_async)
    # reload the sync wrapper resolution
    from hermes_core.adapters import price as price_mod

    monkeypatch.setattr(price_mod, "fetch", fake_async)

    candle = fetch("EUR/USD")
    assert not inspect.iscoroutine(candle), "default fetch leaked a coroutine!"
    assert isinstance(candle, dict)
    assert candle["price"] == 1.1


def test_default_fetch_fail_soft_on_exception(monkeypatch):
    async def boom(pair, force=False):
        raise RuntimeError("feed down")

    from hermes_core.adapters import price as price_mod

    monkeypatch.setattr(price_mod, "fetch", boom)
    candle = fetch("EUR/USD")
    assert candle is None  # fail-soft, no raise


# ── httpx backend usable as a sync fetch_fn ───────────────────────────────────
def _ok_transport(req):
    return httpx.Response(200, json={"price": 1.10, "candle_ts": 1000.0})


def test_http_backend_sync_fetch_via_mock():
    c = HttpPriceClient(
        base_url="https://fake.test", transport=httpx.MockTransport(_ok_transport)
    )
    candle = c.fetch_sync("EUR/USD")
    assert candle is not None
    assert candle["price"] == 1.10


def test_make_default_fetch_backend_switch():
    fn = make_default_fetch(backend="http", api_url="https://fake.test")
    # http backend, no network: returns None (no base reachable) — must not raise
    assert callable(fn)
    candle = fn("EUR/USD")
    assert candle is None  # fail-soft without a live transport


# ── loop uses the real default fetch without crashing (no unawaited coroutine) ─
def test_loop_runs_with_real_default_fetch(monkeypatch, tmp_path):
    """run_cycle with NO injected fetch_fn must use the default sync adapter and
    complete without a coroutine TypeError."""

    async def fake_async(pair, force=False):
        return {"price": 1.10, "high": 1.10, "low": 1.10, "candle_ts": 1.0, "ts": 1.0}

    from hermes_core.adapters import price as price_mod

    monkeypatch.setattr(price_mod, "fetch", fake_async)
    monkeypatch.setattr(price_mod, "seed_history", lambda p, m=300: [])

    reg: dict = {}
    # forex EUR/USD is london_only; use a NY-agnostic path by forcing session via
    # a neutral pair is not available, so just assert no coroutine crash occurs.
    summary = run_cycle(
        "forex", 1,
        health_registry=reg,
        now_fn=lambda: 10 * 3600.0,
        chart_context_fn=lambda p: "",
        ensemble_fn=lambda p: "neutral",
        consecutive_failures=0,
    )
    assert isinstance(summary, dict)
    assert "cycle" in summary
    # price_adapter health must reflect the (mocked) fetch succeeding
    assert reg.get("price_adapter") is True
