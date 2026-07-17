"""Market data adapters (Session 2 / S19).

Exports a SYNCHRONOUS default ``fetch`` / ``seed_history`` so the sync trade
loop (hermes_core.engines.loop) can call ``fetch_fn(pair)`` without await.
The underlying yfinance implementation is async; the sync wrappers run it
safely. The async originals are still available as ``fetch_async`` /
``seed_history_async`` for concurrent callers.

S19 added an httpx-based REST backend (hermes_core.adapters.http_price);
``make_default_fetch`` selects the backend via env PRICE_BACKEND
("yfinance" default, "http" opt-in) — additive, default behaviour unchanged.
"""

from .aggregate import PriceAggregator, make_aggregator_fetch
from .http_price import HttpPriceClient
from .price import fetch as _fetch_async
from .price import fetch_sync, seed_history_sync
from .price import seed_history as _seed_async
from .ws_price import PriceStream, make_stream_fetch

# Public `fetch` / `seed_history` are the SYNCHRONOUS wrappers so the sync trade
# loop can call fetch_fn(pair) without await (S19 fix for the coroutine leak).
fetch = fetch_sync
seed_history = seed_history_sync

# async originals kept available for concurrent callers
fetch_async = _fetch_async
seed_history_async = _seed_async

__all__ = [
    "fetch",
    "seed_history",
    "fetch_async",
    "seed_history_async",
    "fetch_sync",
    "seed_history_sync",
    "HttpPriceClient",
    "make_default_fetch",
    "PriceStream",
    "make_stream_fetch",
    "PriceAggregator",
    "make_aggregator_fetch",
]


def make_default_fetch(backend: str | None = None, api_url: str | None = None,
                       api_key: str | None = None, pairs: list[str] | None = None):
    """Return a synchronous ``fetch_fn(pair, *, force=False)`` for the loop.

    backend: "yfinance" (default), "http", or "aggregate". Selection is env-driven
    when ``backend`` is None (PRICE_BACKEND). Default stays yfinance -> the running
    path is NOT disrupted by the new backends.
    """
    import os

    chosen = backend or os.environ.get("PRICE_BACKEND", "yfinance").lower()
    if chosen == "http":
        client = HttpPriceClient(base_url=api_url, api_key=api_key)

        def _http_fetch(pair: str, *, force: bool = False):
            return client.fetch_sync(pair, force=force)

        return _http_fetch
    if chosen == "aggregate":
        agg_pairs = pairs or []
        agg = PriceAggregator(agg_pairs)

        def _agg_fetch(pair: str, *, force: bool = False):
            return agg.fetch_fn(pair, force=force)

        return _agg_fetch
    return fetch_sync
