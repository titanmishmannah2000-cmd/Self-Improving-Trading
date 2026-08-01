# Railway: BTC/USDT project (bots/btc + scoped dashboard)

Dedicated Railway project for the BTC/USDT paper path. The legacy
`self-improving-trading` project (forex / gold / crypto / multi-bot dashboard)
is left alone.

## Services (exactly two)

| Service     | `HERMES_BOT_NAME` | Role |
|-------------|-------------------|------|
| `btc` (or legacy name `crypto` on the service) | `btc` | BTC/USDT paper bot (`bots/btc/config.yaml`) |
| `dashboard` | `dashboard` | Same UI, scoped with `DASHBOARD_BOTS=btc` |

Same Docker image / `entrypoint.py` dispatch as the multi-bot project.
Bot code lives in `bots/btc/` — not `bots/crypto/` (crypto is restored to BTC/USD + ETH/USD).

## Required env

### Shared (both services)

- `INGEST_TOKEN` — identical on bot and dashboard
- `HERMES_STATE_ROOT=/data` — volume mount → `/data/btc/state/`
- LLM / Discord keys as needed

### Bot service

- `HERMES_BOT_NAME=btc`
- `DASHBOARD_API_URL=https://dashboard-production-bb88.up.railway.app`
- `PRICE_BACKEND=aggregate`
- Phase-0 freeze flags (`BOOK_RISK=1`, other HIF off)
- Optional BTC cost envs: `BTC_MAKER_FEE_PCT`, `BTC_TAKER_FEE_PCT`, …

### Dashboard service

- `HERMES_BOT_NAME=dashboard`
- `DASHBOARD_BOTS=btc`
- `DASHBOARD_TITLE=Hermes BTC/USDT`
- `DASHBOARD_DB=/data/dashboard.db`

## Live project

| | |
|--|--|
| Project | `hermes-btc-usdt` |
| Project ID | `0fd46635-709b-4d55-a948-5d7eb7b557bb` |
| Dashboard | https://dashboard-production-bb88.up.railway.app |
| Git branch | `BTC/USDT` |

Both services are connected to GitHub
`titanmishmannah2000-cmd/Self-Improving-Trading` @ branch `BTC/USDT`.

## Ops notes

- State is under `/data/btc/state/` (fresh volume path). Old `/data/crypto` from early BTC focus is not migrated.
- Re-link legacy multi-bot project: `railway link --project 026694c2-7d92-43a0-96fe-6d90f57bae77`
- Provision helper: `uv run python tools/setup_railway_btc_usdt.py`
