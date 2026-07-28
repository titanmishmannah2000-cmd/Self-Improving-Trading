"""Unit tests for profitability health snapshot builder."""

from __future__ import annotations

import json
import sqlite3
import time

from profitability_health import build_profitability_health, _freeze_from_heartbeat, _price_sane


def _mem_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE latest_state (
            bot TEXT PRIMARY KEY,
            heartbeat_json TEXT,
            open_trades_json TEXT,
            strategy_json TEXT,
            goal_json TEXT,
            flatlined_json TEXT,
            received_at TEXT,
            gp_promote_gate_json TEXT
        );
        CREATE TABLE trades (
            id TEXT,
            bot TEXT,
            pair TEXT,
            pnl_pct REAL,
            exit_reason TEXT,
            entry_ts TEXT,
            exit_ts TEXT
        );
        """
    )
    return conn


def test_freeze_ok_book_risk_only():
    hb = {
        "hif_flags": {
            "flags": {
                "BOOK_RISK": True,
                "SOFT_WEIGHTS": False,
                "KELLY_SIZING": False,
                "REGIME_SIZING": False,
                "ENTRY_RANKING": False,
                "EXIT_INTEL": False,
                "PROBE_SIZING": False,
                "SKIP_SHADOW_REFLECT": False,
                "SKIP_SHADOW_PROMOTE": False,
                "CRISIS_RECOMMEND": False,
                "GP_PROMOTE": False,
            },
            "enabled": ["BOOK_RISK"],
        }
    }
    f = _freeze_from_heartbeat(hb)
    assert f["ok"] is True
    assert f["enabled"] == ["BOOK_RISK"]


def test_price_sane_rejects_stub_gold():
    assert _price_sane("XAU/USD", 1.1) is False
    assert _price_sane("XAU/USD", 2400.0) is True


def test_build_ok_fleet():
    now = time.time()
    conn = _mem_conn()

    def good_hb(prices):
        return {
            "ts": now - 30,
            "status": "ok",
            "cycle": 10,
            "prices": prices,
            "hif_flags": {
                "flags": {
                    "BOOK_RISK": True,
                    "SOFT_WEIGHTS": False,
                    "KELLY_SIZING": False,
                    "REGIME_SIZING": False,
                    "ENTRY_RANKING": False,
                    "EXIT_INTEL": False,
                    "PROBE_SIZING": False,
                    "SKIP_SHADOW_REFLECT": False,
                    "SKIP_SHADOW_PROMOTE": False,
                    "CRISIS_RECOMMEND": False,
                    "GP_PROMOTE": False,
                },
                "enabled": ["BOOK_RISK"],
            },
        }

    for bot, prices in (
        ("forex", {"EUR/USD": 1.08, "GBP/USD": 1.27}),
        ("gold", {"XAU/USD": 2350.0}),
        ("crypto", {"BTC/USD": 65000.0, "ETH/USD": 3200.0}),
    ):
        conn.execute(
            "INSERT INTO latest_state (bot, heartbeat_json, open_trades_json) VALUES (?,?,?)",
            (bot, json.dumps(good_hb(prices)), "[]"),
        )
    conn.commit()

    report = build_profitability_health(get_conn=lambda: conn, now=now)
    assert report["level"] == "ok"
    assert report["what_to_report"] == []
    assert all(report["bots"][b]["level"] == "ok" for b in ("forex", "gold", "crypto"))


def test_build_fail_insane_price():
    now = time.time()
    conn = _mem_conn()
    hb = {
        "ts": now - 10,
        "status": "ok",
        "prices": {"XAU/USD": 1.1},
        "hif_flags": {
            "flags": {"BOOK_RISK": True, "GP_PROMOTE": False},
            "enabled": ["BOOK_RISK"],
        },
    }
    # Minimal flags still need all OFF keys absent/false — GP_PROMOTE False ok;
    # missing OFF keys in flags dict means they're not enabled (good).
    # But PHASE0_OFF checks flags.get(key) — missing is Fine.
    # PHASE0_ON BOOK_RISK True — ok.
    # enabled from flags with truthy only = [BOOK_RISK] — ok.
    conn.execute(
        "INSERT INTO latest_state (bot, heartbeat_json, open_trades_json) VALUES (?,?,?)",
        ("gold", json.dumps(hb), "[]"),
    )
    # Other bots missing → fail
    report = build_profitability_health(get_conn=lambda: conn, now=now, bots=("gold",))
    assert report["level"] == "fail"
    assert any(i["code"] == "insane_price" for i in report["issues"])
