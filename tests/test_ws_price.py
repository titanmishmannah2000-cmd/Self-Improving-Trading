"""Tests for the websocket price stream. Network-free.

Monkeypatches ``websockets.connect`` with an in-memory async fake so no socket
is opened. Verifies the cache fills from streamed frames and that ``fetch_fn``
is a safe drop-in for the poll loop (returns latest, None when stale/absent).
"""

from __future__ import annotations

import asyncio

import pytest

from hermes_core.adapters.ws_price import PriceStream, _parse_message, _to_symbol


def test_symbol_map():
    assert _to_symbol("EUR/USD") == "EUR-USD"
    assert _to_symbol("BTC/USD") == "BTC-USD"


def test_parse_message_roundtrip():
    raw = '{"product_id": "BTC-USD", "price": "63039.10"}'
    c = _parse_message(raw, {"BTC/USD": "BTC-USD"})
    assert c is not None
    assert c["pair"] == "BTC/USD"
    assert c["price"] == 63039.10


def test_parse_message_coinbase_string_price():
    # Coinbase sends price as a string and uses product_id with dashes.
    raw = '{"type": "ticker", "product_id": "ETH-USD", "price": "1854.71"}'
    c = _parse_message(raw, {"ETH/USD": "ETH-USD"})
    assert c is not None
    assert c["pair"] == "ETH/USD"
    assert c["price"] == 1854.71


def test_parse_message_ignores_subscriptions_ack():
    raw = '{"type": "subscriptions", "channels": [{"name": "ticker"}]}'
    assert _parse_message(raw, {"BTC/USD": "BTC-USD"}) is None


def test_parse_message_ignores_unknown_symbol():
    raw = '{"type": "ticker", "product_id": "ZZZ-USD", "price": "1.0"}'
    assert _parse_message(raw, {"BTC/USD": "BTC-USD"}) is None


def test_parse_message_garbage_none():
    assert _parse_message("not json", {"BTC/USD": "BTC-USD"}) is None


# ── fake websocket frame source ──────────────────────────────────────────────
class _FakeWS:
    def __init__(self, frames):
        self._frames = frames
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, msg):
        self.sent.append(msg)

    async def close(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            # keep the receive loop alive but yield nothing new
            await asyncio.sleep(0.01)
            return '{"symbol": "NONE", "price": 0}'
        return self._frames.pop(0)


def _fake_connect(frames):
    async def _conn(url, **kw):
        return _FakeWS(frames)
    return _conn


@pytest.fixture()
def frames():
    return [
        '{"type": "ticker", "product_id": "BTC-USD", "price": "1.10"}',
        '{"type": "ticker", "product_id": "BTC-USD", "price": "1.12"}',
        '{"type": "ticker", "product_id": "ETH-USD", "price": "1.30"}',
    ]


async def test_stream_fills_cache_and_fetch_fn_returns_latest(frames, monkeypatch):
    monkeypatch.setattr("hermes_core.adapters.ws_price.websockets.connect", _fake_connect(frames))
    stream = PriceStream(["BTC/USD", "ETH/USD"], url="wss://fake.test")
    await stream.connect()
    # allow the receive loop to process frames
    await asyncio.sleep(0.1)
    # latest BTC/USD cached
    c = stream.fetch_fn("BTC/USD")
    assert c is not None
    assert c["price"] == 1.12  # last frame for BTC-USD
    g = stream.fetch_fn("ETH/USD")
    assert g is not None
    assert g["price"] == 1.30
    # history buffered
    assert len(stream.seed_history_fn("BTC/USD")) >= 2
    await stream.aclose()


async def test_fetch_fn_none_before_connect():
    stream = PriceStream(["EUR/USD"])
    # no connect() called -> no cache -> loop-safe None
    assert stream.fetch_fn("EUR/USD") is None


async def test_stale_guard_returns_none(frames, monkeypatch):
    monkeypatch.setattr("hermes_core.adapters.ws_price.websockets.connect", _fake_connect(frames))
    stream = PriceStream(["EUR/USD"], url="wss://fake.test", stale_s=0.0)
    await stream.connect()
    await asyncio.sleep(0.05)
    # stale_s=0 -> any cached candle is immediately stale
    assert stream.fetch_fn("EUR/USD") is None
    await stream.aclose()


async def test_connect_fail_soft(monkeypatch):
    async def _boom(url, **kw):
        raise OSError("refused")
    monkeypatch.setattr("hermes_core.adapters.ws_price.websockets.connect", _boom)
    stream = PriceStream(["EUR/USD"], url="wss://fake.test")
    await stream.connect()  # must not raise
    assert stream.fetch_fn("EUR/USD") is None
    await stream.aclose()


def test_make_stream_fetch_returns_stream():
    s = PriceStream(["EUR/USD"])
    assert isinstance(s, PriceStream)


# ── auto-reconnect [GUARD L63] ──────────────────────────────────────────────
class _FlakyFakeWS:
    """First iteration raises (simulates socket drop); later frames resume."""

    def __init__(self, frames, drop_after=2):
        self._frames = list(frames)
        self._seen = 0
        self.drop_after = drop_after
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    async def close(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        self._seen += 1
        if self._seen == self.drop_after:
            raise ConnectionResetError("socket dropped")  # [GUARD L63] -> reconnect
        if not self._frames:
            await asyncio.sleep(0.01)
            return '{"symbol": "NONE", "price": 0}'
        return self._frames.pop(0)


def _flaky_connect(frames, fail_first=False):
    state = {"calls": 0, "dropped": False}

    async def _conn(url, **kw):
        state["calls"] += 1
        if fail_first and state["calls"] == 1:
            raise OSError("refused")
        # only the FIRST successful connection drops mid-stream; reconnects are
        # stable so the re-delivered frames actually arrive.
        drop = not state["dropped"]
        state["dropped"] = True
        return _FlakyFakeWS(list(frames), drop_after=2 if drop else 10**9)

    return _conn


async def test_stream_auto_reconnect_after_drop(monkeypatch):
    monkeypatch.setattr("hermes_core.adapters.ws_price.RECONNECT_S", 0.1)
    frames = [
        '{"type": "ticker", "product_id": "BTC-USD", "price": "1.10"}',
        '{"type": "ticker", "product_id": "BTC-USD", "price": "1.11"}',  # seen before the drop
    ]
    monkeypatch.setattr(
        "hermes_core.adapters.ws_price.websockets.connect",
        _flaky_connect(frames),
    )
    stream = PriceStream(["BTC/USD"], url="wss://fake.test")
    await stream.connect()
    await asyncio.sleep(0.4)  # drop, then reconnect + resume
    c = stream.fetch_fn("BTC/USD")
    assert c is not None
    assert c["price"] == 1.11
    await stream.aclose()


async def test_stream_reconnect_after_initial_refused(monkeypatch):
    monkeypatch.setattr("hermes_core.adapters.ws_price.RECONNECT_S", 0.1)
    frames = ['{"type": "ticker", "product_id": "BTC-USD", "price": "1.20"}']
    monkeypatch.setattr(
        "hermes_core.adapters.ws_price.websockets.connect",
        _flaky_connect(frames, fail_first=True),
    )
    stream = PriceStream(["BTC/USD"], url="wss://fake.test")
    await stream.connect()
    await asyncio.sleep(0.4)  # first connect refused -> reconnect succeeds
    c = stream.fetch_fn("BTC/USD")
    assert c is not None
    assert c["price"] == 1.20
    await stream.aclose()


async def test_on_tick_callback_fires(monkeypatch):
    ticks = []
    frames = ['{"type": "ticker", "product_id": "BTC-USD", "price": "1.55"}']
    monkeypatch.setattr(
        "hermes_core.adapters.ws_price.websockets.connect",
        _fake_connect(frames),
    )
    stream = PriceStream(
        ["BTC/USD"], url="wss://fake.test", on_tick=lambda p, pr: ticks.append((p, pr))
    )
    await stream.connect()
    await asyncio.sleep(0.1)
    assert ticks and ticks[0] == ("BTC/USD", 1.55)
    await stream.aclose()

