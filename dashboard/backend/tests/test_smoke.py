"""
Backend smoke test — verifies every dashboard endpoint the frontend consumes
boots and returns 200 against a fresh in-memory SQLite DB.

Run:  pytest backend/tests/test_smoke.py
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Use an isolated DB so the test never touches real data.
_DB = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = _DB

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(main.app)


# ── Static / health ────────────────────────────────────────────────
def test_health():
    assert client.get("/api/health").status_code == 200


def test_quick_test():
    assert client.get("/api/quick-test").status_code == 200


# ── Core dashboard endpoints (Live / Activity / Reports) ──────────
@pytest.mark.parametrize(
    "ep",
    [
        "/api/overview",
        "/api/discovered",
        "/api/cortex",
        "/api/heartbeat/forex",
        "/api/price/forex",
        "/api/daily-summary",
        "/api/lifetime-summary",
        "/api/skip-analysis/forex",
        "/api/strategy-params/forex",
        "/api/per-version/forex",
        "/api/bot/forex/pulse",
        "/api/alerts",
        "/api/auth/status",
    ],
)
def test_core_get_endpoints(ep):
    r = client.get(ep)
    assert r.status_code == 200, f"{ep} -> {r.status_code}: {r.text[:200]}"


# ── Audit endpoints ───────────────────────────────────────────────
@pytest.mark.parametrize(
    "ep",
    [
        "/api/audit/findings",
        "/api/audit/summary",
        "/api/audit/runs",
        "/api/audit/correlate",
    ],
)
def test_audit_get_endpoints(ep):
    r = client.get(ep)
    assert r.status_code == 200, f"{ep} -> {r.status_code}: {r.text[:200]}"


# ── Live ingest + ask (POST paths) ────────────────────────────────
def test_ingest_and_overview_populates():
    payload = {
        "recent_trades": [
            {"id": "t1", "pair": "EUR/USD", "pnl_pct": 1.1, "exit_reason": "tp", "hold_cycles": 3},
        ],
        "recent_skips": [{"pair": "AUD/USD", "reason_skipped": "no_setup"}],
        "recent_hypotheses": [],
    }
    assert client.post("/api/ingest/forex", json=payload).status_code == 200
    ov = client.get("/api/overview").json()
    assert len(ov["bots"]["forex"]["recent_trades"]) == 1
    assert len(ov["bots"]["forex"]["recent_skips"]) == 1


def test_ask_degrades_without_llm_key():
    # sentinel.check_live_anomalies must not crash on missing monitor_alerts table
    r = client.post("/api/ask", json={"question": "why this trade?"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # Without an LLM key it must not 500; it reports unconfigured gracefully
    assert "not configured" in body.get("answer", "").lower()


def test_auth_setup_and_login():
    pw = "smoketest123"
    s = client.post("/api/auth/setup", json={"password": pw, "confirm": pw})
    assert s.status_code == 200 and "token" in s.json()
    login = client.post("/api/auth/login", json={"password": pw})
    assert login.status_code == 200 and "token" in login.json()
