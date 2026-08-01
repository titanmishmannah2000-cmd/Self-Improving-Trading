# Railway: BTC/USDT project (bot + dashboard)

Dedicated Railway project for the BTC/USDT paper path. The legacy
`self-improving-trading` project (forex / gold / crypto / multi-bot dashboard)
is left alone.

## Services (exactly two)

| Service     | `HERMES_BOT_NAME` | Role |
|-------------|-------------------|------|
| `crypto`    | `crypto`          | BTC/USDT paper bot (`bots/crypto/config.yaml`) |
| `dashboard` | `dashboard`       | Same UI as before, scoped with `DASHBOARD_BOTS=crypto` |

Same Docker image / `entrypoint.py` dispatch as the multi-bot project.

## Required env

### Shared (both services)

- `INGEST_TOKEN` — identical on bot and dashboard
- `HERMES_STATE_ROOT=/data` — volume mount
- LLM / Discord keys as needed (`GEMINI_API_KEY`, `GROQ_API_KEY`, …)

### `crypto` service

- `HERMES_BOT_NAME=crypto`
- `DASHBOARD_API_URL=https://<dashboard public domain>` (set after domain exists)
- `PRICE_BACKEND=aggregate`
- Phase-0 freeze flags (`BOOK_RISK=1`, other HIF off) — same as profitability path
- Optional BTC cost envs: `BTC_MAKER_FEE_PCT`, `BTC_TAKER_FEE_PCT`, `BTC_SLIPPAGE_FLOOR_BPS`, `BTC_SLIPPAGE_ATR_K`, `COST_STRESS_MULT`

### `dashboard` service

- `HERMES_BOT_NAME=dashboard`
- `DASHBOARD_BOTS=crypto` — hides forex/gold sections; overview only serves crypto
- `DASHBOARD_TITLE=Hermes BTC/USDT` (optional)
- `DASHBOARD_DB=/data/dashboard.db`
- Public HTTP domain on the dashboard service (bot has no public domain)

## Volumes

- `crypto` → `/data` (bot state under `/data/crypto/state`)
- `dashboard` → `/data` (SQLite ingest DB)

## Provisioning

From repo root (logged into Railway CLI):

```bash
uv run python tools/setup_railway_btc_usdt.py
```

The GitHub branch `BTC/USDT` must exist on origin for auto-deploys. Until it is
pushed, deploy the working tree:

```bash
railway link --project 0fd46635-709b-4d55-a948-5d7eb7b557bb
railway up --service dashboard --detach
railway up --service crypto --detach
```

After the dashboard domain exists, set on crypto:

`DASHBOARD_API_URL=https://dashboard-production-bb88.up.railway.app`

## Live project (provisioned)

| | |
|--|--|
| Project | `hermes-btc-usdt` |
| Project ID | `0fd46635-709b-4d55-a948-5d7eb7b557bb` |
| Dashboard | https://dashboard-production-bb88.up.railway.app |
| Services | `crypto`, `dashboard` (volumes on `/data`) |

## Ops notes

- Do **not** point the old multi-bot dashboard at this crypto service (separate ingest DB).
- Pause or remove the old project’s `crypto` service once this one is healthy to avoid double paper books.
- To re-link the CLI to the legacy project:  
  `railway link --project 026694c2-7d92-43a0-96fe-6d90f57bae77`
