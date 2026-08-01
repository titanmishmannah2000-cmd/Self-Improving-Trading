"""DASHBOARD_BOTS scopes the dashboard to a subset of bots (BTC/USDT project)."""

from __future__ import annotations

import importlib


def test_ui_config_defaults_to_all_bots(monkeypatch):
    monkeypatch.delenv("DASHBOARD_BOTS", raising=False)
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    import dashboard.backend.main as m

    importlib.reload(m)
    assert m.VALID_BOTS == {"forex", "gold", "crypto", "btc"}
    from fastapi.testclient import TestClient

    c = TestClient(m.app)
    r = c.get("/api/ui-config")
    assert r.status_code == 200
    body = r.json()
    assert set(body["bots"]) == {"forex", "gold", "crypto", "btc"}
    assert body["scope"] == "multi"


def test_ui_config_btc_only(monkeypatch):
    monkeypatch.setenv("DASHBOARD_BOTS", "btc")
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    import dashboard.backend.main as m

    importlib.reload(m)
    assert m.VALID_BOTS == {"btc"}
    from fastapi.testclient import TestClient

    c = TestClient(m.app)
    r = c.get("/api/ui-config")
    assert r.status_code == 200
    body = r.json()
    assert body["bots"] == ["btc"]
    assert body["scope"] == "btc"
    assert body["focus_pairs"]["btc"] == ["BTC/USDT"]
    ov = c.get("/api/overview").json()
    assert ov["active_bots"] == ["btc"]
    assert set(ov["bots"].keys()) == {"btc"}
