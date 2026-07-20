"""
Hermes Dashboard — Backend API (v3.1 — HERMES_REDEPLOY_MARKER_2026-07-14T18-50Z)

PUSH-BASED MODE: each bot POSTs its FULL state to /api/ingest/{bot}
every cycle. This backend UPSERTS every trade/hypothesis/skip into
a SQLite database (on a Railway volume), so nothing is ever lost —
not even if the backend restarts, and not even if a bot's own
rolling window only sends the last N records (dedup by unique id).

Endpoints:
  POST /api/ingest/{bot_name}       — bots push here every cycle
  GET  /api/overview                — live snapshot (current state + recent activity)
  GET  /api/bot/{bot_name}/trades   — full trade history for one bot
  GET  /api/daily-summary           — today's activity, all bots
  GET  /api/lifetime-summary        — all-time stats, all bots
  GET  /api/export-text             — clean text block, paste-ready for analysis
"""

import json
import os
import sqlite3
import secrets
import sys
import math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

# Ensure sibling modules (live_compat, audit_*, etc.) resolve whether this file
# is run directly (python main.py) or imported as dashboard.backend.main.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

FOREX_PAIRS = ["EUR/USD", "GBP/USD", "AUD/USD", "GBP/JPY"]

# ── Quarantine: trades contaminated by known bugs (kept in raw data, excluded
# from WR/PnL aggregates, score guard, reflection). XAG/USD 2026-07-10/11 were
# priced off XAU/USD due to bug fixed in 42a2688. See user ruling 2026-07-14. ──
CONTAMINATED_TRADE_IDS = {
    "trade_1783026188",  # XAG/USD entry 4133.3 (gold-priced) 2026-07-09
    "trade_1783026250",  # XAG/USD entry 4132.3 (gold-priced) 2026-07-09
    "trade_1783649915",  # XAG/USD entry 4133.5 (gold-priced) 2026-07-10
    "trade_1783700739",  # XAG/USD entry 4120.8 (gold-priced) 2026-07-11
}
# Robust safety net: any XAG/USD trade entered at >1000 is priced off XAU/USD
# (real silver ~60), not a genuine silver fill. Catches future gold-priced rows
# without needing to list every ID. Raw rows preserved, just not aggregated.
CONTAMINATED_PAIR = "XAG/USD"
CONTAMINATED_ENTRY_MAX = 1000.0


# ── Wilson Score Interval ──

def wilson_score_interval(wins: int, total: int, confidence: float = 0.95):
    """Wilson score interval for win rate confidence. Returns (lower%, upper%)."""
    if total == 0:
        return 0.0, 0.0
    p = wins / total
    z = 1.96  # 95% confidence
    if confidence == 0.99:
        z = 2.576
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * math.sqrt((p * (1 - p) / total) + (z * z / (4 * total * total)))
    lower = max(0, (centre - margin) / denom) * 100
    upper = min(100, (centre + margin) / denom) * 100
    return round(lower, 1), round(upper, 1)


PAIR_TICKERS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "AUD/USD": "AUDUSD=X",
    "GBP/JPY": "GBPJPY=X",
    "XAU/USD": "GC=F",
    "XAG/USD": "SI=F",
    "BTC/USD": "BTC-USD",
    "ETH/USD": "ETH-USD",
}

PAIR_MAP = {
    "EUR_USD": "EUR/USD", "GBP_USD": "GBP/USD",
    "AUD_USD": "AUD/USD", "GBP_JPY": "GBP/JPY",
    "XAU_USD": "XAU/USD", "XAG_USD": "XAG/USD",
}

app = FastAPI(title="Hermes Dashboard API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

VALID_BOTS = {"gold", "forex", "crypto"}
VALID_BOT_ALIASES = {
    "crypto": {"crypto", "hermes-crypto", "hermes-crypto-bot"},
    "forex": {"forex", "hermes-forex", "hermes-forex-bot"},
    "gold": {"gold", "hermes-gold", "hermes-gold-bot"},
}

# ── LIVE PIPELINE COMPAT ──────────────────────────────────────────────────
# Teach this backend to read the ACTUAL live bot state files (bots/{bot}/state)
# and serve them in the shapes the redesigned frontend expects. Registered
# BEFORE the base routes so these take precedence where they overlap.
from live_compat import register as _register_live_compat
_register_live_compat(app, lambda: INGEST_TOKEN, VALID_BOTS)

# Quick test route registered immediately after FastAPI app creation
@app.get("/api/quick-test")
def quick_test():
    return {"status": "quick test route works", "ts": datetime.now(timezone.utc).isoformat()}

# Rebuild marker: Force Railway to pick up the latest routes (discovered, cortex, audit endpoints)
INGEST_TOKEN = os.getenv("INGEST_TOKEN", "")
if not INGEST_TOKEN:
    print("[WARN] INGEST_TOKEN not set — ingest endpoints are unprotected", flush=True)

# DB lives on a Railway volume so it survives restarts/redeploys.
# Mount a volume at /data on this service and set DB_PATH=/data/hermes.db
# (falls back to local disk if no volume is mounted — fine for testing,
# but data will reset on redeploy without a real volume).
DB_PATH = os.getenv("DASHBOARD_DB") or os.getenv("DB_PATH", "/data/hermes.db")
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = lambda cursor, row: dict(sqlite3.Row(cursor, row))
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT NOT NULL,
            bot TEXT NOT NULL,
            pair TEXT,
            entry_price REAL,
            exit_price REAL,
            entry_ts TEXT,
            exit_ts TEXT,
            pnl_pct REAL,
            exit_reason TEXT,
            hold_cycles INTEGER,
            entry_rsi REAL,
            entry_regime TEXT,
            entry_quality_score REAL,
            strategy_type TEXT,
            chart_context TEXT,
            raw_json TEXT,
            PRIMARY KEY (bot, id)
        );

        CREATE TABLE IF NOT EXISTS hypotheses (
            bot TEXT NOT NULL,
            ts TEXT NOT NULL,
            pair TEXT,
            version_from TEXT,
            version_to TEXT,
            variable TEXT,
            old_value TEXT,
            new_value TEXT,
            reasoning TEXT,
            mode TEXT,
            raw_json TEXT,
            PRIMARY KEY (bot, ts, variable)
        );

        CREATE TABLE IF NOT EXISTS skips (
            bot TEXT NOT NULL,
            ts TEXT NOT NULL,
            pair TEXT,
            reason_skipped TEXT,
            rsi_at_skip REAL,
            price_at_skip REAL,
            missed_pnl REAL,
            raw_json TEXT,
            PRIMARY KEY (bot, ts, pair)
        );

        CREATE TABLE IF NOT EXISTS latest_state (
            bot TEXT PRIMARY KEY,
            strategy_json TEXT,
            goal_json TEXT,
            heartbeat_json TEXT,
            open_trades_json TEXT DEFAULT '[]',
            discovered_json TEXT DEFAULT '{}',
            received_at TEXT
        );

        CREATE TABLE IF NOT EXISTS dismissed_alerts (
            alert_key TEXT PRIMARY KEY,
            dismissed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS bot_status (
            bot TEXT PRIMARY KEY,
            desired_state TEXT NOT NULL DEFAULT 'running',
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS auth_tokens (
            token TEXT PRIMARY KEY,
            created_at TEXT,
            expires_at TEXT
        );

        CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS monitor_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            message TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS monitor_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            metric_value REAL,
            recorded_at TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()


init_db()

# ── Migration: add open_trades_json column to existing databases ──
try:
    conn = get_conn()
    conn.execute("ALTER TABLE latest_state ADD COLUMN discovered_json TEXT DEFAULT '{}'")
    conn.commit()
    conn.close()
except sqlite3.OperationalError:
    pass

# ── Migration: add cortex_json to existing databases ──
try:
    conn = get_conn()
    conn.execute("ALTER TABLE latest_state ADD COLUMN cortex_json TEXT DEFAULT '{}'")
    conn.commit()
    conn.close()
except sqlite3.OperationalError:
    pass

# ── Migration: add flatlined_json to existing databases ──
try:
    conn = get_conn()
    conn.execute("ALTER TABLE latest_state ADD COLUMN flatlined_json TEXT DEFAULT '{}'")
    conn.commit()
    conn.close()
except sqlite3.OperationalError:
    pass

try:
    conn = get_conn()
    conn.execute("ALTER TABLE latest_state ADD COLUMN open_trades_json TEXT DEFAULT '[]'")
    conn.commit()
    conn.close()
except sqlite3.OperationalError:
    pass


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def upsert_trades(conn, bot: str, trades: list):
    for t in trades:
        tid = t.get("id") or f"{t.get('entry_ts','')}-{t.get('pair', t.get('asset',''))}"
        conn.execute(
            """
            INSERT INTO trades (id, bot, pair, entry_price, exit_price, entry_ts, exit_ts,
                pnl_pct, exit_reason, hold_cycles, entry_rsi, entry_regime,
                entry_quality_score, strategy_type, chart_context, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot, id) DO UPDATE SET
                exit_price=excluded.exit_price,
                exit_ts=excluded.exit_ts,
                pnl_pct=excluded.pnl_pct,
                exit_reason=excluded.exit_reason,
                hold_cycles=excluded.hold_cycles,
                raw_json=excluded.raw_json
            """,
            (
                tid, bot, t.get("pair") or t.get("asset"),
                t.get("entry_price"), t.get("exit_price"),
                t.get("entry_ts"), t.get("exit_ts"),
                t.get("pnl_pct"), t.get("exit_reason"), t.get("hold_cycles"),
                t.get("entry_rsi"), t.get("entry_regime"),
                t.get("entry_quality_score") or t.get("quality"),
                t.get("strategy_type"), t.get("chart_context"),
                json.dumps(t),
            ),
        )


def upsert_hypotheses(conn, bot: str, hyps: list):
    for h in hyps:
        ts = h.get("ts") or utcnow_iso()
        conn.execute(
            """
            INSERT INTO hypotheses (bot, ts, pair, version_from, version_to,
                variable, old_value, new_value, reasoning, mode, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot, ts, variable) DO NOTHING
            """,
            (
                bot, ts, h.get("pair") or h.get("symbol"),
                h.get("version_from"), h.get("version_to"),
                h.get("variable"), str(h.get("old_value")), str(h.get("new_value")),
                h.get("reasoning"), h.get("mode"), json.dumps(h),
            ),
        )


def upsert_skips(conn, bot: str, skips: list):
    for s in skips:
        ts = s.get("ts") or utcnow_iso()
        conn.execute(
            """
            INSERT INTO skips (bot, ts, pair, reason_skipped, rsi_at_skip,
                price_at_skip, missed_pnl, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot, ts, pair) DO UPDATE SET
                missed_pnl=excluded.missed_pnl,
                raw_json=excluded.raw_json
            """,
            (
                bot, ts, s.get("pair"), s.get("reason_skipped"),
                s.get("rsi_at_skip"), s.get("price_at_skip"),
                s.get("missed_pnl"), json.dumps(s),
            ),
        )


@app.get("/api/health")
def health():
    conn = get_conn()
    counts = {}
    for bot in VALID_BOTS:
        row = conn.execute("SELECT COUNT(*) c FROM trades WHERE bot=?", (bot,)).fetchone()
        counts[bot] = row["c"]
    conn.close()
    return {"status": "ok-v2-new-code", "ts": utcnow_iso(), "deployment_timestamp": "2026-07-14T18:20:00Z", "trade_counts": counts}


@app.get("/api/health/ping")
def health_ping():
    """Minimal health endpoint for external uptime monitors. No auth needed. No trading data."""
    return {"status": "ok", "last_seen": utcnow_iso()}




@app.get("/api/test-rebuild-2026-07-14")
def test_rebuild():
    """Test endpoint to verify latest code is deployed."""
    return {"message": "rebuild works — deployed successfully", "ts": utcnow_iso()}


@app.get("/api/spark")
def spark(pair: str, bars: int = 30):
    """Return last N closing prices for a pair sparkline.

    Primary source: yfinance. Fallback: the bot's own rolling live-price
    history (pushed each cycle via the heartbeat) — this covers pairs whose
    yfinance ticker is unreliable (e.g. gold XAU/USD, silver XAG/USD).
    """
    # Fallback helper: pull the bot's rolling price history for this pair.
    bot_for_pair = None
    for _b in VALID_BOTS:
        try:
            row = get_conn().execute(
                "SELECT heartbeat_json FROM latest_state WHERE bot=?", (_b,)
            ).fetchone()
            if row and row["heartbeat_json"]:
                hb = json.loads(row["heartbeat_json"])
                ph = hb.get("price_history", {}) or {}
                if pair in ph and len(ph[pair]) >= 2:
                    bot_for_pair = _b
                    break
        except Exception:
            continue
    ticker = PAIR_TICKERS.get(pair)
    if ticker:
        try:
            import yfinance as yf
            import warnings
            warnings.filterwarnings("ignore")
            df = yf.download(ticker, period="3d", interval="5m", progress=False, auto_adjust=True)
            if df is not None and len(df) > 0:
                if hasattr(df.columns, "levels"):
                    df.columns = df.columns.get_level_values(0)
                df = df.dropna(subset=["Close"])
                prices = df["Close"].tail(bars).tolist()
                if len(prices) >= 2:
                    return {"pair": pair, "prices": [round(p, 5) for p in prices]}
        except Exception:
            pass
    # Fallback to the bot's rolling live-price history.
    if bot_for_pair:
        try:
            row = get_conn().execute(
                "SELECT heartbeat_json FROM latest_state WHERE bot=?", (bot_for_pair,)
            ).fetchone()
            if row and row["heartbeat_json"]:
                hb = json.loads(row["heartbeat_json"])
                ph = (hb.get("price_history", {}) or {}).get(pair, [])
                if len(ph) >= 2:
                    return {"pair": pair, "prices": [round(float(p), 5) for p in ph[-bars:]]}
        except Exception:
            pass
    return {"pair": pair, "prices": []}


@app.post("/api/ingest/{bot_name}")
async def ingest(bot_name: str, request: Request):
    normalized = bot_name
    if bot_name not in VALID_BOTS:
        for known in VALID_BOT_ALIASES:
            if bot_name in VALID_BOT_ALIASES[known]:
                normalized = known
                break
        if normalized not in VALID_BOTS:
            raise HTTPException(404, f"Unknown bot '{bot_name}'")
    bot_name = normalized

    if INGEST_TOKEN:
        if request.headers.get("X-Ingest-Token", "") != INGEST_TOKEN:
            raise HTTPException(401, "Invalid or missing ingest token")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Body must be valid JSON")

    conn = get_conn()
    try:
        # Guard: forex bot should never receive gold pair data
        if bot_name == "forex":
            gold_pairs = {"XAU/USD", "XAG/USD"}
            trades = [t for t in payload.get("recent_trades", []) if t.get("pair", t.get("asset", "")) not in gold_pairs]
            hyps = [h for h in payload.get("recent_hypotheses", []) if h.get("pair", "") not in gold_pairs]
            skips = [s for s in payload.get("recent_skips", []) if s.get("pair", "") not in gold_pairs]
        else:
            trades = payload.get("recent_trades", [])
            hyps = payload.get("recent_hypotheses", [])
            skips = payload.get("recent_skips", [])
        upsert_trades(conn, bot_name, trades)
        upsert_hypotheses(conn, bot_name, hyps)
        upsert_skips(conn, bot_name, skips)

        # Clean stale open trades before storing (bots may push dated entries)
        open_trades = payload.get("recent_open_trades", [])
        if open_trades:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            open_trades = [t for t in open_trades if (t.get("entry_ts") or "") > cutoff]

        conn.execute(
            """
            INSERT INTO latest_state (bot, strategy_json, goal_json, heartbeat_json, open_trades_json, discovered_json, cortex_json, flatlined_json, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot) DO UPDATE SET
                strategy_json=excluded.strategy_json,
                goal_json=excluded.goal_json,
                heartbeat_json=excluded.heartbeat_json,
                open_trades_json=excluded.open_trades_json,
                discovered_json=excluded.discovered_json,
                cortex_json=excluded.cortex_json,
                flatlined_json=excluded.flatlined_json,
                received_at=excluded.received_at
            """,
            (
                bot_name,
                json.dumps(payload.get("strategies") or payload.get("strategy")),
                json.dumps(payload.get("goal")),
                json.dumps(payload.get("heartbeat")),
                json.dumps(open_trades),
                json.dumps(payload.get("discovered", {})),
                json.dumps(payload.get("cortex", {})),
                json.dumps(payload.get("flatlined_pairs", {})),
                utcnow_iso(),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    return {"status": "received", "bot": bot_name, "ts": utcnow_iso()}


# ───────────────────── Bot Control (pause/resume) ─────────────────────

BOT_SERVICE_MAP = {
    "gold": "5cead7a3-6bdd-4258-b0b5-af69dc7da99e",
    "forex": "554ed089-b68a-44c0-aeb6-741253ab98b0",
}

_RAILWAY_TOKEN = os.getenv("RAILWAY_TOKEN", "")
_RAILWAY_ENV = os.getenv("RAILWAY_ENVIRONMENT_ID", "952f0a72-4c9f-4a79-85cb-d6fb178252a6")


@app.get("/api/bot/{bot_name}/pulse")
def bot_pulse(bot_name: str):
    """Return whether the bot is currently running or paused."""
    normalized = bot_name
    if bot_name not in VALID_BOTS:
        for known in VALID_BOT_ALIASES:
            if bot_name in VALID_BOT_ALIASES[known]:
                normalized = known
                break
        if normalized not in VALID_BOTS:
            raise HTTPException(404, f"Unknown bot '{bot_name}'")
    bot_name = normalized
    conn = get_conn()
    row = conn.execute("SELECT desired_state FROM bot_status WHERE bot=?", (bot_name,)).fetchone()
    conn.close()
    desired = row["desired_state"] if row else "running"
    return {"bot": bot_name, "desired_state": desired}


@app.post("/api/bot/{bot_name}/toggle")
def bot_toggle(bot_name: str):
    """Toggle a bot between running and paused states.
    Uses Railway GraphQL API to remove/redeploy the latest deployment."""
    normalized = bot_name
    if bot_name not in VALID_BOTS:
        for known in VALID_BOT_ALIASES:
            if bot_name in VALID_BOT_ALIASES[known]:
                normalized = known
                break
        if normalized not in VALID_BOTS:
            raise HTTPException(404, f"Unknown bot '{bot_name}'")
    bot_name = normalized

    conn = get_conn()
    row = conn.execute("SELECT desired_state FROM bot_status WHERE bot=?", (bot_name,)).fetchone()
    current = row["desired_state"] if row else "running"
    new_state = "paused" if current == "running" else "running"

    conn.execute(
        "INSERT INTO bot_status (bot, desired_state, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(bot) DO UPDATE SET desired_state=excluded.desired_state, updated_at=excluded.updated_at",
        (bot_name, new_state, utcnow_iso()),
    )
    conn.commit()
    conn.close()

    svc_id = BOT_SERVICE_MAP.get(bot_name)
    if svc_id and _RAILWAY_TOKEN:
        import httpx as _hx
        if new_state == "paused":
            q = '{ service(id: "%s") { deployments(last: 1) { edges { node { id } } } } }' % svc_id
            try:
                with _hx.Client(timeout=20) as cl:
                    r = cl.post("https://api.railway.app/graphql/v2",
                                json={"query": q},
                                headers={"Authorization": f"Bearer {_RAILWAY_TOKEN}"})
                    data = r.json()
                    edges = data.get("data", {}).get("service", {}).get("deployments", {}).get("edges", [])
                    if edges:
                        dep_id = edges[0]["node"]["id"]
                        m1 = 'mutation { deploymentStop(id: "%s") }' % dep_id
                        cl.post("https://api.railway.app/graphql/v2",
                                json={"query": m1},
                                headers={"Authorization": f"Bearer {_RAILWAY_TOKEN}"})
                        m2 = 'mutation { deploymentRemove(id: "%s") }' % dep_id
                        cl.post("https://api.railway.app/graphql/v2",
                                json={"query": m2},
                                headers={"Authorization": f"Bearer {_RAILWAY_TOKEN}"})
                        print(f"[TOGGLE] {bot_name} dep {dep_id[:8]} stopped+removed", flush=True)
            except Exception as e:
                print(f"[TOGGLE] Pause failed: {e}", flush=True)
        else:
            gql = 'mutation { serviceInstanceRedeploy(environmentId: "%s", serviceId: "%s") }' % (_RAILWAY_ENV, svc_id)
            try:
                with _hx.Client(timeout=10) as cl:
                    resp = cl.post("https://api.railway.app/graphql/v2",
                                   json={"query": gql},
                                   headers={"Authorization": f"Bearer {_RAILWAY_TOKEN}"})
                    print(f"[TOGGLE] {bot_name} redeploy: {resp.status_code}", flush=True)
            except Exception as e:
                print(f"[TOGGLE] Resume failed: {e}", flush=True)

    return {"bot": bot_name, "desired_state": new_state}


def row_to_trade(r) -> dict:
    raw = json.loads(r["raw_json"]) if r["raw_json"] else {}
    raw["pair"] = r["pair"]
    raw["entry_price"] = r["entry_price"]
    raw["exit_price"] = r["exit_price"]
    raw["entry_ts"] = r["entry_ts"]
    raw["exit_ts"] = r["exit_ts"]
    raw["pnl_pct"] = r["pnl_pct"]
    raw["exit_reason"] = r["exit_reason"]
    raw["hold_cycles"] = r["hold_cycles"]
    return raw


@app.get("/api/overview")
def overview():
    conn = get_conn()
    result = {"ts": utcnow_iso(), "bots": {}}
    for bot in VALID_BOTS:
        state_row = conn.execute("SELECT * FROM latest_state WHERE bot=?",
            (bot,)).fetchone()
        strategy = json.loads(state_row["strategy_json"]) if state_row and state_row["strategy_json"] else None

        # Determine valid pairs for this bot from strategy
        valid_pairs = list(strategy.keys()) if strategy else []

        trades = conn.execute(
            "SELECT * FROM trades WHERE bot=? ORDER BY COALESCE(exit_ts, entry_ts) DESC LIMIT 300",
            (bot,),
        ).fetchall()
        # Filter trades to only show valid pairs (clean up stale data)
        if valid_pairs:
            trades = [t for t in trades if t["pair"] in valid_pairs]
        hyps = conn.execute(
            "SELECT * FROM hypotheses WHERE bot=? ORDER BY ts DESC LIMIT 30", (bot,)
        ).fetchall()
        skips = conn.execute(
            "SELECT * FROM skips WHERE bot=? ORDER BY ts DESC LIMIT 50", (bot,)
        ).fetchall()

        heartbeat = json.loads(state_row["heartbeat_json"]) if state_row and state_row["heartbeat_json"] else {}
        heartbeat = heartbeat or {}
        strategy = json.loads(state_row["strategy_json"]) if state_row and state_row["strategy_json"] else None
        strategy = strategy or {}
        # Open trades: the bot pushes its CURRENT open_positions every cycle as
        # recent_open_trades (carrying entry_type, e.g. 'gp_ensemble' for the GP
        # brain). Treat that push as authoritative for "what is open now" — it
        # is always fresh (entry_ts set each cycle). We keep the 24h staleness
        # cutoff, but we NO LONGER discard pushed open trades just because they
        # aren't (yet) rows in the trades table: open positions are only written
        # to trades on EXIT, so a live open position would never match and was
        # being silently dropped. Enrich with trades-table entry_type if present.
        open_trades = json.loads(state_row["open_trades_json"]) if state_row and state_row["open_trades_json"] else []
        open_trades = open_trades or []
        if open_trades:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            open_trades = [t for t in open_trades if (t.get("entry_ts") or "") > cutoff]
        if open_trades:
            live_by_id = {r["id"]: r for r in conn.execute(
                "SELECT * FROM trades WHERE bot=? AND exit_ts IS NULL", (bot,)).fetchall()}
            for t in open_trades:
                if not t.get("entry_type"):
                    src = live_by_id.get(t.get("id"))
                    if src and src["raw_json"]:
                        try:
                            t["entry_type"] = json.loads(src["raw_json"]).get("entry_type")
                        except Exception:
                            pass
        if not open_trades:
            live = conn.execute(
                "SELECT * FROM trades WHERE bot=? AND exit_ts IS NULL ORDER BY entry_ts DESC",
                (bot,),
            ).fetchall()
            open_trades = [row_to_trade(r) for r in live]

        result["bots"][bot] = {
            "strategy": strategy,
            "goal": json.loads(state_row["goal_json"]) if state_row and state_row["goal_json"] else None,
            "heartbeat": heartbeat,
            # Sticky prices: prefer this cycle's live quote, but fall back to the
            # last known good tick from price_history so a single bad fetch poll
            # (e.g. silver no_candle) doesn't blank the card mid-stream — a real
            # ticker keeps showing the last quote between refreshes.
            "prices": (lambda hp: {**{p: ph[-1] for p, ph in (hp.get("price_history", {}) or {}).items() if ph}, **(hp.get("prices", {}) or {})})(heartbeat) if isinstance(heartbeat, dict) else {},
            "price_history": heartbeat.get("price_history", {}) if isinstance(heartbeat, dict) else {},
            "_received_at": state_row["received_at"] if state_row else None,
            "recent_trades": [row_to_trade(r) for r in reversed(trades)],
            "recent_open_trades": open_trades,
            "recent_hypotheses": [dict(json.loads(r["raw_json"])) for r in reversed(hyps)],
            "recent_skips": [dict(json.loads(r["raw_json"])) for r in reversed(skips)],
            "live_indicators": {
                "regimes": heartbeat.get("regimes", {}),
                "active_pairs": heartbeat.get("status", "unknown"),
                "cycle": heartbeat.get("cycle"),
                "flatlined_pairs": json.loads(state_row["flatlined_json"]) if state_row and state_row["flatlined_json"] else {},
                "discovery_stale_days": (
                    round((datetime.now(timezone.utc) - datetime.fromisoformat(heartbeat["last_discovery_run_ts"])).total_seconds() / 86400, 1)
                ) if heartbeat and heartbeat.get("last_discovery_run_ts") else None,
            },
        }

    conn.close()
    return result


@app.get("/api/bot/{bot_name}/trades")
def bot_trades(bot_name: str, pair: Optional[str] = None, limit: int = 5000):
    if bot_name not in VALID_BOTS:
        raise HTTPException(404, "Unknown bot")
    conn = get_conn()
    if pair:
        rows = conn.execute(
            "SELECT * FROM trades WHERE bot=? AND pair=? ORDER BY COALESCE(exit_ts, entry_ts) ASC LIMIT ?",
            (bot_name, pair, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trades WHERE bot=? ORDER BY COALESCE(exit_ts, entry_ts) ASC LIMIT ?",
            (bot_name, limit),
        ).fetchall()
    conn.close()
    return [row_to_trade(r) for r in rows]


@app.get("/api/trades/{bot_name}")
def api_bot_trades(bot_name: str, pair: Optional[str] = None, limit: int = 5000):
    """Per-bot trade read-back (used by dashboard tabs + S18 isolation test)."""
    return bot_trades(bot_name, pair=pair, limit=limit)


# ───────────────────── Alerts ─────────────────────

ALERT_RULES = {
    "zero_win_rate": {
        "severity": "high",
        "title_fmt": "{pair} win rate 0% after {n} trades today",
    },
}


def compute_alerts(conn) -> list:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    alerts = []

    dismissed = {
        r["alert_key"] for r in conn.execute("SELECT alert_key FROM dismissed_alerts").fetchall()
    }

    for bot in VALID_BOTS:
        rows = conn.execute(
            "SELECT * FROM trades WHERE bot=? AND exit_reason IS NOT NULL AND (exit_ts LIKE ?)",
            (bot, f"{today}%"),
        ).fetchall()

        by_pair = {}
        for r in rows:
            p = r["pair"] or "unknown"
            by_pair.setdefault(p, []).append(r["pnl_pct"] or 0)

        for pair, pnls in by_pair.items():
            if len(pnls) >= 3 and all(p <= 0 for p in pnls):
                key = f"zero_win_rate:{bot}:{pair}:{today}"
                if key not in dismissed:
                    alerts.append(
                        {
                            "key": key,
                            "bot": bot,
                            "pair": pair,
                            "severity": "high",
                            "title": f"{pair} win rate 0% after {len(pnls)} trades today",
                            "detail": f"Total today: {round(sum(pnls), 3)}% — review filters/strategy for this pair.",
                            "ts": utcnow_iso(),
                        }
                    )

    return alerts


@app.get("/api/alerts")
def get_alerts():
    conn = get_conn()
    alerts = compute_alerts(conn)
    conn.close()
    return {"alerts": alerts, "count": len(alerts)}


@app.post("/api/alerts/dismiss")
async def dismiss_alert(request: Request):
    body = await request.json()
    key = body.get("key")
    if not key:
        raise HTTPException(400, "Missing 'key'")
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO dismissed_alerts (alert_key, dismissed_at) VALUES (?, ?)",
        (key, utcnow_iso()),
    )
    conn.commit()
    conn.close()
    return {"status": "dismissed", "key": key}


def _summarize(rows, label: str) -> dict:
    # Quarantine: exclude trades contaminated by known bugs from WR/PnL aggregates
    # (raw rows preserved, just not counted). XAG/USD 2026-07-10/11 were priced
    # off XAU/USD — bug fixed in 42a2688. See user ruling 2026-07-14.
    CONTAM = CONTAMINATED_TRADE_IDS
    rows = [r for r in rows if r["id"] not in CONTAM]
    # Safety net: drop any XAG/USD row entered gold-priced (>1000) — see above.
    rows = [
        r for r in rows
        if not (r.get("pair") == CONTAMINATED_PAIR
                and (r.get("entry_price") or r.get("entry") or 0) > CONTAMINATED_ENTRY_MAX)
    ]
    closed = [r for r in rows if r["exit_reason"]]
    pnls = [r["pnl_pct"] for r in closed if r["pnl_pct"] is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    by_pair = {}
    for r in closed:
        p = r["pair"] or "unknown"
        by_pair.setdefault(p, {"trades": 0, "pnl": 0.0, "wins": 0})
        by_pair[p]["trades"] += 1
        by_pair[p]["pnl"] += r["pnl_pct"] or 0
        if (r["pnl_pct"] or 0) > 0:
            by_pair[p]["wins"] += 1

    return {
        "label": label,
        "closed_trades": len(closed),
        "open_trades": len(rows) - len(closed),
        "total_pnl_pct": round(sum(pnls), 3) if pnls else 0,
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "wr_lower": wilson_score_interval(len(wins), len(closed))[0] if closed else 0,
        "wr_upper": wilson_score_interval(len(wins), len(closed))[1] if closed else 0,
        "low_confidence": len(closed) < 10,
        "avg_win_pct": round(sum(wins) / len(wins), 3) if wins else 0,
        "avg_loss_pct": round(sum(losses) / len(losses), 3) if losses else 0,
        "by_pair": {
            p: {
                "trades": d["trades"],
                "total_pnl_pct": round(d["pnl"], 3),
                "win_rate_pct": round(d["wins"] / d["trades"] * 100, 1) if d["trades"] else 0,
                "wr_lower": wilson_score_interval(d["wins"], d["trades"])[0] if d["trades"] else 0,
                "wr_upper": wilson_score_interval(d["wins"], d["trades"])[1] if d["trades"] else 0,
                "low_confidence": d["trades"] < 10,
            }
            for p, d in by_pair.items()
        },
    }


@app.get("/api/daily-summary")
def daily_summary():
    conn = get_conn()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    result = {"date": cutoff[:10], "range": "last_24h", "cutoff_iso": cutoff, "bots": {}}

    for bot in VALID_BOTS:
        rows = conn.execute(
            "SELECT * FROM trades WHERE bot=? AND (entry_ts >= ? OR exit_ts >= ?)",
            (bot, cutoff, cutoff),
        ).fetchall()
        hyps = conn.execute(
            "SELECT * FROM hypotheses WHERE bot=? AND ts >= ?", (bot, cutoff)
        ).fetchall()
        result["bots"][bot] = _summarize(rows, "last 24h")
        result["bots"][bot]["reflections_today"] = len(hyps)
        result["bots"][bot]["reflections_detail"] = [
            {"pair": h["pair"], "variable": h["variable"], "old": h["old_value"],
             "new": h["new_value"], "reasoning": h["reasoning"]}
            for h in hyps
        ]

    conn.close()
    return result


@app.get("/api/lifetime-summary")
def lifetime_summary():
    conn = get_conn()
    result = {"bots": {}}

    for bot in VALID_BOTS:
        rows = conn.execute("SELECT * FROM trades WHERE bot=?", (bot,)).fetchall()
        hyps = conn.execute("SELECT COUNT(*) c FROM hypotheses WHERE bot=?", (bot,)).fetchone()
        first_row = conn.execute(
            "SELECT MIN(entry_ts) m FROM trades WHERE bot=?", (bot,)
        ).fetchone()

        result["bots"][bot] = _summarize(rows, "lifetime")
        result["bots"][bot]["total_reflections"] = hyps["c"]
        result["bots"][bot]["tracking_since"] = first_row["m"]

    conn.close()
    return result


@app.get("/api/range-summary")
def range_summary(
    bot_name: Optional[str] = None,
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
):
    conn = get_conn()
    result = {"bots": {}, "from_ts": from_ts, "to_ts": to_ts}

    bots = [bot_name] if bot_name else list(VALID_BOTS)

    for bot in bots:
        if bot not in VALID_BOTS:
            continue

        query = "SELECT * FROM trades WHERE bot=?"
        params: list = [bot]

        if from_ts:
            query += " AND (entry_ts >= ? OR exit_ts >= ?)"
            params.extend([from_ts, from_ts])
        if to_ts:
            query += " AND (entry_ts <= ? OR exit_ts <= ?)"
            params.extend([to_ts, to_ts])

        query += " ORDER BY COALESCE(exit_ts, entry_ts) ASC"
        rows = conn.execute(query, params).fetchall()

        hyp_query = "SELECT * FROM hypotheses WHERE bot=?"
        hyp_params: list = [bot]
        if from_ts:
            hyp_query += " AND ts >= ?"
            hyp_params.append(from_ts)
        if to_ts:
            hyp_query += " AND ts <= ?"
            hyp_params.append(to_ts)
        hyps = conn.execute(hyp_query, hyp_params).fetchall()

        label = "custom range" if (from_ts or to_ts) else "lifetime"
        summary = _summarize(rows, label)
        summary["reflections_in_range"] = len(hyps)
        summary["reflections_detail"] = [
            {"pair": h["pair"], "variable": h["variable"], "old": h["old_value"],
             "new": h["new_value"], "reasoning": h["reasoning"]}
            for h in hyps
        ]

        result["bots"][bot] = summary

    conn.close()
    return result


@app.get("/api/skip-analysis/{bot_name}")
def skip_analysis(bot_name: str):
    if bot_name not in VALID_BOTS:
        raise HTTPException(404, "Unknown bot")
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM skips WHERE bot=? ORDER BY ts DESC LIMIT 200", (bot_name,)
    ).fetchall()
    conn.close()

    by_pair = {}
    for r in rows:
        raw = json.loads(r["raw_json"]) if r["raw_json"] else {}
        pair = r["pair"] or "unknown"
        reason = r["reason_skipped"] or raw.get("reason_skipped", "unknown")
        by_pair.setdefault(pair, {"total": 0, "reasons": {}, "missed_pnl_sum": 0, "missed_pnl_count": 0})
        by_pair[pair]["total"] += 1
        by_pair[pair]["reasons"][reason] = by_pair[pair]["reasons"].get(reason, 0) + 1
        mp = r["missed_pnl"] or raw.get("missed_pnl")
        if mp is not None:
            by_pair[pair]["missed_pnl_sum"] += mp
            by_pair[pair]["missed_pnl_count"] += 1

    return {"bot": bot_name, "by_pair": by_pair, "total_skips": len(rows)}


@app.get("/api/strategy-params/{bot_name}")
def strategy_params(bot_name: str):
    if bot_name not in VALID_BOTS:
        raise HTTPException(404, "Unknown bot")
    conn = get_conn()
    state_row = conn.execute("SELECT * FROM latest_state WHERE bot=?", (bot_name,)).fetchone()
    conn.close()
    if not state_row or not state_row["strategy_json"]:
        return {"bot": bot_name, "pairs": {}}
    strategy = json.loads(state_row["strategy_json"])
    return {"bot": bot_name, "pairs": strategy}


@app.get("/api/detailed-report/{bot_name}")
def detailed_report(
    bot_name: str,
    pair: Optional[str] = None,
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
):
    if bot_name not in VALID_BOTS:
        raise HTTPException(404, "Unknown bot")
    conn = get_conn()

    if not from_ts and not to_ts:
        from_ts = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    query = "SELECT * FROM trades WHERE bot=?"
    params: list = [bot_name]
    if pair:
        query += " AND pair=?"
        params.append(pair)
    if from_ts:
        query += " AND (entry_ts >= ? OR exit_ts >= ?)"
        params.extend([from_ts, from_ts])
    if to_ts:
        query += " AND (entry_ts <= ? OR exit_ts <= ?)"
        params.extend([to_ts, to_ts])
    query += " ORDER BY COALESCE(exit_ts, entry_ts) ASC"
    rows = conn.execute(query, params).fetchall()

    hyp_query = "SELECT * FROM hypotheses WHERE bot=?"
    hyp_params: list = [bot_name]
    if pair:
        hyp_query += " AND pair=?"
        hyp_params.append(pair)
    if from_ts:
        hyp_query += " AND ts >= ?"
        hyp_params.append(from_ts)
    if to_ts:
        hyp_query += " AND ts <= ?"
        hyp_params.append(to_ts)
    hyps = conn.execute(hyp_query, hyp_params).fetchall()

    skip_query = "SELECT * FROM skips WHERE bot=?"
    skip_params: list = [bot_name]
    if pair:
        skip_query += " AND pair=?"
        skip_params.append(pair)
    if from_ts:
        skip_query += " AND ts >= ?"
        skip_params.append(from_ts)
    if to_ts:
        skip_query += " AND ts <= ?"
        skip_params.append(to_ts)
    skips = conn.execute(skip_query, skip_params).fetchall()

    conn.close()

    closed = [r for r in rows if r["exit_reason"]]
    pnls = [r["pnl_pct"] for r in closed if r["pnl_pct"] is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls) if pnls else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    sharpe = (avg_win / abs(avg_loss)) if avg_loss != 0 else 0
    max_dd = min(pnls) if pnls else 0

    lines = [
        f"DETAILED REPORT — {bot_name.upper()}",
        f"Pair: {pair or 'All'}",
        f"Range: {from_ts[:19] if from_ts else '—'} → {to_ts[:19] if to_ts else 'now'}",
        f"Generated: {utcnow_iso()[:19]}",
        "=" * 50,
        "",
        f"Closed trades: {len(closed)}",
        f"Total P&L: {total_pnl:+.4f}%",
        f"Win rate: {win_rate:.1f}%",
        f"Avg win: {avg_win:+.4f}%",
        f"Avg loss: {avg_loss:+.4f}%",
        f"Sharpe proxy: {sharpe:.2f}",
        f"Max single loss: {max_dd:+.4f}%",
        "",
        "-" * 50,
        "TRADES:",
        "-" * 50,
    ]
    for r in rows:
        entry = r["entry_ts"][:19] if r["entry_ts"] else "—"
        exit_ = r["exit_ts"][:19] if r["exit_ts"] else "—"
        raw = json.loads(r["raw_json"]) if r["raw_json"] else {}
        strat_ver = raw.get("strategy_version", "?")
        pnl = r["pnl_pct"]
        pnl_str = f"{pnl:+.4f}%" if pnl is not None else "open"
        lines.append(
            f"  [{entry}] → [{exit_}] | "
            f"Entry: {r['entry_price']} | Exit: {r['exit_price']} | "
            f"PnL: {pnl_str} | {r['exit_reason'] or 'open'} | "
            f"Held: {r['hold_cycles']}c | v{strat_ver}"
        )

    if hyps:
        lines += ["", "-" * 50, "REFLECTIONS:", "-" * 50]
        for h in hyps:
            lines.append(
                f"  [{h['ts'][:19]}] {h['pair']}: {h['variable']} {h['old_value']} → {h['new_value']}"
            )
            if h["reasoning"]:
                lines.append(f"    Reasoning: {h['reasoning']}")

    if skips:
        lines += ["", "-" * 50, "SKIPS (last 20):", "-" * 50]
        for s in skips[:20]:
            lines.append(f"  [{s['ts'][:19]}] {s['pair']}: {s['reason_skipped']} @ {s['price_at_skip']}")

    return PlainTextResponse("\n".join(lines))


@app.get("/api/export-text", response_class=PlainTextResponse)
def export_text():
    conn = get_conn()
    now = utcnow_iso()
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"HERMES SUMMARY — {now[:19]}", f"Tracking since: {_first_trade_date(conn)}", "=" * 50, ""]
    for bot in VALID_BOTS:
        # System status
        state_row = conn.execute("SELECT * FROM latest_state WHERE bot=?",
            (bot,)).fetchone()
        strat = json.loads(state_row["strategy_json"]) if state_row and state_row["strategy_json"] else {}
        hb = json.loads(state_row["heartbeat_json"]) if state_row and state_row["heartbeat_json"] else {}
        open_json = json.loads(state_row["open_trades_json"]) if state_row and state_row["open_trades_json"] else []

        # Only include trades for valid pairs
        valid_pairs = list(strat.keys()) if strat else []
        rows = conn.execute(
            "SELECT * FROM trades WHERE bot=? ORDER BY COALESCE(exit_ts, entry_ts) ASC",
            (bot,),
        ).fetchall()
        if valid_pairs:
            rows = [r for r in rows if r["pair"] in valid_pairs]
        closed = [r for r in rows if r["exit_reason"]]
        pnls = [r["pnl_pct"] for r in closed if r["pnl_pct"] is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        # Pulse status
        pulse_ok = ""
        try:
            bs = conn.execute("SELECT desired_state FROM bot_status WHERE bot=?", (bot,)).fetchone()
            if bs:
                pulse_ok = f" ({bs['desired_state']})"
        except Exception:
            pass

        # Skip paused bots
        if "paused" in pulse_ok.lower():
            lines.append(f"## {bot.upper()} BOT (paused — excluded from summary)")
            lines.append("")
            lines.append("=" * 50)
            lines.append("")
            continue

        lines.append(f"## {bot.upper()} BOT{pulse_ok}")
        if hb:
            lines.append(f"Cycle: {hb.get('cycle','?')} | Regimes: {hb.get('regimes',{})}")
        lines.append("")

        # 1. LIFETIME stats
        total_pnl = sum(pnls) if pnls else 0
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        pf = abs(avg_win / avg_loss) if avg_loss != 0 else 0
        lines.append("─" * 40)
        lines.append("LIFETIME STATS")
        lines.append("─" * 40)
        lines.append(f"  Closed trades: {len(closed)} | Open: {len(rows) - len(closed)}")
        lines.append(f"  Total PnL: {total_pnl:+.4f}%")
        lines.append(f"  Win rate: {len(wins)/len(closed)*100:.1f}%" if closed else "  Win rate: N/A")
        lines.append(f"  Avg win: +{avg_win:.4f}% | Avg loss: {avg_loss:.4f}%")
        lines.append(f"  Profit factor: {pf:.3f}")
        lines.append("")

        # 2. BY PAIR LIFETIME
        by_pair = {}
        for r in closed:
            p = r["pair"] or "unknown"
            by_pair.setdefault(p, {"trades": 0, "pnl": 0, "wins": 0, "pairs_pnls": [], "stops": 0, "targets": 0, "timeouts": 0})
            by_pair[p]["trades"] += 1
            by_pair[p]["pnl"] += r["pnl_pct"] or 0
            by_pair[p]["pairs_pnls"].append(r["pnl_pct"] or 0)
            if (r["pnl_pct"] or 0) > 0:
                by_pair[p]["wins"] += 1
            if r["exit_reason"] == "stop_loss":
                by_pair[p]["stops"] += 1
            elif r["exit_reason"] == "profit_target":
                by_pair[p]["targets"] += 1
            elif r["exit_reason"] == "time_exit":
                by_pair[p]["timeouts"] += 1

        lines.append("─" * 40)
        lines.append("BY PAIR — LIFETIME")
        lines.append("─" * 40)
        header = f"  {'Pair':<12} {'Trades':<8} {'WR%':<8} {'PnL%':<10} {'AvgWin%':<10} {'AvgLoss%':<10} {'Stops':<7} {'Tgts':<7} {'Time':<7}"
        lines.append(header)
        lines.append("  " + "-" * (len(header) - 2))
        for p, d in sorted(by_pair.items()):
            pw = [x for x in d["pairs_pnls"] if x > 0]
            pl = [x for x in d["pairs_pnls"] if x <= 0]
            aw = sum(pw) / len(pw) if pw else 0
            al = sum(pl) / len(pl) if pl else 0
            wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            lines.append(f"  {p:<12} {d['trades']:<8} {wr:<7.1f}% {d['pnl']:<+9.4f}% {aw:<+9.4f}% {al:<+9.4f}% {d['stops']:<7} {d['targets']:<7} {d['timeouts']:<7}")
        lines.append("")

        # ── PER-VERSION PERFORMANCE ──
        versions = {}
        for r in closed:
            raw = json.loads(r["raw_json"]) if r["raw_json"] else {}
            # strategy_version is inside raw_json (the full trade record JSON)
            v = raw.get("strategy_version") or str(r.get("strategy_type", "") or "")
            if not v or v == "None":
                v = "?"
            if v not in versions:
                versions[v] = {"trades": 0, "pnls": [], "stops": 0, "targets": 0, "timeouts": 0}
            versions[v]["trades"] += 1
            pnl = r["pnl_pct"] if r["pnl_pct"] else 0
            versions[v]["pnls"].append(pnl)
            if r["exit_reason"] == "stop_loss":
                versions[v]["stops"] += 1
            elif r["exit_reason"] == "profit_target":
                versions[v]["targets"] += 1
            elif r["exit_reason"] == "time_exit":
                versions[v]["timeouts"] += 1
        lines.append("─" * 40)
        lines.append("PER-VERSION PERFORMANCE")
        lines.append("─" * 40)
        sorted_versions = sorted(versions.keys())
        for i, v in enumerate(sorted_versions):
            vs = versions[v]
            avg = sum(vs["pnls"]) / len(vs["pnls"]) if vs["pnls"] else 0
            wins = [p for p in vs["pnls"] if p > 0]
            wr = len(wins) / len(vs["pnls"]) * 100 if vs["pnls"] else 0
            total = sum(vs["pnls"])
            change = ""
            if i > 0:
                prev_v = sorted_versions[i - 1]
                prev_total = versions[prev_v]["pnls"]
                prev_avg = sum(prev_total) / len(prev_total) if prev_total else 0
                if avg > prev_avg:
                    change = " ▲ improved"
                elif avg < prev_avg:
                    change = " ▼ declined"
                else:
                    change = " → same"
            lines.append(f"  v{v:<4}: {vs['trades']:<3} trades | WR={wr:<5.1f}% | avg={avg:<+9.4f}% | total={total:<+10.4f}% | Stops={vs['stops']} Tgts={vs['targets']} Time={vs['timeouts']}{change}")
        lines.append("")

        # 3. BY PAIR RECENT (last 10)
        lines.append("─" * 40)
        lines.append("BY PAIR — LAST 10 TRADES")
        lines.append("─" * 40)
        for p, d in sorted(by_pair.items()):
            pair_rows = [r for r in closed if r["pair"] == p]
            recent = pair_rows[-10:]
            if not recent:
                continue
            rpnls = [r["pnl_pct"] for r in recent if r["pnl_pct"] is not None]
            rwins = [x for x in rpnls if x > 0]
            rlosses = [x for x in rpnls if x <= 0]
            rwr = len(rwins) / len(recent) * 100 if recent else 0
            rpnl = sum(rpnls) if rpnls else 0
            lwr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
            trend = "improving" if rwr > lwr + 5 else ("declining" if rwr < lwr - 5 else "stable")
            lines.append(f"  {p:<12} {len(recent):<5} trades | WR: {rwr:<5.1f}% | PnL: {rpnl:<+9.4f}% | Trend: {trend}")
            last5 = recent[-5:]
            for t in last5:
                lines.append(f"    {t['pnl_pct']:+.4f}% ({t['exit_reason']})")
        lines.append("")

        # 4. OPEN POSITIONS
        lines.append("─" * 40)
        lines.append("OPEN POSITIONS")
        lines.append("─" * 40)
        if open_json:
            for t in open_json:
                entry = t.get("entry_price", "?")
                ur = t.get("_unrealised_pct", 0)
                held = t.get("hold_cycles", "?")
                stop = t.get("_stop_price", 0)
                target = entry * 1.03 if isinstance(entry, (int, float)) else 0
                stop_dist = ((stop - entry) / entry * 100) if isinstance(entry, (int, float)) and entry and stop else 0
                tgt_dist = ((target - entry) / entry * 100) if isinstance(entry, (int, float)) and entry and target else 0
                pair = t.get("asset", "?")
                lines.append(f"  {pair}: Entry={entry} | Unrealised: {ur:+.4f}% | Held: {held}c")
                lines.append(f"    Stop: {stop} ({stop_dist:+.3f}%) | Target: {target:.5f} ({tgt_dist:+.3f}%)")
        else:
            lines.append("  None")
        lines.append("")

        # 5. CURRENT MARKET STATE (computed live from yfinance)
        lines.append("─" * 40)
        lines.append("CURRENT MARKET STATE")
        lines.append("─" * 40)
        regimes = hb.get("regimes", {})
        chart_ctxs = hb.get("chart_contexts", {}) or {}
        # Only show pairs that belong to this bot (from strategy config or regimes)
        if strat:
            market_pairs = [p for p in ["EUR/USD", "GBP/USD", "AUD/USD", "GBP/JPY", "XAU/USD", "XAG/USD"] if p in strat]
        else:
            market_pairs = list(regimes.keys()) or ["XAU/USD"]
        live_indicators = _fetch_live_indicators(market_pairs)
        for pair_name in market_pairs:
            li = live_indicators.get(pair_name, {})
            rsi = li.get("rsi", "?")
            adx = li.get("adx", "?")
            regime = regimes.get(pair_name, "?")
            bb_pos = li.get("bb_position", "?")
            bb_lower = li.get("bb_lower", "?")
            bb_mid = li.get("bb_mid", "?")
            bb_upper = li.get("bb_upper", "?")
            price = li.get("price", "?")
            chart = chart_ctxs.get(pair_name, "")
            chart_short = (chart[:120] + "...") if len(chart) > 120 else chart
            lines.append(f"  {pair_name}: RSI={rsi} ADX={adx} Regime={regime} BB={bb_pos}")
            if isinstance(bb_lower, float) and isinstance(price, float):
                lines.append(f"    Price={price:.5f} | BB: Lower={bb_lower:.5f} Mid={bb_mid:.5f} Upper={bb_upper:.5f}")
            if chart_short:
                lines.append(f"    Chart: {chart_short}")
        lines.append("")

        # 6. STRATEGY PARAMETERS
        lines.append("─" * 40)
        lines.append("STRATEGY PARAMETERS")
        lines.append("─" * 40)
        for pair_name, s in strat.items():
            if not isinstance(s, dict):
                continue
            st = s.get("strategy_type", "?")
            ver = s.get("version", "?")
            entry = s.get("entry", {})
            thresh = entry.get("mr_entry_rsi") or entry.get("threshold", "?")
            sl = s.get("stop_loss_pct", "?")
            pt = s.get("profit_target_pct", "?")
            atr_floor = s.get("use_atr_floor", "?")
            bear = s.get("allow_bear_entries", False)
            lines.append(f"  {pair_name}: {st} v{ver} | Thresh={thresh} | Stop={sl}% | Target={pt}% | Floor={atr_floor} | Bear={bear}")
        lines.append("")

        # 7. REFLECTIONS
        hyps = conn.execute("SELECT * FROM hypotheses WHERE bot=? ORDER BY ts DESC LIMIT 500", (bot,)).fetchall()
        if valid_pairs:
            hyps = [h for h in hyps if h["pair"] in valid_pairs]
        if hyps:
            lines.append("─" * 40)
            lines.append("REFLECTIONS")
            lines.append("─" * 40)
            for h in hyps:
                lines.append(f"  [{h['ts'][:19]}] {h['pair']}: {h['variable']} {h['old_value']} → {h['new_value']}")
                if h["reasoning"]:
                    lines.append(f"    Reasoning: {h['reasoning']}")
        lines.append("")

        # 8. TODAY section
        today_rows = conn.execute(
            "SELECT * FROM trades WHERE bot=? AND (entry_ts LIKE ? OR exit_ts LIKE ?) ORDER BY COALESCE(exit_ts, entry_ts) ASC",
            (bot, f"{today_str}%", f"{today_str}%"),
        ).fetchall()
        today_closed = [r for r in today_rows if r["exit_reason"]]
        t_pnls = [r["pnl_pct"] for r in today_closed if r["pnl_pct"] is not None]
        t_wins = [p for p in t_pnls if p > 0]
        today_total = sum(t_pnls) if t_pnls else 0
        today_wr = len(t_wins) / len(today_closed) * 100 if today_closed else 0

        lines.append("─" * 40)
        lines.append(f"TODAY ({today_str})")
        lines.append("─" * 40)
        lines.append(f"  Closed: {len(today_closed)} | PnL: {today_total:+.4f}% | WR: {today_wr:.1f}%")
        # Per-pair today breakdown
        today_by_pair = {}
        for r in today_closed:
            p = r["pair"] or "unknown"
            today_by_pair.setdefault(p, {"trades": 0, "pnl": 0, "wins": 0})
            today_by_pair[p]["trades"] += 1
            today_by_pair[p]["pnl"] += r["pnl_pct"] or 0
            if (r["pnl_pct"] or 0) > 0:
                today_by_pair[p]["wins"] += 1
        if today_by_pair:
            for p, d in sorted(today_by_pair.items()):
                wrp = d["wins"] / d["trades"] * 100 if d["trades"] else 0
                lines.append(f"    {p}: {d['trades']} trades | {d['pnl']:+.4f}% | {wrp:.1f}% WR")
        lines.append("")
        lines.append("=" * 50)
        lines.append("")

    conn.close()
    return "\n".join(lines)


def _first_trade_date(conn):
    row = conn.execute("SELECT entry_ts FROM trades ORDER BY entry_ts ASC LIMIT 1").fetchone()
    if row and row["entry_ts"]:
        return row["entry_ts"][:10]
    return "—"


def _fetch_live_indicators(pairs):
    """Compute RSI, ADX, BB for each pair from yfinance. Uses pure Python (no numpy)."""
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf

    def _rsi(prices, period=14):
        if len(prices) < period + 1: return None
        chg = prices[-1] - prices[-period-1]
        gains = sum(max(prices[i] - prices[i-1], 0) for i in range(-period, 0))
        losses = sum(max(prices[i-1] - prices[i], 0) for i in range(-period, 0))
        if losses == 0: return 100.0
        rs = gains / losses if losses else 0
        return round(100.0 - (100.0 / (1.0 + rs)), 1)

    def _adx(prices, period=14):
        if len(prices) < period * 2: return None
        up_sum = sum(max(prices[i] - prices[i-1], 0) for i in range(-period, 0))
        down_sum = sum(max(prices[i-1] - prices[i], 0) for i in range(-period, 0))
        tr_sum = sum(max(prices[i] - prices[i-1], 0) for i in range(-period, 0))
        atr = tr_sum / period if period else 0
        if atr == 0: return None
        plus_di = 100 * up_sum / period / atr
        minus_di = 100 * down_sum / period / atr
        dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di) if (plus_di + minus_di) > 0 else 0
        return round(dx, 1)

    def _bb(prices, period=20, std_dev=1.5):
        if len(prices) < period: return None, None, None, None
        recent = prices[-period:]
        ma = sum(recent) / period
        variance = sum((x - ma) ** 2 for x in recent) / period
        sd = variance ** 0.5
        lower = round(ma - std_dev * sd, 5)
        upper = round(ma + std_dev * sd, 5)
        bw = round(2 * std_dev * sd / ma * 100, 4) if ma > 0 else 0
        return round(ma, 5), upper, lower, bw

    result = {}
    for pair in pairs:
        ticker = PAIR_TICKERS.get(pair)
        if not ticker:
            result[pair] = {}
            continue
        try:
            df = yf.download(ticker, period="2d", interval="5m", progress=False, auto_adjust=True)
            if df is None or len(df) < 50:
                result[pair] = {}
                continue
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
            df = df.dropna(subset=["Close"]).copy()
            closes = [float(v) for v in df["Close"].values]
            price = round(closes[-1], 5)
            rsi = _rsi(closes)
            adx = _adx(closes)
            bm, bu, bl, bw = _bb(closes)

            bb_pos = "?"
            if bm and bl and bw is not None:
                if price <= bl * 1.001:
                    bb_pos = "touching lower"
                elif price <= (bl + bm) / 2:
                    bb_pos = "near lower"
                elif price >= bm * 0.999:
                    bb_pos = "at middle"
                elif price >= bu * 0.999:
                    bb_pos = "at upper"
                else:
                    bb_pos = "above middle"

            result[pair] = {
                "rsi": rsi if rsi is not None else "?",
                "adx": adx if adx is not None else "?",
                "price": price,
                "bb_position": bb_pos,
                "bb_lower": bl,
                "bb_mid": bm,
                "bb_upper": bu,
                "bb_bandwidth": bw,
            }
        except Exception as e:
            result[pair] = {"error": str(e)[:80]}
    return result


PAIR_MAP = {
    "EUR_USD": "EUR/USD", "GBP_USD": "GBP/USD",
    "AUD_USD": "AUD/USD", "GBP_JPY": "GBP/JPY",
    "XAU_USD": "XAU/USD", "XAG_USD": "XAG/USD",
}
SLASH_PAIRS = {v: k for k, v in PAIR_MAP.items()}

@app.get("/api/export-text/pair/{pair_name}", response_class=PlainTextResponse)
def export_pair(pair_name: str):
    pair = PAIR_MAP.get(pair_name, pair_name.replace("_", "/"))
    conn = get_conn()

    # Determine bot from pair: gold pairs vs forex pairs
    gold_pairs = {"XAU/USD", "XAG/USD"}
    bot = "gold" if pair in gold_pairs else "forex"
    trades = conn.execute("SELECT * FROM trades WHERE bot=? AND pair=? ORDER BY COALESCE(exit_ts, entry_ts) ASC", (bot, pair)).fetchall()
    # Fallback: if no data for guessed bot, try the other one
    if not trades:
        bot = "forex" if bot == "gold" else "gold"
        trades = conn.execute("SELECT * FROM trades WHERE bot=? AND pair=? ORDER BY COALESCE(exit_ts, entry_ts) ASC", (bot, pair)).fetchall()
    closed = [r for r in trades if r["exit_reason"]]
    pnls = [r["pnl_pct"] for r in closed if r["pnl_pct"] is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # State + strategy
    state_row = conn.execute("SELECT * FROM latest_state WHERE bot=?", (bot,)).fetchone()
    strat_all = json.loads(state_row["strategy_json"]) if state_row and state_row["strategy_json"] else {}
    hb = json.loads(state_row["heartbeat_json"]) if state_row and state_row["heartbeat_json"] else {}
    open_json = json.loads(state_row["open_trades_json"]) if state_row and state_row["open_trades_json"] else []
    s = strat_all.get(pair, {})
    entry_conf = s.get("entry", {})

    # Hypotheses + skips
    hyps = conn.execute("SELECT * FROM hypotheses WHERE bot=? AND pair=? ORDER BY ts DESC LIMIT 500", (bot, pair)).fetchall()
    skips = conn.execute("SELECT * FROM skips WHERE bot=? AND pair=? ORDER BY ts DESC", (bot, pair)).fetchall()
    first_trade = trades[0]["entry_ts"][:10] if trades else "—"
    now_str = utcnow_iso()[:19]
    conn.close()

    # Current open trade
    open_trade = None
    for t in open_json:
        if t.get("asset") == pair:
            open_trade = t
            break

    # Calculate stats
    total_pnl = sum(pnls) if pnls else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    pf = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    avg_hold = (sum([r["hold_cycles"] for r in closed if r["hold_cycles"]]) / len([r for r in closed if r["hold_cycles"]])) if closed and any(r["hold_cycles"] for r in closed) else 0
    max_dd = min(pnls) if pnls else 0
    pairs_pnls = [r["pnl_pct"] for r in closed if r["pnl_pct"] is not None]

    # Exit breakdown
    stops = sum(1 for r in closed if r["exit_reason"] == "stop_loss")
    targets = sum(1 for r in closed if r["exit_reason"] == "profit_target")
    timeouts = sum(1 for r in closed if r["exit_reason"] == "time_exit")

    lines = []
    # Header
    lines.append("=" * 50)
    lines.append(f"HERMES — {pair} PAIR ANALYSIS")
    lines.append(f"Generated: {now_str}")
    stype = s.get("strategy_type", "?")
    ver = s.get("version", "?")
    lines.append(f"Strategy: {stype} | Version: {ver}")
    lines.append("=" * 50)
    lines.append("")

    # CURRENT STATE
    lines.append("## CURRENT STATE")
    regimes = hb.get("regimes", {})
    live = hb.get("live_indicators", {}) or {}
    chart_ctxs = hb.get("chart_contexts", {}) or {}
    lv = live.get(pair, {})
    rsi = lv.get("rsi", "?")
    adx = lv.get("adx", "?")
    regime = regimes.get(pair, "?")
    bb_pos = lv.get("bb_position", "?")
    bb_lower = lv.get("bb_lower", "?")
    bb_mid = lv.get("bb_mid", "?")
    bb_upper = lv.get("bb_upper", "?")
    bb_bw = lv.get("bb_bandwidth", "?")
    bb_std = s.get("bb_std_dev", "1.5")
    chart_txt = chart_ctxs.get(pair, "") or lv.get("chart_context", "")
    sess = entry_conf.get("session_filter", "24h")
    in_sess = "yes" if sess == "24h" else "?"

    thresh = entry_conf.get("mr_entry_rsi") or entry_conf.get("threshold", "?")
    adx_th = s.get("adx_threshold", "?")
    bear_ok = s.get("allow_bear_entries", False)
    last_price = hb.get("last_price", "?")
    last_cycle = hb.get("ts", "?")[:19] if hb.get("ts") else "?"
    lines.append(f"  RSI: {rsi} | Threshold: {thresh}")
    lines.append(f"  ADX: {adx} | Threshold: {adx_th}")
    lines.append(f"  Regime: {regime} | Bear entries: {bear_ok}")
    lines.append(f"  BB Position: {bb_pos} | Lower={bb_lower} Mid={bb_mid} Upper={bb_upper}")
    lines.append(f"  BB Std Dev: {bb_std} | Bandwidth: {bb_bw}")
    lines.append(f"  Chart context: {chart_txt}")
    lines.append(f"  Session filter: {sess} | In session: {in_sess}")
    lines.append(f"  Last price: {last_price} | Last update: {last_cycle}")
    lines.append("")

    # OPEN POSITION
    lines.append("## OPEN POSITION")
    if open_trade:
        entry_px = open_trade.get("entry_price", "?")
        entry_ts = open_trade.get("entry_ts", "?")
        held = open_trade.get("hold_cycles", "?")
        ur = open_trade.get("_unrealised_pct", 0)
        stop_px = open_trade.get("_stop_price", 0)
        target_px = entry_px * 1.03 if isinstance(entry_px, (int, float)) else 0
        stop_dist = ((stop_px - entry_px) / entry_px * 100) if isinstance(entry_px, (int, float)) and entry_px else 0
        tgt_dist = ((target_px - entry_px) / entry_px * 100) if isinstance(entry_px, (int, float)) and entry_px else 0
        held_mins = f"{held}c ({held} min)" if held != "?" else "?"
        entry_rsi = open_trade.get("_entry_rsi", "?")
        entry_reg = open_trade.get("_entry_regime", "?")
        entry_q = open_trade.get("entry_quality_score", "?")
        entry_ver = open_trade.get("strategy_version", "?")
        lines.append(f"  Status: OPEN")
        lines.append(f"  Entry price: {entry_px}")
        lines.append(f"  Entry time: {entry_ts}")
        lines.append(f"  Hold time: {held_mins}")
        lines.append(f"  Unrealised PnL: {ur:+.4f}%")
        lines.append(f"  Stop price: {stop_px} | Distance to stop: {stop_dist:+.3f}%")
        lines.append(f"  Target price: {target_px:.5f} | Distance to target: {tgt_dist:+.3f}%")
        lines.append(f"  Entry RSI: {entry_rsi}")
        lines.append(f"  Entry regime: {entry_reg}")
        lines.append(f"  Entry quality score: {entry_q}/10")
        lines.append(f"  Strategy version at entry: {entry_ver}")
    else:
        lines.append("  Status: none")
    lines.append("")

    # LIFETIME PERFORMANCE
    lines.append("## LIFETIME PERFORMANCE")
    lines.append(f"  Total closed trades: {len(closed)}")
    lines.append(f"  Win rate: {len(wins)/len(closed)*100:.1f}%" if closed else "  Win rate: N/A")
    lines.append(f"  Total PnL: {total_pnl:+.4f}%")
    lines.append(f"  Avg win: +{avg_win:.4f}%")
    lines.append(f"  Avg loss: {avg_loss:.4f}%")
    lines.append(f"  Profit factor: {pf:.3f}x")
    lines.append(f"  Avg hold time: {avg_hold:.0f} cycles")
    lines.append(f"  Max drawdown: {max_dd:+.4f}%")
    lines.append(f"  Tracking since: {first_trade}")
    lines.append("")
    tot = len(closed)
    lines.append(f"  Exit breakdown:")
    lines.append(f"    Stop loss:     {stops:>4} trades ({stops/tot*100:.0f}%)" if tot else f"    Stop loss:     {stops:>4}")
    lines.append(f"    Profit target: {targets:>4} trades ({targets/tot*100:.0f}%)" if tot else f"    Profit target: {targets:>4}")
    lines.append(f"    Time exit:     {timeouts:>4} trades ({timeouts/tot*100:.0f}%)" if tot else f"    Time exit:     {timeouts:>4}")
    lines.append("")

    # RECENT vs LIFETIME COMPARISON
    lines.append("## RECENT vs LIFETIME COMPARISON")
    windows = {}
    for label, n in [("Last 5", 5), ("Last 10", 10), ("Last 20", 20), ("Lifetime", len(closed))]:
        subset = closed[-n:]
        sub_pnls = [r["pnl_pct"] for r in subset if r["pnl_pct"] is not None]
        sub_wins = [p for p in sub_pnls if p > 0]
        sub_wr = len(sub_wins) / len(subset) * 100 if subset else 0
        sub_avg = sum(sub_pnls) / len(sub_pnls) if sub_pnls else 0
        windows[label] = (sub_wr, sub_avg)
    lines.append(f"                 {'Last 5':>10} {'Last 10':>10} {'Last 20':>10} {'Lifetime':>10}")
    wr5, a5 = windows.get("Last 5", (0, 0))
    wr10, a10 = windows.get("Last 10", (0, 0))
    wr20, a20 = windows.get("Last 20", (0, 0))
    wrL, aL = windows.get("Lifetime", (0, 0))
    lines.append(f"  Win rate:      {wr5:>9.1f}% {wr10:>9.1f}% {wr20:>9.1f}% {wrL:>9.1f}%")
    lines.append(f"  Avg PnL/trade: {a5:>9.4f}% {a10:>9.4f}% {a20:>9.4f}% {aL:>9.4f}%")
    trend = "improving" if wr5 > wrL + 10 else ("declining" if wr5 < wrL - 10 else "stable")
    lines.append(f"  Trend:         {trend:>10}")
    lines.append("")

    # LAST 10 TRADES
    lines.append("## LAST 10 TRADES (most recent first)")
    lines.append(f"  # | {'Date':<12} | {'Entry':<10} | {'Exit':<10} | {'PnL%':<10} | {'Exit reason':<15} | {'Hold':<6} | {'RSI':<6} | {'Regime':<8}")
    lines.append(f"  {'-'*3} {'-'*12} {'-'*10} {'-'*10} {'-'*10} {'-'*15} {'-'*6} {'-'*6} {'-'*8}")
    for idx, r in enumerate(reversed(closed[-10:]), 1):
        d = (r["entry_ts"] or "?")[:10]
        en = r["entry_price"] or "?"
        ex = r["exit_price"] or "?"
        pnl = r["pnl_pct"] or 0
        reason = r["exit_reason"] or "?"
        held = r["hold_cycles"] or "?"
        raw = json.loads(r["raw_json"]) if r["raw_json"] else {}
        ersi = raw.get("entry_rsi", "?")
        ereg = raw.get("entry_regime", "?")
        lines.append(f"  {idx:<3} | {d:<12} | {str(en):<10} | {str(ex):<10} | {pnl:<+9.4f}% | {reason:<15} | {str(held):<6} | {str(ersi):<6} | {ereg:<8}")
    lines.append("")

    # STRATEGY PARAMETERS
    lines.append("## STRATEGY PARAMETERS (current)")
    flat_params = {
        "strategy_type": s.get("strategy_type"),
        "version": s.get("version"),
        "mr_entry_rsi": entry_conf.get("mr_entry_rsi"),
        "rsi threshold": entry_conf.get("threshold"),
        "bb_std_dev": s.get("bb_std_dev"),
        "stop_loss_pct": s.get("stop_loss_pct"),
        "profit_target_pct": s.get("profit_target_pct"),
        "trailing_stop_pct": s.get("trailing_stop_pct"),
        "time_exit_cycles": s.get("time_exit_cycles"),
        "atr_multiplier": s.get("atr_multiplier"),
        "use_atr_floor": s.get("use_atr_floor"),
        "atr_floor_pct": s.get("atr_floor_pct"),
        "allow_bear_entries": s.get("allow_bear_entries"),
        "adx_threshold": s.get("adx_threshold"),
        "vol_threshold_pct": s.get("vol_threshold_pct"),
        "session_filter": entry_conf.get("session_filter"),
        "position_size_r": s.get("position_size_r"),
    }
    for k, v in flat_params.items():
        if v is not None:
            lines.append(f"  {k}: {v}")
    lines.append("")

    # REFLECTION HISTORY
    lines.append("## REFLECTION HISTORY (all for this pair)")
    if hyps:
        for h in hyps:
            ts = h["ts"][:16] if h["ts"] else "?"
            var = h["variable"]
            ov = h["old_value"]
            nv = h["new_value"]
            lines.append(f"  [{ts}] {var} {ov}→{nv}")
            if h["reasoning"]:
                lines.append(f"    Reasoning: {h['reasoning']}")
    else:
        lines.append("  No reflections for this pair.")
    lines.append("")

    # SKIP ANALYSIS
    lines.append("## SKIP ANALYSIS (last 20 skips)")
    skip_reasons = {}
    for sk in skips[:20]:
        r = sk["reason_skipped"]
        skip_reasons[r] = skip_reasons.get(r, 0) + 1
    total_skips = sum(skip_reasons.values())
    lines.append("  Most common skip reasons:")
    if skip_reasons:
        for r, c in sorted(skip_reasons.items(), key=lambda x: -x[1]):
            pct = c / total_skips * 100
            lines.append(f"    {r:<25}: {c:>3} times ({pct:.0f}%)")
    lines.append("")
    lines.append("  Recent skips (last 5):")
    for sk in skips[:5]:
        ts = sk["ts"][:16] if sk["ts"] else "?"
        rsn = sk["reason_skipped"]
        rsi_val = sk["rsi_at_skip"] or "?"
        px = sk["price_at_skip"] or "?"
        lines.append(f"    [{ts}] {rsn} | RSI: {rsi_val} | Price: {px}")
    lines.append("")

    # WHAT WOULD TRIGGER AN ENTRY
    lines.append("## WHAT WOULD TRIGGER AN ENTRY RIGHT NOW")
    rsi_ok = isinstance(rsi, (int, float)) and isinstance(thresh, (int, float)) and rsi < thresh
    bb_ok = False
    try:
        lv_price = float(last_price) if last_price != "?" else 0
        lv_lower = float(bb_lower) if bb_lower != "?" else 0
        bb_ok = lv_price > 0 and lv_lower > 0 and lv_price <= lv_lower
    except (ValueError, TypeError):
        pass
    adx_ok = isinstance(adx, (int, float)) and isinstance(adx_th, (int, float)) and adx >= adx_th
    bear_blocked = regime == "BEAR" and not bear_ok

    conditions_met = sum([rsi_ok, bb_ok, adx_ok, not bear_blocked])
    if conditions_met == 4:
        lines.append("  READY TO ENTRY — all conditions met:")
        lines.append(f"    ✅ RSI {rsi} < threshold {thresh}")
        lines.append(f"    ✅ BB lower touch confirmed")
        lines.append(f"    ✅ ADX {adx} >= {adx_th}")
        lines.append(f"    ✅ Regime: {regime} (bear entries {'allowed' if bear_ok else 'blocked'})")
        lines.append("    → Entry would fire on next cycle")
    elif conditions_met >= 2:
        lines.append(f"  CLOSE TO ENTRY — {conditions_met} conditions met, {4 - conditions_met} blocking:")
        lines.append(f"    {'✅' if rsi_ok else '❌'} RSI {rsi} {'<' if rsi_ok else '>='} threshold {thresh}" + (f" (gap {rsi - thresh:.1f} pts)" if not rsi_ok else ""))
        lines.append(f"    {'✅' if bb_ok else '❌'} BB lower{' not yet ' if not bb_ok else ' '}touched" + (f" (price {last_price}, BB lower {bb_lower})" if not bb_ok else ""))
        lines.append(f"    {'✅' if adx_ok else '❌'} ADX {adx} {'>=' if adx_ok else '<'} {adx_th}" + (f" (gap {adx_th - adx:.1f})" if not adx_ok else ""))
        if regime == "BEAR":
            lines.append(f"    {'⚠' if bear_ok else '❌'} Bear entries: {'allowed' if bear_ok else 'blocked'}")
        lines.append(f"    → Nearest trigger: {'RSI drop to ' + str(thresh) if not rsi_ok else 'price touch BB lower' if not bb_ok else 'ADX rising to ' + str(adx_th)}")
    else:
        lines.append("  NOT CLOSE — blocked by:")
        lines.append(f"    ❌ RSI {rsi} >= threshold {thresh}" if not rsi_ok else "")
        lines.append(f"    ❌ BB lower not touched" if not bb_ok else "")
        lines.append(f"    ❌ ADX {adx} < {adx_th}" if not adx_ok else "")
        if not bear_ok and regime == "BEAR":
            lines.append(f"    ❌ Bear regime — bear entries blocked")
        lines.append(f"    → Furthest from entry on: RSI" if not rsi_ok else "BB touch" if not bb_ok else "ADX" if not adx_ok else "regime")

    lines.append("")
    lines.append("=" * 50)
    return "\n".join(lines)


# ───────────────────── Auth ─────────────────────


@app.get("/api/auth/status")
def auth_status():
    conn = get_conn()
    row = conn.execute("SELECT value FROM app_config WHERE key='dashboard_password'").fetchone()
    conn.close()
    return {"setup_required": row is None}


@app.post("/api/auth/setup")
async def auth_setup(request: Request):
    conn = get_conn()
    row = conn.execute("SELECT value FROM app_config WHERE key='dashboard_password'").fetchone()
    if row:
        conn.close()
        raise HTTPException(400, "Already configured")

    body = await request.json()
    password = body.get("password", "")
    confirm = body.get("confirm", "")

    if password != confirm:
        conn.close()
        raise HTTPException(400, "Passwords do not match")
    if len(password) < 6:
        conn.close()
        raise HTTPException(400, "Password too short")

    # Save password locally
    conn.execute("INSERT OR REPLACE INTO app_config (key, value) VALUES ('dashboard_password', ?)", (password,))
    conn.commit()

    # Generate auth token
    token = secrets.token_hex(32)
    now = utcnow_iso()
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    conn.execute("INSERT INTO auth_tokens (token, created_at, expires_at) VALUES (?, ?, ?)",
                 (token, now, expires))
    conn.commit()
    conn.close()

    return {"token": token, "message": "Password set"}


@app.post("/api/auth/login")
async def auth_login(request: Request):
    conn = get_conn()
    row = conn.execute("SELECT value FROM app_config WHERE key='dashboard_password'").fetchone()
    if not row:
        conn.close()
        raise HTTPException(400, "Setup required first")

    body = await request.json()
    password = body.get("password", "")

    if password != row["value"]:
        conn.close()
        raise HTTPException(401, "Incorrect password")

    token = secrets.token_hex(32)
    now = utcnow_iso()
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    conn.execute("INSERT INTO auth_tokens (token, created_at, expires_at) VALUES (?, ?, ?)",
                 (token, now, expires))
    conn.commit()
    conn.close()

    return {"token": token}


@app.get("/api/auth/verify")
def auth_verify(request: Request):
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return {"valid": False}

    token = auth_header[7:]
    now = utcnow_iso()

    conn = get_conn()
    # Cleanup expired tokens
    conn.execute("DELETE FROM auth_tokens WHERE expires_at < ?", (now,))
    conn.commit()

    row = conn.execute(
        "SELECT * FROM auth_tokens WHERE token=? AND expires_at > ?",
        (token, now),
    ).fetchone()
    conn.close()

    return {"valid": row is not None}


class ChatBody(BaseModel):
    question: str


@app.post("/api/chat")
def chat(body: ChatBody):
    """Answer a question about the bot's trading activity using available data."""
    question = body.question.strip().lower()
    if not question:
        raise HTTPException(400, "question is required")

    conn = get_conn()
    forex_state = conn.execute("SELECT * FROM latest_state WHERE bot='forex'").fetchone()
    gold_state = conn.execute("SELECT * FROM latest_state WHERE bot='gold'").fetchone()
    forex_trades = conn.execute("SELECT * FROM trades WHERE bot='forex' ORDER BY COALESCE(exit_ts, entry_ts) DESC LIMIT 10").fetchall()
    gold_trades = conn.execute("SELECT * FROM trades WHERE bot='gold' ORDER BY COALESCE(exit_ts, entry_ts) DESC LIMIT 10").fetchall()
    forex_strat = json.loads(forex_state["strategy_json"]) if forex_state and forex_state["strategy_json"] else {}
    forex_heartbeat = json.loads(forex_state["heartbeat_json"]) if forex_state and forex_state["heartbeat_json"] else {}
    forex_open = json.loads(forex_state["open_trades_json"]) if forex_state and forex_state["open_trades_json"] else []
    gold_open = json.loads(gold_state["open_trades_json"]) if gold_state and gold_state["open_trades_json"] else []
    conn.close()

    open_summary = [f"{t.get('asset','?')}: entry {t.get('entry_price','?')}, stop {t.get('_stop_price','?')}, held {t.get('hold_cycles','?')}c" for t in forex_open]
    gold_summary = [f"{t.get('asset','?')}: entry {t.get('entry_price','?')}, held {t.get('hold_cycles','?')}c" for t in gold_open]
    recent = [f"{t['pair']}: {t['pnl_pct']:+.4f}% ({t['exit_reason']})" for t in forex_trades[:5]]
    gold_recent = [f"{t['pair']}: {t['pnl_pct']:+.4f}% ({t['exit_reason']})" for t in gold_trades[:5]]
    regimes = forex_heartbeat.get("regimes", {})
    cycle = forex_heartbeat.get("cycle", "?")

    lines = []
    if "trade" in question and ("how long" in question or "duration" in question or "estimate" in question):
        if open_summary:
            lines.append("Currently open:")
            lines.extend(f"  • {s}" for s in open_summary)
            lines.append("Mean reversion trades typically exit within 1-6 hours when price bounces to the BB middle band.")
        else:
            lines.append("No open trades. Bot is waiting for entry conditions.")
            lines.append(f"Regimes: {regimes}")
    elif any(w in question for w in ["win rate", "performance", "profit", "how many"]):
        total = len(forex_trades)
        wins_f = sum(1 for t in forex_trades if t.get("pnl_pct", 0) > 0)
        wins_g = sum(1 for t in gold_trades if t.get("pnl_pct", 0) > 0)
        lines.append(f"Forex last {total} trades: {wins_f}/{total} wins ({wins_f/total*100:.0f}%)" if total else "No forex trades yet.")
        lines.extend(f"  {s}" for s in recent)
        if gold_trades:
            g_total = len(gold_trades)
            lines.append(f"Gold last {g_total} trades: {wins_g}/{g_total} wins ({wins_g/g_total*100:.0f}%)")
            lines.extend(f"  {s}" for s in gold_recent)
    elif "strategy" in question or "config" in question:
        for pair, s in forex_strat.items():
            if isinstance(s, dict):
                e = s.get("entry", {})
                lines.append(f"  {pair}: {s.get('strategy_type')}, RSI<{e.get('mr_entry_rsi') or e.get('threshold','?')}, stop {s.get('stop_loss_pct','?')}%, floor {s.get('atr_floor_pct','none')}")
    else:
        lines.append(f"Cycle {cycle}. {'No open trades.' if not open_summary else f'{len(forex_open)} open trade(s).'}")
        lines.append(f"Regimes: {regimes}")

    return {"answer": "\n".join(lines), "question": question}


@app.get("/api/per-version/{bot_name}")
def per_version_performance(bot_name: str, pair: Optional[str] = None):
    if bot_name not in VALID_BOTS:
        raise HTTPException(404, f"Unknown bot '{bot_name}'")
    # Only process trades for pairs that currently exist for this bot
    valid_pairs = set(PAIR_TICKERS.keys())
    if bot_name == "forex":
        valid_pairs = set(FOREX_PAIRS)
    elif bot_name == "gold":
        valid_pairs = {"XAU/USD", "XAG/USD"}

    conn = get_conn()
    query = "SELECT id, raw_json, pnl_pct, exit_reason, pair, entry_price FROM trades WHERE bot=? AND exit_reason IS NOT NULL"
    params = [bot_name]
    if pair:
        query += " AND pair=?"
        params.append(pair)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    rows = [dict(r) for r in rows]

    versions = {}
    pair_versions = {}
    for r in rows:
        # Quarantine: never count contaminated (bugged) trades in version stats.
        if r["id"] in CONTAMINATED_TRADE_IDS:
            continue
        # Safety net: drop gold-priced XAG/USD rows (see CONTAMINATED_* above).
        if (r["pair"] == CONTAMINATED_PAIR
                and (r.get("entry_price") or r.get("entry") or 0) > CONTAMINATED_ENTRY_MAX):
            continue
        p = r["pair"] or "?"
        if p not in valid_pairs:
            continue
        raw = json.loads(r["raw_json"]) if r["raw_json"] else {}
        v = raw.get("strategy_version") or str(r.get("strategy_type", "") or "")
        if not v or v == "None":
            v = "?"
        pnl = r["pnl_pct"] if r["pnl_pct"] else 0
        if v not in versions:
            versions[v] = {"trades": 0, "pnls": [], "wins": 0, "stops": 0, "targets": 0, "timeouts": 0, "pairs": set()}
        versions[v]["trades"] += 1
        versions[v]["pnls"].append(pnl)
        if pnl > 0:
            versions[v]["wins"] += 1
        if r["exit_reason"] == "stop_loss":
            versions[v]["stops"] += 1
        elif r["exit_reason"] == "profit_target":
            versions[v]["targets"] += 1
        elif r["exit_reason"] == "time_exit":
            versions[v]["timeouts"] += 1
        versions[v]["pairs"].add(p)
        pkey = f"{v}::{p}"
        if pkey not in pair_versions:
            pair_versions[pkey] = {"pair": p, "trades": 0, "pnls": [], "wins": 0, "stops": 0, "targets": 0, "timeouts": 0}
        pair_versions[pkey]["trades"] += 1
        pair_versions[pkey]["pnls"].append(pnl)
        if pnl > 0:
            pair_versions[pkey]["wins"] += 1
        if r["exit_reason"] == "stop_loss":
            pair_versions[pkey]["stops"] += 1
        elif r["exit_reason"] == "profit_target":
            pair_versions[pkey]["targets"] += 1
        elif r["exit_reason"] == "time_exit":
            pair_versions[pkey]["timeouts"] += 1

    result = []
    sorted_v = sorted(versions.keys())
    for i, v in enumerate(sorted_v):
        vs = versions[v]
        avg = sum(vs["pnls"]) / len(vs["pnls"]) if vs["pnls"] else 0
        wr = vs["wins"] / len(vs["pnls"]) * 100 if vs["pnls"] else 0
        total = sum(vs["pnls"])
        change = ""
        if i > 0:
            prev_v = sorted_v[i - 1]
            prev_avg = sum(versions[prev_v]["pnls"]) / len(versions[prev_v]["pnls"]) if versions[prev_v]["pnls"] else 0
            if avg > prev_avg:
                change = "improved"
            elif avg < prev_avg:
                change = "declined"
        pair_rows = []
        for pkey, pv in sorted(pair_versions.items()):
            if pkey.startswith(f"{v}::"):
                p_avg = sum(pv["pnls"]) / len(pv["pnls"]) if pv["pnls"] else 0
                p_wr = pv["wins"] / len(pv["pnls"]) * 100 if pv["pnls"] else 0
                p_total = sum(pv["pnls"])
                pair_rows.append({
                    "pair": pv["pair"], "trades": pv["trades"], "win_rate": round(p_wr, 1),
                    "wr_lower": wilson_score_interval(pv["wins"], len(pv["pnls"]))[0] if pv["pnls"] else 0,
                    "wr_upper": wilson_score_interval(pv["wins"], len(pv["pnls"]))[1] if pv["pnls"] else 0,
                    "low_confidence": len(pv["pnls"]) < 10,
                    "avg_pnl": round(p_avg, 4), "total_pnl": round(p_total, 4),
                    "stops": pv["stops"], "targets": pv["targets"], "timeouts": pv["timeouts"],
                })
        result.append({
            "version": v, "trades": vs["trades"], "win_rate": round(wr, 1),
            "wr_lower": wilson_score_interval(vs["wins"], len(vs["pnls"]))[0] if vs["pnls"] else 0,
            "wr_upper": wilson_score_interval(vs["wins"], len(vs["pnls"]))[1] if vs["pnls"] else 0,
            "low_confidence": len(vs["pnls"]) < 10,
            "avg_pnl": round(avg, 4), "total_pnl": round(total, 4),
            "stops": vs["stops"], "targets": vs["targets"], "timeouts": vs["timeouts"],
            "pairs": sorted(vs["pairs"]), "trend": change, "pair_breakdown": pair_rows,
        })
    return {"bot": bot_name, "versions": result}


# ═══════════════════════════════════════════════════════════════
# Discovered Indicators & Degradation Dashboard
# ═══════════════════════════════════════════════════════════════

@app.get("/api/discovered")
def discovered_dashboard():
    """Return all discovered indicators, degradation stats, and ensemble state per pair.
    Reads from both SQLite (Railway) and filesystem (dev) and merges results."""
    print("[DEBUG] /api/discovered route called at", datetime.now(timezone.utc), flush=True)

    pairs = {}
    deg_data = {}
    db_bots = []

    # ── Read from SQLite (Railway — pushed by bots) ──
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT bot, discovered_json, received_at FROM latest_state WHERE discovered_json IS NOT NULL AND discovered_json != '{}'"
        ).fetchall()
        conn.close()
        for r in rows:
            bot = r["bot"]
            db_bots.append(bot)
            try:
                disc = json.loads(r["discovered_json"])
                for pair_name, inds in disc.items():
                    if pair_name not in pairs:
                        pairs[pair_name] = []
                    for ind in inds:
                        pairs[pair_name].append(ind)
            except Exception:
                pass
        if db_bots:
            print(f"[DASHBOARD] Loaded discovered data from {len(db_bots)} bots in DB", flush=True)
    except Exception as e:
        print(f"[DASHBOARD] SQLite read failed: {e}", flush=True)

    # ── Fallback: filesystem (dev) ──
    state_dirs = [
        Path("/app/state/discovered"),
    ]
    discovered_dir = None
    for d in state_dirs:
        if d.exists():
            discovered_dir = d
            break
    if discovered_dir:
        deg_file = discovered_dir / "_live_deg.json"
        if deg_file.exists():
            try: deg_data = json.loads(deg_file.read_text())
            except: pass
        if not pairs:  # Only read filesystem if SQLite had nothing
            for f in sorted(discovered_dir.glob("*.json")):
                if f.name == "_live_deg.json": continue
                try:
                    data = json.loads(f.read_text())
                    pair_name = data.get("pair", f.stem.replace("_", "/"))
                    indicators = data.get("indicators", [])
                    enriched = []
                    for ind in indicators:
                        es = ind.get("expr_str", "")
                        enriched.append({
                            "name": ind.get("name", "?"),
                            "expr": es[:80],
                            "fitness": round(ind.get("fitness", 0), 4),
                            "win_rate": ind.get("win_rate", 0),
                            "total_pnl": round(ind.get("total_pnl", 0), 4),
                            "uses": [k for k in ["volume","dxy","vix","tnx","spx","oil","gold","btc","fvx","eem"] if k in es],
                            "discovered_at": ind.get("discovered_at", "unknown"),
                            "source": ind.get("source", "seed"),
                        })
                    if pair_name not in pairs:
                        pairs[pair_name] = enriched
                except: pass

    ensemble = {}
    for pair, inds in pairs.items():
        if not inds:
            ensemble[pair] = {"status": "no_indicators", "signal": 0}
            continue
        tw = sum((i["fitness"]*i["win_rate"]) for i in inds if i["fitness"]>0)
        bw = sum((i["fitness"]*i["win_rate"]) for i in inds if i.get("win_rate",0.5)>0.5)
        signal = ((bw - (tw-bw))/max(tw,0.001))
        ensemble[pair] = {
            "signal": round(signal, 3),
            "num_indicators": len(inds),
            "multi_dim": sum(1 for i in inds if i["uses"]),
            "best_fitness": max((i["fitness"] for i in inds), default=0),
            "best_wr": max((i["win_rate"] for i in inds), default=0),
        }

    return {
        "pairs": pairs,
        "ensemble": ensemble,
        "degradation": deg_data,
        "total_indicators": sum(len(v) for v in pairs.values()),
        "total_pairs": len(pairs),
    }


# ═══════════════════════════════════════════════════════════════
# Self-Audit System Endpoints
# ═══════════════════════════════════════════════════════════════

# Load findings_store — first try local copy (Railway deploy), then hermes-audit (dev)
_AUDIT_STORE = Path(__file__).resolve().parent / "findings_store.py"
_AUDIT_DIR = Path(__file__).resolve().parent if _AUDIT_STORE.exists() else Path(__file__).resolve().parent.parent / "hermes-audit"

try:
    if _AUDIT_STORE.exists():
        from findings_store import (
            list_findings, get_finding, update_finding_status,
            get_summary_stats, list_runs, get_maturity_history,
            get_latest_maturity_per_domain,
        )
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hermes-audit"))
        from findings_store import (
            list_findings, get_finding, update_finding_status,
            get_summary_stats, list_runs, get_maturity_history,
            get_latest_maturity_per_domain,
        )
    _AUDIT_AVAILABLE = True
    print("[AUDIT] findings_store loaded", flush=True)
except ImportError as e:
    print(f"[AUDIT] findings_store not available: {e}", flush=True)
    _AUDIT_AVAILABLE = False


@app.get("/api/audit/findings")
def audit_list_findings(
    status: str = None,
    domain: str = None,
    severity: str = None,
    type: str = None,
    limit: int = 50,
    offset: int = 0,
):
    """List audit findings with optional filters."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available (findings_store not found)"}
    results = list_findings(
        status=status, domain=domain, severity=severity,
        finding_type=type, limit=limit, offset=offset,
    )
    return {"status": "ok", "count": len(results), "findings": results}


@app.get("/api/version")
def version_marker():
    # TEMP: run the EXACT /api/cortex query and return raw rows
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT bot, cortex_json FROM latest_state WHERE cortex_json IS NOT NULL AND cortex_json != '{}'"
        ).fetchall()
        raw = {r["bot"]: (r["cortex_json"][:80] if r["cortex_json"] else None) for r in rows}
        conn.close()
        return {"version": "v-cortex-fix-2026-07-20", "filtered_rows": raw, "row_count": len(raw)}
    except Exception as e:
        return {"version": "v-cortex-fix-2026-07-20", "error": repr(e), "errtype": type(e).__name__}


@app.get("/api/cortex")
def cortex_dashboard():
    try:
        conn = get_conn()
        rows = conn.execute(
            "SELECT bot, cortex_json FROM latest_state WHERE cortex_json IS NOT NULL AND cortex_json != '{}'"
        ).fetchall()
        conn.close()
        r = {}
        for row in rows:
            try: r[row["bot"]] = json.loads(row["cortex_json"])
            except: pass
        return r if r else {"status": "no_data"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/audit/findings/{finding_id}")
def audit_get_finding(finding_id: str):
    """Get a single finding by ID."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available"}
    finding = get_finding(finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")
    return {"status": "ok", "finding": finding}


class AuditActionRequest(BaseModel):
    finding_id: str


@app.post("/api/audit/findings/{finding_id}/approve")
def audit_approve_finding(finding_id: str):
    """Mark a finding as approved (green-lit for human to apply)."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available"}
    finding = update_finding_status(finding_id, "approved")
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")
    return {"status": "ok", "finding": finding}


@app.post("/api/audit/findings/{finding_id}/reject")
def audit_reject_finding(finding_id: str):
    """Mark a finding as rejected (false positive or won't fix)."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available"}
    finding = update_finding_status(finding_id, "rejected")
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")
    return {"status": "ok", "finding": finding}


@app.post("/api/audit/findings/{finding_id}/apply")
def audit_apply_finding(finding_id: str):
    """Mark a finding as applied (fix has been implemented)."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available"}
    finding = update_finding_status(finding_id, "applied")
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")
    return {"status": "ok", "finding": finding}


@app.get("/api/audit/summary")
def audit_summary():
    """Return audit summary stats including maturity trends."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available"}
    stats = get_summary_stats()
    # Add maturity history per domain
    maturity_history = {}
    for domain in ["static-code", "strategy-logic", "data-logging-integrity",
                   "risk-safety-boundaries", "performance-drift"]:
        history = get_maturity_history(domain, limit=20)
        if history:
            maturity_history[domain] = [
                {"score": h["score"], "intelligence_score": h["intelligence_score"],
                 "compared_to_prior": h["compared_to_prior"],
                 "created_at": h["created_at"]}
                for h in history
            ]
    stats["maturity_history"] = maturity_history

    # Recent runs
    recent_runs = list_runs(limit=5)
    stats["recent_runs"] = [
        {
            "id": r["id"],
            "domains_run": json.loads(r.get("domains_run", "[]")),
            "findings_count": r["findings_count"],
            "critical_count": r["critical_count"],
            "created_at": r["created_at"],
        }
        for r in recent_runs
    ] if recent_runs else []
    return {"status": "ok", "stats": stats}


@app.get("/api/audit/maturity/{domain}")
def audit_maturity_history(domain: str):
    """Return maturity history for a specific domain."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available"}
    valid_domains = ["static-code", "strategy-logic", "data-logging-integrity",
                     "risk-safety-boundaries", "performance-drift"]
    if domain not in valid_domains:
        raise HTTPException(status_code=400, detail=f"Invalid domain. Valid: {valid_domains}")
    history = get_maturity_history(domain, limit=50)
    return {
        "status": "ok",
        "domain": domain,
        "history": [
            {
                "score": h["score"],
                "intelligence_score": h["intelligence_score"],
                "justification": h["justification"],
                "compared_to_prior": h["compared_to_prior"],
                "created_at": h["created_at"],
            }
            for h in history
        ],
    }


@app.get("/api/audit/runs")
def audit_list_runs(limit: int = 10):
    """List recent audit runs."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available"}
    runs = list_runs(limit=limit)
    return {
        "status": "ok",
        "runs": [
            {
                "id": r["id"],
                "domains_run": json.loads(r.get("domains_run", "[]")),
                "findings_count": r["findings_count"],
                "critical_count": r["critical_count"],
                "resolved_prior": json.loads(r.get("resolved_prior", "[]")),
                "regressions": json.loads(r.get("regressions", "[]")),
                "maturity_scores": json.loads(r.get("maturity_scores", "{}")),
                "created_at": r["created_at"],
            }
            for r in runs
        ],
    }


@app.post("/api/audit/run")
def audit_trigger_run():
    """Trigger an ad-hoc audit run with progress tracking."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available"}
    try:
        import sys, uuid
        from findings_store import create_audit_progress, update_audit_progress, get_audit_progress
        
        sys.path.insert(0, str(_AUDIT_DIR))
        from audit_runner import run_audit
        
        run_id = str(uuid.uuid4())[:12]
        create_audit_progress(run_id, domains_total=5)
        
        import threading
        def _run():
            try:
                update_audit_progress(run_id, status="running", progress_pct=5, message="Starting audit...")
                result = run_audit(domains=None, progress_id=run_id)
                update_audit_progress(run_id, status="complete", progress_pct=100, 
                                       domains_done=5, message=f"Complete — {result.get('total_findings', 0)} findings")
                print(f"[AUDIT] Ad-hoc run {run_id} complete: {result.get('total_findings', 0)} findings", flush=True)
            except Exception as e:
                update_audit_progress(run_id, status="error", message=str(e)[:200])
                print(f"[AUDIT] Ad-hoc run {run_id} failed: {e}", flush=True)
        threading.Thread(target=_run, daemon=True).start()
        return {"status": "ok", "run_id": run_id, "message": "Audit started"}
    except ImportError as e:
        return {"status": "error", "message": f"Could not start audit: {e}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/audit/progress/{run_id}")
def audit_get_progress(run_id: str):
    """Get current audit progress."""
    try:
        sys.path.insert(0, str(_AUDIT_DIR))
        from findings_store import get_audit_progress, get_latest_audit_progress
        prog = get_audit_progress(run_id) or {}
        return {"status": "ok", "progress": prog}
    except Exception as e:
        return {"status": "error", "message": str(e)}


class AskRequest(BaseModel):
    question: str
    quick: bool = False


@app.post("/api/ask")
async def ask_question(req: AskRequest):
    """Natural language interface (Layer 8). Ask questions about system health."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available"}
    try:
        sys.path.insert(0, str(_AUDIT_DIR))
        from nli import quick_answer, answer
        import asyncio

        if req.quick:
            ans = quick_answer(req.question)
            if ans:
                return {"status": "ok", "answer": ans, "mode": "rule"}
            return {"status": "ok", "answer": "No quick answer available for that question.", "mode": "rule"}

        # Full LLM-powered answer — run in thread to avoid blocking event loop
        try:
            r = await asyncio.wait_for(
                asyncio.to_thread(answer, req.question),
                timeout=25
            )
            return {
                "status": "ok",
                "answer": r.get("answer", "No answer generated"),
                "has_critical": r.get("has_critical", False),
                "critical_items": r.get("critical_items", []),
                "confidence": r.get("confidence", "low"),
                "mode": "llm",
            }
        except asyncio.TimeoutError:
            return {"status": "ok", "answer": "Query timed out. Try a simpler question or use --quick.", "mode": "llm"}
    except ImportError as e:
        return {"status": "error", "message": f"NLI not available: {e}"}


@app.post("/api/audit/findings/{finding_id}/patch")
def audit_generate_patch(finding_id: str):
    """Generate a code patch for an approved finding (Layer 7)."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available"}
    try:
        sys.path.insert(0, str(_AUDIT_DIR))
        from patch_generator import generate_patch
        # Check if we're on Railway (no local source files)
        _bot_paths_check = list((_AUDIT_DIR.parent / "hermes-forex" / "hermes_forex").glob("*.py")) if (_AUDIT_DIR.parent / "hermes-forex").exists() else []
        if not _bot_paths_check:
            return {"status": "error", "message": "Patch generation requires local filesystem access. Run the dashboard API locally to use this feature.", "patchable": False}
        result = generate_patch(finding_id)
        return {"status": "ok" if result.get("status") == "ok" else "error", **result}
    except ImportError as e:
        return {"status": "error", "message": f"Patch generator not available: {e}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/audit/findings/{finding_id}/patch")
def audit_view_patch(finding_id: str):
    """View the generated patch file for a finding."""
    _pdir = _AUDIT_DIR / "generated_patches"
    patch_file = _pdir / f"{finding_id}.patch"
    if not patch_file.exists():
        return {"status": "error", "message": "No patch generated for this finding"}
    try:
        content = patch_file.read_text(encoding="utf-8")
        return {"status": "ok", "patch": content, "filename": f"{finding_id}.patch"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/audit/correlate")
def audit_correlate():
    """Return PnL correlations for all findings (Layer 5)."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available"}
    try:
        sys.path.insert(0, str(_AUDIT_DIR))
        from findings_store import list_findings
        from outcome_correlator import correlate_finding

        findings = list_findings(limit=100)
        correlations = {}
        for f in findings:
            if f.get("domain") == "test-domain":
                continue
            r = correlate_finding(f)
            if r["potential_trades_found"] > 0 or r["confidence"] > 30:
                correlations[f["id"]] = {
                    "potential_trades_found": r["potential_trades_found"],
                    "total_pnl_impact": r["total_pnl_impact"],
                    "win_rate_impact": r["win_rate_impact"],
                }
        return {"status": "ok", "correlations": correlations}
    except ImportError as e:
        return {"status": "error", "message": f"Correlator not available: {e}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


class BulkActionRequest(BaseModel):
    action: str  # approve | reject | apply
    severity: str = None  # optional filter
    domain: str = None     # optional filter
    finding_type: str = None  # optional filter


class ProgressRequest(BaseModel):
    progress: int  # 0-100
    assignee: str = ""


@app.post("/api/audit/bulk-action")
def audit_bulk_action(req: BulkActionRequest):
    """Approve/reject/apply all findings matching filters."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available"}
    try:
        import findings_store as store

        # Map action to required current status
        status_map = {"approve": "pending", "reject": "pending", "apply": "approved"}
        required_status = status_map.get(req.action)
        if not required_status:
            return {"status": "error", "message": f"Invalid action: {req.action}"}

        # Get matching findings
        findings = store.list_findings(
            status=required_status,
            severity=req.severity,
            domain=req.domain,
            finding_type=req.finding_type,
            limit=500,
        )

        now = store._ts()
        conn = store._get_conn()
        count = 0
        try:
            for f in findings:
                new_status = {"approve": "approved", "reject": "rejected", "apply": "applied"}[req.action]
                conn.execute(
                    "UPDATE audit_findings SET status = ?, updated_at = ? WHERE id = ?",
                    (new_status, now, f["id"]),
                )
                count += 1
            conn.commit()
        finally:
            conn.close()

        return {"status": "ok", "action": req.action, "count": count,
                "filters": {"severity": req.severity, "domain": req.domain, "type": req.finding_type}}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/api/audit/findings/{finding_id}/progress")
def audit_update_progress(finding_id: str, req: ProgressRequest):
    """Update progress (0-100) and optional assignee for a finding."""
    if not _AUDIT_AVAILABLE:
        return {"status": "error", "message": "Audit system not available"}
    try:
        import findings_store as store
        finding = store.update_finding_progress(finding_id, req.progress, req.assignee)
        if not finding:
            return {"status": "error", "message": "Finding not found"}
        return {"status": "ok", "finding": finding,
                "progress": finding["progress"], "finding_status": finding["status"]}
    except Exception as e:
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8080)))

else:
    # Served via entrypoint.py (Railway single-image, HERMES_BOT_NAME=dashboard).
    # Mount the built frontend (dashboard/frontend/dist) at / so the dashboard
    # URL serves both the UI and the /api/* backend from one port.
    def run() -> None:
        import uvicorn
        from fastapi.staticfiles import StaticFiles
        from fastapi.responses import FileResponse

        dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
        if dist.exists():
            app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

            @app.get("/{full_path:path}")
            async def _spa(full_path: str):
                # /api/* routes are registered above and match first, so this
                # catch-all only ever sees non-asset UI paths → serve index.html.
                from fastapi.responses import FileResponse as _FR
                html = dist / "index.html"
                if html.exists():
                    return _FR(str(html))
                return None  # type: ignore[return-value]

        port = int(os.getenv("PORT", 8080))
        uvicorn.run(app, host="0.0.0.0", port=port)


# ── Data cleanup: remove gold pair trades/hypotheses erroneously stored under forex bot ──
try:
    conn = get_conn()
    dt = conn.execute("DELETE FROM trades WHERE bot='forex' AND pair IN ('XAU/USD', 'XAG/USD')").rowcount
    dh = conn.execute("DELETE FROM hypotheses WHERE bot='forex' AND pair IN ('XAU/USD', 'XAG/USD')").rowcount
    ds = conn.execute("DELETE FROM skips WHERE bot='forex' AND pair IN ('XAU/USD', 'XAG/USD')").rowcount
    if dt or dh or ds:
        conn.commit()
        print(f"[CLEANUP] Removed {dt} trades + {dh} hypotheses + {ds} skips (gold pairs under forex)", flush=True)
    conn.close()
except Exception:
    pass

# deploy-tag: xag-quarantine-v2 (force rebuild)
