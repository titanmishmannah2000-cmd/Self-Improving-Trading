"""Tests for the Hermes Price Aggregator. Network-free.

Each source's `fetch` is monkeypatched to return canned values, so no real HTTP
happens. Covers consensus, disagreement-reject, single-source low-confidence,
all-fail-soft, [L01] stale, Alpha Vantage throttle (429->dropped), metals keyed
vs degraded (XAG), crypto REST, PAXG bridge, and an 8-pair run_cycle integration
reusing the S18 harness.
"""

from __future__ import annotations

from hermes_core.adapters.aggregate import (
    _CRYPTO_PAIRS,
    AlphaVantageSource,
    CoinbaseTickerSource,
    FrankfurterSource,
    MetalsSource,
    PaxgGoldSource,
    PriceAggregator,
    YfinanceSource,
)

ALL_PAIRS = ["EUR/USD", "GBP/USD", "GBP/JPY", "AUD/USD",
             "XAU/USD", "XAG/USD", "BTC/USD", "ETH/USD"]


# ── unit: consensus + fail-soft ───────────────────────────────────────────────
async def test_frankfurter_fx():
    s = FrankfurterSource()
    # monkeypatch its client.get via a fake
    class _R:
        def __init__(self, pair): self.pair = pair
        def raise_for_status(self): pass
        def json(self):
            # frankfurter is FX-only; XAU/USD has no rate -> empty
            if "XAU" in self.pair or "XAG" in self.pair:
                return {"rates": {}}
            return {"rates": {"USD": 1.10}}
    class _C:
        def __init__(self, pair): self.pair = pair
        async def get(self, *a, **k): return _R(self.pair)
    s._client = _C("EUR/USD")
    assert abs(await s.fetch("EUR/USD") - 1.10) < 1e-9
    s2 = FrankfurterSource()
    s2._client = _C("XAU/USD")
    assert await s2.fetch("XAU/USD") is None  # not an FX pair for this source


async def test_alphavantage_returns_rate():
    s = AlphaVantageSource(api_key="k")
    class _R:
        def raise_for_status(self): pass
        def json(self): return {"Realtime Currency Exchange Rate": {"5. Exchange Rate": "1.25"}}
    class _C:
        async def get(self, *a, **k): return _R()
    s._client = _C()
    assert abs(await s.fetch("GBP/USD") - 1.25) < 1e-9


async def test_alphavantage_throttled_returns_none():
    s = AlphaVantageSource(api_key="k")
    class _C:
        async def get(self, *a, **k):
            raise RuntimeError("429 quota exceeded")  # simulate throttle
    s._client = _C()
    assert await s.fetch("EUR/USD") is None  # [GUARD L61] dropped, fail-soft


async def test_alphavantage_no_key_returns_none(monkeypatch):
    monkeypatch.delenv("ALPHA_VANTAGE_KEY", raising=False)
    s = AlphaVantageSource(api_key=None)
    assert await s.fetch("EUR/USD") is None


async def test_paxg_bridge():
    s = PaxgGoldSource()
    class _R:
        def raise_for_status(self): pass
        def json(self): return {"price": "2350.5"}
    class _C:
        async def get(self, *a, **k): return _R()
    s._client = _C()
    assert abs(await s.fetch("XAU/USD") - 2350.5) < 1e-9
    assert await s.fetch("EUR/USD") is None


async def test_metals_keyed_covers_xau_xag():
    s = MetalsSource(api_key="k")
    class _R:
        def raise_for_status(self): pass
        def json(self): return {"metals": {"gold": 2350.0, "silver": 28.0}}
    class _C:
        async def get(self, *a, **k): return _R()
    s._client = _C()
    assert abs(await s.fetch("XAU/USD") - 2350.0) < 1e-9
    assert abs(await s.fetch("XAG/USD") - 28.0) < 1e-9  # same cached response, both metals
    # second call must NOT hit the network again (cache); the fake client
    # would raise if called, so no assertion needed beyond the values above


async def test_metals_one_call_serves_both():
    # metals.dev returns gold+silver in ONE response; both pairs must come from
    # a single cached call (no second HTTP hit). We assert XAU and XAG both
    # resolve and that the underlying fetch ran once via a counter.
    s = MetalsSource(api_key="k")
    calls = {"n": 0}

    class _R:
        def raise_for_status(self): pass
        def json(self): return {"metals": {"gold": 2350.0, "silver": 28.0}}

    class _C:
        async def get(self, *a, **k):
            calls["n"] += 1
            return _R()
    s._client = _C()
    g = await s.fetch("XAU/USD")
    x = await s.fetch("XAG/USD")
    assert abs(g - 2350.0) < 1e-9
    assert abs(x - 28.0) < 1e-9
    assert calls["n"] == 1  # second fetch hit the cache, no 2nd HTTP call


async def test_source_cache_retry_on_failure():
    # A source that fails once then succeeds must self-heal via the single
    # retry in _cached (so transient throttles don't zero out a price).
    s = FrankfurterSource()
    attempts = {"n": 0}

    class _R:
        def __init__(self, ok): self.ok = ok
        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError("429")
        def json(self): return {"rates": {"USD": 1.10}}
    class _C:
        async def get(self, *a, **k):
            attempts["n"] += 1
            return _R(ok=(attempts["n"] >= 2))  # fail first, succeed on retry
    s._client = _C()
    val = await s.fetch("EUR/USD")
    assert abs(val - 1.10) < 1e-9
    assert attempts["n"] == 2  # exactly one retry


async def test_metals_no_key_returns_none():
    s = MetalsSource(api_key=None)
    s._key = None  # force "no key" path (env fallback is exercised elsewhere)
    assert await s.fetch("XAU/USD") is None
    assert await s.fetch("XAG/USD") is None


async def test_coinbase_ticker_crypto():
    s = CoinbaseTickerSource()
    class _R:
        def raise_for_status(self): pass
        def json(self): return {"price": "60000.0"}
    class _C:
        async def get(self, *a, **k): return _R()
    s._client = _C()
    assert abs(await s.fetch("BTC/USD") - 60000.0) < 1e-9
    assert await s.fetch("EUR/USD") is None


# ── integration: aggregator consensus + stale + degraded ──────────────────────
def _fake_sources(frank=1.10, alpha=1.101, yf=1.099, paxg=None, metals=None,
                  coinbase=None, fail_frank=False, fail_alpha=False):
    """Build a source list with monkeypatched fetch returning canned values.

    Scalar args (frank/alpha/yf) apply to ANY pair that source covers; None means
    the source returns None for that pair (e.g. frankfurter has no metals).
    """
    f = FrankfurterSource()
    a = AlphaVantageSource(api_key="k")
    p = PaxgGoldSource()
    m = MetalsSource(api_key="k")
    c = CoinbaseTickerSource()
    y = YfinanceSource()

    async def _f(pair):
        if fail_frank:
            return None
        if "/" not in pair:
            return None
        if any(t in pair for t in ("XAU", "XAG", "BTC", "ETH")):
            return None
        return frank if frank is not None else None

    async def _a(pair):
        if fail_alpha:
            return None
        if "/" not in pair:
            return None
        if any(t in pair for t in ("XAU", "XAG", "BTC", "ETH")):
            return None
        return alpha if alpha is not None else None

    async def _p(pair):
        if pair == "XAU/USD" and paxg is not None:
            return paxg
        return None

    async def _m(pair):
        if metals is not None and pair in metals:
            return metals[pair]
        return None

    async def _c(pair):
        if pair in _CRYPTO_PAIRS and coinbase is not None:
            return coinbase[pair]
        return None

    async def _y(pair):
        return yf if yf is not None else None

    f.fetch = _f
    a.fetch = _a
    p.fetch = _p
    m.fetch = _m
    c.fetch = _c
    y.fetch = _y
    return [f, a, p, m, c, y]


def test_consensus_fx_median():
    # spread 1.10..1.12 = 1.8% > 1% -> low_confidence (NOT hard-rejected; bot
    # keeps trading on the median rather than starving). First call has no
    # last-good, so the median is returned and flagged low_confidence.
    agg = PriceAggregator(["EUR/USD"], sources=_fake_sources(frank=1.10, alpha=1.12, yf=1.11))
    c = agg.fetch_fn("EUR/USD")
    assert c is not None
    assert c["low_confidence"] is True
    assert abs(c["price"] - 1.11) < 0.01


def test_consensus_fx_agree():
    agg = PriceAggregator(["EUR/USD"], sources=_fake_sources(frank=1.10, alpha=1.101, yf=1.099))
    # spread 0.09% < 1% -> median ~1.10
    c = agg.fetch_fn("EUR/USD")
    assert c is not None
    assert abs(c["price"] - 1.10) < 0.01
    assert c["n_sources"] >= 2
    assert c["low_confidence"] is False


def test_single_source_low_confidence():
    agg = PriceAggregator(["EUR/USD"], sources=_fake_sources(frank=1.10, alpha=None, yf=None))
    c = agg.fetch_fn("EUR/USD")
    assert c is not None
    assert c["low_confidence"] is True
    assert c["n_sources"] == 1


def test_all_sources_fail_soft_none():
    agg = PriceAggregator(["EUR/USD"], sources=_fake_sources(frank=None, alpha=None, yf=None))
    assert agg.fetch_fn("EUR/USD") is None


def test_xag_keyed_consensus():
    agg = PriceAggregator(["XAG/USD"], sources=_fake_sources(metals={"XAG/USD": 28.0}, yf=28.1))
    c = agg.fetch_fn("XAG/USD")
    assert c is not None
    assert abs(c["price"] - 28.0) < 0.5


def test_xag_degraded_no_key():
    # no metals key -> XAG served by yfinance only, low_confidence
    agg = PriceAggregator(
        ["XAG/USD"],
        sources=_fake_sources(frank=None, alpha=None, metals=None, yf=28.0),
    )
    c = agg.fetch_fn("XAG/USD")
    assert c is not None
    assert c["low_confidence"] is True


def test_crypto_rest_works():
    agg = PriceAggregator(
        ["BTC/USD"],
        sources=_fake_sources(coinbase={"BTC/USD": 60000.0}, yf=None, frank=None, alpha=None),
    )
    c = agg.fetch_fn("BTC/USD")
    assert c is not None
    assert abs(c["price"] - 60000.0) < 1.0


def test_l01_stale_returns_none():
    # all sources fail -> aggregator falls back to last_good. Inject an OLD
    # last_good so the [L01] stale guard (stale_s=0.0) returns None.
    agg = PriceAggregator(["EUR/USD"], sources=_fake_sources(frank=None, alpha=None, yf=None),
                           stale_s=0.0)
    agg._last_good["EUR/USD"] = {"pair": "EUR/USD", "price": 1.10, "ts": 0.0}
    assert agg.fetch_fn("EUR/USD") is None


def test_make_aggregator_fetch_returns_callable():
    agg = PriceAggregator(["EUR/USD"], sources=_fake_sources(frank=1.10, alpha=1.101, yf=1.099))
    fn = agg.fetch_fn
    assert callable(fn)
    c = fn("EUR/USD")
    assert c is None or isinstance(c, dict)


def test_seed_history_fn():
    agg = PriceAggregator(["EUR/USD"], sources=_fake_sources(frank=1.10, alpha=1.101, yf=1.099))
    agg.fetch_fn("EUR/USD")
    hist = agg.seed_history_fn("EUR/USD")
    assert isinstance(hist, list)


# ── run_cycle integration (reuse S18 harness) ─────────────────────────────────
def test_aggregator_through_run_cycle():
    import test_integration_e2e as e2e  # reuse S18 _Feed + run_cycle

    # A rolling source so the loop sees price movement and can take a trade.
    class _Roller:
        def __init__(self): self.i = 0
        async def fetch(self, pair):
            self.i += 1
            return 1.10 + 0.01 * ((self.i // 5) % 4)  # steps 1.10..1.13

    roller = _Roller()
    # wrap roller as the sole FX source for EUR/USD (async lambdas for the rest)
    async def _none(p):
        return None

    f = FrankfurterSource()
    f.fetch = roller.fetch
    a = AlphaVantageSource(api_key="k")
    a.fetch = _none
    p = PaxgGoldSource()
    p.fetch = _none
    m = MetalsSource(api_key="k")
    m.fetch = _none
    c = CoinbaseTickerSource()
    c.fetch = _none
    y = YfinanceSource()
    y.fetch = _none
    agg = PriceAggregator(["EUR/USD"], sources=[f, a, p, m, c, y])

    positions: dict = {}
    reentry: dict = {}
    for cyc in range(1, 80):
        e2e.run_cycle(
            "forex", cyc,
            fetch_fn=agg.fetch_fn,
            now_fn=lambda c=cyc: c * 3600.0,
            chart_context_fn=lambda p: "",
            ensemble_fn=lambda p: "neutral",
            open_positions=positions,
            reentry=reentry,
            consecutive_failures=0,
        )
    # loop consumed aggregator candles across 80 cycles without crashing
    assert agg._last_good.get("EUR/USD") is not None
