"""DASHBOARD_BOTS scopes the dashboard to a subset of bots (BTC/USDT project)."""

from __future__ import annotations

import importlib


def test_ui_config_defaults_to_all_bots(monkeypatch):
    monkeypatch.delenv("DASHBOARD_BOTS", raising=False)
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    import dashboard.backend.main as m

    importlib.reload(m)
    assert m.VALID_BOTS == {"forex", "gold", "crypto"}
    from fastapi.testclient import TestClient

    c = TestClient(m.app)
    r = c.get("/api/ui-config")
    assert r.status_code == 200
    body = r.json()
    assert set(body["bots"]) == {"forex", "gold", "crypto"}
    assert body["scope"] == "multi"


def test_ui_config_crypto_only(monkeypatch):
    monkeypatch.setenv("DASHBOARD_BOTS", "crypto")
    monkeypatch.setenv("INGEST_TOKEN", "tok")
    import dashboard.backend.main as m

    importlib.reload(m)
    assert m.VALID_BOTS == {"crypto"}
    from fastapi.testclient import TestClient

    c = TestClient(m.app)
    r = c.get("/api/ui-config")
    assert r.status_code == 200
    body = r.json()
    assert body["bots"] == ["crypto"]
    assert body["scope"] == "btc"
    assert body["focus_pairs"]["crypto"] == ["BTC/USDT"]
    ov = c.get("/api/overview").json()
    assert ov["active_bots"] == ["crypto"]
    assert set(ov["bots"].keys()) == {"crypto"}
