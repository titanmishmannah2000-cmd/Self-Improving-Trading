# 30-day paper soak — operator redeploy checklist

Complete after the soak-readiness code is deployed. Do **not** start the 30-day clock until go/no-go is green.

## Railway / env

1. Redeploy `forex`, `gold`, `crypto`, and `dashboard` from the same image.
2. Confirm `railway.json` uses `restartPolicyType: ALWAYS` (uncapped restarts — do not set `restartPolicyMaxRetries: 3`).
3. Confirm per-service `HERMES_BOT_NAME` is set (`forex` | `gold` | `crypto` | `dashboard`).
4. Confirm `HERMES_STATE_ROOT` points at the persistent volume (e.g. `/data`).
5. `PRICE_BACKEND=aggregate` (default). Keep GoldAPI for metals (no key).
6. Set `DASHBOARD_API_URL` + `INGEST_TOKEN` on bots; `DASHBOARD_DB` / `DB_PATH` on dashboard (not a Windows path). `INGEST_TOKEN` is required when `RAILWAY_ENVIRONMENT` is set.
7. Optional: `HALT_ENTRIES=1` to freeze new entries without killing the process; or touch `{bot}/state/halt`.
8. Optional: `HALT_FLATTEN=1` to force-close orphan paper positions on process stop when a halt file is present.
9. Optional: `L21_FLATLINE=0` to log novel-regime events without pausing entries (escape hatch if L21 over-fires).
10. `GP_PROMOTE=1` only when you want GP paper entries (shadow invent always runs).
11. `GP_EXCLUDE_PAIRS` still seeds cold-start bans (`GBP/JPY,BTC/USD` by default).
12. Keep `REFLECT_AUTO_DEPLOY=0` for soak (reflection may approve; YAML deploy stays off unless you opt in).

## Local / volume hygiene

```bash
python tools/state_hygiene.py --rebuild-learning --rotate-skips
python tools/rebuild_cortex.py
```

This quarantines legacy `state/` runtime files, removes `live_prices_*.json` stubs + stub heartbeats, deletes `goldbot/`, bootstraps `{forex,gold,crypto}/state/trades.jsonl`, sets soak sessions to `24h`, and rebuilds cortex/policy from post-scrub trades.

## Go / no-go (before the clock)

```bash
python -m hermes_core.engines.self_audit
# or
python -c "from hermes_core.engines.self_audit import run_all; import json; print(json.dumps(run_all(), indent=2))"
```

Require `go_nogo: true` for forex, gold, and crypto (heartbeat age &lt; 10m, non-synthetic prices, trades file present, archive isolated, **not** effectively paused, **not** halted). Soft checks (GP admitted / shadow active) may stay red briefly while invent runs — start the soak when classical fills are appending **or** GP reject logs show invent is healthy.

### Clear / recover halt after SLOs heal

Idle/feed auto-halts recover automatically when skip SLOs are healthy (`maybe_recover_halt`). For a stuck halt after a known-good recovery:

```bash
# From a bot container / local with HERMES_STATE_ROOT set:
python -c "from hermes_core.engines.soak_controls import clear_halt; clear_halt('forex')"
# or delete the file:
# rm $HERMES_STATE_ROOT/forex/state/halt
# Also unset HALT_ENTRIES=0 if set on the service.
```

Re-run `self_audit` until `not_halted` and `go_nogo` are green for all three bots.

## Stamp the 30-day clock (only after go/no-go)

`start_soak_clock.py` **refuses** to stamp when `go_nogo` is false (use `--force` only as an ops escape hatch).

```bash
python tools/start_soak_clock.py forex gold crypto
```

On Railway (image must include `tools/`):

```bash
railway ssh -s forex -- uv run python tools/start_soak_clock.py forex
railway ssh -s gold -- uv run python tools/start_soak_clock.py gold
railway ssh -s crypto -- uv run python tools/start_soak_clock.py crypto
```

## During the 30 days

- Automated: each bot runs `soak_monitor` (every `SOAK_MONITOR_INTERVAL_S`,
  default **300s**) and Discord-alerts on heartbeat staleness, go/no-go RED,
  **halt_active**, invent `chronic_timeout_backoff`, stale pulses, and high
  `admit_zero_streak`. A weekly Discord digest summarizes pulse health even
  when green. Failed Discord notifies are **not** latched forever.
- Manual weekly (optional): confirm Discord digest arrived; skim WR / DD.
- Auto-halt triggers: synthetic prices, feed-error spike, idle/pause SLO,
  drawdown / `failure_below`, policy rollback, or manual `halt` file.
  Recoverable idle/feed halts auto-clear when SLOs recover.
- Open positions persist to `{bot}/state/open_book.json` and restore on restart.
- L21 novel-regime flatline pauses **new entries** for 60 cycles and appends
  `{bot}/state/flatline_log.jsonl` (alert after 3× `NOVEL_REGIME` on a pair).
- Expect **clean data + possible mild paper edge**, not guaranteed profit.

## Discovery soak watch-outs (post-hardening)

- Invent timeout abandons the waiter (does **not** hang the discovery daemon).
  After repeated timeouts the invent budget auto-shrinks, then skips invent for
  `DISCOVERY_TIMEOUT_COOLDOWN_S` (`status=chronic_timeout_backoff`).
- Abandoned invent writes are fenced by `write_token` — a newer invent pass
  cannot be clobbered by a late worker.
- `admit_zero` is still possible under strict S10; pulses include `near_misses`
  + `admit_zero_streak` (Discord soft-alert every `DISCOVERY_ADMIT_ZERO_ALERT_AFTER`).
- GP lockout unlocks after `GP_LOCKOUT_DECAY_S` (default 6h); exile reinstates
  after `EXILE_DECAY_S` (default 7d) even without 100-entry recovery.
- `hypotheses_kb.jsonl` auto-rotates past `HYPOTHESES_KB_MAX_LINES`; hygiene
  also rotates on default run.

Manual one-shot:
```bash
python -m cron.soak_monitor
SOAK_MONITOR_FORCE_WEEKLY=1 python -m cron.soak_monitor
```
