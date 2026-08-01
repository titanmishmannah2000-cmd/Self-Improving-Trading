# BTC/USDT Focus Roadmap

**Decision:** One market only — `BTC/USDT` (or venue equivalent `BTC/USD` on Coinbase).  
**Source of lessons:** r/algotrading consensus + live-success process patterns + BTC microstructure research (2025–2026).  
**Goal:** Get to cost-aware positive expectancy as fast as honesty allows — not invent theater.

---

## North star pipeline

```
W1/D1 regime gate
    → 4H (or D1) signal
    → meta skip / trade-or-pass
    → size (vol-scaled, 0.5–1% risk)
    → exit stack (ATR / trail / time)
    → promote only if: OOS + cost×2 + trial-deflated metrics pass
```

Optional parallel Strategy A (faster path to green): **BTC funding cash-and-carry** (long spot + short perp). Directional trend is Strategy B.

---

## Phase 0 — Scope freeze (do first)

### Steps
1. Declare universe = `{BTC/USDT}` only in bot config, invent, promote, scorecard, dashboard filters.
2. Disable / archive forex + gold + ETH live paths (keep git history; do not delete learning code yet — just stop running it).
3. Kill cross-bot lesson transfer (no FX/gold hypotheses applied to BTC).
4. Fix dual state roots if present (`crypto/state` vs `bots/crypto/state`) — one canonical `HERMES_STATE_ROOT`.
5. Remove / quarantine misplaced discovered artifacts (EUR, XAU under crypto folders).
6. Rename mental model: bot = `btc` (or keep `crypto` name but pairs list is length 1).

### Done when
- Only BTC appears in heartbeat, invent, promote gate, open positions, scorecard.
- No scheduled job invents or reflects on non-BTC pairs.

### Exit criteria
- One Railway/service (or local) process trading/shadowing BTC only for ≥24h without touching other pairs.

---

## Phase 1 — Cost engine (Reddit #1)

### Why
Edges that look fine on mid prices die on maker/taker + slippage. BTC is liquid, but high turnover still loses.

### Steps
1. Pick primary venue (Coinbase Advanced **or** Binance/Bybit spot). Document fee tier table.
2. Implement `CostModel`:
   - Maker fee %, taker fee %
   - Round-trip = entry + exit (assume worst of maker/taker policy you actually use)
   - Slippage model: `max(floor_bps, k * ATR_pct)` or book-depth proxy
   - Optional: withdraw/network fees **out of scope** for trading P&L
3. Wire costs into:
   - Paper fill simulation (entry/exit prices haircut)
   - Backtest / reflection OOS gate
   - GP / promote expectancy
   - Live scorecard
4. Add **2× cost stress**: strategy must stay positive expectancy at `2 * all_in_round_trip`.
5. Define **minimum edge per trade** (e.g. expected R after costs ≥ 0.15R or EV_$ ≥ N× fee). Reject opens below that.
6. Log every closed trade with: gross PnL, fees, slippage estimate, net PnL.

### Done when
- No promote/scorecard path uses flat `0.05%` generic FX default for BTC.
- Synthetic “mid-only” scorecard is labeled fiction or removed.

### Exit criteria
- Replay last N shadow/paper trades with cost model; expectancy sign must match cost-aware metric, not mid fantasy.

---

## Phase 2 — Regime gate (higher TF → execute lower TF)

### Why
BTC chop destroys trend systems. Reddit consensus: identify market type on bigger TF, execute on smaller.

### Steps
1. Define **regime TF = daily (primary), weekly (confirm)**.
2. Regime labels (start simple, one method):
   - **Trend up:** price > SMA200 (D1) and SMA50 > SMA200, or ADX≥25 + +DI > −DI
   - **Trend down:** inverse
   - **Chop / range:** else (or BB bandwidth low + ADX&lt;20)
3. Policy:
   - Trend up → allow **long-only** momentum/trend
   - Trend down → **flat** for v1 (spot long-only) OR allow shorts only if shorting is explicit Strategy B later
   - Chop → **flat** (cash). No invent-driven forced trades.
4. Execution TF = **4H** (preferred) or **D1**. Do not use 1H as primary invent/live eval.
5. Persist regime per bar close into state (`regime.jsonl` or heartbeat fields).
6. Dashboard: show current regime + “allowed sides”.

### Done when
- Loop refuses entries when regime = chop (hard guard, not soft weight only).
- Invent labels formulas with regime tags; ensemble only votes in matching regime.

### Exit criteria
- ≥30 days paper: chop periods show ~0 forced entries; trend periods show concentrated trades.

---

## Phase 3 — Signal stack (simple first) — **SHIPPED (Strategy B v1)**

### Why
Published TA+ML dumps fail OOS. Simple trend + sit beats complex invent early.

### Steps
1. Ship **Strategy B v1** (directional) as one of:
   - Donchian / channel breakout on 4H with D1 filter, **or**
   - EMA 21/50 (or 50/200) on D1 with 4H entry refinement
2. Freeze GP invent complexity: tiny search or **off** until v1 has 50+ closed cost-aware trades.
3. If invent stays on: same TF as live (4H/D1), horizon matched to hold time, OOS + permutation required.
4. Ban feature dumps: no “50 indicators → ML selector → subset of lucky months”.
5. Document economic rationale in one paragraph per strategy (why BTC should trend / break out).

### Implemented (2026-08)
- `strategy_type: donchian_breakout` on `bots/btc` (`regime_split: false`).
- Entry uses prior-N Donchian on **4H** closes (`gp_invent_prices`); D1 gate unchanged (Phase 2).
- `invent.enabled: false` — discovery thread not started; `_maybe_discover` also no-ops.
- Economic rationale in `BTC_USDT.yaml` header.
- Funding Strategy A: **not** started (optional).

### Done when
- One primary directional rule set is coded, tested with costs, and runnable without invent.
- (Optional) Funding A runs with its own kill switches.

### Exit criteria
- Directional: walk-forward or rolling OOS with WFE-style sanity (OOS retains meaningful fraction of IS).
- Funding A: positive net funding − fees over a multi-week window in paper/live micro.

---

## Phase 4 — Meta skip / trade-or-pass

### Why
Primary model picks side; secondary decides whether to bet. Filters false positives; sits in cash.

### Steps
1. Log every candidate signal that was **skipped** (reason codes: regime, RR, cost floor, flat price, etc.).
2. Build shadow labels: would skipped trades have won? Would taken trades have lost?
3. Train/update a **simple meta gate** (start rule-based, not deep ML):
   - Examples: vol too low vs fee; BB bandwidth; hour-of-day; distance to D1 MA; recent WR of setup class
4. Meta outputs: `take` | `skip` | `probe_size`.
5. Promote meta only after holdout; default-safe = skip when uncertain.
6. Reuse existing skip-shadow learning path if present — retarget to BTC only.

### Done when
- Every cycle can explain “signal fired but skipped because meta/cost/regime”.
- Meta cannot increase size above base without evidence_n threshold.

### Exit criteria
- Cost-aware expectancy of **taken** trades &gt; expectancy of **all raw signals** over same window.

---

## Phase 5 — Sizing and exits (more important than entries)

### Why
Random-ish entries + good exits/sizing can beat clever entries + bad money management.

### Steps
1. Risk per trade: **0.5–1%** of equity (spot, no leverage until Phase 7).
2. Vol-scale size: `risk_cash / stop_distance`; ATR stop (e.g. 1.5–3× ATR on exec TF).
3. Probe sizing: thin evidence → 25–50% of normal size.
4. Exit stack (tune in this order):
   - Hard stop (ATR or structure)
   - Profit target **or** trailing stop (not both fighting)
   - Time stop (max bars / days in trade)
   - Optional MFE giveback rule after evidence
5. Book risk: max one BTC directional position for v1; no pyramiding until proven.
6. Daily loss kill switch (e.g. −2R or −3% day → flat rest of day).

### Done when
- Entry code is thinner than exit/sizing code.
- Every open position has stop, invalidation, and time stop at open.

### Exit criteria
- Monte Carlo / shuffle of entry times still survivable under the exit+size rules (sanity check).

---

## Phase 6 — Self-improvement without overfitting

### Why
Multiple concurrent learners = multiple testing. Reddit / López de Prado: deflate for trials.

### Steps
1. Count **trials**: each invent generation winner, each L1 axis proposal, each skip-shadow promote attempt.
2. Promote bar rises with trial count (deflated Sharpe / higher min expectancy / higher min N).
3. One-variable-only reflection remains law for param tweaks.
4. Pre-register walk-forward protocol (IS:OOS ratio, roll step) **before** optimizing.
5. Never peek OOS to choose which invent variant to keep.
6. Holdout block never used until final gate before micro-live.
7. Shadow always on; live promote off until gates pass.

### Done when
- Promote gate JSON shows trial_count and required threshold.
- Reflection cannot deploy without backtest + cost×2 + trial rule.

### Exit criteria
- Written promote checklist signed off (even if only by you) before `GP_PROMOTE` / auto-deploy flips.

---

## Phase 7 — Paper → micro-live

### Steps
1. Paper with **realistic fills** (Phase 1 model) for ≥50 closed directional trades **or** ≥4 weeks funding A.
2. Scorecard gates: net expectancy &gt; 0 at 1× and 2× cost; max DD under policy; min trade count.
3. Micro-live: smallest venue size, **1× spot**, 0.25–0.5× research size.
4. Compare live slippage vs model; recalibrate cost model weekly.
5. Scale only on live (not paper) evidence; freeze invent during first live soak.

### Done when
- Micro-live runbook filled for BTC (venue, keys, kill switch, max daily loss).

### Exit criteria
- Live net expectancy same sign as paper for first soak window; if not, halt and fix costs/execution before size-up.

---

## Phase map (checklist)

| Phase | Name | Blocks |
|------:|------|--------|
| 0 | Scope freeze | Everything |
| 1 | Cost engine | Promote, scorecard, paper fills |
| 2 | Regime gate | Entries |
| 3 | Simple signal (+ optional funding A) | Edge source |
| 4 | Meta skip | Selectivity |
| 5 | Size + exits | Survival |
| 6 | Trial-aware improve | Anti-overfit |
| 7 | Micro-live | Real money |

Suggested order is strict: **0 → 1 → 2 → 3 → 5** in parallel with early **4** logging; **6** wraps promote; **7** last. Do not jump to 7 because invent “looks good” on mid prices.

---

## What NOT to port blindly

### From old Hermes (forex / gold / multi-bot)

1. **Multi-pair universe “for diversification”**  
   BTC and ETH are highly correlated; EUR+GBP even more so. One pair. Adding ETH “just in case” splits samples and fake-diversifies.

2. **Flat `SCORECARD_COST_PCT = 0.05%` FX default**  
   Wrong market, wrong scale. BTC needs venue fee + slippage model.

3. **1H invent / short-horizon GP as primary**  
   H1 and below is where fees + noise dominate on BTC/ETH studies. Port the *gate discipline*, not the crypto invent TF from the old bot.

4. **Mean-reversion as default crypto strategy**  
   Gold/FX MR habits do not transfer. BTC v1 = trend + sit (chop = flat).

5. **Soft weights instead of hard regime blocks**  
   Soft-weighting a bad regime still bleeds. Chop must be a hard no-trade for v1.

6. **Cross-asset cortex / shared lessons**  
   “What worked on XAU” must not mutate BTC YAML. Isolate state.

7. **HIF flag spaghetti on by default**  
   Kelly, book risk, entry ranking, exit intel — port **one at a time** after baseline expectancy exists. Don’t flip all env flags to feel advanced.

8. **Promote seed bans / FX pair lists**  
   `GP_EXCLUDE_PAIRS` folklore (GBP/JPY, old BTC ban) is not a substitute for a live BTC cost-aware gate. Rebuild gate for BTC only.

9. **Dashboard vanity (GP Brain badge) as success metric**  
   Success = cost-aware expectancy and DD, not invent activity.

10. **Reflection every N closes with auto-deploy**  
    Port OOS + one-variable rules; do **not** port eager auto-deploy. Prefer propose → shadow → human/gate promote.

### From r/algotrading hype / papers

11. **Sentiment / Twitter / Fear&Greed / “AI predicts BTC”**  
    Famous replication work: mostly p-hacked; dies with costs and honest OOS.

12. **On-chain vanity features early**  
    Easy to overfit; add only after simple price+vol system is green.

13. **Copying SPX diagonal / 0DTE playbooks onto BTC options**  
    Different microstructure, assignment/expiry, and IV dynamics. Learn spot process first.

14. **HFT / latency / maker-rebate games**  
    Wrong game for a ~minute loop retail system.

15. **Optimizing walk-forward until it looks good**  
    Pre-register WFO; don’t shop rolling vs anchored after seeing results.

16. **Win-rate chasing**  
    Trend systems often win &lt;50%. Optimize expectancy and DD, not WR.

17. **“Beautiful 3-month equity curve = done”**  
    Short samples lie. Require trade count + cost stress + holdout.

18. **Buying someone’s BTC bot / signal service**  
    Process and cost model are the product; rented curves are not.

### From “crypto-native” habits

19. **High leverage perps as the main book**  
    Liquidation is how retail dies. Spot 1× (or funding A with conservative margin) until proven.

20. **Trading every funding / every 15m scalp**  
    Turnover is the enemy. Fewer trades with large post-cost EV.

21. **Alt rotation (SOL, memecoins) “for more edge”**  
    Violates one-market focus; worse books; more scams/gaps.

22. **Assuming BTC ETF era = easy TA**  
    Institutions dampen some dislocations; your edge must be simpler and more selective, not more complex.

23. **Ignoring funding if you run perps**  
    Funding is P&L. Directional bots that ignore it mismeasure expectancy.

24. **Paper mid fills forever**  
    If paper never applies Phase 1 costs, live will surprise you.

---

## Success metrics (BTC only)

| Metric | Target (research → micro-live) |
|--------|--------------------------------|
| All-in round-trip cost model | Documented + logged per trade |
| Expectancy @ 1× cost | &gt; 0 |
| Expectancy @ 2× cost | ≥ 0 (or policy-defined floor) |
| Max DD (paper soak) | Under written limit (e.g. 15–20%) |
| Min closed trades before promote | ≥ 50 directional **or** ≥ 4 weeks funding A |
| Regime compliance | 0 intentional entries in chop (v1) |
| Trial accounting | Every promote shows N_trials + threshold |

---

## Anti-goals

- Beating BTC buy-and-hold in every month.
- Maximum invent throughput.
- Multi-bot Railway bill for unused FX/gold.
- Perfect prediction — prefer correct **non-trading**.

---

## Document control

- **Status:** Decision-locked to BTC/USDT focus (user 2026-08-02).
- **Next engineering action:** Phase 0 scope freeze + Phase 1 cost model skeleton.
- **Related:** `docs/PROFITABILITY_MICRO_LIVE_RUNBOOK.md` (retarget to BTC when implementing Phase 7).
