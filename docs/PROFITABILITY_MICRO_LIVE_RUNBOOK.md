# Hermes Profitability Path — Micro-Live Runbook (Phase 5)

## Preconditions (all must be green)

1. Phase 0 freeze: `BOOK_RISK=1`, all other HIF off, `REFLECT_AUTO_DEPLOY=0`, `GP_PROMOTE=0` until Phase 4.
2. Focus universe (all three bots): gold=`XAU/USD`, forex=`EUR/USD`+`GBP/USD`, crypto=`BTC/USD`+`ETH/USD`.
3. Feed health 24h+: `uv run python -m tools.health_check --bot gold --with-freeze` (also forex + crypto).
4. Phase 1 exit gate continue on at least one pair per bot: `uv run python -m tools.scorecard --bot <bot> --gate --min-n 20`
5. Phase 2 Bayesian allocator soak OK (if `SOFT_WEIGHTS=1`).
6. Phase 3 canary reflection held ≥15 trades (or stay at `REFLECT_DEPLOY_STAGE=prove`).
7. Phase 4: `GP_PROMOTE=1` only for gate-green pairs (cost-aware expectancy).

## Enable micro-live (one pair)

Railway / `.env`:

```
MICRO_LIVE=1
MICRO_LIVE_SIZE_MULT=0.25
REGIME_DECAY=1
HALT_ENTRIES=0
```

Restart only the chosen bot service. Keep the other bot on paper or halted.

## Daily checklist

```
uv run python -m tools.health_check --bot <bot> --with-freeze
uv run python -m tools.scorecard --bot <bot> --gate --min-n 20
```

Halt if live expectancy after costs diverges >20% from paper scorecard for the same window, or if `regime_decay` suppresses the pair.

## Halt / recover

```
HALT_ENTRIES=1          # freeze new entries; exits continue
MICRO_LIVE=0            # restore full paper size
REGIME_DECAY=0          # optional — clear decay suppress via state file
```

Clear decay suppress: delete `{HERMES_STATE_ROOT}/{bot}/state/regime_decay.json` pair entry or call `clear_pair_suppress`.

## Success (year-end)

Positive cost-adjusted expectancy on micro-live for ≥1 pair + documented decay trip in paper or live. Not: more engines enabled.
