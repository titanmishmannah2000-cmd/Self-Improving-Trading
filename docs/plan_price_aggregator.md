# Plan: Hermes Price Aggregator (self-built multi-source FX/metals feed)

**Session:** S19 real-time deps — new engine, built to the same discipline as every
other engine this session (drop-in `fetch_fn`, env-driven, fail-soft, [L01] stale
guard, guard tags, ruff/G-except/G-secrets clean, full network-free test suite,
gate green BEFORE it touches the running path).

**Why:** every trustworthy FX/metals feed costs money or needs a broker. Yahoo
(yfinance) is free but a delayed scrape that gets IP-banned. We "invent our own
way": stop depending on ONE scrape and instead OWN a multi-source, cross-checked,
staleness-aware feed. Verified-free, no-key sources (tested 2026-07-16):
- `https://api.frankfurter.dev/v1/latest`  (ECB FX, free, no key)  ✅ 200
- `https://open.er-api.com/v6/latest/USD`  (FX) — DROPPED: only refreshes
  once per day, too stale for a trading loop. Replaced by Alpha Vantage below.
- `https://www.alphavantage.co/query` — FX from `CURRENCY_EXCHANGE_RATE`
  (user's `ALPHA_VANTAGE_KEY`, env, never hardcoded). Free tier is rate-limited
  (~5/min, ~25/day on some plans) and FX quotes are delayed (~15min on free),
  but still far fresher than daily open.er-api. Used as a reliable independent
  FX cross-check alongside Frankfurter.
- `https://api.exchange.coinbase.com/products/PAXG-USD/ticker` (real-time gold
  proxy via PAXG token; WS also available)                         ✅ 200
- **metals.dev API (user's key, env `METALS_API_KEY`)** — covers BOTH XAU/USD
  and XAG/USD. Keyed, but the user already has a subscription; never hardcoded.
- yfinance kept as a 4th cross-check / fallback.

**Current live pairs (authoritative, from bots/*/config.yaml) — 8 total:**
| Bot    | Pairs                                  | Covered by               |
|--------|----------------------------------------|--------------------------|
| forex  | EUR/USD, GBP/USD, GBP/JPY, AUD/USD     | Frankfurter + OpenErApi  |
| gold   | XAU/USD, XAG/USD                       | PAXG (free) + metals.dev (keyed) |
| crypto | BTC/USD, ETH/USD                       | Coinbase public WS       |

**XAU/XAG (metals) — COVERED via user's metals.dev key.** There is no free
no-key silver source (Coinbase has no XAG ticker, frankfurter has no XAG). But
the user has a **metals.dev API key** (env `METALS_API_KEY`, never hardcoded —
G-secrets stays clean) which returns both XAU/USD and XAG/USD. When the key is
set, `MetalsSource` (metals.dev) registers and BOTH gold and silver run through
full consensus. Bonus: XAU/USD then has **double coverage** — PAXG proxy (free,
real-time) + metals.dev (keyed) — so gold is the best-sourced pair in the system.
When the key is absent (CI/tests), XAU still works via PAXG (free) and XAG
degrades gracefully to yfinance-only, flagged `low_confidence=True`. The
aggregator NEVER hard-blocks or crashes on a missing key. Result: **all 8 pairs
on a real multi-source feed** (7 fully free + metals.dev via the user's existing
subscription covering gold + silver).

**Scope answers (defaults — override if you disagree):**
- Real-time tolerance: **"fresh price every few seconds from merged free
  sources" is acceptable for paper trading.** Strategy A (aggregate) is the
  primary for FX; Strategy B (PAXG WS) gives genuinely real-time gold; crypto WS
  gives real-time BTC/ETH. Sub-second tick streaming is OUT of scope (not needed
  at 60s cycle).
- Must-have assets: **all 8 current pairs** (FX + XAU/USD + XAG/USD + BTC/ETH).
  Oil / indices (US30) are OUT of initial scope (no free real-time source) but
  the source registry is extensible — add later without refactor.

---

## 1. Interface (drop-in, identical contract to existing `fetch_fn`)

`hermes_core/adapters/aggregate.py`

```python
class PriceAggregator:
    def __init__(self, pairs, *, sources=None, stale_s=STALE_S_MAX,
                 consensus_pct=0.01, http_client=None):
        ...
    def fetch_fn(self, pair: str) -> dict | None:
        """Same signature/semantics as yfinance fetch_sync.

        Returns consensus Candle for `pair`, or None if:
          - no source answered, OR
          - consensus rejected (sources disagree beyond consensus_pct), OR
          - last-good price is older than stale_s  [L01]
        Never raises (fail-soft): a source error -> that source is dropped for
        this cycle, not a crash.
        """
    def seed_history_fn(self, pair, max_candles=300) -> list[dict]:
        """Returns buffered recent consensus candles (oldest-first)."""

def make_aggregator_fetch(pairs, *, backend="aggregate", **kw):
    """Builder. backend='aggregate' selects this engine; default stays yfinance
    so the running path is UNCHANGED until opted in."""
```

The Candle dict shape matches what `loop.py` already consumes
(`{"pair","price","high","low","candle_ts","ts"}`) — zero loop changes.

## 2. Source registry (pluggable, env-driven URLs, no hardcoded secrets)

Each source is a small adapter implementing `async fetch(pair) -> float | None`.
Registered sources (all return a USD-quoted price; FX inverted from USD base):
- `FrankfurterSource`  — GET frankfurter.dev, parse `rates`. Covers ALL forex
  pairs (EUR/USD, GBP/USD, GBP/JPY, AUD/USD, and any future FX add) — free, no key.
- `AlphaVantageSource` — GET alphavantage.co `CURRENCY_EXCHANGE_RATE`
  (user's `ALPHA_VANTAGE_KEY`, env, never hardcoded). Reliable independent FX
  cross-check. Free tier is rate-limited (~5/min, daily quota) and FX is delayed
  (~15min on free); on HTTP 429 / quota the source returns None and is dropped
  for that cycle (fail-soft) so it never breaks the loop — Frankfurter + yfinance
  still cover FX. Replaces the dropped open.er-api (daily refresh, too stale).
- `PaxgGoldSource`     — GET Coinbase PAXG-USD ticker -> XAU/USD proxy. Real-time
  gold, free, no key. Free real-time coverage for gold even without the key.
- `CoinbaseWsSource`   — (crypto) public WS tickers for BTC/USD, ETH/USD — free,
  no key, real-time. Shares the `PriceStream` class already built/tested in S19.
- `MetalsSource`       — KEYED via env `METALS_API_KEY` (user's metals.dev
  subscription). Registered when the key is present -> covers BOTH XAU/USD and
  XAG/USD through full consensus. Gives gold double coverage (PAXG + metals.dev)
  and brings silver up to full. When absent (CI/tests), XAU still works via PAXG
  and XAG degrades to yfinance-only flagged `low_confidence` (never blocked).
- `YfinanceSource`     — wraps existing `fetch_sync` as a cross-check / fallback;
  also the sole/secondary source for XAG when no metals key.

URLs read from env (no secrets hardcoded — G-secrets clean). Two sources are
keyed via env (`ALPHA_VANTAGE_KEY`, `METALS_API_KEY`); both user-supplied.
Frankfurter + Coinbase PAXG/WS are free no-key. New sources (oil, indices) added
by appending to the registry — no core change. **Expansion rule:** adding FX
pairs (EUR/JPY, USD/CAD, USD/CHF, NZD/USD) or crypto tickers (SOL, etc.) is
**config-only** — just add the pair to the bot's `config.yaml`; Frankfurter/
Alpha Vantage/Coinbase already return them. No engine code changes.
changes. Silver/oil/indices need one new registered source each.

## 3. Consensus logic (the "own way")

Per cycle, per pair:
1. Poll ALL enabled sources concurrently (httpx, `asyncio.gather`,
   per-source timeout ~3s).
2. Collect non-None prices. Drop any source that errored (fail-soft, [GUARD L##]).
3. **Vote:** if >=2 prices, take median; require
   `(max-min)/median <= consensus_pct` (default 1%) else REJECT consensus ->
   fall back to last-good price if within stale_s, else None (loop's stale
   handling takes over).
4. Single source only (others down) -> accept that price but mark
   `low_confidence=True` in the Candle so the loop/health can see it.
5. Cache the accepted consensus price + timestamp as last-good.

The [L01] stale guard and the existing health_registry ("price_adapter") in
`loop.py` are reused unchanged — aggregator just feeds them clean data.

## 4. Guard tags & discipline (same as other engines)

- Every try/except + every "source rejected / stale" branch tagged `[GUARD L##]`
  so `tools/verify_guard_tags.py` picks it up (confirm that tool exists; if not,
  add the tag convention + a lightweight checker — flag in review).
- No `except:` bare; `contextlib.suppress` or `except Exception: # noqa: BLE001`
  where appropriate (matches loop.py / ws_price.py style).
- Secrets: none hardcoded (Frankfurter + Coinbase are free no-key; Alpha Vantage
  and metals.dev keys read from env only) — G-secrets stays clean.

## 5. Tests (network-free, full coverage) — `tests/test_aggregate.py`

Monkeypatch each source's `fetch` to return canned values, covering ALL 8 live
pairs (forex x4, gold x2, crypto x2):
- consensus (FX): Frankfurter + AlphaVantage agree within 1% -> median returned
  for EUR/USD (AlphaVantage mocked via `ALPHA_VANTAGE_KEY` env in test).
- AlphaVantage throttled: mocked HTTP 429 -> source returns None, dropped for
  cycle (fail-soft); Frankfurter + yfinance still produce consensus.
- disagreement: 3 sources spread >1% -> consensus rejected, last-good returned
  if fresh, else None.
- single source up (others raise) -> price accepted, `low_confidence` set.
- all sources fail -> None (fail-soft, no raise).
- [L01] stale: last-good older than stale_s -> None.
- PAXG bridge: Coinbase ticker JSON -> XAU/USD proxy Candle.
- crypto WS: mocked `PriceStream` cache -> BTC/USD, ETH/USD candles.
- XAG keyed: with `METALS_API_KEY` set, MetalsSource registered and XAG/USD runs
  full consensus (median from metals.dev + yfinance). XAU also gains double
  coverage (PAXG + metals.dev).
- XAG degraded: with `METALS_API_KEY` absent, XAU still works via PAXG (free) and
  XAG served by YfinanceSource only, Candle flagged `low_confidence=True`; never
  None/crash when others up.
- `seed_history_fn` returns buffered candles in order.
- `make_aggregator_fetch` returns a callable with the right contract.
- integration: aggregator `fetch_fn` driven through `run_cycle` (reuse S18
  `_Feed`-style harness, pairs = all 8) asserts the loop consumes consensus
  candles per pair without error and health_registry["price_adapter"] stays True.

## 6. Verification gate (run before declaring done)

```
uv run ruff check .            # All checks passed!
uv run python tools/verify_no_bare_except.py   # G-except: clean
uv run python tools/verify_no_secrets.py       # G-secret: clean
uv run pytest -q              # full suite green (current 168 -> +N new, 0 broken)
```
Plus: aggregator is **opt-in** via `make_aggregator_fetch` / env
`PRICE_BACKEND=aggregate`; default `fetch_sync` (yfinance) unchanged, so the
production running path is untouched until you flip the switch.

## 7. Out of scope (this increment)

- Sub-second tick streaming (websockets already built/tested, opt-in separately).
- **Oil / indices (US30):** no free real-time source exists; registry extensible,
  add a source later. NOT included now.
- Proxy rotation for yfinance (separate optional fallback; not in this engine).
- Live flip to production (only after you greenlight + I test against the real
  free endpoints with evidence).

**Expansion (post-approval, config-only where possible):** adding FX pairs
(EUR/JPY, USD/CAD, USD/CHF, NZD/USD) or crypto tickers (SOL, etc.) = add to the
bot's `config.yaml` only; Frankfurter/OpenErApi/Coinbase already cover them — no
- Silver needs `METALS_API_KEY` (user's metals.dev subscription, also covers
  XAU) to go from degraded to full — already covered by the user; just supply
  the env var in prod. Oil/indices need a new registered source.

---

## Files touched
- NEW `hermes_core/adapters/aggregate.py`
- NEW `tests/test_aggregate.py`
- EDIT `hermes_core/adapters/__init__.py` (export `PriceAggregator`,
  `make_aggregator_fetch`; add to `__all__`)
- EDIT `hermes_core/adapters/__init__.py` `make_default_fetch` to honor
  `backend="aggregate"` (additive; default unchanged)

## Rollout
1. Build + tests green (this plan). 2. You approve. 3. I implement, keep opt-in,
   run gate. 4. Only after you say "flip it on", I wire `PRICE_BACKEND=aggregate`
   and test live against the 3 free endpoints with real output as evidence.
