"""Session 16 / Phase 16 tests for the dashboard backend API.

Network-free. The SQLite DB and HERMES_STATE are redirected to a temp dir via
an autouse fixture. The handler is driven in-process via main.test_client()
(no real socket). Both an INGEST_TOKEN and an empty-token rejection path are
exercised.

Blueprint exact names preserved:
  test_ingest_forex, test_no_collision, test_unknown_bot_404, test_overview_both,
  test_persist_restart
plus token rejection and explicit empty-tab handling.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import dashboard.backend.main as m

client = TestClient(m.app)


@pytest.fixture(autouse=True)
def _tmp_backend(tmp_path, monkeypatch):
    """Isolate the dashboard DB + HERMES_STATE and set a known ingest token."""
    db = tmp_path / "dashboard.db"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("DASHBOARD_DB", str(db))
    monkeypatch.setenv("HERMES_STATE", str(state))
    monkeypatch.setenv("INGEST_TOKEN", "secret-token")
    # reset module-level cached paths
    m.DB_PATH = str(db)
    m.STATE_DIR = str(state)
    m.INGEST_TOKEN = "secret-token"
    monkeypatch.setattr(m, "get_conn", m.get_conn)  # re-init below
    m.init_db()
    # Replicate the import-time migration (cortex_json/flatlined_json) that the
    # production DB has but init_db()'s base schema omits.
    conn = m.get_conn()
    for _col in ("discovered_json", "cortex_json", "flatlined_json", "open_trades_json"):
        try:
            conn.execute(f"ALTER TABLE latest_state ADD COLUMN {_col} TEXT DEFAULT '{{}}'")
        except Exception:
            pass
    conn.commit()
    conn.close()
    yield


VALID_RECORD = {
    "id": "T1", "pair": "EUR/USD", "entry_price": 1.10, "exit_price": 1.11,
    "pnl_pct": 0.9, "strategy_type": "gp_ensemble", "entry_regime": "BULL",
}
TOKEN = {"X-Ingest-Token": "secret-token"}


# ── blueprint Phase 16 success criteria ───────────────────────────────────
def test_ingest_forex():
    c = client
    r = c.post("/api/ingest/forex", json=VALID_RECORD, headers=TOKEN)
    assert r.status_code == 200
    trades = c.get("/api/trades/forex").json()
    assert any(t["id"] == VALID_RECORD["id"] for t in trades)


def test_no_collision():
    # two different bots, same id -> both retained, distinct (PK bot,id)
    c = client
    rec_x = dict(VALID_RECORD, id="X")
    c.post("/api/ingest/forex", json=rec_x, headers=TOKEN)
    c.post("/api/ingest/gold", json=dict(VALID_RECORD, id="X"), headers=TOKEN)
    fx = c.get("/api/trades/forex").json()
    gd = c.get("/api/trades/gold").json()
    assert any(t["id"] == "X" and t["bot"] == "forex" for t in fx)
    assert any(t["id"] == "X" and t["bot"] == "gold" for t in gd)
    # the forex "X" must NOT have been overwritten by gold's "X"
    fx_x = [t for t in fx if t["id"] == "X"]
    assert len(fx_x) == 1 and fx_x[0]["pair"] == "EUR/USD"


def test_unknown_bot_404():
    c = client
    r = c.post("/api/ingest/unknown_bot", json={}, headers=TOKEN)
    assert r.status_code == 404


def test_overview_both():
    c = client
    c.post("/api/ingest/forex", json=VALID_RECORD, headers=TOKEN)
    c.post("/api/ingest/gold", json=dict(VALID_RECORD, id="G1"), headers=TOKEN)
    o = c.get("/api/overview").json()
    assert "forex" in o and "gold" in o
    assert o["forex"]["trades"] >= 1
    assert o["gold"]["trades"] >= 1


def test_persist_restart():
    # ingest, then simulate a restart by building a fresh client (same DB file)
    c = client
    c.post("/api/ingest/forex", json=dict(VALID_RECORD, id="Y"), headers=TOKEN)
    c2 = m.test_client()   # new handler, same on-disk SQLite
    trades = c2.get("/api/trades/forex").json()
    assert any(t["id"] == "Y" for t in trades)


# ── auth + empty-tab handling ───────────────────────────────────────────────
def test_ingest_requires_token():
    c = client
    r = c.post("/api/ingest/forex", json=VALID_RECORD, headers={})
    assert r.status_code == 401


def test_unknown_bot_read_returns_empty_not_500():
    c = client
    assert c.get("/api/trades/unknown_bot").json() == []
    assert c.get("/api/discovered/unknown_bot").json() == []


def test_empty_tab_explicit_not_500():
    c = client
    assert c.get("/api/trades/forex").status_code == 200
    assert c.get("/api/trades/forex").json() == []   # no data yet, explicit []


def test_daily_and_lifetime_summary_not_500():
    """Regression: sqlite3.Row has no .get(); _summarize used r.get() and 500'd
    the Reports tab (/api/daily-summary). Dict-like rows must support both."""
    c = client
    c.post("/api/ingest/forex", json=VALID_RECORD, headers=TOKEN)
    d = c.get("/api/daily-summary")
    assert d.status_code == 200, d.text
    assert "bots" in d.json() and "forex" in d.json()["bots"]
    lt = c.get("/api/lifetime-summary")
    assert lt.status_code == 200, lt.text
    assert lt.json()["bots"]["forex"]["closed_trades"] >= 0


def test_gp_open_trade_surfaces_in_overview():
    """Regression: the bot pushes its live open_positions every cycle as
    recent_open_trades (carrying entry_type='gp_ensemble' for GP-brain
    entries). /api/overview must surface them with entry_type intact -- the
    prior cross-check against the trades table silently dropped every live
    open position (open positions are only written to trades on EXIT), so GP
    entries (and all live opens) never reached the dashboard.

    Self-contained: own temp DB + direct overview() call (no shared fixture).
    """
    import tempfile as _tf
    import json
    from datetime import datetime, timezone
    import dashboard.backend.main as mm

    d = _tf.mkdtemp()
    mm.DB_PATH = f"{d}/dash.db"
    mm.init_db()
    conn = mm.get_conn()
    for _col in ("discovered_json", "cortex_json", "flatlined_json", "open_trades_json"):
        try:
            conn.execute(f"ALTER TABLE latest_state ADD COLUMN {_col} TEXT DEFAULT '{{}}'")
        except Exception:
            pass

    ts = datetime.now(timezone.utc).isoformat()
    gp_open = {
        "id": "gold:XAU/USD:1700000000", "bot": "gold", "pair": "XAU/USD",
        "entry_type": "gp_ensemble", "entry_price": 4000.0, "size": 0.1,
        "entry_ts": ts, "stop_loss_pct": 1.5, "profit_target_pct": 3.0,
        "held_cycles": 3, "unrealised_pct": 0.32,
    }
    conn.execute(
        """INSERT INTO latest_state
           (bot, strategy_json, goal_json, heartbeat_json, open_trades_json,
            discovered_json, cortex_json, flatlined_json, received_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("gold",
         '{"XAU/USD": {"strategy_type": "rsi_momentum"}}',
         "{}", '{"prices": {"XAU/USD": 4013.0}}',
         json.dumps([gp_open]), "{}", "{}", "{}", ts),
    )
    conn.commit()
    o = mm.overview()
    open_trades = o["bots"]["gold"]["recent_open_trades"]
    gp = [t for t in open_trades if t.get("entry_type") == "gp_ensemble"]
    assert gp, f"GP open trade dropped from overview: {open_trades}"
    assert gp[0]["pair"] == "XAU/USD"
    assert gp[0].get("unrealised_pct") == 0.32


def test_overview_open_trades_not_filtered_by_age_and_no_ghost_fallback():
    """Live opens must survive >24h entry_ts and must NOT be inferred from
    trades-table rows missing exit_reason (ghost opens that inflated pair cards
    while PortfolioPulse under-counted).
    """
    import json
    import tempfile as _tf
    from datetime import datetime, timedelta, timezone

    import dashboard.backend.main as mm

    d = _tf.mkdtemp()
    mm.DB_PATH = f"{d}/dash.db"
    mm.init_db()
    conn = mm.get_conn()
    for _col in ("discovered_json", "cortex_json", "flatlined_json", "open_trades_json"):
        try:
            conn.execute(f"ALTER TABLE latest_state ADD COLUMN {_col} TEXT DEFAULT '{{}}'")
        except Exception:
            pass

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    opens = [
        {"id": "forex:EUR/USD:1", "pair": "EUR/USD", "entry_type": "gp_ensemble",
         "entry_ts": old_ts, "entry_price": 1.1, "held_cycles": 100},
        {"id": "forex:GBP/USD:1", "pair": "GBP/USD", "entry_type": "gp_ensemble",
         "entry_ts": old_ts, "entry_price": 1.3, "held_cycles": 80},
        {"id": "forex:AUD/USD:1", "pair": "AUD/USD", "entry_type": "mean_reversion",
         "entry_ts": old_ts, "entry_price": 0.7, "held_cycles": 50},
    ]
    # Ghost: trades row with no exit — must NOT appear as a live open.
    conn.execute(
        "INSERT INTO trades (id, bot, pair, entry_price, entry_ts, exit_reason, raw_json) "
        "VALUES (?,?,?,?,?,?,?)",
        ("ghost", "forex", "GBP/JPY", 1.5, old_ts, None,
         json.dumps({"id": "ghost", "pair": "GBP/JPY", "entry_type": "gp_ensemble"})),
    )
    conn.execute(
        """INSERT INTO latest_state
           (bot, strategy_json, goal_json, heartbeat_json, open_trades_json,
            discovered_json, cortex_json, flatlined_json, received_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        ("forex", json.dumps({"EUR/USD": {}, "GBP/USD": {}, "AUD/USD": {}, "GBP/JPY": {}}),
         "{}", "{}", json.dumps(opens), "{}", "{}", "{}",
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    o = mm.overview()
    live = o["bots"]["forex"]["recent_open_trades"]
    pairs = {t["pair"] for t in live}
    assert pairs == {"EUR/USD", "GBP/USD", "AUD/USD"}
    assert "GBP/JPY" not in pairs  # ghost excluded
    assert sum(1 for t in live if t.get("entry_type") == "gp_ensemble") == 2
