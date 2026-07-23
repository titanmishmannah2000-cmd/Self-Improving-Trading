# GP Engine — Deep Spec ("The GP Brain")

> Complete, source-verified description of the Genetic-Programming discovery
> engine, the live ensemble, the promotion path, and the dashboard surfacing.
> Values copied from `genetic.py` / `entry.py` / `loop.py`, not memory.

Last updated: 2026-07-19

---

## 1. What the GP brain IS

A **genetic-programming discovery engine** that evolves small symbolic
indicator expressions over market-data primitives, then admits only those that
survive rigorous out-of-sample statistical gates. The survivors become
"indicators" that are persisted and later **voted** into a directional signal
(the "GP ensemble") for live paper trading.

It is **NOT**:
- A neural net, LLM, or learned weight model — it evolves *expression trees*.
- Crypto-signal-aware (D8 hard isolation: only market-data primitives).
- A tradeable-symbol discoverer — it only discovers *indicators*; the pair
  universe is fixed (FX, gold, silver, BTC, ETH).

It is **faithfully ported** from the older `hermes_trading.genetic_discovery`
engine, including its real admission regime (daily bars, horizon=60) and its
noise-control philosophy (OOS corr ≥ 0.15 + permutation p < 0.05 + walk-forward
majority). The earlier in-system version had regressed to a 5m/h1 regime and an
"OOS OR Sharpe" gate that admitted ~30% noise; both were reverted (commits
`beefebf`, `bd949e9`).

---

## 2. The discovery pipeline (`genetic.py::discover`)

```
discover(pair, prices, volumes=None, *, generations=60, pop_size=40,
         seed=7, top_k=5, horizon=1)
        │
        ▼  prices = daily closes (loop calls with horizon=60, 2y daily)
   ┌─ train/test split (60/40) ─────────────────────────────┐
   │  • train = prices[:cut]   (GA evolves HERE only)         │
   │  • test  = prices[cut:]   (genuine OOS eval HERE)        │
   └─────────────────────────────────────────────────────────┘
        ▼
   _evolve_population(train, pop_size=40, generations=60, horizon, rng)
        │  → population of expression trees (elitism top 10%, crossover+mutation)
        ▼
   for each unique candidate expr:
        sig_test = _signal_for_expr(expr, test)
        oos = _compute_fitness(sig_test, test, horizon)   # |corr(sig, fwd-ret)|
        if oos < OOS_FLOOR (0.15): continue
        p_val = _permutation_pvalue(sig_test, test, horizon, n_perm=200, seed)
        if p_val >= PERM_PVALUE_FLOOR (0.05): continue     # luck firewall
        if len(sig_test) >= N_FOLDS*15 (5*15=75):
            kfold_med, frac = _honest_oos(sig_test, test, horizon)  # 5-fold
            _kfold_ok = (frac >= 0.8) and (kfold_med >= OOS_FLOOR)
            if not _kfold_ok: continue                      # walk-forward majority
        win_rate, total_pnl = _compute_signal_stats(sig_test, test, horizon)
        if redundancy_check(...) == "REJECTED": continue    # |pearson|>0.8 vs existing
        if not _novelty_ok(expr, population): continue
        admit → indicator dict (name, expr, fitness, oos_corr, perm_pvalue,
                win_rate, total_pnl, complexity, horizon, interval="1d")
        if len(admitted) >= top_k (5): break
        ▼
   _save_discovered(pair, admitted)  →  state/discovered/{pair}.json
   return admitted
```

### 2.1 Expression primitives (D8: market-data ONLY)
- **Features** (`FEATURES`): `price, ret, sma5, sma10, sma20, sma50, rsi, vol,
  roc20, mom10, min20, max20, stdev20, ema20`.
- **Operators** (`OPERATORS`): `add, sub, mul, div`.
- No crypto feed, no on-chain, no fear-&-greed, no BTC-specific input is
  reachable from this module's dependency chain (hard isolation verified in code).

### 2.2 Admission gates (ALL must pass)
| Gate | Constant | Value | Purpose |
|------|----------|-------|---------|
| OOS correlation floor | `OOS_FLOOR` | 0.15 | min held-out |corr(signal, forward return)| |
| Permutation null-test | `PERM_PVALUE_FLOOR` | 0.05 | reject signals indistinguishable from label-shuffle noise |
| Walk-forward majority | `frac ≥ 0.8` AND `median ≥ OOS_FLOOR` | (5 folds) | kill GA's "lucky single split" hunt |
| Redundancy | `REDUNDANCY_R` | 0.8 | drop indicators too-correlated with an already-admitted one |
| Novelty | `_novelty_ok` | — | reject degenerate/duplicate expression trees |
| Complexity penalty | `COMPLEXITY_PENALTY` | 0.001 | fitness = oos_corr − 0.001·complexity |

**Why these together:** A bare `OOS ≥ 0.15 + permutation` still admits ~20%
noise under full GA evolution (the GA hunts the one lucky 60/40 split). The
walk-forward k-fold majority (≥4 of 5 folds clearing 0.15, median ≥ 0.15) is
the old engine's real noise control and drives FDR down to <5% on synthetic
noise (validated by `test_genetic.py::test_random_low_rate`).

### 2.3 Persistence & cross-pair sharing
- Admitted indicators saved to `state/discovered/{PAIR}.json` (survives restart).
  The raw tree (`_expr`) is dropped before JSON serialization.
- `SHARED_INDICATOR_GROUPS` reuses indicators across cointegrated/related pairs:
  - `{"XAU/USD", "XAG/USD"}` (gold ↔ silver)
  - `{"EUR/USD", "GBP/USD", "AUD/USD"}` (USD-complex)
- Shared indicators are loaded at **0.5 weight** (`_shared_penalty`) in the
  ensemble. This is knowledge-sharing only — the tradeable universe is unchanged.

---

## 3. The live ensemble (`entry.py::gp_ensemble_signal`)

```
gp_ensemble_signal(pair, prices, strategy=None,
                   consensus_threshold=0.2, min_active=2, z_threshold=0.5,
                   daily_prices=None, promote=False)

  eval_prices = daily_prices if (daily_prices and len>=50) else prices
                ↑ GP brain is evaluated on the DAILY regime it was discovered on
                  (fixes the prior 5m/1d mismatch caveat)
  inds = load_discovered_indicators(pair, include_shared=True)
  for ind in inds:
      series = [ _gp_eval_last(expr, eval_prices[:i+1]) for i in range(49, len) ]
      last = series[-1]; mu, sd = mean, stdev(series)
      if sd < 1e-9: skip
      z = (last - mu) / sd
      if |z| < z_threshold (0.5): skip          # indicator not "activated"
      sig = sign(z)
      weight = max(fitness * win_rate * shared_penalty, 0.1 * shared_penalty)
      votes.append((sig*weight, weight, name))
  if len(votes) < min_active (2): return None
  strength = clamp(total_ws / total_w, -1, 1)
  if |strength| < consensus_threshold (0.2): return None
  consensus = "bullish"/"bearish"  (→ "strong_*" if |strength|>0.6)
  return Signal("gp_ensemble", |strength|, size, pair, {
      "shadow": not promote,
      "gp_strength": strength, "consensus": consensus,
      "num_active": len(votes),
      "entry_type": "gp_ensemble" if promote else "shadow",
      "evaluated_on": "daily" if daily else "live",
  })
```

### 3.1 Daily price source
`gp_daily_prices(pair)` → `seed_history_interval_sync(pair, interval="1d",
period="2y", max_candles=500)` (from `adapters.price`), cached 30 min. Returns
`None` on failure → ensemble falls back to live `prices` (degraded but never
crashes). Fully fail-soft.

### 3.2 Shadow vs promote
- **Shadow** (`promote=False`, default): `entry_type="shadow"`, `shadow=True`.
  Used by `_log_gp_shadow` in `loop.py` to write a paper-only record to
  `state/{bot}/gp_shadow.jsonl` every 300s per pair. **Never opens an order.**
  This is the out-of-sample track record required before any live promotion
  (faithful to "shadow/log-only first").
- **Promote** (`promote=True`): `entry_type="gp_ensemble"`, `shadow=False`.
  Becomes a real (paper) entry candidate that flows through the **same** RR
  guard, position sizing, and exit evaluation as traditional entries.

---

## 4. Promotion path (`loop.py`)

```
in run_cycle(), after traditional evaluate_entry():

  sig = evaluate_entry(...)                  # traditional (mean_reversion/rsi_momentum)
  if sig is None and get_env("GP_PROMOTE") == "1":
      if gp_promote_gate.is_promote_allowed(bot, pair):   # expectancy gate
          _gp_sig = gp_ensemble_signal(pair, prices, strategy,
                                       daily_prices=gp_daily_prices(pair),
                                       promote=True)
          if _gp_sig is not None:
              sig = _gp_sig
  # traditional entries win; GP is a tie-break fallback → never a double open
  if sig is None: skip
  if not check_rr_guard(sl, tp): skip          # R:R ≥ 1.0
  size = compute_position_size(...)  capped to MAX_POSITION_SIZE
  open_positions[pair] = { ..., "entry_type": sig.meta.get("entry_type",
                                                           "mean_reversion") }
  cortex.record_entry(pair, entry_type)
```

**GP promote gate** (`hermes_core/engines/gp_promote_gate.py`):
- Persists `{bot}/state/gp_promote_gate.json` with per-pair `banned`, rolling
  paper/shadow PnL samples, expectancy, cooldown timestamp.
- `GP_EXCLUDE_PAIRS` **seeds** initial bans only (cold start); thereafter the
  gate bans when mean expectancy ≤ `GP_PROMOTE_GATE_BAN` and unbans when
  ≥ `GP_PROMOTE_GATE_UNBAN` (hysteresis), after ≥ `GP_PROMOTE_GATE_MIN_SAMPLES`
  and outside `GP_PROMOTE_GATE_COOLDOWN_S`.
- Invent + `_log_gp_shadow` keep running while banned; shadow forward-PnL and
  closed GP paper trades feed `record_pnl` / `observe_shadow`.

**Environment switches (Railway, per service):**
| Var | Default | Effect |
|-----|---------|--------|
| `GP_PROMOTE` | unset | `"1"` enables GP brain paper promotion |
| `GP_EXCLUDE_PAIRS` | `"GBP/JPY,BTC/USD"` | seeds initial promote-gate bans |
| `GP_PROMOTE_GATE_MIN_SAMPLES` | `30` | min PnL samples before ban/unban flip |
| `GP_PROMOTE_GATE_BAN` | `-0.05` | mean % expectancy → ban if ≤ |
| `GP_PROMOTE_GATE_UNBAN` | `0.05` | mean % expectancy → unban if ≥ |
| `GP_PROMOTE_GATE_COOLDOWN_S` | `86400` | seconds after a flip before another flip |

**Historical daily-regime paper expectancy** (basis for the original exclude list):
XAU/USD +4.28%, XAG/USD +4.28%, ETH/USD +16.77%, EUR/USD +1.43%, AUD/USD +0.79%,
GBP/USD +0.53%, GBP/JPY −6.30% (seeded ban), BTC/USD −26.01% (seeded ban).

---

## 5. Dashboard surfacing (verified live)

End-to-end data path that makes the **"GP Brain"** badge appear:

```
loop.py  open_positions[pair]["entry_type"]="gp_ensemble"
   │  (_log_trade also tags closed trades with entry_type)
   ▼
bots/_runner.py  recent_open_trades.append({ ..., "entry_type": pos.get("entry_type","mean_reversion") })
   │  → pushed to dashboard /api/ingest/{bot} every cycle
   ▼
dashboard/backend/main.py  stores open_trades_json; overview() returns
   pushed recent_open_trades VERBATIM as authoritative open positions
   │  (fix: earlier version cross-checked against trades table and dropped
   │   all live opens — commit 5f5b2bc)
   ▼
dashboard/frontend/src/App.jsx
   • PortfolioPulse: counts bot.recent_open_trades where entry_type=="gp_ensemble"
     → teal "GP BRAIN: N" tile (.pulse-stat-gp)
   • PairCard (.pc-head): shows "GP Brain" pill (.pc-strategy-gp_ensemble) when
     openTrade?.entry_type === "gp_ensemble", wrapped in .pc-strategies
     (right-aligned, wrap-safe) next to the base strategy pill
   • Detail panel: same pill next to strategyLabel
```

**Three bugs that were fixed to get here (see skill `hermes-dashboard-gp-surface`):**
1. `overview()` dropped all live opens (cross-checked vs trades table) → `5f5b2bc`.
2. Badge only in detail panel, not the card grid → `0730b07`.
3. Flex `space-between` with 3 header children overflowed the card → `e369ee0`
   (wrapped pills in `.pc-strategies` + `.pc-head { flex-wrap }`).

**CSS:**
| Class | Appearance |
|-------|-----------|
| `.pc-strategy-gp_ensemble` | teal pill — bg #14303a, color #5fd0e6, border 1px #2c7d92 |
| `.pulse-stat-gp` | portfolio tile — bg #14303a, teal number/label |
| `.pc-strategies` | flex group, right-aligned, `flex-wrap:wrap` |
| `.pc-head` | `display:flex; justify-content:space-between; flex-wrap:wrap; gap:8px` |

---

## 6. Verification status (as of 2026-07-19)

- **Tests:** `tests/test_genetic.py` (8), `tests/test_gp_entry.py` (14),
  `tests/test_dashboard_api.py::test_gp_open_trade_surfaces_in_overview` (1)
  → **25 passed**. `test_random_low_rate` confirms FDR < 5% on synthetic noise.
- **Live discovery:** forex/gold/crypto bots admit 4–5 indicators/pair on the
  daily regime; `/api/discovered` returns ~27–32 indicators across 7–8 pairs.
- **Live promotion:** `GP_PROMOTE=1` set; GBP/USD, XAU/USD, XAG/USD, ETH/USD
  confirmed opening `entry_type="gp_ensemble"` positions (verified via Railway
  logs + `/api/overview`).
- **Live dashboard:** "GP BRAIN" portfolio tile + per-card "GP Brain" pill
  confirmed present and contained inside cards (vision-verified screenshot).

---

## 7. Failure modes & guards (all fail-soft)

| Risk | Guard |
|------|-------|
| Daily fetch fails | fall back to live `prices`; still `None` → no GP signal, traditional only |
| Expression eval crashes | `_gp_eval_last` catches → returns 0.0 |
| Promotion path throws | `except Exception: sig=None` — GP never breaks the cycle |
| No indicators discovered | `load_discovered_indicators` → `[]` → `gp_ensemble_signal` returns `None` |
| Discovery hangs | bounded background thread (`DISCOVERY_INTERVAL_S`, hard 12s/60s timeout) |
| FX history degenerate (was 1 tick) | `seed_history_fn` returns 300-candle series |
