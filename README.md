# HERMES Trading — Paper-Trading Rebuild

Production-grade paper-trading rebuild of the Hermes multi-bot self-trading system.
**Live scope:** forex + gold. Crypto config is reference-only.

## Start here

Read [`docs/orientation.md`](docs/orientation.md) before any build work.

**Source of truth:**
- [`docs/HERMES_MASTER_BLUEPRINT_v4.md`](docs/HERMES_MASTER_BLUEPRINT_v4.md) — architecture, guards, contracts
- [`docs/HERMES_REBUILD_EXECUTION_ROADMAP_v4.md`](docs/HERMES_REBUILD_EXECUTION_ROADMAP_v4.md) — discipline, CI gates, sessions S0–S18

## Build order

Execute Appendix A sessions strictly in order: **S0 → S1 → … → S18**.
Do not start a session until the previous session's EXIT GATE is green.

## Quick start (dev)

```bash
uv sync --extra dev
uv run ruff check .
uv run mypy hermes_core/
uv run pytest
```

## Architecture

- One shared `hermes_core/` engine package
- Bots (`forex`, `gold`) are config instances — no per-bot engine forks
- State lives on `/data` volume; code is read-only at runtime

## Session status

| Session | Status |
|---------|--------|
| S0 | ✅ Scaffold + CI wiring (ruff clean, pytest green, G-except clean) |
| S1 | ✅ Config system — schema + loader + validator, 6 per-pair strategy YAMLs, XAU/USD=momentum, reflection_every=5 |
| S17 | ✅ Dashboard frontend (React/Vite, Blueprint way) — `dashboard/frontend/` SPA: tab nav + bot selector + 60s auto-refresh; one component per tab (Overview/Trades/Skips/Discovered/Cortex/Flatline/Heartbeat), each fed ONLY by its S16 endpoint; bot identity from `src/bots.js` config (never hard-coded in components); diagnostic "pipeline gap for {bot}" empty state (standing rule 4.4). `npm test` (vitest+jsdom) 5/5 Phase-17 blueprint tests PASS (overview_6_pairs, bot_selector, auto_refresh, discovered_tab, empty_state); `npm run build` compiles (40 modules); wired under pytest via tests/test_frontend.py shim. |
| S18 | ✅ Full-system integration (Phase 18) — `tests/test_integration_e2e.py` (8 tests, 7 pass + 1 xfail=honest Discord gap). Drives the S7 60s loop for forex+gold end-to-end with deterministic candles: real entry→exit trades complete (≥3), ZERO unhandled exceptions, engine health registry green (price_adapter/indicators/config/chart_vision). Directly exercises every implemented guard (L04/L13/L14/L15/L16/L18/L21/L23/L24/L26/L27/L29/L35/L36/L40/L45/L53) with triggering inputs. Pushes completed trades to the LIVE S16 API and reads back: dashboard tabs populate AND bot identity is isolated by composite PK (forex trade never appears under gold/crypto). Reflection fires after 5 closed trades (cadence override). SURFACED 2 REAL BUGS: (1) `crypto/config.yaml` was schema-invalid (`bot:` was a string not nested dict) and would crash `run_cycle` — fixed to proper forex/gold schema; (2) `crisis_learning` L53 `_crisis_backtest` requires a real crash series to classify as crisis. Discord alert flagged as UNIMPLEMENTED gap (no webhook-send in code). Full gate: ruff clean, G-except clean, G-secrets clean, 143 passed + 1 xfailed. |
