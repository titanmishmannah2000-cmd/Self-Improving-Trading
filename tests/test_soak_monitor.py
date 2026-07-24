"""Soak monitor — weekly digest + regression Discord alerts."""

from __future__ import annotations

import json
import time

import hermes_core.engines.soak_monitor as sm


def _write_hb(tmp_path, bot: str, *, age_s: float = 30.0):
    state = tmp_path / bot / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "heartbeat.json").write_text(
        json.dumps(
            {
                "ts": time.time() - age_s,
                "cycle": 9,
                "last_discovery_run_ts": "2026-07-24T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    (state / "trades.jsonl").write_text("", encoding="utf-8")
    return state


def test_evaluate_alerts_heartbeat_and_go_nogo():
    snap = {
        "bot": "forex",
        "heartbeat_age_s": 60 * 60,
        "go_nogo": False,
        "failed_checks": ["heartbeat_fresh"],
        "pairs": ["EUR/USD"],
        "pulses": {},
    }
    keys = {a["key"] for a in sm.evaluate_alerts(snap)}
    assert "heartbeat_stale" in keys
    assert "go_nogo_red" in keys


def test_evaluate_alerts_invent_signals():
    snap = {
        "bot": "crypto",
        "heartbeat_age_s": 10,
        "go_nogo": True,
        "failed_checks": [],
        "pairs": ["BTC/USD"],
        "pulses": {
            "BTC/USD": {
                "status": "chronic_timeout_backoff",
                "admitted": 0,
                "admit_zero_streak": 10,
                "timeout_streak": 4,
                "near_misses": 3,
                "age_s": 10.0,
            }
        },
    }
    keys = {a["key"] for a in sm.evaluate_alerts(snap)}
    assert "chronic_timeout:BTC/USD" in keys
    assert "admit_zero:BTC/USD" in keys


def test_run_once_persists_latch_and_dedupes(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_BOT_NAME", "forex")
    monkeypatch.setattr(sm, "SOAK_MONITOR_NOTIFY", False)
    monkeypatch.setattr(sm, "SOAK_WEEKLY_DIGEST_S", 10**9)  # no weekly
    monkeypatch.setattr(sm, "SOAK_HB_ALERT_AGE_S", 60)
    _write_hb(tmp_path, "forex", age_s=5)

    monkeypatch.setattr(
        sm,
        "collect_bot_snapshot",
        lambda bot: {
            "bot": bot,
            "heartbeat_age_s": 5.0,
            "go_nogo": True,
            "failed_checks": [],
            "pairs": ["EUR/USD"],
            "pulses": {
                "EUR/USD": {
                    "status": "admit_zero",
                    "admitted": 0,
                    "admit_zero_streak": 9,
                    "timeout_streak": 0,
                    "near_misses": 2,
                    "age_s": 100.0,
                }
            },
            "cycle": 1,
        },
    )
    monkeypatch.setattr(sm, "SOAK_ADMIT_ZERO_ALERT", 8)

    out1 = sm.run_once("forex")
    assert "admit_zero:EUR/USD" in out1["sent"]
    out2 = sm.run_once("forex")
    assert out2["sent"] == []  # deduped while condition still open

    latch = json.loads((tmp_path / "forex" / "state" / "soak_monitor.json").read_text())
    assert "admit_zero:EUR/USD" in latch["active_alert_keys"]


def test_weekly_digest_formats(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(sm, "SOAK_MONITOR_NOTIFY", False)
    sent: list[str] = []

    def fake_notify(msg, *, bot, guard):
        sent.append(msg)
        return True

    monkeypatch.setattr(sm, "_notify", fake_notify)
    monkeypatch.setattr(
        sm,
        "collect_bot_snapshot",
        lambda bot: {
            "bot": bot,
            "heartbeat_age_s": 12.0,
            "go_nogo": True,
            "failed_checks": [],
            "pairs": ["XAU/USD"],
            "pulses": {
                "XAU/USD": {
                    "status": "ok",
                    "admitted": 2,
                    "admit_zero_streak": 0,
                    "near_misses": 1,
                    "age_s": 300.0,
                }
            },
            "cycle": 42,
        },
    )
    out = sm.run_once("gold", force_weekly=True)
    assert out["weekly_sent"] is True
    assert any("soak-weekly" in m for m in sent)
    assert any("XAU/USD" in m for m in sent)
