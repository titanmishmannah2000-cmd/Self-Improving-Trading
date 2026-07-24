# 30-day paper soak — operator redeploy checklist

Complete after the soak-readiness code is deployed. Do **not** start the 30-day clock until go/no-go is green.

## Railway / env

1. Redeploy `forex`, `gold`, `crypto`, and `dashboard` from the same image.
2. Confirm per-service `HERMES_BOT_NAME` is set (`forex` | `gold` | `crypto` | `dashboard`).
3. Confirm `HERMES_STATE_ROOT` points at the persistent volume (e.g. `/data`).
4. `PRICE_BACKEND=aggregate` (default). Keep GoldAPI for metals (no key).
5. Set `DASHBOARD_API_URL` + `INGEST_TOKEN` on bots; `DASHBOARD_DB` / `DB_PATH` on dashboard (not a Windows path).
6. Optional: `HALT_ENTRIES=1` to freeze new entries without killing the process; or touch `{bot}/state/halt`.
7. `GP_PROMOTE=1` only when you want GP paper entries (shadow invent always runs).
8. `GP_EXCLUDE_PAIRS` still seeds cold-start bans (`GBP/JPY,BTC/USD` by default).

## Local / volume hygiene

```bash
python tools/state_hygiene.py --rebuild-learning --rotate-skips
```

This quarantines legacy `state/` runtime files, removes `live_prices_*.json` stubs + stub heartbeats, deletes `goldbot/`, bootstraps `{forex,gold,crypto}/state/trades.jsonl`, sets soak sessions to `24h`, and rebuilds cortex/policy from post-scrub trades.

## Go / no-go

```bash
python -m hermes_core.engines.self_audit
# or
python -c "from hermes_core.engines.self_audit import run_all; import json; print(json.dumps(run_all(), indent=2))"
```

Require `go_nogo: true` for forex, gold, and crypto (heartbeat age &lt; 10m, non-synthetic prices, trades file present, archive isolated). Soft checks (GP admitted / shadow active) may stay red briefly while invent runs — start the soak when classical fills are appending **or** GP reject logs show invent is healthy.

## During the 30 days

- Weekly: WR, expectancy, DD, admit rate, skip mix, heartbeat age.
- Auto-halt triggers: synthetic prices, feed-error spike, idle/pause SLO
  (all recent skips are `no_signal`/feed/BB for hours), or manual `halt` file.
- L21 novel-regime flatline pauses **new entries** for 60 cycles and appends
  `{bot}/state/flatline_log.jsonl` (alert after 3× `NOVEL_REGIME` on a pair).
- DD past config `max_drawdown` / `failure_below` → halt and investigate.
- Expect **clean data + possible mild paper edge**, not guaranteed profit.
