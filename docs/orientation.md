# HERMES REBUILD — ORIENTATION (READ THIS FIRST)

## What this project is
This is a **paper-trading rebuild** of the Hermes multi-bot self-trading /
self-improving system — **forex and gold are the live scope; crypto is
reference-only** (its adapters and signals exist in the old system but are
not part of this rebuild's bots). No real money is at risk. The engineering
bar is nonetheless **production-grade**: strict typing, test-gated phases,
structured logging, and zero tolerance for silent failure — this is not a
prototype.

## The two source documents
There are exactly two authoritative files. Read both before doing anything
else.

- **`HERMES_MASTER_BLUEPRINT_v4.md` — the WHAT.**
  The architecture, the 18 build phases, all 66 enumerated guard layers, the
  per-pair configuration, and a verbatim source-code appendix from the
  original system. **This file is the source of truth.** If anything else
  ever conflicts with it, the blueprint wins.

- **`HERMES_REBUILD_EXECUTION_ROADMAP_v4.md` — the HOW.**
  The engineering discipline layered on top of the blueprint: coding
  standards, testing gates, error handling, observability, CI/CD, and
  change management. Its **Appendix A** is the operational layer — a
  session-by-session build sequence, **S0 through S18**, one session per
  blueprint phase.

  **One deliberate exception, stated in the roadmap's own header:** the L2
  reflection score gate. The blueprint documents the current production
  value as `score>=55` and flags it as a regression from the original `65`.
  The roadmap does **not** inherit that regression — it uses the corrected
  values, `score>=65` to reach L2, and a new protected tier at `score>=75`
  requiring unanimous 3-of-3 model consensus (rather than the standard
  2-of-3). This is the only place the roadmap knowingly departs from the
  blueprint's documented-as-found value; everything else in the roadmap
  defers to the blueprint.

## Working method
Build **one session at a time**, strictly in the dependency order given in
the roadmap's Appendix A.1 quick-reference. Do not start a session until the
previous session's **EXIT GATE** is fully green. For each session, in order:
read the blueprint section it cites, build to the stated CONTRACT signature,
wire the stated GUARDS, and make the stated TESTS pass. Nothing about *how*
to do this belongs in this file — that detail lives in the roadmap itself.

## Non-negotiables
These come from the blueprint's own lessons-learned section (Section 9) and
must hold across every session, not just the one that first introduces them:

- One shared engine package; bots are config instances, never code forks.
- State lives on the `/data` volume; code is read-only at runtime.
- Adapters fail soft — return `None`, never raise, never loop forever.
- Guard layers live inside the engine itself, not only in the orchestrator.
- No single LLM ever changes a live parameter: 2-of-3 consensus, the score
  gate, and an OOS-first backtest are all required.
- One variable changes at a time — never a bundled change.
- OOS validation runs as Phase 0 of backtesting, never last.
- Gold and silver are momentum strategies, never mean-reversion.
- No crypto-specific signals in the forex or gold bots.
- "Done" means a live log line shows the behavior actually firing — not
  that the code compiled or the tests merely passed.
- Never invent a number that isn't in the source. If it's not there, stop
  and ask, or flag it as open — don't guess.

## Where to start
Begin at **SESSION 0** in `HERMES_REBUILD_EXECUTION_ROADMAP_v4.md`,
Appendix A. Before writing anything, confirm both source documents have
been read in full. Then post the S0 plan and wait for its EXIT GATE to be
green before moving to S1.
