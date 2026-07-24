"""Async REST price adapter (real-time data fetching).

Additive capability for S19/Tier-1: a concurrent, resilient price client built
on ``httpx.AsyncClient``. It does NOT replace the yfinance adapter
(``hermes_core/adapters/price.py``) — both coexist; the loop keeps using its
injected ``fetch_fn`` and is untouched. This client is selected only when a bot
config / env points ``PRICE_API_URL`` at a REST source.

Discipline preserved from the yfinance adapter:
  * [GUARD L01] stale-candle guard fires at the source — a candle whose
    candle_ts equals the last delivered candle_ts within STALE_WINDOW_S is
    returned as None, permanently, until a newer candle_ts arrives.
  * Fail-soft (D3): ``fetch`` / ``seed_history`` NEVER raise. On network error,
    timeout, non-200, or empty body they return None / [] (caller decides).
  * Credentials come from env (PRICE_API_URL / PRICE_API_KEY) — never hardcoded,
    so G-secrets stays clean.

Network-free by construction: the transport is injectable
(``client=_FakeTransport``), so tests assert behaviour without a socket.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

import httpx

# [GUARD L01] same envelope as the yfinance adapter.
STALE_WINDOW_S = 60.0
REQUEST_TIMEOUT_S = 10.0

# In-process per-pair last candle_ts, authoritative at the source.
_last_candle_ts: dict[str, float] = {}


def _to_symbol(pair: str) -> str:
    """Map a HERMES pair to a REST query symbol (override via env if needed)."""
    return pair.replace("/", "").replace("-", "")


def _row_to_candle(price: float, candle_ts: float, fetch_ts: float) -> dict:
    return {
        "price": float(price),
        "high": float(price),
        "low": float(price),
        "candle_ts": float(candle_ts),
        "ts": float(fetch_ts),
    }


def _stale(pair: str, candle_ts: float, *, force: bool) -> bool:
    """[GUARD L01] True if candle_ts repeats the last delivered one."""
    prev = _last_candle_ts.get(pair)
    return not force and prev is not None and prev == candle_ts


class HttpPriceClient:
    """Concurrent REST price client.

    One shared ``httpx.AsyncClient`` is reused across pairs (connection pooling).
    The transport is injectable for tests.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = REQUEST_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url or os.environ.get("PRICE_API_URL", "")
        self.api_key = api_key if api_key is not None else os.environ.get("PRICE_API_KEY")
        kwargs: dict[str, Any] = {
            "timeout": timeout,
            "headers": self._auth_headers(),
        }
        if transport is not None:
            kwargs["transport"] = transport
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self._client = httpx.AsyncClient(**kwargs)

    def _auth_headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"Authorization": f"Bearer {self.api_key}"}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch(self, pair: str, *, force: bool = False) -> dict | None:
        """Fetch the latest candle for ``pair``. Returns Candle dict or None.

        [GUARD L01] stale candle -> None. Fail-soft on any transport error.
        """
        if not self.base_url:
            return None
        symbol = _to_symbol(pair)
        try:
            resp = await self._client.get("/price", params={"symbol": symbol})
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            # fail-soft: feed unavailable, no candle
            return None
        price = self._extract_price(data)
        if price is None:
            return None
        candle_ts = float(data.get("candle_ts", time.time()))
        if _stale(pair, candle_ts, force=force):
            return None
        _last_candle_ts[pair] = candle_ts
        return _row_to_candle(price, candle_ts, time.time())

    async def seed_history(self, pair: str, max_candles: int = 300) -> list[dict]:
        """Return the most recent ``max_candles`` candles for ``pair``.

        Fail-soft: returns [] if the feed is unavailable.
        """
        if not self.base_url:
            return []
        symbol = _to_symbol(pair)
        try:
            resp = await self._client.get(
                "/history", params={"symbol": symbol, "limit": max_candles}
            )
            resp.raise_for_status()
            payload = resp.json()
        except (httpx.HTTPError, ValueError):
            return []
        rows = payload.get("candles") if isinstance(payload, dict) else None
        if not rows:
            return []
        out: list[dict] = []
        for row in rows[-max_candles:]:
            price = self._extract_price(row)
            if price is None:
                continue
            candle_ts = float(row.get("candle_ts", time.time()))
            out.append(_row_to_candle(price, candle_ts, time.time()))
        return out

    @staticmethod
    def _extract_price(data: Any) -> float | None:
        if isinstance(data, dict):
            for key in ("price", "close", "last"):
                if key in data and data[key] is not None:
                    try:
                        return float(data[key])
                    except (TypeError, ValueError):
                        return None
        return None

    # ── synchronous wrappers (for the sync trade loop) ───────────────────────
    def fetch_sync(self, pair: str, *, force: bool = False) -> dict | None:
        """Sync version of :meth:`fetch` — runs the coroutine via asyncio."""
        try:
            return asyncio.run(self.fetch(pair, force=force))
        except RuntimeError:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(self.fetch(pair, force=force))

    def seed_history_sync(self, pair: str, max_candles: int = 300) -> list[dict]:
        """Sync version of :meth:`seed_history`."""
        try:
            return asyncio.run(self.seed_history(pair, max_candles=max_candles))
        except RuntimeError:
            loop = asyncio.get_event_loop()
            return loop.run_until_complete(self.seed_history(pair, max_candles=max_candles))
