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
import calendar
import contextlib
import os
import time
from collections.abc import Callable

import httpx

from hermes_core.adapters.price import fetch_sync as _yf_fetch
from hermes_core.adapters.price import seed_history_sync as _yf_seed_history
from hermes_core.adapters.tick_history import (
    TICK_HISTORY_MAX,
    TICK_MOVE_MIN_PCT,
    TICK_SAMPLE_MIN_S,
    append_bucketed_tick,
    series_is_flat,
)
from hermes_core.adapters.ws_price import STALE_S_MAX, PriceStream

# [L01] stale window for a cached consensus candle (seconds).
STALE_S_MAX_LOCAL = STALE_S_MAX
CONSENSUS_PCT = 0.01  # sources must agree within 1% or consensus is rejected
SOURCE_TIMEOUT = 3.0  # per-source httpx timeout (seconds)
# Indicator seeding needs a real multi-bar series. Prefer yfinance 5m (live
# indicator cadence). Yahoo intermittently returns empty/"delisted" for FX;
# below that floor we use Frankfurter daily and/or the live rolling tick buffer
# instead of a single last-good tick.
HISTORY_MIN_BARS = 50
# TICK_* constants imported from tick_history (shared with crypto WS).

# crypto pairs served by a live websocket stream (Coinbase public WS, free RT)
_CRYPTO_PAIRS = {"BTC/USD", "ETH/USD"}
_METAL_PAIRS = frozenset({"XAU/USD", "XAG/USD"})
_FX_PAIRS = frozenset({"EUR/USD", "GBP/USD", "AUD/USD", "GBP/JPY"})


def _is_synthetic_fx_quote(pair: str, candle: dict | None) -> bool:
    """True for placeholder quotes (live_prices ``*-x`` / price==1.0 stubs).

    The full FX stub-ladder check (1.10–1.13 cycling across majors) lives in
    ``soak_controls.price_sanity_book`` so a lone ~1.10 EUR print is not killed.
    """
    if not candle or pair not in _FX_PAIRS:
        return False
    try:
        px = float(candle.get("price"))
    except (TypeError, ValueError):
        return True
    return abs(px - 1.0) < 1e-12


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
            return _yf_seed_history("XAU/USD", max_candles=max_candles) or []
        except Exception:  # noqa: BLE001
            return []
    if pair == "XAG/USD":
        try:
            silver = _yf_seed_history("XAG/USD", max_candles=max_candles) or []
            if silver:
                return silver
            # SI=F empty → rescale gold history into silver price space
            gold_h = _yf_seed_history("XAU/USD", max_candles=max_candles) or []
            if not gold_h:
                return []
            xau = _goldapi_spot("XAU")
            xag = _goldapi_spot("XAG")
            if xau is None or xag is None or xag == 0:
                return []
            ratio = xau / xag  # current gold/silver ratio (~80)
            out = []
            for c in gold_h:
                out.append(
                    {
                        "price": c["price"] / ratio,
                        "high": c.get("high", c["price"]) / ratio,
                        "low": c.get("low", c["price"]) / ratio,
                        "ts": c.get("ts"),
                        "candle_ts": c.get("candle_ts"),
                    }
                )
            return out
        except Exception:  # noqa: BLE001
            return []
    return []


def _frankfurter_fx_history(pair: str, max_candles: int = 300) -> list[dict]:
    """Daily ECB FX history via Frankfurter (free, no key). Fail-soft -> [].

    Used when yfinance returns empty/'delisted' for FX so mean-reversion still
    gets a real multi-bar series (BB/RSI/ADX) instead of a single live tick.
    """
    if "/" not in pair or pair in _METAL_PAIRS or pair in _CRYPTO_PAIRS:
        return []
    base, quote = pair.split("/", 1)
    if not base or not quote:
        return []
    try:
        # ~max_candles weekdays ≈ calendar span with slack for weekends/holidays.
        days = max(int(max_candles * 1.7), 90)
        end = time.strftime("%Y-%m-%d", time.gmtime())
        start = time.strftime("%Y-%m-%d", time.gmtime(time.time() - days * 86400))
        url = f"https://api.frankfurter.dev/v1/{start}..{end}"
        r = httpx.get(url, params={"base": base, "symbols": quote}, timeout=8.0)
        r.raise_for_status()
        rates = r.json().get("rates") or {}
        if not isinstance(rates, dict) or not rates:
            return []
        out: list[dict] = []
        for day in sorted(rates.keys()):
            row = rates[day]
            if not isinstance(row, dict):
                continue
            val = row.get(quote)
            if val is None:
                continue
            price = float(val)
            # Approximate a daily candle_ts at 16:00 UTC (ECB ref window-ish).
            try:
                y, m, d = (int(x) for x in day.split("-"))
                candle_ts = calendar.timegm((y, m, d, 16, 0, 0, 0, 0, 0))
            except Exception:  # noqa: BLE001
                candle_ts = time.time()
            out.append(
                {
                    "pair": pair,
                    "price": price,
                    "high": price,
                    "low": price,
                    "ts": time.time(),
                    "candle_ts": float(candle_ts),
                }
            )
        return out[-max_candles:]
    except Exception:  # noqa: BLE001 — fail-soft
        return []


def _yf_intraday_history(pair: str, max_candles: int = 300) -> list[dict]:
    """Yahoo intraday history (5m → 15m → 1h). Fail-soft → []."""
    from hermes_core.adapters.price import seed_history_interval_sync

    for interval, period in (("5m", "60d"), ("15m", "60d"), ("1h", "60d")):
        try:
            hist = (
                seed_history_interval_sync(
                    pair,
                    interval=interval,
                    period=period,
                    max_candles=max_candles,
                )
                or []
            )
        except Exception:  # noqa: BLE001
            hist = []
        if len(hist) >= HISTORY_MIN_BARS:
            return hist
    return []


def _external_history(pair: str, max_candles: int = 300) -> list[dict]:
    """Best-effort INTRADAY history from free external sources (no broker).

    FX/crypto/metals: yfinance 5m/15m/1h only. Frankfurter daily is intentionally
    NOT returned here — daily bars mismatch live indicator cadence; seed_history_fn
    may use Frankfurter only as a cold-start last resort after the tick buffer.
    """
    if pair in _METAL_PAIRS:
        # metals: prefer dedicated 5m seed (GC=F / SI=F), then intraday ladder
        yf = _yf_history(pair, max_candles)
        if len(yf) >= HISTORY_MIN_BARS:
            return yf
        return _yf_intraday_history(pair, max_candles)
    if pair in _CRYPTO_PAIRS:
        return _yf_intraday_history(pair, max_candles)
    # FX: intraday Yahoo only (no daily Frankfurter at this layer).
    return _yf_intraday_history(pair, max_candles)


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

    def __init__(self, api_key: str | None = None, client: httpx.AsyncClient | None = None):
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
            r = await self._get_client().get(
                self.URL,
                params={
                    "function": "CURRENCY_EXCHANGE_RATE",
                    "from_currency": from_c,
                    "to_currency": to_c,
                    "apikey": self._key,
                },
            )
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

    def __init__(self, api_key: str | None = None, client: httpx.AsyncClient | None = None):
        super().__init__()
        self._key = api_key if api_key is not None else os.environ.get("METALS_API_KEY")
        self._client = client
        self._cache_ttl = 30.0
        self._min_interval = 5.0

    async def _fetch_metals(self) -> dict | None:
        client = self._get_client()
        r = await client.get(
            self.URL,
            params={
                "api_key": self._key,
                "currency": "USD",
                "unit": "troy_ounce",
            },
        )
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
                    pass
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
        # Rolling live consensus ticks for indicator seeding when Yahoo/Frankfurter
        # history is empty. Oldest-first; capped at TICK_HISTORY_MAX.
        self._tick_history: dict[str, list[dict]] = {p: [] for p in pairs}
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
            # BUT never recycle synthetic FX stub quotes (1.0 / 1.1x ladder).
            prev = self._last_good.get(pair)
            if prev is not None and _is_synthetic_fx_quote(pair, prev):
                return None
            return prev

        consensus = self._consensus(prices)
        # Spread check: delayed free sources (Alpha Vantage ~15min, yfinance) can
        # legitimately differ from Frankfurter by >1% intraday, so we do NOT hard
        # reject on disagreement (that would starve the bot). Instead we flag
        # low_confidence and prefer a FRESH last-good when sources diverge widely.
        #
        # CRITICAL: single-source is NORMAL for XAG/USD (GoldAPI only — PAXG and
        # yfinance don't cover silver). Treating n<2 like disagreement and
        # returning the OLD last_good made [L01] stale-guard return None after
        # 60s → perpetual no_candle → regime blank on the dashboard while the
        # sticky price still showed the last good tick.
        lo, hi = min(prices), max(prices)
        spread = (hi - lo) / consensus if consensus else 0.0
        disagree = spread > self.consensus_pct
        low_conf = (len(prices) < 2) or disagree
        if disagree and pair in self._last_good:
            prev = self._last_good[pair]
            prev_age = time.time() - float(prev.get("ts", 0))
            if prev_age <= self.stale_s:
                return prev
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
                self._record_tick_unlocked(pair, candle)
        return candle

    def _record_tick_unlocked(self, pair: str, candle: dict) -> None:
        """Append ``candle`` to the rolling live buffer (caller holds ``_lock``)."""
        hist = self._tick_history.setdefault(pair, [])
        append_bucketed_tick(
            hist,
            candle,
            move_min_pct=TICK_MOVE_MIN_PCT,
            sample_min_s=TICK_SAMPLE_MIN_S,
            max_len=TICK_HISTORY_MAX,
        )

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
            prev = self._last_good.get(pair)
            if prev is not None and _is_synthetic_fx_quote(pair, prev):
                return None
            return prev
        if candle is None:
            return None
        if _is_synthetic_fx_quote(pair, candle):
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
            return pool.submit(lambda: asyncio.run(self._fetch_async(pair))).result(timeout=30.0)

    def seed_history_fn(self, pair: str, max_candles: int = 300) -> list[dict]:
        """Multi-bar history for indicator seeding (oldest-first). [GUARD L61]

        Priority:
          1. Crypto websocket buffer (when long enough)
          2. External INTRADAY history (yfinance 5m/15m/1h)
          3. Rolling live consensus tick buffer (built as the bot cycles)
          4. Frankfurter daily FX (cold-start last resort only — wrong cadence)
          5. Last-good single tick (degenerate — loop will usually BB-skip)

        Yahoo intermittently returns empty/"delisted" for FX+futures; without
        (2)/(3) the bot collapses to one tick → ``bb_bandwidth:0`` forever.
        Daily Frankfurter must NOT outrank the live tick buffer.
        """
        if pair in _CRYPTO_PAIRS:
            ws = self._crypto.seed_history_fn(pair, max_candles) or []
            # Reject duplicate-filled WS buffers (pre-bucket legacy / tip spam).
            ws_ok = len(ws) >= HISTORY_MIN_BARS and not series_is_flat(ws)
            if ws_ok:
                return ws
            try:
                external = _external_history(pair, max_candles=max_candles) or []
            except Exception:  # noqa: BLE001
                external = []
            if len(external) >= HISTORY_MIN_BARS and not series_is_flat(external):
                return external[-max_candles:]
            if len(ws) >= 2 and not series_is_flat(ws):
                return ws
            if len(external) >= 2 and not series_is_flat(external):
                return external[-max_candles:]
            # Prefer varied external over a flat WS wall of identical closes.
            if external and (not ws or series_is_flat(ws)):
                return external[-max_candles:]
            return ws or external

        external: list[dict] = []
        try:
            external = _external_history(pair, max_candles=max_candles) or []
        except Exception:  # noqa: BLE001 — fail-soft into live buffer
            external = []

        live = self._last_good.get(pair)
        if len(external) >= HISTORY_MIN_BARS:
            out = [dict(c) for c in external[-max_candles:]]
            # Stitch the freshest live quote onto the tip so indicators see now.
            if live is not None and out:
                tip = dict(out[-1])
                tip["price"] = float(live["price"])
                tip["high"] = max(float(tip.get("high", tip["price"])), float(live["price"]))
                tip["low"] = min(float(tip.get("low", tip["price"])), float(live["price"]))
                tip["ts"] = float(live.get("ts", tip.get("ts", time.time())))
                out[-1] = tip
            return out

        buf = list(self._tick_history.get(pair) or [])
        if len(buf) >= 30:
            return buf[-max_candles:]

        # Cold-start only: daily ECB FX when Yahoo + tick buffer are both short.
        if pair not in _METAL_PAIRS and pair not in _CRYPTO_PAIRS:
            try:
                frank = _frankfurter_fx_history(pair, max_candles=max_candles) or []
            except Exception:  # noqa: BLE001
                frank = []
            if len(frank) >= HISTORY_MIN_BARS:
                return frank[-max_candles:]

        # Prefer a short external series over a single tick when that's all we have.
        if len(external) >= 2:
            return external[-max_candles:]
        if len(buf) >= 2:
            return buf[-max_candles:]
        if live is not None and not _is_synthetic_fx_quote(pair, live):
            return [live]
        return []


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
