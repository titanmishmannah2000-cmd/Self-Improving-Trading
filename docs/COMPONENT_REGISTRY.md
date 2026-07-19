# Component Registry — Hermes Self-Improving Trading

> Inventory of every engine/service, module, and how they interact.
> Parameter values copied from source, not memory.

Last updated: 2026-07-19

---

## System Architecture (data flow)

```
                         ┌─────────────────────────────────────────┐
                         │            RAILWAY (single image)        │
                         │  dispatched by HERMES_BOT_NAME env       │
                         │                                           │
   FREE PRICE FEEDS       │  ┌──────────────────────────────────┐    │
   (yfinance, AlphaVantage│  │  BOT PROCESS  (forex/gold/crypto) │    │
    keyed, Coinbase,      │  │                                   │    │
    GoldAPI.io free)      │  │  loop.py  ── run_cycle()          │    │
        │                 │  │     │  traditional strategy engine │    │
        ▼                 │  │     │  (mean_reversion/rsi_momentum)│   │
   adapters/aggregate.py  │  │     │  genetic.py (GP discovery)   │    │
   adapters/price.py      │  │     │  entry.py (GP ensemble)      │    │
        │                 │  │     │  cortex (entry memory)       │    │
        └──► prices/regimes│  │     └──► open_positions[entry_type]│   │
                           │  │              │                      │    │
                           │  │  _push_state every cycle           │    │
                           │  └──────────────┼───────────────────┘    │
                           │                 ▼                        │
                           │  ┌──────────────────────────────────┐    │
                           │  │  DASHBOARD BACKEND (FastAPI)      │    │
                           │  │   /api/ingest/{bot} → SQLite      │    │
                           │  │   /api/overview, /api/discovered  │    │
                           │  │   /api/heartbeat, /api/cortex     │    │
                           │  └──────────────┬───────────────────┘    │
                           │                 ▼                        │
                           │  ┌──────────────────────────────────┐    │
                           │  │  DASHBOARD FRONTEND (React SPA)   │    │
                           │  │   App.jsx  builds from src/ in    │    │
                           │  │   Dockerfile; shows GP Brain badge │    │
                           │  └──────────────────────────────────┘    │
                           └─────────────────────────────────────────┘

  state/  (per bot):  trades.jsonl, gp_shadow.jsonl, gp_state.json
  state/discovered/{pair}.json   ← GP indicators (read by dashboard /api/discovered)
  state/cortex/*.json            ← cortex entry memory
```

---

## ENGINE A — BOT CORE (run_cycle)

### A.1 — loop.py (Trading Bot Loop)
**File:** `hermes_core/engines/loop.py`
**Intelligence Level:** 6/7 (autonomous per-cycle decision loop, fail-soft)

**What it does:** Drives one bot. Each cycle: fetch prices+regimes, evaluate
the traditional entry, fall back to the GP ensemble if no traditional signal,
evaluate exits, apply RR guard + position sizing, record entries to cortex,
log trades, and push full state to the dashboard.

**How:**
- `_work()` thread: per-cycle loop.
- `run_cycle(pair, prices, strategy, ..., history_fn, ...)`: the core.
  - Calls `evaluate_entry(...)` (traditional signal).
  - If `sig is None` AND `GP_PROMOTE=="1"` AND `pair not in GP_EXCLUDE_PAIRS`
    → calls `gp_ensemble_signal(pair, prices, strategy, daily_prices=gp_daily_prices(pair), promote=True)`.
  - Traditional entries always win; GP is a tie-break fallback → never a double open.
  - RR guard `check_rr_guard(sl, tp)` requires R:R ≥ 1.0 before committing.
  - Position sizing `compute_position_size(...)` clamped to `MAX_POSITION_SIZE`.
  - `open_positions[pair]["entry_type"] = sig.meta.get("entry_type","mean_reversion")` → this is the tag that flows to the dashboard.
  - `_log_trade(...)` records closed trades, also carrying `entry_type`.
  - `_log_gp_shadow(...)` (separate, always-on) logs the GP "would-be" signal
    every 300s per pair to `gp_shadow.jsonl` — observation only, never an order.
- `_maybe_discover(bot, pair, prices)`: throttled GP discovery, at most once per
  `DISCOVERY_INTERVAL_S` (default 3600) per (bot,pair), persisted to
  `state/discovered/{pair}.json`. Runs in a bounded background thread so it
  never blocks the heartbeat.

**Key functions:** `run_cycle`, `_work`, `_maybe_discover`, `_log_gp_shadow`,
`_log_trade`, `_log_skip`.

**Interacts with:** `entry.py` (gp_ensemble_signal, gp_daily_prices),
`genetic.py` (load_discovered_indicators), `cortex`, `adapters.aggregate`
(history_fn), dashboard `/api/ingest/{bot}`.

**Key constants (loop.py):**
| Constant | Value | Meaning |
|----------|-------|---------|
| `DISCOVERY_INTERVAL_S` | 3600 (env override) | min seconds between GP discoveries per pair |
| `GP_SHADOW_LOG_INTERVAL_S` | 300 | min seconds between GP shadow log records per (bot,pair) |
| `MAX_POSITION_SIZE` | (imported) | hard cap on position size |
| `GP_PROMOTE` env | `"1"` to enable | promotes GP brain to real (paper) entries |
| `GP_EXCLUDE_PAIRS` env | `"GBP/JPY,BTC/USD"` default | pairs never GP-promoted (negative daily paper expectancy) |

---

## ENGINE B — TRADITIONAL STRATEGY ENGINE

### B.1 — strategy / evaluate_entry (mean_reversion, rsi_momentum)
**File:** `hermes_core/engines/entry.py` (traditional half) + strategy modules
**Intelligence Level:** 4/7

**What it does:** Produces the base signal for each pair from two strategies:
`mean_reversion` (Bollinger/RSI/ADX/Session filter chain) and `rsi_momentum`
(RSI/Volume/Regime/Quality/Chart filter chain). This is the pill shown by
default on every card; the GP brain only overrides when it produces an open
position.

**How:** `evaluate_entry(pair, prices, strategy, context, ensemble, ...)`
returns a `Signal` or `None`. Each filter in the chain can skip the trade
(reason recorded → dashboard shows WHY). RR guard + sizing applied in `loop.py`.

**Interacts with:** `loop.py`, `cortex` (records entry by type), dashboard.

---

## ENGINE C — GP DISCOVERY (genetic programming)

### C.1 — genetic.py (Discovery Engine)
**File:** `hermes_core/engines/genetic.py`
**Intelligence Level:** 7/7 (autonomous GA evolution + multi-gate admission)

**What it does:** Evolves small symbolic indicator expressions over
price/volume features, then admits only those that survive genuine
out-of-sample checks. Discovered indicators persist to
`state/discovered/{pair}.json` and feed the GP ensemble.

**How:** see `docs/engines/GP_ENGINE.md` (full spec). Summary:
- Real GA: population of expression trees, elitist survival (top 10%),
  crossover + mutation, depth cap, complexity penalty.
- Admission gates (ALL required): genuine OOS corr ≥ `OOS_FLOOR` on a held-out
  split; permutation null-test p < `PERM_PVALUE_FLOOR`; walk-forward k-fold
  majority (`frac ≥ 0.8` AND median fold-corr ≥ `OOS_FLOOR`); redundancy +
  novelty gates.
- Runs on the OLD engine's working regime: **daily bars, 2y, horizon = 60**
  (the original `daily/h60` config — NOT the 5m/h1 mismatch that produced 0 admits).
- D8 hard isolation: feature/operator set is market-data primitives ONLY
  (price, returns, SMA/EMA, RSI, vol, roc, min/max/stdev, momentum); NO
  crypto-specific signals are reachable.
- Cross-pair sharing: `SHARED_INDICATOR_GROUPS` reuses gold↔silver and
  EUR/USD↔GBP/USD↔AUD/USD indicators at 0.5 weight (knowledge only; the
  tradeable universe is unchanged).

**Key functions:** `discover(...)`, `_evolve_population`, `_signal_for_expr`,
`_compute_fitness`, `_oos_corr`, `_permutation_pvalue`, `_honest_oos` (k-fold),
`_sharpe`, `redundancy_check`, `_novelty_ok`, `load_discovered_indicators`,
`_save_discovered`.

**Interacts with:** `adapters.price.seed_history_interval_sync` (daily fetch),
`loop.py` (`_maybe_discover`), `entry.py` (consumes discovered indicators),
dashboard `/api/discovered` (reads `state/discovered/*.json`).

**Key constants (genetic.py):**
| Constant | Value | Meaning |
|----------|-------|---------|
| `OOS_FLOOR` | 0.15 | min held-out |corr| to admit (restored to old-engine value) |
| `COMPLEXITY_PENALTY` | 0.001 | fitness penalty per node |
| `REDUNDANCY_R` | 0.8 | |pearson| above this vs existing → REJECTED |
| `PERM_PVALUE_FLOOR` | 0.05 | reject if OOS corr not better than shuffled-label noise |
| `N_FOLDS` | 5 | walk-forward k-fold folds |
| `FEATURES` | price,ret,sma5/10/20/50,rsi,vol,roc20,mom10,min20,max20,stdev20,ema20 | primitive set (market-only) |
| `OPERATORS` | add,sub,mul,div | expression operators |
| k-fold majority | `frac ≥ 0.8` AND `median ≥ OOS_FLOOR` | walk-forward admission rule |
| `discover()` defaults | generations=60, pop_size=40, seed=7, top_k=5, horizon=1 | evolution params (loop calls horizon=60) |
| discovery regime | interval="1d", period="2y", max_candles=500 | data the GA runs on |

---

## ENGINE D — GP ENSEMBLE SIGNAL (live evaluation)

### D.1 — entry.py (GP half) — gp_ensemble_signal / gp_daily_prices
**File:** `hermes_core/engines/entry.py` (lines ~166–344)
**Intelligence Level:** 6/7

**What it does:** Turns discovered indicators into a live directional vote
("the GP brain's opinion") for a pair, on the SAME daily regime they were
discovered on. Returns a `Signal` tagged `entry_type="gp_ensemble"` (when
`promote=True`) or `entry_type="shadow"` (observation only).

**How:** see `docs/engines/GP_ENGINE.md`. Summary:
- `gp_daily_prices(pair)`: fetches 2y daily closes via `seed_history_interval_sync`
  (cached 30 min); falls back to live `prices` if daily unavailable.
- `gp_ensemble_signal(...)`: evaluates each indicator's expression on the daily
  window, z-scores its signal series vs itself, votes `sign(z)` weighted by
  `fitness × win_rate × shared_penalty`. Requires `min_active ≥ 2` indicators
  firing and `|consensus strength| ≥ consensus_threshold` (0.2).
- `simulate_gp_paper_pnl(...)`: pure, network-free paper-trade sim used as the
  evidence required before any live promotion.

**Key functions:** `gp_daily_prices`, `_gp_parse`, `_gp_eval_last`,
`gp_ensemble_signal`, `simulate_gp_paper_pnl`.

**Interacts with:** `genetic.load_discovered_indicators`,
`adapters.price.seed_history_interval_sync`, `loop.py` (promotion + shadow
logging).

**Key params (gp_ensemble_signal defaults):**
| Param | Default | Meaning |
|-------|---------|---------|
| `consensus_threshold` | 0.2 | min |strength| to emit a signal |
| `min_active` | 2 | min indicators that must fire (z beyond threshold) |
| `z_threshold` | 0.5 | min |z| for an indicator to vote |
| `daily_prices` | None → fetched | series evaluated on (daily regime) |
| `promote` | False | False=shadow (observe), True=real paper candidate |

---

## ENGINE E — CORTEX (entry memory)
**File:** `hermes_core/engines/cortex.py`
**Intelligence Level:** 3/7

**What it does:** Records entries per `entry_type` (incl. `gp_ensemble`) and
outcomes; supports auto-exile of consistently losing behaviours across cycles.
`loop.py` calls `cortex.record_entry(pair, entry_type)` on every open.

**Interacts with:** `loop.py`, dashboard `/api/cortex`.

---

## ENGINE F — PRICE ADAPTERS

### F.1 — adapters/aggregate.py
**File:** `hermes_core/adapters/aggregate.py`
**Intelligence Level:** 4/7

**What it does:** Multi-source price consensus (frankfurter, AlphaVantage
keyed, Coinbase PAXG/BTC/ETH, GoldAPI.io free for XAU/XAG, yfinance) with
median consensus; spread > 1% → `low_conf` + last-good. `seed_history_fn`
returns real 300-candle FX history (the fix that stopped "bots blind").

**Interacts with:** `loop.py`, `price.py`.

### F.2 — adapters/price.py
**File:** `hermes_core/adapters/price.py`
**Intelligence Level:** 4/7

**What it does:** `seed_history_interval_sync(pair, interval, period, max_candles)`
— interval-aware history fetcher. Added so GP discovery runs on real **daily**
bars (the regime fix). Also holds the gold→silver rescale (free, ratio-based,
no invented data) so XAG/USD GP discovery is not skipped.

**Key functions:** `seed_history_sync`, `seed_history_interval_sync`.
**Interacts with:** `genetic.py` (discovery), `entry.py` (gp_daily_prices).

---

## ENGINE G — DASHBOARD BACKEND

### G.1 — dashboard/backend/main.py (FastAPI)
**File:** `dashboard/backend/main.py`
**Intelligence Level:** 4/7

**What it does:** Receives bot state via `/api/ingest/{bot}`, stores in SQLite
(`latest_state` table, WAL mode). Serves `/api/overview` (prices, regimes,
open trades, cortex, pulse), `/api/discovered`, `/api/heartbeat/{bot}`,
`/api/cortex`, `/healthz`.

**Key behaviour (GP-relevant):** `overview()` returns the pushed
`recent_open_trades` **verbatim** as authoritative for OPEN positions — each
carries `entry_type` (incl. `"gp_ensemble"`). A prior bug cross-checked opens
against the `trades` table (opens are never there until exit) and dropped ALL
live opens; fixed in commit `5f5b2bc`.

**Interacts with:** bot `/api/ingest`, frontend (JSON), SQLite.

---

## ENGINE H — DASHBOARD FRONTEND

### H.1 — dashboard/frontend/src/App.jsx (React SPA)
**File:** `dashboard/frontend/src/App.jsx`
**Intelligence Level:** 3/7

**What it does:** Renders live monitor — Foreign Exchange + Gold + Crypto
sections, pair cards, detail panel, portfolio pulse, Discovered/Cortex tabs.
Builds from `src/` in the Dockerfile (no separate asset upload); served by the
backend container.

**GP-relevant surfaces (all verified live):**
- **Portfolio tile** (`PortfolioPulse`): counts `bot.recent_open_trades` where
  `entry_type === "gp_ensemble"` → "GP BRAIN: N" tile.
- **PairCard grid** (`PairCard`): teal **"GP Brain"** pill in `.pc-head`,
  wrapped in `.pc-strategies` (right-aligned, wrap-safe) next to the base
  strategy pill, shown when `openTrade?.entry_type === "gp_ensemble"`.
- **Detail panel**: same "GP Brain" pill next to `strategyLabel`.

**Interacts with:** backend REST API (`/api/overview` etc.).

### H.2 — dashboard/frontend/src/App.css
**File:** `dashboard/frontend/src/App.css`
**GP-relevant classes:**
| Class | Style |
|-------|-------|
| `.pc-strategy-gp_ensemble` | teal pill: bg #14303a, color #5fd0e6, border 1px #2c7d92 |
| `.pulse-stat-gp` | portfolio tile: bg #14303a, teal number/label |
| `.pc-strategies` | flex group wrapping both pills, right-aligned, wrap-safe |
| `.pc-head` | `flex-wrap:wrap` + gap (so 3 children don't overflow) |

---

## Known issues / audit log

### 2026-07-19 — Dashboard & pipeline audit (B1–B10, X1–X2)
User reported: closed-trades counter = 0, no cumulative chart, empty Activity,
empty Reports, empty Cortex. Root-cause trace found a single master bug feeding
most failures.

- **B1/B2 (FIXED `6d614b5`):** Every `evaluate_exit` result — including
  breakeven/trailing stop-*adjustments* — was written to the closed-trades log,
  and the record used key `reason` while the backend reads `exit_reason`. Net:
  all "closed" rows had `exit_reason=None` + `entry_price==exit_price` → closed
  counter, cumulative chart, Activity feed, and Reports all read zero.
  - Fix: extracted `_process_exit()`; breakeven/trailing only move the stop
    (position stays open, nothing logged); a real close logs a record carrying
    `id`, `exit_reason`, `entry_ts`, `exit_ts`, and the true `entry_type`.
  - Positions now stamped with `id` + `entry_ts` at open for stable identity.
  - Added `tests/test_exit_logging.py` (5 tests: breakeven/trailing = no close;
    sl/tp/partial = correctly-keyed record). All 30 targeted tests pass.
- **B8 (FIXED same commit):** Cortex `record_outcome` was hardcoded
  `"mean_reversion"` regardless of the actual entry type; now uses the real
  `entry_type` (so GP-brain win-rate is measured correctly).
- **B3–B7, B9–B10, X1–X2:** See audit list — pending (Cortex empty needs the
  full cortex summary pushed; GP outcome credit + evolution gap not yet done).


---

## Intelligence maturity summary

| Engine | Level | Autonomy |
|--------|-------|----------|
| GP Discovery (genetic.py) | 7 | Full autonomous GA + gates |
| Bot loop (loop.py) | 6 | Autonomous per-cycle, fail-soft |
| GP Ensemble (entry.py) | 6 | Autonomous live vote |
| Traditional strategy | 4 | Rule-based filters |
| Price adapters | 4 | Source consensus |
| Dashboard backend | 4 | API + storage |
| Cortex | 3 | Memory + exile |
| Frontend | 3 | Render only |
