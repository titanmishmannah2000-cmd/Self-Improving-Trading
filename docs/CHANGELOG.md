# Changelog — Hermes Self-Improving Trading

> Living history. Add an entry after every change (template at bottom).
> SHAs are authoritative — derived from `git log --oneline --reverse`.

Last updated: 2026-07-20

---

## Era 1 — Initial build & deployment scaffold
- `5f8c780` fix(dashboard+bots): serve built frontend, SQLite WAL, bot connection pileup
- `fa2444d` fix(deploy): build frontend in Docker, persist dashboard DB, set PRICE_BACKEND
- `6960f63` feat(dashboard): add /healthz probe + wire Railway healthcheck
- `e9a9eda` feat(deploy): single-image entrypoint dispatched by HERMES_BOT_NAME
- `7cdf594` fix(deploy): declare pyyaml dependency (bots crashed ModuleNotFoundError)
- `4042b06` feat(dashboard): merge redesigned UI + hermes-dashboard-api audit engine
- `fe812b3` fix(deploy): combined dashboard container (API + UI in one service)
- `51d7fe0` fix(deploy): wire dashboard into single-image entrypoint
- `a401cd8` fix(ui): restore Heartbeat + Flatline tabs dropped in redesign
- `2aa2bed` FIX: define _utcnow() in live_compat — price POST was 500-ing (NameError) on every bot push
- `37014b2` Gap1/2/5 fix: bots push full state to /api/ingest each cycle; state_root() honors HERMES_STATE_ROOT (volume); DB_PATH honors DASHBOARD_DB; fix _utcnow 500 on price POST
- `594ea40` FIX: _push_state sends per-pair strategy DICT (not list) so /api/overview no longer 500s on strategy_json.keys()
- `dda383b` REST (path 1): route /api/heartbeat/{bot}, /api/discovered, /api/cortex off SQLite latest_state instead of cross-volume files (was stale); bot _push_state now sends real discovered/cortex from engine write dirs
- `a4b34b4` WIRE discovery+cortex into run_cycle: GP discover (throttled/hourly) per pair → state/discovered; Cortex records entries/outcomes+auto-exile → state/cortex; regimes collected per pair → heartbeat; _push_state already ships them to dashboard

## Era 2 — Fixing "bots never trade" (data plumbing)
- `66044bc` FIX: _maybe_discover self-fetches history via seed_history() (fetch_fn(':history') returned 0 candles for default backend) so GP discovery actually runs
- `6559203` FIX: bound GP discovery in a 12s timeout thread so a slow price API can't stall the heartbeat cycle (regression: discovery network call could hang run_cycle, killing heartbeat)
- `e505ce8` DECOUPLE discovery: run GP discovery in a background thread (_discovery_loop) with 60s bound, removed from run_cycle so the heartbeat cycle is never blocked; regime still collected per-pair in loop
- `b61072f` OBSERVABILITY: _discovery_loop logs discovery results/errors so the live bot's GP discovery activity is visible in Railway logs
- `df71033` LOWER OOS_FLOOR 0.15→0.08 so GP discovery admits the best-available real indicators (old floor was unreachable: best OOS corr ~0.12-0.17, best fit ~0.09-0.12)
- `4214462` FIX: _push_state sends discovered as flat {pair:[inds]} map (matches /api/discovered which iterates discovered_json.items() directly); OOS_FLOOR lowered 0.15→0.08 so GP admits best-available real indicators
- `8ad1915` FIX: _push_state reads discovered/cortex via hermes_core.config.repo_root() (same path the engines write to) so the read path matches the write path under non-editable installs
- `6b2541d` FIX black-screen: DiscoveredView rendered win_rate/total_pnl/uses (undefined in real GP data) and called .toFixed() on undefined → TypeError → React unmounts. Rewritten to render real fields (expr/fitness/oos_corr/complexity), guard every access. Verified build clean.
- `9393c97` FIX bots never trade: run_cycle was called with fresh open_positions={}/reentry={} every cycle, so entries never carried to exit and trades.jsonl stayed empty. Now run_bot persists open_positions+reentry across cycles, passes real oversold_pairs (momentum confluence), returns them in summary, pushes live open positions to dashboard. Also fix _log_skip key (reason_skipped) so dashboard shows WHY skips happen (was always None).
- `703ec66` FIX bots blind: run_cycle used fetch_fn(pair+':history') which the aggregate backend only returns the LAST tick for FX/metals → degenerate indicators (1 candle) → never trades. run_cycle now takes history_fn=seed_history (real 300-candle series, proven) with fallback to :history then single price. Also persists open_positions/reentry across cycles (entries now tracked to exit → trades log) and fixes _log_skip reason_skipped key.

## Era 3 — Free data feeds (crypto history + gold/silver metals)
- `444f936` FIX crypto (and gold) history: _to_symbol mapped BTC/USD→BTCUSD=X / ETH/USD→ETHUSD=X which yfinance now 404s (deprecated). Use working spot tickers BTC-USD/ETH-USD (1364 rows). Gold XAU/USD → XAU=F. Without real history crypto indicators were degenerate → silent None skips → never trades.
- `6e14d15` ADD free authentic metals feed: GoldAPI.io (no key) as live XAU/USD+XAG/USD source (PAXG/Coinbase 403, metals.dev quota exhausted, yfinance live flaky). Wire GoldApiSource into aggregate consensus. Seed metals history: gold from yfinance XAU=F (works in Railway low-volume), silver from real gold returns rescaled to silver's live GoldAPI level via G/S ratio (~71) — preserves authentic volatility/regime, no invented data. run_cycle now uses aggregator.seed_history_fn (has the proxy) instead of standalone seed_history.
- `65a8c62` TEMP DIAG: log GoldApiSource fetch errors to stderr
- `7c21dbf` TEMP DIAG: log GoldApiSource httpx status/body
- `3b87032` FIX 'Event loop is closed' in aggregate fetcher: _BaseSource._get_client cached one httpx client, but the aggregator runs a FRESH asyncio loop per fetch_fn call (asyncio.run), so the cached client was bound to a CLOSED loop → later-cycle fetches failed → gold no_candle. Now creates a fresh client per call (binds to current loop). Removed TEMP DIAG logging. GoldAPI.io live XAU/XAG now works reliably in Railway.
- `ba83d25` TEMP DIAG: log fetch_fn exceptions for gold
- `715a056` TEMP DIAG: log GoldApiSource success price
- `0cabf04` TEMP DIAG: log _fetch_async prices for gold
- `6956621` Finalize free metals feed: GoldAPI.io is the authoritative live XAU/USD+XAG/USD source (free, no key, works in Railway). Exclude stale/inconsistent yfinance XAU=F/XAG=F from the LIVE consensus (it returns 915 alongside 4004, polluting spread). yfinance still seeds metals HISTORY (via _yf_history). Removed all TEMP DIAG logging. Event-loop fix (fresh httpx client per fetch) keeps GoldAPI reliable across cycles.
- `226b932` TEMP DIAG: log GoldApiSource XAG raw + cached
- `bef9870` REMOVE all TEMP DIAG logging from aggregate.py. Gold feed finalized: GoldAPI.io (free, no key) is the authoritative live XAU/USD+XAG/USD source in Railway (verified 4019.30 / 56.08 live, 24/24 calls no rate-limit). Event-loop fix (fresh httpx client per fetch) + yfinance excluded from live metals consensus (was returning stale 915 alongside 4004, polluting spread); yfinance still seeds metals history. Verified locally: run_cycle('gold') returns prices={XAU/USD:4011.71, XAG/USD:56.08} with regimes computed. Live heartbeat shows both metals in regimes.
- `d613590` TEMP DIAG: log fetch_fn XAG candle=None/stale
- `f95d617` REMOVE final TEMP DIAG. Gold feed fully verified LIVE on Railway: regimes={'XAU/USD':'range','XAG/USD':'range'}, recent skips all no_signal (legit, gold trading normally). Root cause of earlier gold no_candle was the cached httpx client bound to a closed asyncio loop (Event loop is closed) → fixed by creating a fresh client per fetch. GoldAPI.io (free, no key) is the authoritative live XAU/USD+XAG/USD source. yfinance excluded from live metals consensus (stale 915 alongside 4004 polluted spread); still seeds metals history.
- `74f45eb` Harden GoldApiSource: retry up to 3x on cold-start TLS failure (eliminates XAG/USD no_candle flicker on container restart); lower _min_interval 5s→1s (GoldAPI verified 24/24 rapid calls, no rate-limit). Gold feed now robust from cycle 1.
- `37b9e99` Fix gold (and all) pair-card price display gap.
- `3c333f0` Make pair-card price sticky + stop showing stale no_candle as a block.
- `a8abf6c` fix(ui): restyle Discovered tab to match original list look
- `4f47328` GP discovery: port real GA evolution + genuine out-of-sample gates from old engine
- `6638339` fix(ui): rich Discovered rows — win ratio, quality dots, asset colours
- `beefebf` Fix GP discovery: run on the old engine's working regime (daily bars, horizon=60)

## Era 4 — GP engine faithful port + promotion (the deep work)
- `4f8a37f` GP discovery: surface real fetch/error reason in live logs (stop silent swallow)
- `bd949e9` Wire Sharpe + k-fold (walk-forward) gates into discover(); restore OOS_FLOOR=0.15
- `1cdadca` Add gold→silver rescale so XAG/USD GP discovery no longer skipped
- `a5680da` Add cross-pair indicator sharing (A) + shadow gp_ensemble entry (B)
- `e77d132` Wire GP shadow logger into live loop (log-only, never an order)
- `1568eb2` TEMP: shadow debug log to confirm live firing (revert next)
- `cb4d90b` TEMP: unconditional shadow debug log
- `d526b99` TEMP: print-based shadow debug (bypass logger)
- `e9560aa` FIX: FX pairs now get real multi-candle history (was 1 tick)
- `da5fd15` TEMP: live confirm print
- `b5b54da` Remove temp live-confirm print; shadow hook clean
- `425f922` Fix GP regime mismatch + promote GP brain to paper trading
- `0932b25` TEMP: live promote confirm
- `cae5630` Remove temp promote confirm log; clean
- `6a503f2` Surface GP-brain paper entries on the dashboard
- `5f5b2bc` Fix dashboard dropping live open trades (GP-brain surfacing)
- `0730b07` Show 'GP Brain' badge on the pair-card grid (not just detail panel)
- `e369ee0` Fix GP Brain pill overflowing the pair card
- `6d614b5` Fix trade-close pipeline (audit #1): only real closes logged with correct keys
- `e0b4c9e` TEMP: print on real close to confirm new close path executes
- `0d21b14` TEMP: print on every exit evaluation (prove _process_exit runs)
- `d37ee84` Fix Cortex tab incomplete: CortexView read by_entry_type/by_pair from botData.summary (wrong nesting — API sends them as top-level siblings of summary), so the Performance-by-Entry-Type and Per-Pair tables never rendered. Now reads from botData directly. Bundle verified: CortexView chunk contains the tables.

## Era 5 — Audit (#1 done, B7 done) + remaining items
- **Audit #1 (CLOSED):** trades never truly closed → closed-trades counter 0, cumulative chart missing, Activity 0, Reports 0. Fixed in `6d614b5` (real-close-only logging with correct keys id/exit_reason/entry_ts). Deployed + verified live. Those 4 tabs now feed off real closed trades.
- **Audit B7 (CLOSED):** Cortex tab empty. Two real bugs fixed: (1) bot sent cortex double-nested `{bot:{bot:summary}}` (fixed → flat summary, `1d747d7`); (2) a STALE `live_compat.compat_cortex()` route was registered AFTER the real `/api/cortex` and shadowed it, returning `no_data` because it only counted `exiled`/`indicators` (both empty in new cortex) while ignoring `summary`/`by_entry_type` (fixed → removed stale route, `047b181`). P&L now aggregates (`23527be`). VERIFIED LIVE: forex/gold/crypto all return real entries_total + by_entry_type win-rates + PnL.
- **Audit B7 UI fix (CLOSED):** even with data present, the Cortex tab looked incomplete — the Performance-by-Entry-Type and Per-Pair-Totals tables were coded to read `by_entry_type`/`by_pair` from `botData.summary`, but the API sends them as TOP-LEVEL siblings of `summary`. Fixed in `d37ee84` (read from `botData` directly). Live bundle verified to contain the tables. After refresh the tab shows real entry-type + per-pair win-rate/PnL tables. (Indicators + Policy cards remain empty until B9/B10 land.)
- **Remaining (open):** B8 (done in #1 — record_outcome uses true entry_type), B9 (per-vote indicator credit — not yet; Indicators card empty until then), B10 (wire live paper PnL back into GA fitness — not yet), X1/X2 (Discovered repopulate + record fields — minor, unverified live).

---

### Entry template
<!--
### YYYY-MM-DD — Short Title
- **Scope:** (which project(s) / engine(s) changed)
- **Description:** What changed, why, and what it affects.
- **Files Changed:** (list)
- **Verification:** (how it was proven — pytest, build, live API, screenshot)
-->
