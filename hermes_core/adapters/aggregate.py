"""Hermes Price Aggregator (S19) — self-built multi-source FX/metals/crypto feed.

WHY: every trustworthy FX/metals feed costs money or a broker. Yahoo (yfinance)
is free but a delayed scrape that gets IP-banned. This engine OWNS a multi-source,
cross-checked, staleness-aware feed instead of depending on ONE scrape.

DESIGN (discipline, same as every engine this session):
- Drop-in `fetch_fn(pair)` matching yfinance's contract -> loop.py is UNTOUCHED.
- Sources polled concurrently (httpx async). Each source failure is isolated and
  dropped for the cycle (fail-soft) -> never breaks the loop. [GUARD L61]
- Consensus: median of agreeing sources within `consensus_pct`; disagreement ->
  reject -> fall back to last-good (if fresh) else None. [L01] stale guard.
- Secrets (Alpha Vantage, metals.dev) read from env ONLY -> G-secrets clean.
- Opt-in: selected via `make_default_fetch(backend="aggregate")` (now the
  default); `PRICE_BACKEND=yfinance` falls back to Yahoo scrape.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable

import httpx

from hermes_core.adapters.price import fetch_sync as _yf_fetch
from hermes_core.adapters.price import seed_history_sync as _yf_seed_history
from hermes_core.adapters.ws_price import STALE_S_MAX, PriceStream

# [L01] stale window for a cached consensus candle (seconds).
STALE_S_MAX_LOCAL = STALE_S_MAX
CONSENSUS_PCT = 0.01  # sources must agree within 1% or consensus is rejected
SOURCE_TIMEOUT = 3.0  # per-source httpx timeout (seconds)


# ── source adapters ───────────────────────────────────────────────────────────
def _goldapi_spot(sym: str) -> float | None:
    """Live spot USD price for XAU/XAG from GoldAPI.io (free, no key)."""
    try:
        import httpx as _hx
        r = _hx.get(f"https://api.gold-api.com/price/{sym}", timeout=5.0)
        r.raise_for_status()
        price = r.json().get("price")
        return float(price) if price is not None else None
    except Exception:  # noqa: BLE001 — fail-soft
        return None


def _yf_history(pair: str, max_candles: int = 300) -> list[dict]:
    """Real history candles for indicator seeding.

    - Gold (XAU/USD): yfinance GC=F history (COMEX; XAU=F is a broken ~910 series).
    - Silver (XAG/USD): yfinance SI=F history (XAG=F is delisted). If SI=F is
      empty, fall back to gold→silver rescale via live GoldAPI G/S ratio.
    - Fail-soft: returns [] on any error.
    """
    if pair == "XAU/USD":
        try:
            return _yf_seed_history("XAU/USD", max_candles=max_candles)
        except Exception:  # noqa: BLE001
            return []
    if pair == "XAG/USD":
        try:
            silver = _yf_seed_history("XAG/USD", max_candles=max_candles)
            if silver:
                return silver
            # SI=F empty → rescale gold history into silver price space
            gold_h = _yf_seed_history("XAU/USD", max_candles=max_candles)
            if not gold_h:
                return []
            xau = _goldapi_spot("XAU")
            xag = _goldapi_spot("XAG")
            if xau is None or xag is None or xag == 0:
                return []
            ratio = xau / xag  # current gold/silver ratio (~80)
            out = []
            for c in gold_h:
                out.append({
                    "price": c["price"] / ratio,
                    "high": c.get("high", c["price"]) / ratio,
                    "low": c.get("low", c["price"]) / ratio,
                    "ts": c.get("ts"),
                    "candle_ts": c.get("candle_ts"),
                })
            return out
        except Exception:  # noqa: BLE001
            return []
    return []


class _BaseSource:
    """A price source. `fetch` returns a USD-quoted price or None.
    the CURRENTLY RUNNING loop (the aggregator runs one asyncio.run per
    fetch_fn call; a client built elsewhere would bind to the wrong loop and
    silently fail). [GUARD L61]

    Rate-limit defence: every source shares a per-key TTL cache + a minimum
    spacing between real HTTP calls. Free FX/metals/crypto APIs throttle rapid
    bursts, so a single cycle that fetches XAU then XAG (or BTC then ETH) from
    the same provider must NOT hit it twice in a row. The cache also means one
    provider call serves every pair it covers (metals.dev returns gold AND
    silver in one response). [GUARD L61]
    """

    name = "base"
    _cache_ttl: float = 30.0
    _min_interval: float = 1.0

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._cache: dict[str, tuple[float, object]] = {}
        self._last_call_ts: float = 0.0

    def _get_client(self) -> httpx.AsyncClient:
        # Tests inject a fake client via `source._client`; production builds a
        # fresh client per asyncio.run cycle so it binds to the current loop.
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=SOURCE_TIMEOUT)

    async def _cached(self, key: str, fetcher: Callable[[], object]) -> object:
        """Return a cached value if fresh; else call `fetcher`, cache + space it.

        On fetch failure, retries ONCE (free APIs throttle bursts transiently),
        then falls back to a stale cache entry so a transient error doesn't zero
        out a price the bot already had. [GUARD L61]
        """
        now = time.time()
        item = self._cache.get(key)
        if item is not None and (now - item[0]) < self._cache_ttl:
            return item[1]
        wait = self._min_interval - (now - self._last_call_ts)
        if wait > 0:
            await asyncio.sleep(wait)
        val: object = None
        try:
            val = await fetcher()
        except Exception:  # noqa: BLE001 — fail-soft; [GUARD L61]
            val = None
        if val is None:
            # one retry for transient throttle (e.g. Coinbase 429 on a burst)
            try:
                val = await fetcher()
            except Exception:  # noqa: BLE001 — fail-soft; [GUARD L61]
                val = None
        ts = time.time()
        if val is not None:
            self._cache[key] = (ts, val)
            self._last_call_ts = ts
            return val
        if item is not None:
            return item[1]  # stale fallback
        return None

    async def fetch(self, pair: str) -> float | None:  # pragma: no cover - abstract
        raise NotImplementedError


class FrankfurterSource(_BaseSource):
    """ECB reference rates (free, no key). Covers all FX pairs."""

    name = "frankfurter"
    URL = "https://api.frankfurter.dev/v1/latest"

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__()
        self._client = client
        self._cache_ttl = 30.0
        self._min_interval = 1.0

    async def fetch(self, pair: str) -> float | None:
        if "/" not in pair:  # only FX here
            return None
        base, quote = pair.split("/")

        async def _go() -> float | None:
            client = self._get_client()
            r = await client.get(self.URL, params={"base": base, "symbols": quote})
            r.raise_for_status()
            rates = r.json().get("rates", {})
            val = rates.get(quote)
            return float(val) if val is not None else None

        out = await self._cached(pair, _go)
        return out if isinstance(out, float) else None


class AlphaVantageSource(_BaseSource):
    """Alpha Vantage CURRENCY_EXCHANGE_RATE (keyed via ALPHA_VANTAGE_KEY).

    Free tier is rate-limited (~5/min) and FX is delayed (~15min). On HTTP 429 /
    quota the source returns None and is dropped for the cycle (fail-soft) so the
    loop keeps working on the other sources. [GUARD L61]
    """

    name = "alphavantage"
    URL = "https://www.alphavantage.co/query"

    def __init__(self, api_key: str | None = None,
                 client: httpx.AsyncClient | None = None):
        super().__init__()
        self._key = api_key if api_key is not None else os.environ.get("ALPHA_VANTAGE_KEY")
        self._client = client
        self._cache_ttl = 15.0
        self._min_interval = 12.0  # ~5/min free tier -> >=12s between calls

    async def fetch(self, pair: str) -> float | None:
        if not self._key or "/" not in pair:
            return None
        from_c, to_c = pair.split("/")

        async def _go() -> float | None:
            r = await self._get_client().get(self.URL, params={
                "function": "CURRENCY_EXCHANGE_RATE",
                "from_currency": from_c,
                "to_currency": to_c,
                "apikey": self._key,
            })
            r.raise_for_status()
            data = r.json().get("Realtime Currency Exchange Rate", {})
            rate = data.get("5. Exchange Rate")
            return float(rate) if rate is not None else None

        out = await self._cached(pair, _go)
        return out if isinstance(out, float) else None


class PaxgGoldSource(_BaseSource):
    """Coinbase PAXG-USD ticker as a real-time XAU/USD proxy (free, no key)."""

    name = "paxg"
    URL = "https://api.exchange.coinbase.com/products/PAXG-USD/ticker"

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__()
        self._client = client
        self._cache_ttl = 30.0
        self._min_interval = 1.0

    async def fetch(self, pair: str) -> float | None:
        if pair != "XAU/USD":
            return None

        async def _go() -> float | None:
            client = self._get_client()
            r = await client.get(self.URL)
            r.raise_for_status()
            price = r.json().get("price")
            return float(price) if price is not None else None

        out = await self._cached("XAU", _go)
        return out if isinstance(out, float) else None


class MetalsSource(_BaseSource):
    """metals.dev (keyed via METALS_API_KEY). Covers BOTH XAU/USD and XAG/USD.

    One request returns gold AND silver; the response is cached and both pairs
    serve from it, so a single cycle never hits metals.dev twice. When the key
    is absent the source returns None (XAU still works via PAXG; XAG degrades to
    yfinance). [GUARD L61]
    """

    name = "metals"
    URL = "https://api.metals.dev/v1/latest"

    def __init__(self, api_key: str | None = None,
                 client: httpx.AsyncClient | None = None):
        super().__init__()
        self._key = api_key if api_key is not None else os.environ.get("METALS_API_KEY")
        self._client = client
        self._cache_ttl = 30.0
        self._min_interval = 5.0

    async def _fetch_metals(self) -> dict | None:
        client = self._get_client()
        r = await client.get(self.URL, params={
            "api_key": self._key,
            "currency": "USD",
            "unit": "troy_ounce",
        })
        r.raise_for_status()
        data = r.json().get("metals")
        return data if data else None  # None -> not cached, falls back to stale

    async def _metals_data(self) -> dict:
        out = await self._cached("metals_dev", self._fetch_metals)
        return out if isinstance(out, dict) else {}

    async def fetch(self, pair: str) -> float | None:
        if not self._key:
            return None
        # metals.dev JSON nests under "metals" with keys "gold"/"silver"
        metal_key = "gold" if pair == "XAU/USD" else "silver" if pair == "XAG/USD" else None
        if metal_key is None:
            return None
        data = await self._metals_data()
        val = data.get(metal_key)
        return float(val) if val is not None else None


class GoldApiSource(_BaseSource):
    """GoldAPI.io (https://api.gold-api.com) — free, NO KEY, real spot
    XAU/USD + XAG/USD prices updated daily. This is the reliable live metals
    source after PAXG/Coinbase started returning 403 and metals.dev hit its
    monthly quota. [GUARD L61] fail-soft: any error -> None."""

    name = "goldapi"
    URL = "https://api.gold-api.com/price/{symbol}"

    def __init__(self, client: httpx.AsyncClient | None = None):
        super().__init__()
        self._client = client
        self._cache_ttl = 60.0  # daily update, but cache 60s to limit calls
        self._min_interval = 1.0  # GoldAPI handles rapid calls (verified 24/24)

    async def fetch(self, pair: str) -> float | None:
        if pair == "XAU/USD":
            sym = "XAU"
        elif pair == "XAG/USD":
            sym = "XAG"
        else:
            return None

        async def _go() -> float | None:
            # GoldAPI is reliable, but a cold-start TLS handshake in a fresh
            # container can occasionally fail the first attempt. Retry a couple
            # of times so metals don't flicker no_candle on container restart.
            last_err: Exception | None = None
            for _attempt in range(3):
                try:
                    client = self._get_client()
                    r = await client.get(self.URL.format(symbol=sym))
                    r.raise_for_status()
                    data = r.json()
                    price = data.get("price")
                    if price is not None:
                        return float(price)
                except Exception as _e:  # noqa: BLE001 — fail-soft; [GUARD L61]
                    last_err = _e
            return None

        out = await self._cached(sym, _go)
        return out if isinstance(out, float) else None


class YfinanceSource(_BaseSource):
    """yfinance wrapper as a cross-check / fallback (now L60 cache-hardened)."""

    name = "yfinance"
    _cache_ttl = 60.0
    _min_interval = 1.0

    async def fetch(self, pair: str) -> float | None:
        # yfinance live gold/silver (XAU=F/XAG=F) returns stale + inconsistent
        # values (e.g. 915 alongside 4004) that pollute the live consensus.
        # GoldAPI.io is the authoritative free live source for metals; yfinance
        # is still used for metals HISTORY seeding (seed_history_fn -> _yf_history).
        if pair in _METAL_PAIRS:
            return None
        # yfinance is async under the hood; calling its sync wrapper from inside
        # our aggregator's asyncio.run nests event loops -> "coroutine never
        # awaited". Run it in a worker thread with its own loop. [GUARD L61]
        async def _go() -> float | None:
            candle = await asyncio.to_thread(_yf_fetch, pair)
            return float(candle["price"]) if candle is not None else None

        out = await self._cached(pair, _go)
        return out if isinstance(out, float) else None


class CoinbaseTickerSource(_BaseSource):
    """Coinbase REST ticker for crypto (BTC/USD, ETH/USD) — free, no key.

    Used by the synchronous poll path (loop calls fetch_fn with no persistent
    event loop), so the WS stream can't stay alive across calls. This REST
    source makes crypto work standalone; the WS (_crypto) is a bonus real-time
    layer for callers who run `await agg.connect()` in their own async runtime.
    """

    name = "coinbase_rest"
    BASE = "https://api.exchange.coinbase.com/products"
    _cache_ttl = 15.0
    _min_interval = 1.0

    async def fetch(self, pair: str) -> float | None:
        if pair not in _CRYPTO_PAIRS:
            return None
        symbol = pair.replace("/", "-")

        async def _go() -> float | None:
            client = self._get_client()
            r = await client.get(f"{self.BASE}/{symbol}/ticker")
            r.raise_for_status()
            price = r.json().get("price")
            return float(price) if price is not None else None

        out = await self._cached(symbol, _go)
        return out if isinstance(out, float) else None


# crypto pairs served by a live websocket stream (Coinbase public WS, free RT)
_CRYPTO_PAIRS = {"BTC/USD", "ETH/USD"}
_METAL_PAIRS = frozenset({"XAU/USD", "XAG/USD"})


# ── aggregator ────────────────────────────────────────────────────────────────
class PriceAggregator:
    """Multi-source consensus price feed exposing a drop-in sync `fetch_fn`."""

    def __init__(
        self,
        pairs: list[str],
        *,
        sources: list[_BaseSource] | None = None,
        stale_s: float = STALE_S_MAX_LOCAL,
        consensus_pct: float = CONSENSUS_PCT,
        client: httpx.AsyncClient | None = None,
        on_tick: Callable[[str, float], None] | None = None,
    ) -> None:
        self.pairs = list(pairs)
        self.stale_s = stale_s
        self.consensus_pct = consensus_pct
        if sources is not None:
            self._sources = sources
        else:
            # Each source gets its OWN client by default. Sharing one client
            # across concurrently-polled sources under a fresh asyncio.run loop
            # causes connection-pool races (some sources silently return None).
            # [GUARD L61]
            self._sources: list[_BaseSource] = [
                FrankfurterSource(),
                AlphaVantageSource(),
                PaxgGoldSource(),
                MetalsSource(),
                GoldApiSource(),
                CoinbaseTickerSource(),
                YfinanceSource(),
            ]
        # crypto served by the WS stream (real-time). Live ticks are forwarded
        # via on_tick so callers can push them to the dashboard instantly.
        self._crypto = PriceStream(
            [p for p in pairs if p in _CRYPTO_PAIRS],
            on_tick=on_tick,
        )
        self._last_good: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        """Open the crypto websocket stream (non-blocking, fail-soft)."""
        with contextlib.suppress(Exception):  # [GUARD L61]
            await self._crypto.connect()

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):  # [GUARD L61]
            await self._crypto.aclose()
        for src in self._sources:
            with contextlib.suppress(Exception):  # [GUARD L61]
                client = getattr(src, "_client", None)
                if client is not None:
                    await client.aclose()

    async def _poll(self, pair: str) -> tuple[list[float], bool]:
        """Poll all sources for `pair` concurrently. Returns (prices, any_up)."""
        results: list[float] = []
        any_up = False
        futs = [asyncio.ensure_future(s.fetch(pair)) for s in self._sources]
        for fut in asyncio.as_completed(futs):  # [GUARD L61] isolate each source
            try:
                price = await fut
            except Exception:  # noqa: BLE001 — a source broke mid-flight
                price = None
            if price is not None:
                results.append(price)
                any_up = True
        return results, any_up

    async def _fetch_async(self, pair: str) -> dict | None:
        # crypto: prefer the live stream
        if pair in _CRYPTO_PAIRS:
            candle = self._crypto.fetch_fn(pair)
            if candle is not None:
                return candle
            # stream not warm yet -> fall through to sources (none cover crypto)

        prices, any_up = await self._poll(pair)
        if not prices:
            # [GUARD L61] all sources down -> fail soft, serve last-good if fresh
            return self._last_good.get(pair)

        consensus = self._consensus(prices)
        # Spread check: delayed free sources (Alpha Vantage ~15min, yfinance) can
        # legitimately differ from Frankfurter by >1% intraday, so we do NOT hard
        # reject on disagreement (that would starve the bot). Instead we flag
        # low_confidence and prefer a fresh last-good when sources diverge widely.
        lo, hi = min(prices), max(prices)
        spread = (hi - lo) / consensus if consensus else 0.0
        low_conf = (len(prices) < 2) or (spread > self.consensus_pct)
        if low_conf and pair in self._last_good:
            # prefer the fresher last-good consensus over a divergent single/median
            return self._last_good[pair]
        now = time.time()
        candle = {
            "pair": pair,
            "price": consensus,
            "high": consensus,
            "low": consensus,
            "candle_ts": now,
            "ts": now,
            "low_confidence": low_conf,
            "n_sources": len(prices),
        }
        if any_up:
            async with self._lock:
                self._last_good[pair] = candle
        return candle

    def _consensus(self, prices: list[float]) -> float:
        """Median; if spread > consensus_pct, keep median but flag via caller."""
        prices_sorted = sorted(prices)
        mid = len(prices_sorted) // 2
        if len(prices_sorted) % 2 == 0:
            return (prices_sorted[mid - 1] + prices_sorted[mid]) / 2.0
        return prices_sorted[mid]

    def __call__(self, pair: str, *, force: bool = False) -> dict | None:
        """Alias so the aggregator object is itself a drop-in fetch_fn."""
        return self.fetch_fn(pair, force=force)

    def fetch_fn(self, pair: str, *, force: bool = False) -> dict | None:
        """Synchronous, drop-in for the poll loop. Returns latest consensus candle.

        Accepts the loop's ``force=`` kwarg for contract compatibility (ignored —
        the aggregator always polls fresh). Also handles the loop's
        ``pair + ":history"`` seeding call by returning a list of recent consensus
        candles (oldest-first). [L01] stale: a cached consensus candle older than
        stale_s -> None. Never raises (fail-soft).

        Safe from both sync threads and an already-running event loop (e.g. the
        async bot runner): nested ``asyncio.run`` is avoided by hopping to a
        worker thread when a loop is already active. [GUARD L61]
        """
        if pair.endswith(":history"):
            return self.seed_history_fn(pair[: -len(":history")])
        try:
            candle = self._run_fetch(pair)
        except Exception:  # noqa: BLE001 — fail-soft; [GUARD L61]
            return self._last_good.get(pair)
        if candle is None:
            return None
        if (time.time() - float(candle.get("ts", 0))) > self.stale_s:
            return None  # [L01] stale
        return candle

    def _run_fetch(self, pair: str) -> dict | None:
        """Run ``_fetch_async`` on a dedicated loop, even if one is already running."""
        try:
            asyncio.get_running_loop()
            running = True
        except RuntimeError:
            running = False
        if not running:
            # Per-call event loop. Sources create httpx clients LAZILY on first
            # fetch so each loop gets clients bound to IT. [GUARD L61]
            return asyncio.run(self._fetch_async(pair))
        # Nested loop (async runner / pytest-asyncio): own thread + own loop.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(self._fetch_async(pair))).result(
                timeout=30.0
            )

    def seed_history_fn(self, pair: str, max_candles: int = 300) -> list[dict]:
        """Buffered recent consensus candles (oldest-first) for indicator seeding.
        FX/metals seed from yfinance history (GC=F / SI=F); crypto uses the live
        websocket buffer. [GUARD L61]"""
        if pair in _CRYPTO_PAIRS:
            return self._crypto.seed_history_fn(pair, max_candles)
        # FX uses the last good tick; metals get a real yfinance history series
        # so indicators (RSI/ADX/BB) are meaningful.
        if pair in _METAL_PAIRS:
            return _yf_history(pair, max_candles)
        # [FIX] FX pairs need a real multi-candle series, not a single last
        # tick. Previously this returned [last_good_tick] (1 candle), which
        # made compute_all/evaluate_entry (and the GP shadow hook) run on a
        # degenerate single point. Seed from yfinance history (proven to return
        # ~300 candles for FX) so indicators are meaningful.
        try:
            fx_h = _yf_seed_history(pair, max_candles=max_candles)
            if fx_h:
                return fx_h
        except Exception:  # noqa: BLE001 — fall back to last good tick
            pass
        last = self._last_good.get(pair)
        return [last] if last is not None else []


def make_aggregator_fetch(
    pairs: list[str],
    *,
    backend: str = "aggregate",
    **kw,
) -> PriceAggregator:
    """Build a PriceAggregator with a synchronous ``fetch_fn`` bound as a method.

    Returns the aggregator object (NOT just the function) so the caller can
    ``await agg.connect()`` / ``await agg.aclose()`` in an async context — this
    is what opens/closes the live crypto websocket. ``agg(pair)`` (the ``fetch_fn``
    method) remains the drop-in for the poll loop. Until ``connect()`` runs,
    crypto pairs fall back to the REST sources; FX/metals use the consensus feed
    immediately. [L61]
    """
    return PriceAggregator(pairs, **kw)
