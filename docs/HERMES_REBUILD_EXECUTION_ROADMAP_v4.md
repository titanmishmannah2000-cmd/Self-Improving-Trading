# HERMES TRADING — REBUILD EXECUTION ROADMAP (v4)
### Engineering-discipline companion to HERMES_MASTER_BLUEPRINT_v4.md

> **v4 changes (this release):** (1) removed a fabricated data point — an earlier draft cited
> "GBP/JPY (score 77)" in §8.2; the blueprint has no such figure, so the example now uses only the
> verified EUR/USD score of 70; (2) the reflection-cadence change (5→10 trades) was being applied
> silently in §8.2, the Section 9 table, and Session 18 without being declared as a departure from
> the blueprint — it is now stated as **Deliberate Departure #2** in this header, consistently
> labeled everywhere it's used; (3) fixed a dangling citation ("corrected cadence, 9.4" — Section 9
> is a single table, not subdivided into 9.1-9.4) to point to this header instead; (4) re-pointed at
> `HERMES_MASTER_BLUEPRINT_v4.md`, whose Open Items Registry now marks the re-entry-cooldown and
> partial-close facts this document cites (Sessions 4-5) as RESOLVED rather than NOT FOUND IN SOURCE
> — those citations were correct, the blueprint's registry just hadn't caught up to its own Section 3
> yet. No other content changed from v3.
>
> **Purpose.** The blueprint is the *what* — 18 phases, 66 guard layers, the architecture, the
> per-phase test bodies. This document is the *how-we-stay-disciplined*: coding standards, testing
> gates, error-handling philosophy, observability, dependency management, CI/CD, documentation, and
> change management that make the rebuilt system low-bug, traceable, and non-redundant **in
> practice**, not just on paper. It does not repeat the blueprint's phase test bodies, GP math,
> crisis vectors, or LLM prompt text — those stay in blueprint Sections 7, 11, and Appendices B/E/F.
> This document layers process on top of them.
>
> **Source of truth hierarchy.** `HERMES_MASTER_BLUEPRINT_v4.md` is authoritative on every numeric
> value, guard trigger, and contract signature. If anything below disagrees with the blueprint, the
> blueprint wins and this document is corrected in the same change. **Two exceptions, stated once
> here and binding everywhere below — nowhere else in this document departs from the blueprint:**
>
> - **Deliberate Departure #1 — the L2 reflection score gate.** The blueprint's Section 3 entry for
>   L41 documents the *current, buggy* production value (`score>=55`) and explicitly flags it as a
>   regression from the original `65`. This roadmap does not inherit that regression — every
>   reference to the score gate below uses the **corrected** values: `score>=65` to reach L2
>   (restored), and a **new protected tier at `score>=75` requiring unanimous 3/3 model consensus**
>   rather than the standard 2/3. This exists because blueprint Section 9's lessons record why the
>   weakened gate is a lesson, not a spec.
> - **Deliberate Departure #2 — reflection cadence (RETIFIED by user 2026-07-16).** The blueprint
>   documents `reflection_every=5` (tiered to `3` for a pair's first 30 closed trades, then `5`).
>   This v4 roadmap temporarily departed to a **flat `reflection_every=10`**. **User decision
>   (2026-07-16): adopt a flat `reflection_every=5`** everywhere below (§8.2, Section 9 row 9,
>   Session 9, Session 18) — the blueprint's base cadence, NOT its tiered 3/5. The v4 flat-10
>   departure is now retired by this user override. The original 10 rationale (a 5-trade sample deemed
>   too small) is noted but overridden; if production sample size proves insufficient, revisit via the
>   Section 10 cadence review rather than silently re-departing. This restores L37 (reflection latch)
>   to fire every 5 closed trades per pair, matching blueprint Section 3 and the blueprint data-flow.
>
> **How to read this document.** Sections 1-8 are principles. Section 9 is the phase-by-phase gate
> table (skim it first for orientation). **Appendix A is the operational layer** — one
> self-contained, mechanically-parseable work order per build session (S0-S18). If you are the LLM
> executing the rebuild, read Sections 0-8 once for context, then work Appendix A strictly in order,
> re-reading only the cited blueprint section for each session as you reach it.

---

## 0. THE DISCIPLINE PRINCIPLES (non-negotiable, derived from Section 9 lessons + operational history)

Ten rules. D1-D8 exist because the blueprint's Section 9 records exactly how the old system broke.
D9-D10 exist because of what happened *around* the code — the Discord alert flood and the three
follow-up passes needed to complete the blueprint itself — failure modes the blueprint doesn't
narrate but this project's own history proves are just as real.

| # | Principle | Forbids | Born from |
|---|-----------|---------|-----------|
| D1 | **Single shared engine; bots are config instances.** No engine code branches on `bot=="forex"/"gold"/"crypto"` except the config loader. | Two drifted 2,650-line `loop.py` copies | Lesson 2 |
| D2 | **State on a separate persistent volume (`/data`); code is read-only.** Engine never writes inside the repo. | State wiped on redeploy | Lesson 3 |
| D3 | **Adapters fail soft: return `None`, never raise, never loop forever.** A missing candle is a skip, not a crash. | "198 fake trades" weekend-duplicate crash | Lesson 7 |
| D4 | **Guard layers live in the engine, not the orchestrator only.** A guard bypassable by calling an engine directly is not a guard. | Stale-candle guard lived in `loop`, not adapter | Lesson 7 |
| D5 | **No single LLM changes a live param.** Every mutation requires 2/3 consensus at `score>=65`, and **unanimous 3/3 at `score>=75`** (corrected gate — see header). | One biased LLM changing live params; a weakened gate silently unprotecting winning strategies | Lesson 6 + score-gate regression |
| D6 | **OOS validation is Phase 0 of every backtest, not the last.** A change failing OOS is rejected before historical metrics are even computed. | Bad changes shipping because OOS ran last | Lesson 8 |
| D7 | **One variable at a time.** A reflection proposal mutates exactly one param; never a bundle. | v06→v07 cliff from an un-isolated change | Lesson 1 |
| D8 | **Gold/silver are momentum, never mean_reversion. No crypto-specific signals in forex/gold.** | Wrong strategy type from day 1; FNG/BTC-onchain noise in non-crypto bots | Lessons 4, 5 |
| D9 | **Alerts are budgeted, not unlimited.** Max 1 Discord alert per `(bot, pair, guard)` per 15-minute window; excess firings increment a counter and post once as a summary. | Automated cron/reflection output drowning a channel meant for human conversation, making incident response itself the incident | Operational history: alert-channel collapse during the original build |
| D10 | **The blueprint is a generated artifact, not hand-maintained truth.** Guard enumeration (Section 3) and data schemas (Appendix C) are extracted from code by a script, and CI fails if the committed blueprint disagrees with what that script finds. | A blueprint slowly drifting out of sync with code until a manual audit is needed to catch up | This project's own history: the blueprint needed three separate completeness passes before it was usable |

---

## 1. CODING STANDARDS

**1.1 Language & style.** Python 3.11+. `ruff` is the single formatter (lint + import-sort +
format) — no competing `black`/`isort` config to drift out of sync with each other. Line length 100.
Type hints mandatory on every public function; `mypy --strict` runs on `hermes_core/` with zero
tolerance for `Any` in a `compute_*` or `check_*` return type.

**1.2 Engine interface contracts are frozen and versioned.** The engine signatures in blueprint
Section 6 (`PriceAdapter.fetch`, `IndicatorEngine.compute_all`, `EntryEngine.evaluate`,
`ExitEngine.evaluate`, `RiskEngine.size`, `ReflectEngine.reflect`, `BacktestEngine.validate`,
`GeneticEngine.discover`, `GPIntelligence.score`, `Cortex.record_*`, `PolicyEngine.evaluate`,
`ChartVision.get_context`, `SelfAudit.run`) are a frozen API. Adding or changing a parameter on any
of them is a breaking change requiring a documented handover note (Section 10 of the blueprint) and
a major-version bump of the `hermes_core` package. Engines talk to each other **only** through these
signatures and the shared `models.py` dataclasses (`Trade`, `Strategy`, `Goal`, `Indicator`, `Signal`,
`Exit`, `Policy`) — never through ad-hoc dicts passed engine-to-engine.

**1.3 No duplication — mechanically enforced (implements D1).** `hermes_core/` is the only place
engine logic lives. `bots/{forex,gold,crypto}/` contain *only* `config.yaml` + per-pair strategy
YAML — no `.py` files beyond a 10-15 line `main.py` that imports and runs the shared engine with that
bot's config. A grep for `if bot ==` or `if pair in (...)` inside `hermes_core/` is a failing CI lint
rule (`G-rep`), not a style suggestion. This is the exact rule that would have prevented the original
two 2,650-line `loop.py` files from ever existing as separate files in the first place.

**1.4 Every guard carries its blueprint ID in its docstring.** Any function implementing one of the
66 enumerated guards (blueprint Section 3, L01-L66) must tag its docstring with `[GUARD L##]` plus
the exact blueprint line citation it implements, e.g.:

```python
def _is_novel_regime(features: dict, history: list) -> bool:
    """
    [GUARD L21] Novel-regime flatline detector.
    Blueprint ref: Section 3, loop.py:1813-1817 (novelty > 3.0*median → pause 60 cycles).
    """
```

A companion script (`tools/verify_guard_tags.py`, built in Session 0) diffs the L01-L66 list in the
blueprint against docstring tags actually found in `hermes_core/` and fails CI (`G-guard-tags`) if
any guard has zero matches. This is what makes a repeat of the original L51 bug — a guard
(`is_contaminated`) that existed in gold's `reflect.py` but was never ported to forex's — structurally
impossible rather than merely unlikely: with one shared engine (D1) there is only one copy of the
function to tag, so it cannot exist in one bot and not the other.

**1.5 Pure functions for all math.** `compute_rsi`, `compute_atr`, `compute_roc`, `compute_bb`,
`compute_adx` are pure: `prices -> value`, no I/O, no global state, no network. This determinism is
what makes the blueprint's pytest cases reproducible and lets the same function run identically in
the live loop, the backtest simulator, and the dashboard's export endpoint without three separate
implementations drifting apart.

**1.6 Dataclasses at internal boundaries, dicts only at the state/dashboard edge.** Internal engine
state uses typed `models.py` dataclasses. Raw `dict`/JSON is used only when crossing the filesystem
(`/data`) or the dashboard HTTP boundary. This catches field-name typos at import/type-check time
instead of in a production dashboard tab silently showing nothing.

---

## 2. TESTING GATES — the real definition of "done"

The blueprint's Section 7 specifies each phase's test *content* (exact numeric inputs, exact expected
outputs). This section defines the *gate* those tests must clear, plus layers the blueprint doesn't
specify at all.

**2.1 Per-phase gate (entry / exit).** A phase exits only when:
- (a) every `def test_*` in the blueprint's Section 7 block for that phase passes locally **and** in CI;
- (b) `ruff` and `mypy --strict` are clean on every file the phase touched;
- (c) the phase's function signatures match their Section 6 contract exactly (`G-contract`);
- (d) mutation testing on the phase's pure functions (`indicators/`, `engines/risk.py`,
  `engines/exit.py`) achieves **≥80% killed mutants** — see 2.4;
- (e) every guard the phase is responsible for (Section 9 table below) has a dedicated regression
  test, tagged per 1.4, verified present by `G-guard-tags`;
- (f) every numeric threshold the phase introduces has a boundary test one unit on each side of the
  threshold (e.g. `rsi=39.99` vs `rsi=40.00` for a `<40` guard, `adx=14.99` vs `adx=15.00`) — this is
  what directly targets the documented session-boundary edge case (16:59:59 vs 17:00:00 UTC).

Entry to phase N requires phase N−1's (a)-(f) all green. **No phase skips a dependency**, and the
blueprint's "0 estimated LLM sessions" phases (1-6, deterministic code) must be fully green before
any LLM-driven phase (9-15) begins.

**2.2 Test pyramid, enforced by ratio, not just presence.** Unit tests (pure engine functions) ≥70%
of the suite; integration tests (loop + state round-trip) ≥20%; end-to-end tests (Phase 18-style)
≤10%. CI fails the build if the ratio inverts — this is what stops the suite from silently becoming
"a handful of slow end-to-end tests and nothing else," which would make every regression expensive to
localize.

**2.3 Golden-master / characterization fixtures for every guard.** Each of the 66 guards gets one
locked fixture that must never silently change behavior without a reviewed, labeled diff:
- L01 stale-candle: a repeated `candle_ts` across two calls must yield `None` on the second call.
- L02 flat-price: `high==low` for N consecutive candles → no entry, skip logged.
- L13 ensemble-context skip: an MR long is blocked when discovered-indicator consensus is
  `bearish`/`strong_bearish` — this is the direct regression test for the v06→v07 cliff.
- L21 novel-regime flatline: novelty `>3.0*median` → pair paused 60 cycles.
- L22 consecutive-loss flatline: N consecutive stop-loss exits → flatline + `flatline_log.jsonl` entry.
These fixtures may only be edited via a flagged, second-reviewer-approved change (Section 6.3) — they
are the regression net for lessons 1 and 7 specifically, and for every guard added after launch.

**2.4 Mutation testing — the real proof a threshold test actually tests the threshold.** Line
coverage proves a line executed; it does not prove a test would catch someone flipping `<` to `<=`
on a guard threshold. `mutmut` (or `cosmic-ray`) runs on `indicators/`, `engines/risk.py`, and
`engines/exit.py` every phase touching them. A surviving mutant that flips a comparison operator on
a guard threshold is a release blocker — this exact bug class (a boundary silently inverted) is
precisely what produced the v06→v07 cliff's mechanism, even though the specific mutant there was a
missing context check rather than an operator flip.

**2.5 Property-based smoke tests.** `hypothesis` runs one fuzz pass per CI build against:
`RiskEngine.size` (output always in `0..0.5`, regardless of input combination), `ExitEngine.evaluate`
(exactly one `Exit` reason ever returned, never zero-or-many), `IndicatorEngine.compute_all`
(monotonic price inputs produce bounded, non-NaN outputs). These catch the class of bug no hand-
written test case anticipates.

---

## 3. ERROR-HANDLING PHILOSOPHY

**3.1 Three failure classes, three behaviors — plus the asymmetry rule that matters most for a
trading system.**

- **Recoverable / transient** (network blip, API 429, yfinance timeout): retry with exponential
  backoff, cap 3 attempts, base delay 2s (matches blueprint's `RETRY_ATTEMPTS=3`,
  `RETRY_BASE_DELAY=2`), then return `None` / skip. Never blocks the 60-second loop. (Implements D3.)
- **Data invalid** (stale candle, flat price, NaN indicator): drop the sample, log a `skip` with an
  explicit reason string, do not trade this cycle. L01/L02 are the canonical handlers for this class.
- **Fatal / unrecoverable** (corrupt state file, schema drift on read): stop *that pair's* loop only
  — never the whole bot — emit a CRITICAL Discord alert (respecting the D9 budget), and leave the
  volume untouched for forensic recovery. **Do not auto-reseed default state in production.**
  Re-seeding silently on a read failure is exactly how state loss becomes invisible (lesson 3);
  reseeding is a `_seed_default_state()` call reserved for genuinely empty volumes on first boot.

**3.2 The asymmetry rule: fail-closed on money, fail-open on intelligence.** This resolves an
ambiguity neither the blueprint nor a flat three-tier list fully specifies on its own: what happens
when a *guard itself* fails to evaluate, as opposed to when a guard evaluates and fires.

```
Rule: Any exception in the ENTRY or EXIT decision path (risk sizing, stop/target math,
      guard evaluation for L01-L36) defaults to "no trade" / "no exit change" — fail-closed.
Rule: Any exception in an INTELLIGENCE-only layer (chart vision, GP ensemble scoring,
      crisis-learning lookup) defaults to a NEUTRAL signal, logs the failure, flips that
      engine's health flag, and the loop CONTINUES — fail-open. It never crashes the bot
      and never silently escalates to a hard block.
```

Concretely: if `ChartVision.get_context()` throws, the loop does not treat that as `"avoid"` (which
would be an unwarranted fail-closed on an intelligence signal) — it logs Tier 2, returns a neutral
context string, and the health registry shows `chart_vision: degraded`. But if `compute_atr()` throws
while calculating a stop price, the trade is **not** entered — Tier 3, that pair pauses, a human is
alerted.

**3.3 Exceptions never cross an engine boundary silently.** Every `except` either (a) returns the
engine's documented `Optional[...]` "no decision" value, or (b) logs `bot`, `pair`, `cycle`, and the
exception type before re-raising. A bare `except:` — or an `except Exception: pass` with no logging,
no counter increment, and no re-raise — is a hard lint failure (`G-except`), enforced project-wide,
no exceptions to the exception rule.

**3.4 Idempotent, atomic state writes.** All `/data` writes are either append-only (`.jsonl` files)
or atomic-rename (`strategies/{pair}.yaml`: write to temp, validate, then rename). A crashed
`reflect` process mid-write must never leave a half-written, unparseable strategy file on disk.

**3.5 Fail-closed on LLM consensus.** If the vote falls below the required threshold (2/3 at
`score>=65`, or 3/3 at `score>=75` — see D5/header note) or confidence is below 0.40, the parameter
is **not** changed and the existing YAML stands. A failed or inconclusive LLM call is always a *skip*,
never a *default-to-the-proposed-value*.

---

## 4. LOGGING & OBSERVABILITY

**4.1 Structured, pair-scoped logs.** Every log line carries `bot`, `pair`, `cycle`, `ts`. Keep the
existing `print(..., flush=True)` convention for humans reading Railway logs live, but route it
through one thin `log_event()` wrapper that also emits a structured JSON line with those four keys
plus guard-specific fields — so grep, the dashboard, and any future tooling can all parse the same
stream without prose-scraping.

**4.2 The four state-file observability signals — wire all four from day one, not as an afterthought.**
- **Heartbeat** (`write_heartbeat`, `loop.py:1774`): keys `ts, asset, cycle, consecutive_failures,
  last_price, status`. The heartbeat monitor alerts on **90 minutes of silence** — this is the
  top-level "is the bot even alive?" signal and the cheapest possible early-warning.
- **`flatline_log.jsonl`** (`_log_flatline`): every L21/L22 pause, append-logged with reason. The
  audit trail answering "why did this pair stop trading?" without reconstructing it from memory.
- **`skips.json`** (`log_skip`): every skipped entry (cooldown, session, RSI, confluence) with
  `rsi_at_skip`, `price_at_skip`, and the exact guard that blocked it. **Standing rule:** an empty
  skips tab on the dashboard means the guard never fired — that is a data-pipeline question to
  investigate, not evidence the bot is behaving perfectly.
- **`discovered/{pair}.json` + `cortex/indicator_exile.json`**: GP discovery and exile state, pushed
  to the dashboard on every ingest. An empty `discovered` tab means the weekly discovery subprocess
  never completed — a pipeline bug, not a UI bug (see 4.4).

**4.3 Metrics, not just logs.** Export per pair, per bot: cycle count, trade count, skip-count by
guard reason, flatline count, LLM-call latency and fallback-cascade count (DeepSeek → Gemini → Groq),
OOS pass-rate, and guard-fire-rate. A guard firing at more than 3× its own 30-day rolling average is
itself a signal worth a look — it catches both regime shifts and code regressions, and lets a human
decide which.

**4.4 Dashboards are a verification surface, not a display feature.** Standing rule, binding on every
on-call session: when a dashboard tab is empty for a given bot, verify the full pipeline (bot push →
`/ingest/{bot}` → SQLite → read API → frontend render) **before** concluding it's a frontend bug. This
single habit would have shortened several of the diagnostic threads from the original build's history.

**4.5 Alert budget, enforced (implements D9).** Max 1 Discord alert per `(bot, pair, guard)` per
15-minute window. Additional firings within the window increment a counter silently; at window close,
one summary line posts if the counter is nonzero. Channel routing is config (`alerts.yaml` maps
`{tier, guard-category} → webhook_env_var`), not code — Tier 3 (3.1) always routes to a dedicated
critical channel regardless of category, and routine cron/reflection output never lands in the
channel used for live human conversation with the bot.

**4.6 Secrets never appear in logs.** `DISCORD_*`, `*_API_KEY`, `INGEST_TOKEN` are redacted at the
logging layer itself, not by convention. A log line containing what looks like a 40+ character hex
or base64 token fails CI secret-scanning (`G-secret`) even in a test fixture.

---

## 5. DEPENDENCY MANAGEMENT

**5.1 One lockfile for the whole monorepo.** `pyproject.toml` + a committed lockfile
(`uv.lock`/`poetry.lock`). `hermes_core/` has one dependency set; `bots/` and `dashboard/` inherit it.
No per-bot `pip install` drift — this is D1's dependency-layer cousin.

**5.2 Pin, justify, and audit.** Every direct dependency is pinned to an exact version with a
one-line comment justifying it (`pandas-ta = "0.3.14b"  # RSI/ADX/BB, see Phase 3`). `pip-audit` runs
in CI (`G-audit`); a known-vulnerability dependency blocks merge. LLM-provider SDKs are the
highest-churn risk in this system — pin minor versions freely, but review any major bump in its own
dedicated PR, never bundled with a feature change.

**5.3 Adapters are the only place external risk enters.** `yfinance`, `ccxt` (crypto), any market-data
API are reachable only through `hermes_core/adapters/`. Swapping a data source touches one file, not
the engines that consume it — this directly enforces D1/D4. Crypto-specific sources (`ccxt`, BTC
on-chain feeds) are physically excluded from the forex/gold build path (D8).

**5.4 No dead adapters — enforced, not just avoided.** The blueprint's documented dead-adapter problem
(Fear & Greed Index and Bitcoin-news feeds surviving inside the gold bot's package) is prevented by
lint: any import of a crypto-only signal module from `hermes_core`'s forex/gold code path fails CI
(`G-crypto`). An adapter module not referenced by at least one bot's config-declared feature list is
flagged `orphaned-adapter` and fails the build — no adapter ships "just in case" again.

---

## 6. CI/CD

**6.1 Pipeline stages — all must be green to merge to `main`.**

```
G-lint        ruff clean: errors, unused imports, import order, plus the custom
              duplicate-logic guard (G-rep), crypto-import guard (G-crypto),
              and bare-except guard (G-except).
G-types       mypy --strict on hermes_core/.
G-guard-tags  tools/verify_guard_tags.py — every L01-L66 guard has >=1 docstring
              match in hermes_core/ (1.4). Zero matches for any guard = fail.
G-test        Full pytest suite; per-phase wrapper blocks phase N+1 tests from
              running until phase N's own test module is green (2.1).
G-mutate      mutmut/cosmic-ray >=80% killed mutants on indicators/, risk.py, exit.py.
G-contract    Generated check asserting every engine function signature matches
              its Section 6 contract exactly — a silent signature drift fails
              CI before it reaches production.
G-audit       pip-audit clean.
G-secret      No credential literal, and no token-shaped string, in any diff.
G-blueprint   tools/regenerate_blueprint_sections.py output (Section 3 guard list,
              Appendix C schemas) matches the committed blueprint byte-for-byte.
              Mismatch = "blueprint-drift-detected", build fails (implements D10).
```

**6.2 Branch & promotion model.** `main` is always-deployable, always-green. Feature branches per
phase (`phase/07-loop`, `phase/11-l2`) merge only once their own gate and every dependency phase's
gate are green. Railway deploys from GitHub auto-deploy — but per standing operational history,
auto-deploy has not always fired reliably after a repo reconnect, so a human verifies the **live**
endpoint after every deploy, not just the green CI check.

**6.3 "See it fire" is the production-proof standard, not a green compile.** A phase — and
especially Phase 18 — is not done because CI is green. It is done when a live Railway log line shows
the guard, entry, or exit actually firing in staging. This is a stronger and more falsifiable bar
than "the tests pass," and it is the standard applied at every phase exit from Phase 7 onward, not
just at final integration.

**6.4 Shadow deployment, dwell time scaled to blast radius.** Every change is deployed to a shadow
service (same config, paper mode, writes to `/data/{bot}/shadow/`, never touches `/data/{bot}/`
live) before promotion. Minimum dwell time before a human may promote:

```
engine/loop.py, engine/reflect.py (core decision logic)     -> 48 hours
config/pairs/*.yaml (live strategy parameters)               -> 72 hours
engine/genetic.py, gp_intelligence.py                        -> 7 days (needs a
                                                                  full discovery cycle)
dashboard/, cron/ (no trading-decision impact)                -> 24 hours
```

During the dwell window, an automated parity check compares the shadow's trade *decisions* against
the currently-live bot's decisions on the same price ticks. Parity breaking, or any Tier 3 error
appearing in shadow logs, discards the shadow candidate automatically and alerts — the live bot is
never touched by a failed shadow run.

**6.5 No force-merge of a guard regression.** A PR that removes or weakens any guard-layer test
(any of L01-L66, the RR guard, the ATR floor, or the circuit breaker) requires an explicit
"guard-downgrade" label and a second reviewer's sign-off. This is the rule that would have made the
L41 score-gate weakening (65 → 55) a visible, justified, reviewable decision instead of a silent
regression discovered months later.

**6.6 Rollback is a config change, not a redeploy.** Because bots are config instances over state on
a separate volume (D1 + D2), a bad strategy parameter rolls back by reverting one `strategies/
{pair}.yaml` value on the volume — no code deploy required. Every reflection mutation is recorded in
`hypotheses.jsonl` with the exact `old -> new` value, making every live change reversible in a single
write.

---

## 7. DOCUMENTATION STANDARDS

**7.1 Code docs.** Every engine function's docstring states: what it accepts, what it returns, which
guard(s) it implements (tagged per 1.4), and which Section 6 contract line it satisfies. Any new
formula cites its source (a paper, or the specific blueprint section it was derived from).

**7.2 The blueprint stays in sync two ways at once — manual discipline *and* an automated backstop.**
When a phase's contract changes, the blueprint's Section 7 block for that phase is updated in the
*same* PR — a merged code change with a stale blueprint phase fails review. This is the manual half.
The automated half (implements D10, `G-blueprint` in 6.1) regenerates Section 3's guard list and
Appendix C's schemas directly from code and fails CI if they disagree with what's committed. The
manual rule catches drift at review time; the automated rule catches whatever the manual rule misses.
Together they are what prevents the blueprint from ever again needing three separate completeness
passes before it's trustworthy.

**7.3 Architecture Decision Records (ADRs).** Any change to an engine contract (1.2), any new guard
(L67+), or any threshold change on an existing guard gets a one-paragraph ADR: what changed, why, what
alternative was rejected. ADRs live alongside the CHANGELOG and are cross-linked from the relevant
blueprint section.

**7.4 Runbooks are documentation, not tribal knowledge.** The empty-dashboard-tab triage flow (4.4),
the hotfix protocol (8.6), and the "trace a guard in 60 seconds" flow (Section 11 below) are written
down once, in this document, and referenced — never re-explained from scratch in a Discord thread.

---

## 8. CHANGE-MANAGEMENT PROCESS

**8.1 One variable at a time — enforced at runtime, not just by convention (implements D7).**
`ReflectEngine.reflect` emits a `Proposal{param, old, new, confidence}` for **exactly one** parameter.
The blueprint's `one_variable_only: true` goal field is the runtime enforcement; CI additionally
rejects any reflection test fixture that attempts to mutate two params in one proposal. A bundled
change is the v06→v07 cliff in miniature, and this rule exists specifically so that failure mode
cannot recur.

**8.2 The reflection gating pipeline — with the corrected score gate.**

```
Loop fires every 5 closed trades per pair (user override 2026-07-16 — restored to the
blueprint's base cadence of 5; the v4 roadmap's temporary flat-10 departure is retired)
  -> ReflectEngine L1 (rule-based, one variable)
  -> score_trades() computes 0-100 composite score
  -> SCORE GATE (corrected, see header + D5):
       score <  65  -> L1 result applied directly if it passes safety floors; L2 not called
       score >= 65  -> L2 required, standard 2/3 model consensus
       score >= 75  -> L2 required, UNANIMOUS 3/3 model consensus (new protected tier)
  -> BacktestEngine 7-phase validation, OOS (Phase 0) FIRST — oos_delta > -0.2 gate
     must pass before historical metrics are even trusted (implements D6)
  -> only then: write strategies/{pair}.yaml, bump version, append hypotheses.jsonl
```

A change failing OOS is rejected before historical performance is computed at all. The 2/3-or-3/3
vote means no single model ever acts alone on a live parameter (D5), and the 75-tier means a pair
performing as well as EUR/USD (score 70 in the original system, blueprint Section 10) requires every
model to agree before its winning strategy can be touched. [v4 correction: an earlier draft cited
"GBP/JPY (score 77)" here — the blueprint has no such figure for GBP/JPY (it shows only 1 closed
trade for that pair, not enough for a stable score); GBP/USD's recorded score is ~51. EUR/USD is the
only pair with a documented score near the 75-tier, so it is the sole example used here.]

**8.3 Weekly GP discovery is gated identically.** `Cron (Sunday) -> GeneticEngine.discover() ->
BacktestEngine (same OOS-first 7-phase gate) -> GPIntelligence registry`, plus the novelty threshold
(`>3.0*median` for a new indicator; cosine distance `>0.5` for a novel crisis signature). New
indicators land in `discovered/{pair}.json`, visible on the dashboard, in shadow mode — they cannot
influence live entries until they've earned promotion (L60).

**8.4 Every live decision is reconstructable after the fact.** `hypotheses.jsonl` (param `old->new` +
reasoning), `skips.json` (why no trade), `flatline_log.jsonl` (why paused), `cortex/
indicator_exile.json` (why an indicator was banned). Together, these four files answer "why did the
bot do that?" without anyone needing to remember — this is the direct fix for lesson 1's root cause:
a mechanical rule changed a winning strategy and nobody could immediately reconstruct why.

**8.5 Self-audit is on-demand, report-only — not an 08:00 UTC timer, and not an auto-fixer.**
`self_audit.py` runs on demand or via external cron and only reports; it never mutates live state.
Scheduling it is an operations task, not a hardcoded in-code timer — this corrects an assumption in
early blueprint drafts that self-audit ran on an internal daily clock.

**8.6 Hotfix protocol.** A production incident follows, in order: (1) confirm via heartbeat and
dashboard — never via assumption; (2) if it's a bad parameter, revert the single YAML value using the
`old->new` recorded in `hypotheses.jsonl` — no code deploy needed; (3) if it's a code bug, branch
`hotfix/...`, minimal diff, full CI gate green, deploy, **then** write the post-mortem ADR. No "fix
forward" without a permanent regression test (2.3) for the exact failure — the fix and its test are
the same PR, never sequential.

---

## 9. PHASE-BY-PHASE DISCIPLINE SEQUENCE

Each row layers this document's discipline onto one blueprint Section 7 phase. Exit requires the
blueprint's own test block green **plus** the discipline gate here. "Guards" cites the specific
guard layers or numeric constants the phase must wire or respect, with source-line citations where
verified against Appendix A of the blueprint.

| Ph | Blueprint anchor | Discipline focus | Guards / constants touched | Entry criteria | Exit criteria |
|----|---|---|---|---|---|
| 1 | §7 Phase 1 — scaffold+config | Coding std 1.3 (D1), config schema | — | Repo tree matches §6; `config/schema.py` stub | 4 blueprint tests green; `validate_strategy_params` rejects out-of-range/unknown-session; `G-rep` clean; gold `XAU/USD` defaults to `rsi_momentum` |
| 2 | §7 Phase 2 — price adapter | Error-handling 3.1/3.2 (D3), guard L01 | **L01** stale-candle (`loop.py:1612` orig. location — must live in adapter, not loop), retry cap 3 / base 2s (`RETRY_ATTEMPTS`, `RETRY_BASE_DELAY`) | Phase 1 green | 4 blueprint tests green; `G-except` clean; timeout returns `None`, never raises |
| 3 | §7 Phase 3 — indicators | Pure-fn std 1.5, mutation 2.4 | — | Phase 2 green | RSI/ATR/ADX/BB tests green; `mutmut` ≥80%; zero I/O in any indicator function |
| 4 | §7 Phase 4 — entry engine | Guard L13, boundary tests 2.1(f) | **L13** ensemble-context skip (`loop.py:2018-2021`), confluence ≥2 (`loop.py:2349-2352`), re-entry cooldown 30 cycles (`loop.py:2033-2037`), session filters L04 | Phase 3 green | 6 tests green incl. L13 regression fixture (v06→v07 direct test); boundary test at exact session-close second |
| 5 | §7 Phase 5 — exit engine | Mutation 2.4, guard L25-L27 | Stop/target/trailing/time-exit/breakeven/**partial-close at full target** (`loop.py:1627` — verified NOT at target/2) | Phase 3 green | 5 tests green; partial-close trigger verified at full profit_target_pct, not half; `hypothesis` proves exactly one exit reason ever returned |
| 6 | §7 Phase 6 — risk engine | Property testing 2.5 | RR guard (reject R:R<1.0), ATR floor, regime-based sizing (BULL=full, NEUTRAL≈60%, portfolio-correlation reduction) | Phase 3 green | 5 tests green; `hypothesis`: size always in `0..0.5`; RR guard blocks R:R<1.0 |
| 7 | §7 Phase 7 — loop integration | Error-handling 3.2, observability 4.1-4.2 | ALL of L01-L39, circuit breaker (`consecutive_failures>=5` → sleep 300s) | Phases 2-6 green | 24-cycle dry run in paper mode, zero unhandled exceptions; heartbeat + skips written every cycle; health registry live (4.2, 4.4) |
| 8 | §7 Phase 8 — chart vision | Fail-open rule 3.2 | Hard block on `"avoid"`/`"downtrend"` (L14), soft filter on `"sell"`+quality<5 (L16) | Phase 7 green (needs loop context) | 4 tests green; mocked API failure returns neutral context, loop continues (fail-open verified, not fail-closed) |
| 9 | §7 Phase 9 — reflection L1 | Change-mgmt 8.1 (D7) | One-variable-only enforced at runtime, not just tested | Phases 5, 7 green | 5 tests green; reflection fires at 5 trades (user override 2026-07-16 — blueprint base cadence restored; v4 flat-10 retired); param-range floors (L40) enforced; CI rejects any 2-variable fixture |
| 10 | §7 Phase 10 — backtest pipeline | OOS-first 2.1(d)/D6 | **Phase 0 OOS FIRST** (`oos_delta>-0.2`), crisis stress (L54), redundancy `|r|>0.8` reject (L57) | Phases 3, 4 green | All 7 backtest sub-phases individually gate-tested; OOS-random-rejection rate ≥95% empirically confirmed |
| 11 | §7 Phase 11 — LLM consensus L2 | Fail-closed 3.5 (D5), **corrected score gate** | 2/3 at `score>=65`; **3/3 unanimous at `score>=75`** (corrected — see header); confidence gate ≥0.40 | Phases 9, 10 green | 3-model cascade tested (DeepSeek→Gemini→Groq); score-gate unit tests cover 55/65/75 boundaries explicitly; <required-votes → no change applied |
| 12 | §7 Phase 12 — crisis learning | Observability 4.2 (flatline log) | **L21** novel-regime flatline (novelty>3.0×median, 60-cycle pause), cosine distance >0.5 for novel signature | Phase 7 green | 4 tests green; 9-dim signature verified against a known historical window; flatline appends to `flatline_log.jsonl` |
| 13 | §7 Phase 13 — genetic programming | Testing 2.2, dependency 5.4 (D8) | Novelty gate `>3.0*median`; **no crypto-specific signals in forex/gold discovery** | Phase 10 green | 4 tests green; OOS corr≥0.15 discovery confirmed on real data; `G-crypto` clean on this module |
| 14 | §7 Phase 14 — GP intelligence | Contract 1.2, observability 4.2 | `gp_entry_score` default **0.0** (corrected from -0.3 chicken-and-egg deadlock — new indicators must be able to earn their first entries) | Phase 13 green | 4 tests green; ensemble label test; degradation-cull test separates genuine WR decay from regime mismatch; registry written to `discovered/{pair}.json` |
| 15 | §7 Phase 15 — cortex + policy | Documentation 7.3 (ADR), observability 4.2 | Exile log persisted; policy suppressions applied at the entry gate in both directions (MR-suppresses-GP and GP-suppresses-MR) | Phases 12, 14 green | 4 tests green; `indicator_exile.json` + `policy.json` on `/data`, pushed to dashboard, survive restart |
| 16 | §7 Phase 16 — dashboard API | Observability 4.4, dependency 5.1 | **SQLite composite primary key `(bot, id)`** — the structural fix for the bot-identity collision bug, not just a test that catches it | Phases 7, 15 green | 5 tests green; ingest round-trip verified; PK-collision test (two bots, same `id`, no overwrite); empty-tab returns explicit "no data," never a bare error |
| 17 | §7 Phase 17 — dashboard frontend | Observability 4.4 | Empty-data state renders "pipeline gap for {bot}," never a blank panel | Phase 16 green | 4 tests green; every tab renders from real API data; no hard-coded bot name anywhere in frontend code |
| 18 | §7 Phase 18 — full integration | All gates + "see it fire" (6.3) | Every guard L01-L66 exercised at least once end-to-end | Phases 1-17 all green | Both bots run 24h simultaneously, zero unhandled exceptions; ≥3 trades complete entry→exit; reflection fires correctly at 10 closes; **a live Railway log line is captured for at least one guard firing per engine** — compile-green alone does not close this phase |

---

## 10. POST-LAUNCH MAINTENANCE CADENCE

| Cadence | Action | Discipline tied | Verification |
|---|---|---|---|
| **Every cycle (60s)** | Loop runs; heartbeat + skips + flatline written | Obs 4.1-4.2, EH 3.1 | Heartbeat monitor alerts on 90-min silence |
| **Per 5 trades** | L1 + L2 reflection (gated, corrected score thresholds) | Change 8.1-8.2, D5/D7 | `hypotheses.jsonl` shows single-param `old->new`; score-gate tier applied correctly |
| **Daily** | `self_audit.py`, on-demand, report-only | Change 8.5 | Report diffed against contract; zero auto-changes made |
| **Sunday (cron)** | GP discovery → OOS-first backtest → registry | Change 8.3, D6, D8 | New indicators visible on dashboard in shadow before any live influence |
| **Weekly** | Read the week's CHANGELOG/ADRs; review reflection outcome ratio (applied/rejected/reverted) per pair; `pip-audit` + LLM-SDK pin review | Doc 7.2-7.3, Dep 5.2 | Any pair reverting every week flagged for human review; clean audit or major-bump ADR filed |
| **Per incident** | Hotfix protocol (8.6): revert YAML or minimal branch + regression test | CM 8.6, Testing 2.3 | Regression test merged in the same PR as the fix, not after |
| **Per architecture change** | ADR filed + blueprint Section 7 updated in the same PR | Doc 7.2-7.3 | `G-contract` + `G-blueprint` both green |
| **Monthly** | Mutation-suite re-run (confirm ≥80% still holds as code grows); **statistical-sufficiency check**: any pair still below 20 closed trades gets flagged and is excluded from regime-breakdown analysis rather than forced through it on a thin sample | Testing 2.4, blueprint Problem P10 | No guard-threshold mutant survives; thin-sample pairs explicitly listed, not silently included |
| **Quarterly** | Full dependency + CVE audit; `tools/regenerate_blueprint_sections.py` run and diffed by a human once, even though CI already does this on every merge, as a sanity check on the generator itself | Dep 5.2, Doc 7.2 (D10) | Clean audit; generator output matches committed blueprint exactly |

---

## 11. QUICK-REFERENCE OPERATIONAL CARDS

**11.1 "A guard just fired unexpectedly — trace it in 60 seconds."**
```
1. Grep the structured log for "guard":"L##" → gives bot, pair, cycle.
2. Grep hermes_core/ for `[GUARD L##]` → gives the exact function (1.4).
3. Check blueprint Section 3 for that guard's documented trigger condition.
4. Compare the logged numeric fields against the documented trigger.
5. Matches → working as designed; check what changed in market conditions or config.
6. Doesn't match → Tier 3 incident; use the hotfix protocol (8.6) and post-mortem ADR (7.3).
```

**11.2 "I want to add a new guard, L67."**
```
1. Write the function with a [GUARD L67] docstring tag (1.4) + blueprint line citation.
2. Add a unit test (Gate 1) AND a boundary test one unit either side of its threshold (2.1f).
3. Add structured log emission (4.1) and, if it can fire repeatedly, respect the D9 alert budget.
4. File an ADR (7.3) — this is what tools/regenerate_blueprint_sections.py picks up (D10)
   to add L67 to the blueprint's Section 3 automatically on next merge.
5. PR requires a second reviewer if it touches loop.py or reflect.py (6.5's spirit, applied
   to new guards as well as threshold changes on existing ones).
6. Shadow dwell 48h minimum (6.4) before it can fire on the live bot.
```

**11.3 "I want to change a threshold on an existing guard."**
```
1. One PR, one threshold, nothing else bundled in (8.1/D7).
2. ADR states the old value, the new value, and the one-sentence reason (7.3) — this is the
   rule that would have caught the L41 score-gate's silent 65->55 drift before it shipped.
3. If the guard protects a currently high-scoring/protected pair (score>=75 per Section 8.2),
   it requires the full 3/3 model consensus AND a second human reviewer, matching the same
   protection level the bot itself applies to its own strategy changes.
4. Shadow dwell per 6.4, scaled to which file changed.
```

---

## 12. WHAT THIS DOCUMENT DOES NOT COVER (by design)

- The 18 phases' exact test bodies and numeric inputs — blueprint Section 7.
- GP engine math, the 9-dimension crisis vector, the allocation matrix — blueprint Section 11 and
  Appendices B/F.
- Verbatim LLM prompt text — blueprint Appendix E.
- Per-pair configuration values (stop %, target %, session windows) — blueprint Section 8.

If anything in this document conflicts with the blueprint on a technical value, **the blueprint
wins**, except for the score-gate correction stated once in the header and carried consistently
through D5, Section 8.2, Session 11, and Section 9's phase table — that correction is deliberate and
should be back-ported into the blueprint itself at the next revision so this exception can eventually
be retired.

---
*Companion to HERMES_MASTER_BLUEPRINT_v4.md. v4 — combines line-level grounding, mutation/
property testing, frozen engine contracts, and the mechanical session-template execution layer with
the alert-budget discipline (D9), self-verifying blueprint regeneration (D10), risk-tiered shadow
dwell times, and the monthly statistical-sufficiency check. The corrected L2 score gate (65 standard,
75 unanimous) is binding throughout and should be reconciled into the blueprint's own Section 7/Section
3 on next revision.*

---

# APPENDIX A: SESSION-BY-SESSION LLM BUILD SEQUENCE

**How to use this appendix.** Each session is a self-contained work order for one focused LLM coding
session. Execute strictly in order — do not start a session until the previous session's EXIT GATE is
fully green. Every session uses the identical 9-field template so it can be parsed mechanically. Fill
nothing in from memory or assumption — read the cited blueprint section first, every time.

**Global rules, apply to every session (not repeated per-session below):**
- **G-A.** Source of truth = `HERMES_MASTER_BLUEPRINT_v4.md`. Read the cited section before coding.
- **G-B.** One shared engine; bots are config instances (D1). Never branch on bot name inside
  `hermes_core/`.
- **G-C.** State writes go to `/data` only, append-only or atomic-rename (D2, D3).
- **G-D.** Adapters fail soft: return `None`, never raise, never infinite-loop (D3).
- **G-E.** Guards live in the engine, not the orchestrator (D4).
- **G-F.** Type-hint every public function; match its Section 6 contract exactly (1.2).
- **G-G.** Every guard tagged `[GUARD L##]` in its docstring (1.4); `ruff`/`mypy` clean; all cited
  tests green before a session is declared done.
- **G-H.** If a value is not in the blueprint or the source code, **stop and ask** — never invent a
  number.
- **G-I.** The L2 score gate is `65` standard / `75` unanimous-3/3, per this document's header — not
  the blueprint's documented `55` (that value is the regression the rebuild is fixing).

**Session template fields:** 1. `SESSION` — id + title. 2. `BLUEPRINT` — exact section/phase to read
first. 3. `BUILD` — files + functions to create. 4. `CONTRACT` — the Section 6 signature it must
satisfy. 5. `GUARDS` — guard layers to wire or respect. 6. `TESTS` — blueprint test block + extra gate
tests from this document. 7. `ENTRY GATE` — what must already be green. 8. `EXIT GATE` — the
done-definition, combining blueprint tests + Section 9's discipline column. 9. `DO-NOT` — the specific
footgun this session exists to avoid.

---

### SESSION 0 — Repo scaffold & tooling (pre-Phase)
1. **SESSION:** S0 · Monorepo skeleton + CI wiring.
2. **BLUEPRINT:** Section 6 (folder structure, env vars).
3. **BUILD:** `hermes-trading/` tree: `hermes_core/{config,adapters,indicators,engines,state}`,
   `bots/{forex,gold,crypto}/config.yaml`, `dashboard/`, `cron/`, `tests/`, `data/`, `pyproject.toml`,
   `Dockerfile`, `railway.json`. Add `ruff`, `mypy`, `pytest`, `pip-audit`, `mutmut`, `hypothesis` as
   dev-deps. Add `tools/verify_guard_tags.py` and `tools/regenerate_blueprint_sections.py` as stubs
   (they grow real logic starting Session 4 and Session 16 respectively).
4. **CONTRACT:** none — scaffold only.
5. **GUARDS:** none yet, but create `tests/test_no_duplication.py` that greps `hermes_core/` for
   `if bot ==` and fails if found (D1, enforced from session zero).
6. **TESTS:** `pytest` collects 0 real tests but the CI harness runs end-to-end; `ruff .` clean;
   `pip-audit` clean; `G-rep` passes trivially on an empty tree.
7. **ENTRY GATE:** none.
8. **EXIT GATE:** folder tree matches Section 6; `G-lint`/`G-test`/`G-audit` pipeline stages exist and
   pass on the empty repo.
9. **DO-NOT:** do not create two loop files or any per-bot engine copy — even a stub (lesson 2).

---

### SESSION 1 — Config system (Phase 1)
1. **SESSION:** S1 · Config schema + loader + validator.
2. **BLUEPRINT:** Section 7 Phase 1; Section 6 config schema; config loaders `loop.py:1700-1741`.
3. **BUILD:** `hermes_core/config/{schema.py, loader.py, validator.py}`; `bots/forex/config.yaml`
   (pairs `EUR/USD, GBP/USD, GBP/JPY, AUD/USD`); `bots/gold/config.yaml` (`XAU/USD, XAG/USD`,
   `strategy_type: rsi_momentum`); per-pair `state/strategies/{PAIR}.yaml`. Functions:
   `load_config(bot)`, `load_strategy_for_pair(pair)`, `validate_strategy_params(strategy)`,
   `_seed_default_state()`.
4. **CONTRACT:** config dict shape from Section 6 (`bot/goal/global/pairs`); validator enforces
   `STRATEGY_PARAM_RANGES` + `strategy_type ∈ {mean_reversion, rsi_momentum}` + session in the
   allowed set.
5. **GUARDS:** none yet — but `validate_strategy_params` is the first line of defense: reject
   `stop_loss_pct<0.5`, reject an unknown `session_filter`.
6. **TESTS:** blueprint Phase 1 block (`test_load_valid_forex_config`,
   `test_load_stop_loss_below_min`, `test_load_unknown_session_filter`,
   `test_load_gold_config_momentum`) — all green.
7. **ENTRY GATE:** S0 green.
8. **EXIT GATE:** Phase 1 tests green; `XAU/USD` `strategy_type == "rsi_momentum"` (not
   `mean_reversion`); pairs list exact; `G-contract` on `load_config`.
9. **DO-NOT:** do not default gold to `mean_reversion` (lesson 4); do not accept an out-of-range
   param silently — that silent-acceptance is the root mechanism behind lesson 1.

---

### SESSION 2 — Price adapter (Phase 2)
1. **SESSION:** S2 · yfinance adapter with the stale-candle guard living in the right place.
2. **BLUEPRINT:** Section 7 Phase 2; stale-candle state at `loop.py:1612` (original, wrong location);
   `fetch_with_retry` at `loop.py:1727`.
3. **BUILD:** `hermes_core/adapters/price.py`: `async fetch(pair, force=False) -> Optional[dict]`,
   `async seed_history(pair) -> list`.
4. **CONTRACT:** `PriceAdapter.fetch(pair) -> Candle{price, high, low, ts}`;
   `seed_history(pair) -> 300 candles`.
5. **GUARDS:** **L01 stale-candle** — moved into the adapter itself (G-E/D4): same `candle_ts` within
   60s → return `None`. Retry cap 3, base delay 2s.
6. **TESTS:** blueprint Phase 2 block (`test_fetch_keys_non_none`, `test_stale_candle_guard`,
   `test_seed_history_count`, `test_yfinance_timeout_returns_none`) plus a golden-master fixture: a
   repeated `candle_ts` always yields `None` on the second call, permanently.
7. **ENTRY GATE:** S1 green.
8. **EXIT GATE:** timeout returns `None` without raising; stale guard returns `None`; all keys
   present; `G-except` clean; `[GUARD L01]` tag present and `G-guard-tags` finds it.
9. **DO-NOT:** do not let this guard live only in the loop — it belongs in the adapter, at the source,
   or the exact original bug (198 fake weekend trades) can recur the moment anything calls the
   adapter directly, bypassing the loop's own check.

---

### SESSION 3 — Indicator engine (Phase 3)
1. **SESSION:** S3 · Pure indicator math.
2. **BLUEPRINT:** Section 7 Phase 3; `compute_rsi` `loop.py:1792`, `compute_atr` `loop.py:1813`,
   `compute_roc` `loop.py:1803`, `_get_session` `loop.py:1778`.
3. **BUILD:** `hermes_core/indicators/__init__.py`:
   `compute_all(prices) -> {rsi, atr, adx, bb, roc, regime, fast_regime, divergence}` plus each pure
   sub-function.
4. **CONTRACT:** `IndicatorEngine.compute_all(prices) -> {...}` from Section 6.
5. **GUARDS:** none directly — but every downstream guard depends on these outputs being correct and
   bounded.
6. **TESTS:** blueprint Phase 3 block (RSI(14) within 0.1, ATR(14)==0 on flat prices, ADX bounded
   0-100, BB `lower<middle<upper`) plus `mutmut` ≥80% killed on this module.
7. **ENTRY GATE:** S2 green.
8. **EXIT GATE:** all Phase 3 tests green; every function verified pure (no I/O, no network); mutation
   score ≥80%.
9. **DO-NOT:** do not read a file or call the network from inside an indicator function — that breaks
   the determinism the live loop, the backtester, and the dashboard export all rely on sharing one
   implementation.

---

### SESSION 4 — Entry engine (Phase 4)
1. **SESSION:** S4 · Mean-reversion + RSI-momentum entry, with ensemble-context awareness.
2. **BLUEPRINT:** Section 7 Phase 4; confluence check `loop.py:2349-2352`; re-entry cooldown
   `loop.py:2033-2037`; L13 ensemble skip `loop.py:2018-2021`.
3. **BUILD:** `hermes_core/engines/entry.py`:
   `evaluate(pair, indicators, strategy, context) -> Optional[Signal{type, quality, size}]`. Also:
   `tools/verify_guard_tags.py` gains its real logic here (first session with guard-tagged functions
   to check).
4. **CONTRACT:** `EntryEngine.evaluate(...) -> Optional[Signal]` from Section 6.
5. **GUARDS:** **L13** ensemble-context skip (an MR long is blocked when discovered-indicator
   consensus is `bearish`/`strong_bearish`); re-entry cooldown 30 cycles; confluence ≥2 oversold
   pairs required for the RSI-momentum path; session filter (L04).
6. **TESTS:** blueprint Phase 4 block, plus an explicit L13 regression fixture that reproduces the
   v06→v07 cliff's exact precondition (strong recent WR, regime shift to bearish ensemble consensus)
   and asserts the entry is blocked.
7. **ENTRY GATE:** S3 green.
8. **EXIT GATE:** Phase 4 tests green; L13 fixture blocks a bearish-context MR long; confluence<2
   yields no RSI-momentum signal; `G-guard-tags` finds L13's tag and confirms it now has ≥1 match.
9. **DO-NOT:** never let a mechanical MR-long fire while the discovered-indicator ensemble shows
   bearish consensus — that combination, unguarded, is precisely the v06→v07 cliff.

---

### SESSION 5 — Exit engine (Phase 5)
1. **SESSION:** S5 · Stop-loss / target / trailing / time-exit / breakeven / partial-close.
2. **BLUEPRINT:** Section 7 Phase 5; partial-close trigger `loop.py:1627` — **verified at full
   `profit_target_pct`, not at half**.
3. **BUILD:** `hermes_core/engines/exit.py`: `evaluate(trade, indicators) -> Optional[Exit{reason,
   price}]`.
4. **CONTRACT:** `ExitEngine.evaluate(trade, indicators) -> Optional[Exit]` from Section 6.
5. **GUARDS:** stop_loss, profit_target, trailing (ATR-based), time_exit, breakeven, and
   **partial-close fires when `unrealised_pct >= profit_target_pct`** — full target, not half — at a
   50% position fraction.
6. **TESTS:** blueprint Phase 5 block (`test_stop_loss`, `test_profit_target`, `test_time_exit`,
   `test_breakeven`, `test_partial_close`) with the partial-close trigger explicitly asserted at full
   target, plus a `hypothesis` property test proving exactly one `Exit` reason is ever returned, never
   zero or multiple.
7. **ENTRY GATE:** S3 green.
8. **EXIT GATE:** all Phase 5 tests green; partial-close trigger verified at full target;
   `mutmut` ≥80% on exit thresholds.
9. **DO-NOT:** do not trigger partial-close at `target/2` — the correct, source-verified trigger is
   the full target. Do not ever return two exit reasons from one evaluation.

---

### SESSION 6 — Risk engine (Phase 6)
1. **SESSION:** S6 · Position sizing, RR guard, ATR floor.
2. **BLUEPRINT:** Section 7 Phase 6; sizing/RR-guard/ATR-floor logic in the loop's risk section.
3. **BUILD:** `hermes_core/engines/risk.py`: `size(strategy, regime, vol, gp_state) -> float`,
   `check_rr_guard(stop_pct, target_pct) -> bool`, `compute_atr_stop(entry, atr, mult, floor) -> float`.
4. **CONTRACT:** `RiskEngine.size(...) -> float (0..0.5)` from Section 6.
5. **GUARDS:** RR guard (reject R:R<1.0), ATR floor, regime multipliers (BULL≈1.0, NEUTRAL≈0.6, plus
   further reduction per additional open bullish position — portfolio correlation sizing).
6. **TESTS:** blueprint Phase 6 block (`test_size_bull`, `test_size_neutral`,
   `test_size_neutral_two_open`, `test_rr_guard_blocks`, `test_atr_stop_floor`) plus a `hypothesis`
   property test that `size` is always in `0..0.5` regardless of input combination.
7. **ENTRY GATE:** S3 green.
8. **EXIT GATE:** all Phase 6 tests green; RR guard rejects any R:R<1.0 proposal; ATR stop never
   tighter than the configured floor.
9. **DO-NOT:** do not let `size` exceed 0.5 under any regime/volatility combination; do not skip the
   ATR floor check "because the regime looks safe."

---

### SESSION 7 — Trade loop integration (Phase 7)
1. **SESSION:** S7 · 60-second config-driven orchestrator.
2. **BLUEPRINT:** Section 7 Phase 7; `write_heartbeat` at `loop.py:1774`; circuit breaker via
   `MAX_CONSECUTIVE_FAILURES=5`; full data-flow diagram in Section 6.
3. **BUILD:** `hermes_core/engines/loop.py`: `run_cycle()`,
   `write_heartbeat(asset, cycle, consecutive_failures, last_price)`. Fully generic and config-driven
   (D1) — no bot-specific branches anywhere in this file.
4. **CONTRACT:** wires `PriceAdapter -> Indicators -> Entry ⇄ (GP, Chart, Crisis, Cortex, Policy) ->
   Risk -> Exit`, writes state, and pushes to the dashboard via `POST /ingest/{bot}`.
5. **GUARDS:** every guard from L01-L39 plus RR, ATR floor, and the circuit breaker (consecutive
   failures → pause). Heartbeat written every single cycle without exception.
6. **TESTS:** integration test: 50+ simulated cycles with injected candles (including stale, flat, and
   timeout-inducing ones); heartbeat keys present every cycle; skips logged with correct reasons; zero
   unhandled exceptions across the entire run.
7. **ENTRY GATE:** S2-S6 all green.
8. **EXIT GATE:** loop survives every injected failure mode without crashing; heartbeat and skips
   written correctly; `G-except` clean across the whole file; health registry (4.2) live and reporting.
9. **DO-NOT:** do not branch on bot name anywhere in this file (D1); do not swallow any exception
   without logging `bot`, `pair`, and `cycle` first (3.3).

---

### SESSION 8 — Chart vision engine (Phase 8)
1. **SESSION:** S8 · Chart-context generation with a fail-open safety net.
2. **BLUEPRINT:** Section 7 Phase 8; chart context caching and hard-block logic.
3. **BUILD:** `hermes_core/engines/chart_vision.py`: `get_context(pair) -> str`,
   `hard_block(context) -> bool`.
4. **CONTRACT:** `ChartVision.get_context(pair) -> str` from Section 6.
5. **GUARDS:** L14 hard block on `"avoid"`/`"downtrend"` in the returned context; L16 soft filter on
   `"sell"` combined with quality<5.
6. **TESTS:** blueprint Phase 8 block plus: mocked API failure returns a neutral context string and
   the loop continues uninterrupted (3.2 fail-open verified explicitly, not assumed); context is
   cached for 60 minutes to avoid redundant API calls.
7. **ENTRY GATE:** S4 green (entry engine must exist to consume this context).
8. **EXIT GATE:** hard-block returns a correct boolean on the documented trigger phrases; an LLM
   timeout produces a fallback neutral context, never a crash.
9. **DO-NOT:** do not let a chart-vision failure escalate to a hard block by default — a failure here
   is fail-open (neutral), never fail-closed (avoid), per the asymmetry rule in 3.2.

---

### SESSION 9 — Reflection engine L1 (Phase 9)
1. **SESSION:** S9 · Rule-based, one-variable-only reflection.
2. **BLUEPRINT:** Section 7 Phase 9; `one_variable_only` in `goal.yaml`.
3. **BUILD:** `hermes_core/engines/reflect.py` (L1 portion): `layer1_rule_based(pair, trades, goal,
   strategy) -> Proposal`.
4. **CONTRACT:** `ReflectEngine.reflect(...) -> Proposal{param, old, new, confidence}` from Section 6.
5. **GUARDS:** L40 param-range hard gate; L42 one-variable-only, enforced at runtime, not merely by
   convention.
6. **TESTS:** blueprint Phase 9 block, plus a CI-level fixture asserting any attempt to construct a
   two-parameter proposal is rejected before it ever reaches the backtest validator.
7. **ENTRY GATE:** S5, S7 green.
8. **EXIT GATE:** L1 emits exactly one changed parameter per proposal; reflection cadence fires at 5
   closed trades per pair (user override 2026-07-16 — blueprint base cadence of 5 restored); safety floors (L40) enforced.
9. **DO-NOT:** do not let reflection fire on a 5-trade sample — that sample size is not statistically
   meaningful and is part of what produced the original v06→v07 cliff's fragility.

---

### SESSION 10 — Backtest validation pipeline (Phase 10)
1. **SESSION:** S10 · The 7-phase OOS-first validation gate.
2. **BLUEPRINT:** Section 7 Phase 10; OOS gate `oos_delta > -0.2`; crisis stress test (L54);
   redundancy check `|r|>0.8` (L57).
3. **BUILD:** `hermes_core/engines/backtest.py`: `validate(proposal, history) -> Verdict`.
4. **CONTRACT:** `BacktestEngine.validate(proposal, history) -> Verdict{approved, reason}` from
   Section 6.
5. **GUARDS:** **Phase 0 OOS runs first**, before any historical metric is computed or trusted (D6);
   crisis stress across the documented historical windows (L54); redundancy rejection for any proposed
   change too similar to an already-rejected one.
6. **TESTS:** blueprint Phase 10 block, plus an OOS-randomness confirmation test: feeding randomly
   generated proposals should be rejected at the OOS gate ≥95% of the time.
7. **ENTRY GATE:** S3, S4 green.
8. **EXIT GATE:** all 7 internal backtest sub-phases individually gate-tested; OOS runs before
   historical, verified by test ordering, not just by code reading order.
9. **DO-NOT:** do not run OOS validation last "for efficiency" — this exact reordering is what let bad
   changes through in the original system (lesson 8).

---

### SESSION 11 — LLM consensus L2 (Phase 11)
1. **SESSION:** S11 · Three-model consensus with the corrected, tiered score gate.
2. **BLUEPRINT:** Section 7 Phase 11; **this document's header correction is binding here**: score
   gate is `65`/`75`, not the blueprint's documented `55`.
3. **BUILD:** `hermes_core/engines/reflect.py` (L2 portion): `call_deepseek`, `call_gemini`,
   `call_groq`, `call_llm_consensus(proposal, context) -> ConsensusResult`.
4. **CONTRACT:** consensus result carries vote count, required threshold, and final apply/reject
   decision.
5. **GUARDS:** score<65 → L2 never called, L1 result stands or is rejected on its own merits;
   65≤score<75 → 2/3 consensus required; score≥75 → **unanimous 3/3 required**; confidence≥0.40 to
   apply regardless of vote outcome.
6. **TESTS:** blueprint Phase 11 block, with the score-gate test suite **explicitly parameterized at
   55, 65, and 75** so the corrected behavior (65 standard, 75 unanimous) is asserted directly rather
   than left implicit; 3-model cascade (DeepSeek→Gemini→Groq) fallback tested; sub-threshold vote
   count → no change applied, fail-closed (3.5).
7. **ENTRY GATE:** S9, S10 green.
8. **EXIT GATE:** score-gate tests at all three boundary values green; consensus cascade tested end to
   end; a `score=77`, `2/3-only` scenario is explicitly asserted to be **rejected** (this is the exact
   case — mirroring GBP/JPY's original score of 77 — that the corrected gate exists to protect).
9. **DO-NOT:** do not implement the score gate at `55` — that is the documented regression this
   rebuild is correcting, not the target behavior, regardless of what any single blueprint line says
   in isolation (see this document's header for the explicit precedence rule).

---

### SESSION 12 — Crisis learning engine (Phase 12)
1. **SESSION:** S12 · 9-dimensional crisis signature + novel-regime flatline.
2. **BLUEPRINT:** Section 7 Phase 12; Section 11/Appendix F for the 9-dim formula; L21 novelty gate.
3. **BUILD:** `hermes_core/engines/crisis_learning.py`: `signature(prices, volumes) -> 9-tuple`,
   `nearest(signature) -> CrisisMatch`, `save_lived_crisis(signature)`.
4. **CONTRACT:** `CrisisLearning.signature(...) -> tuple[float, ...]`;
   `CrisisLearning.nearest(...) -> CrisisMatch` from Section 6.
5. **GUARDS:** **L21** novel-regime flatline — novelty score `>3.0*median` → pair paused 60 cycles;
   cosine distance `>0.5` classifies a signature as a genuinely novel (not previously seen) crisis.
6. **TESTS:** blueprint Phase 12 block; flatline event correctly appends to `flatline_log.jsonl`; a
   novel signature is persisted via `save_lived_crisis`, append-only, never overwriting prior history.
7. **ENTRY GATE:** S7 green.
8. **EXIT GATE:** 9-dim signature verified numerically against a known historical crisis window;
   flatline triggers and logs correctly; novel-crisis persistence confirmed append-only.
9. **DO-NOT:** do not act on a genuinely novel crisis signature without first pausing (L21) — this is
   the caution the guard exists to enforce; do not overwrite lived-crisis history, ever.

---

### SESSION 13 — Genetic programming engine (Phase 13)
1. **SESSION:** S13 · Symbolic GP discovery, isolated from crypto-specific signals.
2. **BLUEPRINT:** Section 7 Phase 13; fitness = `|corr| - 0.001*complexity`; novelty gate
   `>3.0*median`; GP operator set in `backtest.py`.
3. **BUILD:** `hermes_core/engines/genetic.py`: `discover(pair) -> List[Indicator]`.
4. **CONTRACT:** `GeneticEngine.discover(pair) -> List[Indicator]` from Section 6.
5. **GUARDS:** novelty gate before an indicator is admitted for backtesting at all; **no
   crypto-specific signals (FNG, on-chain feeds) reachable from the forex/gold discovery path** (D8).
6. **TESTS:** blueprint Phase 13 block; fitness formula unit-tested in isolation; novelty gate rejects
   a near-duplicate indicator; `G-crypto` confirmed clean on this file and its imports.
7. **ENTRY GATE:** S10 green (discovery output must pass through the same OOS-first validator).
8. **EXIT GATE:** `discover()` returns well-formed indicators; novelty gate unit-tested at its exact
   threshold; zero crypto-only imports anywhere in this module's dependency chain.
9. **DO-NOT:** do not admit an indicator into the live registry without it separately passing the
   Session 10 backtest gate; do not import or reference FNG/BTC-on-chain signals here (lesson 5).

---

### SESSION 14 — GP intelligence layer (Phase 14)
1. **SESSION:** S14 · Weighted-vote ensemble scoring, suppression, and the corrected default score.
2. **BLUEPRINT:** Section 7 Phase 14; `gp_entry_score` default originally `-0.3` at
   `gp_intelligence.py:253`; allocation key `(pair, regime, strategy)`.
3. **BUILD:** `hermes_core/engines/gp_intelligence.py`: `score(pair, cond) -> float[-1,1]`,
   `should_suppress() -> (bool, reason)`.
4. **CONTRACT:** `GPIntelligence.score(...) -> float[-1,1]`; `should_suppress() -> (bool, reason)`
   from Section 6.
5. **GUARDS:** GP entry gate (bullish consensus + n≥2 indicators + rsi<65 + weight≥0.5 + adx≥20 +
   gp_bad_losses<3 + `gp_entry_score>=0`); default `gp_entry_score` set to **0.0, not -0.3** — the
   original -0.3 default combined with a `>=0` gate created a chicken-and-egg deadlock where a new
   indicator could never accumulate the entries needed to earn a score above the gate it was already
   failing from the start.
6. **TESTS:** blueprint Phase 14 block; score bounded in `[-1,1]`; `should_suppress` returns a
   human-readable reason; the corrected default (0.0) is explicitly asserted, with a named test
   documenting *why* -0.3 was wrong; degradation-cull logic separately tests genuine WR decay versus
   a regime mismatch (the two must not be conflated — an indicator trained in BULL scoring poorly in
   NEUTRAL is regime-mismatched, not degraded, and should be weight-penalized, not culled).
7. **ENTRY GATE:** S13 green.
8. **EXIT GATE:** scoring and suppression both tested; corrected default verified; registry written to
   `discovered/{pair}.json` and visible on the dashboard before it can influence any live entry.
9. **DO-NOT:** do not ship the `-0.3` default — it silently starves every new indicator of the data it
   needs to ever prove itself; do not cull an indicator for regime mismatch alone.

---

### SESSION 15 — Decision cortex + policy engine (Phase 15)
1. **SESSION:** S15 · Memory of past decisions + bidirectional policy suppression.
2. **BLUEPRINT:** Section 7 Phase 15; cortex `record_*`/`best_entry_type`; policy suppressions;
   `indicator_exile.json`, `policy.json`.
3. **BUILD:** `hermes_core/engines/decision_cortex.py`
   (`record_entry/outcome/hypothesis/discovery`, `best_entry_type() -> str`) +
   `hermes_core/engines/policy_engine.py` (`evaluate(cycle, pairs, strategies) -> Policy`).
4. **CONTRACT:** `Cortex.record_*(...)`; `Cortex.best_entry_type() -> str`;
   `PolicyEngine.evaluate(...) -> Policy{suppressions, ...}` from Section 6.
5. **GUARDS:** indicator exile persisted to disk; policy suppressions applied at the entry gate in
   **both** directions — MR can suppress GP, and GP can suppress MR, depending on which is
   out-performing the other for the current pair.
6. **TESTS:** blueprint Phase 15 block; `record_*` persists correctly; `best_entry_type` always
   returns a valid, known type; both suppression directions independently tested; exile and policy
   files written and pushed to the dashboard.
7. **ENTRY GATE:** S12, S14 green.
8. **EXIT GATE:** cortex and policy both tested; `indicator_exile.json` and `policy.json` present on
   `/data` and visible on the dashboard, surviving a process restart.
9. **DO-NOT:** do not let cortex memory be lost on restart — it must persist on `/data` (D2), never
   rebuilt from scratch silently.

---

### SESSION 16 — Dashboard API (Phase 16)
1. **SESSION:** S16 · Ingest endpoint + SQLite storage with the composite-key fix + read API.
2. **BLUEPRINT:** Section 7 Phase 16; SQLite DDL in Appendix H; `/ingest/{bot}`.
3. **BUILD:** `dashboard/backend/main.py`: `POST /ingest/{bot}`, SQLite schema with
   **primary key `(bot, id)`** — composite, not `id` alone — plus one read endpoint per dashboard tab.
   Also: `tools/regenerate_blueprint_sections.py` gains its real logic here, generating Section 3's
   guard list and Appendix C's schemas directly from the now-substantially-complete `hermes_core/`
   codebase (implements D10).
4. **CONTRACT:** ingest accepts a full bot snapshot; the composite primary key is what prevents the
   cross-bot stale-trade collision documented in the original system.
5. **GUARDS:** ingest authenticated via `INGEST_TOKEN` (never logged, per 4.6); unknown bot name
   rejected outright.
6. **TESTS:** blueprint Phase 16 block; a specific PK-collision test — two different bots submitting
   the same `id` — asserts no overwrite occurs; an empty tab query returns an explicit "no data"
   response, never a bare 500 or an empty-looking success.
7. **ENTRY GATE:** S7, S15 green.
8. **EXIT GATE:** every state file has a corresponding read endpoint; the `(bot, id)` PK verified
   under the collision test; `tools/regenerate_blueprint_sections.py` produces its first real,
   non-stub output and it is diffed against the current blueprint manually once.
9. **DO-NOT:** do not use `id` alone as the primary key — that is the exact, documented cause of the
   original dashboard's cross-bot stale-trade contamination; never log `INGEST_TOKEN` in any form.

---

### SESSION 17 — Dashboard frontend (Phase 17)
1. **SESSION:** S17 · Tabs render from the read API, with diagnostic empty states.
2. **BLUEPRINT:** Section 7 Phase 17; tabs: trades, skips, discovered, cortex, flatline, heartbeat.
3. **BUILD:** `dashboard/frontend/`: one component per tab, each fed exclusively by its Session-16
   endpoint.
4. **CONTRACT:** frontend reads the API only — no direct database access; bot identity is read from
   config, never hard-coded per component.
5. **GUARDS:** an empty-data state renders `"pipeline gap for {bot}"` — explicitly diagnostic, never a
   silent blank panel (implements the standing rule in 4.4).
6. **TESTS:** each tab renders correctly against mocked API data; the empty state renders the
   pipeline-gap message, not a blank div; no component contains a hard-coded bot name string anywhere.
7. **ENTRY GATE:** S16 green.
8. **EXIT GATE:** all tabs render correctly; empty state is diagnostic by construction; verified once
   against a real (shadow) bot's live data, not just mocks.
9. **DO-NOT:** do not conclude "frontend bug" the moment a tab is empty — verify the full pipeline
   (4.4) before touching any frontend code in response to an empty tab.

---

### SESSION 18 — Full system integration (Phase 18)
1. **SESSION:** S18 · End-to-end live-shaped run, with "see it fire" as the closing standard.
2. **BLUEPRINT:** Section 7 Phase 18; data-flow diagram, Section 6; handover checklist, Section 10.
3. **BUILD:** `tests/test_integration_e2e.py` plus a staging deploy — no new engine code is written in
   this session; this session only exercises what the previous seventeen built.
4. **CONTRACT:** the full chain from Section 6:
   `PriceAdapter -> Indicators -> Entry ⇄ (GP, Chart, Crisis, Cortex, Policy) -> Risk -> Exit -> Cortex
   -> State -> Dashboard`; reflection fires every 5 trades (user override 2026-07-16 — blueprint base cadence restored); GP cron runs
   Sunday.
5. **GUARDS:** every guard L01-L66, RR, ATR floor, and the circuit breaker are each exercised at least
   once during this session's run.
6. **TESTS:** a live-shaped simulation drives a full cycle that triggers each guard at least once;
   heartbeat, skips, flatline, and discovered state are all independently confirmed observable on the
   dashboard; **the staging Railway log must show at least one real entry and one real exit firing** —
   compile-green alone does not satisfy this.
7. **ENTRY GATE:** S1-S17 all green.
8. **EXIT GATE:** every guard observably fires in staging with a corresponding log line; dashboard
   tabs populate correctly from the live shadow bot; the Section 10 handover checklist (blueprint) is
   fully complete; "see it fire" evidence (an actual captured log line, not an assertion that one
   would appear) is attached to the session's closing note.
9. **DO-NOT:** do not declare the rebuild done on a green compile or a passing test suite alone —
   production-proof, per this document's Section 6.3, requires a live log line for each fired guard,
   captured and kept, not merely expected.

---

## APPENDIX A.1 — EXECUTION ORDER QUICK-REFERENCE

```
S0  scaffold        →  (no deps)
S1  config          →  needs S0
S2  price adapter   →  needs S1
S3  indicators      →  needs S2
S4  entry           →  needs S3
S5  exit            →  needs S3
S6  risk            →  needs S3
S7  loop integ.     →  needs S2, S3, S4, S5, S6
S8  chart vision    →  needs S4
S10 backtest OOS    →  needs S3, S4
S9  reflect L1      →  needs S5, S7          [cadence=5, user override 2026-07-16; v4 flat-10 retired]
S11 L2 consensus    →  needs S9, S10        [Departure #1: corrected score gate 65/75, not 55]
S12 crisis          →  needs S7
S13 genetic GP      →  needs S10
S14 GP intelligence →  needs S13            [corrected default: 0.0, not -0.3]
S15 cortex+policy   →  needs S12, S14
S16 dashboard API   →  needs S7, S15        [composite PK (bot,id)]
S17 dashboard UI    →  needs S16
S18 integration     →  needs S1..S17 (all green)
```

**Parallelizable once S3 is green:** S4, S5, S6 depend only on S3 and can be built in separate
sessions concurrently. Everything else is strictly sequential along the arrows above.

**One-line contract for the implementing LLM:** *"Read the cited BLUEPRINT section, build exactly the
BUILD files to the CONTRACT signature, wire the GUARDS with their `[GUARD L##]` tags, make the TESTS
green including the discipline extras from this document, satisfy the EXIT GATE, never do the DO-NOT,
and if a number isn't in the source — stop and ask. Where this document names a corrected value (the
S11 score gate, the S14 default score, the S16 composite key), that corrected value governs, not the
blueprint's documented as-found value."*

---
*Appendix A is the operational execution layer for HERMES_MASTER_BLUEPRINT_v4.md, merged with the
discipline principles (Section 0) and phase table (Section 9) above. Each session maps 1:1 to a
blueprint Section 7 phase (except S0). Guard labels and numeric constants are cited to their source
lines where verified; where this document's header states a deliberate departure from a documented
blueprint value (Departure #1: score gate; Departure #2: reflection cadence), that departure is
binding and labeled consistently at every session where it matters.*
