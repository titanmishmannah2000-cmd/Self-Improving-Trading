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

## Runtime state layout

All mutable state is written under **`{HERMES_STATE_ROOT}/{bot}/state/`** (default:
`HERMES_STATE_ROOT=/data` on Railway). Set `HERMES_BOT_NAME` per service (`forex`, `gold`, `crypto`).

Key paths (via `hermes_core.state.paths`):
- `heartbeat.json`, `skips.jsonl`, `trades.jsonl` — loop
- `hypotheses.jsonl`, `policy.json`, `flatline_log.jsonl` — reflection / policy / crisis
- `discovered/{PAIR}.json` — GP discovery
- `cortex/` — decision cortex + indicator exile

## Session status

| Session | Status |
|---------|--------|
| S0–S18 | ✅ Core rebuild complete (engines, tests, dashboard, CI) |
| Post-S18 | ✅ State path unification, cron wiring, `self_audit.py`, `G-contract` |
