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

import dashboard.backend.main as m


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
    yield


VALID_RECORD = {
    "id": "T1", "pair": "EUR/USD", "entry_price": 1.10, "exit_price": 1.11,
    "pnl_pct": 0.9, "strategy_type": "gp_ensemble", "entry_regime": "BULL",
}
TOKEN = {"X-Ingest-Token": "secret-token"}


# ── blueprint Phase 16 success criteria ───────────────────────────────────
def test_ingest_forex():
    c = m.test_client()
    r = c.post("/api/ingest/forex", json=VALID_RECORD, headers=TOKEN)
    assert r.status_code == 200
    trades = c.get("/api/trades/forex").json
    assert any(t["id"] == VALID_RECORD["id"] for t in trades)


def test_no_collision():
    # two different bots, same id -> both retained, distinct (PK bot,id)
    c = m.test_client()
    rec_x = dict(VALID_RECORD, id="X")
    c.post("/api/ingest/forex", json=rec_x, headers=TOKEN)
    c.post("/api/ingest/gold", json=dict(VALID_RECORD, id="X"), headers=TOKEN)
    fx = c.get("/api/trades/forex").json
    gd = c.get("/api/trades/gold").json
    assert any(t["id"] == "X" and t["bot"] == "forex" for t in fx)
    assert any(t["id"] == "X" and t["bot"] == "gold" for t in gd)
    # the forex "X" must NOT have been overwritten by gold's "X"
    fx_x = [t for t in fx if t["id"] == "X"]
    assert len(fx_x) == 1 and fx_x[0]["pair"] == "EUR/USD"


def test_unknown_bot_404():
    c = m.test_client()
    r = c.post("/api/ingest/unknown_bot", json={}, headers=TOKEN)
    assert r.status_code == 404


def test_overview_both():
    c = m.test_client()
    c.post("/api/ingest/forex", json=VALID_RECORD, headers=TOKEN)
    c.post("/api/ingest/gold", json=dict(VALID_RECORD, id="G1"), headers=TOKEN)
    o = c.get("/api/overview").json
    assert "forex" in o and "gold" in o
    assert o["forex"]["trades"] >= 1
    assert o["gold"]["trades"] >= 1


def test_persist_restart():
    # ingest, then simulate a restart by building a fresh client (same DB file)
    c = m.test_client()
    c.post("/api/ingest/forex", json=dict(VALID_RECORD, id="Y"), headers=TOKEN)
    c2 = m.test_client()   # new handler, same on-disk SQLite
    trades = c2.get("/api/trades/forex").json
    assert any(t["id"] == "Y" for t in trades)


# ── auth + empty-tab handling ───────────────────────────────────────────────
def test_ingest_requires_token():
    c = m.test_client()
    r = c.post("/api/ingest/forex", json=VALID_RECORD, headers={})
    assert r.status_code == 401


def test_unknown_bot_read_returns_empty_not_500():
    c = m.test_client()
    assert c.get("/api/trades/unknown_bot").json == []
    assert c.get("/api/discovered/unknown_bot").json == []


def test_empty_tab_explicit_not_500():
    c = m.test_client()
    assert c.get("/api/trades/forex").status_code == 200
    assert c.get("/api/trades/forex").json == []   # no data yet, explicit []
