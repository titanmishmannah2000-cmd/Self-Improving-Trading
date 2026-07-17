"""Tests for the async bot runner's real-time price push (S19 feature).

Network-free: httpx.Client is monkeypatched to capture the request, and the
price feed is monkeypatched so run_cycle produces a price snapshot. Proves the
bot forwards its per-cycle prices (and live crypto ticks) to the dashboard.
"""

from __future__ import annotations

import time

import bots._runner as r


class _FakeResp:
    status_code = 200


class _FakeClient:
    """Captures posts; acts as the persistent module-level client."""

    def __init__(self, *a, **k):
        self.posts = []

    def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return _FakeResp()


def _install_fake_client(monkeypatch):
    # Reset the module-level singleton so each test gets a fresh fake.
    r._PUSH_CLIENT = None
    fake = _FakeClient()
    monkeypatch.setattr("bots._runner.httpx.Client", lambda *a, **k: fake)
    return fake


def test_push_prices_posts_to_dashboard(monkeypatch):
    fake = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DASHBOARD_API_URL", "http://dash:8000")
    monkeypatch.setenv("INGEST_TOKEN", "tok")

    r._push_prices("forex", {"EUR/USD": 1.1234})
    assert len(fake.posts) == 1
    sent = fake.posts[0]
    assert sent["url"] == "http://dash:8000/api/price/forex"
    assert sent["json"] == {"prices": {"EUR/USD": 1.1234}}
    assert sent["headers"]["X-Ingest-Token"] == "tok"


def test_push_prices_noop_without_dashboard(monkeypatch):
    # No DASHBOARD_API_URL -> must not attempt a network call / must not raise.
    r._PUSH_CLIENT = None
    monkeypatch.delenv("DASHBOARD_API_URL", raising=False)
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    fake = _FakeClient()
    monkeypatch.setattr("bots._runner.httpx.Client", lambda *a, **k: fake)
    r._push_prices("forex", {"EUR/USD": 1.1})
    assert len(fake.posts) == 0


def test_make_fetcher_returns_callable_for_yfinance(monkeypatch):
    monkeypatch.setenv("PRICE_BACKEND", "yfinance")  # override the local .env aggregate
    fetch_fn, aggregator = r._make_fetcher("forex", ["EUR/USD"])
    assert callable(fetch_fn)
    assert aggregator is None  # yfinance path has no live aggregator


def test_make_fetcher_aggregate_wires_on_tick():
    import os

    os.environ["PRICE_BACKEND"] = "aggregate"
    try:
        fetch_fn, aggregator = r._make_fetcher("crypto", ["BTC/USD", "ETH/USD"])
        assert callable(fetch_fn)
        assert aggregator is not None
        # the aggregator's crypto stream must carry the tick forwarder
        assert aggregator._crypto.on_tick is not None
    finally:
        os.environ.pop("PRICE_BACKEND", None)


def test_on_tick_forwarder_is_throttled(monkeypatch):
    # A tick storm must NOT produce one POST per tick; it must be throttled to
    # at most one push per (bot,pair) per _TICK_MIN_INTERVAL seconds. This is
    # what prevents the SYN_SENT connection pileup under a live crypto WS feed.
    fake = _install_fake_client(monkeypatch)
    monkeypatch.setenv("DASHBOARD_API_URL", "http://dash:8000")
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    # Force a tiny interval so the test runs fast, but still proves the guard.
    r._TICK_MIN_INTERVAL = 0.05
    r._TICK_THROTTLE.clear()
    monkeypatch.setenv("PRICE_BACKEND", "aggregate")

    _, aggregator = r._make_fetcher("crypto", ["BTC/USD"])
    forward = aggregator._crypto.on_tick
    # Simulate a burst of ticks within the throttle window.
    for _ in range(20):
        forward("BTC/USD", 63000.0)
    # At most a couple of posts (first immediate, then at most 1 more after the
    # window passes). Definitely not 20.
    assert len(fake.posts) <= 2, f"too many posts for a tick burst: {len(fake.posts)}"
    # Now wait past the interval and confirm it pushes again (last-value wins).
    time.sleep(0.08)
    forward("BTC/USD", 64000.0)
    assert len(fake.posts) >= 2
