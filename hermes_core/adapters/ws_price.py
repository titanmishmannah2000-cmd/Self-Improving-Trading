"""WebSocket price stream (real-time tick/candle feed) — S19 / #1b(ii).

Design (non-disruptive): the trade loop is POLL-based — it calls
``fetch_fn(pair)`` once per cycle. This module does NOT re-architect the loop.
Instead ``PriceStream`` opens a websocket, receives live ticks/candles, and
keeps the **latest candle per pair** in an in-memory cache. It then exposes a
synchronous ``fetch_fn(pair)`` that returns the cached latest candle — so the
loop's existing poll contract is preserved; streaming just makes every pull
return the freshest streamed price. If the stream isn't connected yet (or a
pair has no data / is stale) ``fetch_fn`` returns None and the loop's existing
stale/empty handling takes over. Fail-soft throughout: a broken socket never
raises into the loop.

[L01] stale guard: a cached candle older than STALE_S_MAX is treated as None.
Reconnect guard [GUARD L63]: the receive loop auto-reconnects with backoff on
socket drop/exception so a transient disconnect does not permanently silence
the stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import Callable

import websockets
from websockets.asyncio.client import ClientConnection

from hermes_core.adapters.tick_history import (
    TICK_HISTORY_MAX,
    append_bucketed_tick,
)

# seconds a cached candle may be old before it is considered stale (L01)
STALE_S_MAX = 60.0
# backoff between reconnect attempts after a socket drop [GUARD L63]
RECONNECT_S = 5.0
# Default live crypto feed: Coinbase Exchange public ticker (free, no key).
DEFAULT_WS_URL = "wss://ws-feed.exchange.coinbase.com"


def _to_symbol(pair: str) -> str:
    """Map a HERMES pair (EUR/USD, BTC/USD) to a Coinbase product (EUR-USD, BTC-USD)."""
    return pair.replace("/", "-").upper()


def _parse_message(raw: str, pair_map: dict[str, str]) -> dict | None:
    """Parse a websocket frame into a Candle dict, or None if not a price tick.

    Accepts either a raw JSON text frame or a dict. Handles both the Coinbase
    ticker shape (``product_id`` + string ``price``) and a generic
    ``symbol``/``price`` shape, so the stream stays drop-in for other feeds.
    Only ``type == "ticker"`` (or a frame with a numeric price) is treated as a
    price update; subscription ack / heartbeat frames are ignored.
    """
    try:
        msg = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(msg, dict):
        return None
    # Ignore non-price control frames (e.g. Coinbase "subscriptions" ack).
    mtype = msg.get("type")
    if mtype not in (None, "ticker", "price", "trade"):
        return None
    # Coinbase uses product_id (BTC-USD); generic feeds use symbol (BTCUSD).
    sym = str(msg.get("product_id") or msg.get("symbol") or msg.get("s") or "").upper()
    hermes_pair = pair_map.get(sym)
    if hermes_pair is None:
        # try reverse lookup by stripping the separator
        hermes_pair = next((p for p, s in pair_map.items() if s == sym), None)
    if hermes_pair is None:
        return None
    price = msg.get("price")
    if price is None:
        price = msg.get("p") or msg.get("last") or msg.get("close")
    if price is None:
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    now = time.time()
    return {
        "pair": hermes_pair,
        "price": price,
        "high": float(msg.get("high", price)),
        "low": float(msg.get("low", price)),
        "candle_ts": float(msg.get("candle_ts", now)),
        "ts": now,
    }


class PriceStream:
    """Live websocket price cache exposing a drop-in synchronous ``fetch_fn``."""

    def __init__(
        self,
        pairs: list[str],
        *,
        url: str | None = None,
        api_key: str | None = None,
        stale_s: float = STALE_S_MAX,
        subscribe_msg: dict | None = None,
        on_tick: Callable[[str, float], None] | None = None,
    ) -> None:
        self.url = url or os.environ.get("PRICE_WS_URL") or DEFAULT_WS_URL
        self.api_key = api_key if api_key is not None else os.environ.get("PRICE_WS_API_KEY")
        self.stale_s = stale_s
        self._pair_map = {p: _to_symbol(p) for p in pairs}
        self._cache: dict[str, dict] = {}
        self._history: dict[str, list[dict]] = {p: [] for p in pairs}
        self._lock = asyncio.Lock()
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task | None = None
        # Coinbase Exchange subscribe shape. Other feeds can override via ctor.
        self._subscribe_msg = subscribe_msg or {
            "type": "subscribe",
            "product_ids": list(self._pair_map.values()),
            "channels": ["ticker"],
        }
        # Optional callback fired on every freshly received tick (pair, price).
        # Used to forward live ticks to the dashboard without waiting for the
        # next 60s cycle. Fail-soft: any exception in the callback is swallowed.
        self.on_tick = on_tick

    # ── connection lifecycle ────────────────────────────────────────────────
    async def _open(self) -> None:
        """Open the websocket and subscribe. Raises on failure (caller retries)."""
        self._ws = await websockets.connect(
            self.url,
            additional_headers=(
                {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            ),
        )
        if self._subscribe_msg:
            await self._ws.send(json.dumps(self._subscribe_msg))

    async def connect(self) -> None:
        """Open the websocket and start the (self-reconnecting) receive loop."""
        if not self.url:
            return
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        """Receive loop with auto-reconnect [GUARD L63]. Never raises out."""
        while True:
            try:
                await self._open()
                async for frame in self._ws:  # type: ignore[union-attr]
                    candle = _parse_message(frame, self._pair_map)
                    if candle is None:
                        continue
                    async with self._lock:
                        self._cache[candle["pair"]] = candle
                        hist = self._history.setdefault(candle["pair"], [])
                        # Same move/time bucketing as FX live ticks — raw Coinbase
                        # ticker spam otherwise fills 300 identical closes and the
                        # loop skips forever on flat_price:unchanged.
                        append_bucketed_tick(hist, candle, max_len=TICK_HISTORY_MAX)
                    if self.on_tick is not None:
                        with contextlib.suppress(Exception):  # [GUARD L63]
                            self.on_tick(candle["pair"], float(candle["price"]))
            except Exception:  # noqa: BLE001 — socket dropped; reconnect [GUARD L63]
                self._ws = None
                await asyncio.sleep(RECONNECT_S)
                continue  # loop re-opens the socket
            # clean close of the stream -> reopen
            with contextlib.suppress(Exception):
                if self._ws is not None:
                    await self._ws.close()
            self._ws = None
            await asyncio.sleep(RECONNECT_S)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
            self._ws = None

    # ── synchronous, loop-compatible accessors ──────────────────────────────
    def fetch_fn(self, pair: str) -> dict | None:
        """Drop-in fetch_fn for the poll loop. Returns latest cached candle.

        [L01] stale cached candle -> None. Not connected / no data -> None.
        Never raises.
        """
        if pair.endswith(":history"):
            return self._history_fn(pair[: -len(":history")])
        candle = self._cache.get(pair)
        if candle is None:
            return None
        if (time.time() - float(candle.get("ts", 0))) > self.stale_s:
            return None  # [L01] stale
        return candle

    def _history_fn(self, pair: str) -> list[dict]:
        return list(self._history.get(pair, []))

    def seed_history_fn(self, pair: str, max_candles: int = 300) -> list[dict]:
        """Return buffered recent candles (oldest-first) for indicator seeding."""
        hist = self._history.get(pair, [])
        return hist[-max_candles:]


def make_stream_fetch(
    pairs: list[str],
    *,
    url: str | None = None,
    api_key: str | None = None,
    on_tick: Callable[[str, float], None] | None = None,
) -> PriceStream:
    """Build a ``PriceStream`` and return its instance.

    The caller is responsible for ``await stream.connect()`` (and ``aclose``)
    in an async context; until then ``fetch_fn`` returns None and the loop
    falls back to its stale/empty handling — so the running path is safe even
    before the socket is open.
    """
    return PriceStream(pairs, url=url, api_key=api_key, on_tick=on_tick)
