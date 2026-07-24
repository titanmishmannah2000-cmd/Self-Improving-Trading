"""GP promote gate — ban / unban / hysteresis / min samples / cooldown."""

from __future__ import annotations

import pytest

from hermes_core.engines import gp_promote_gate as gpg


@pytest.fixture
def gate_env(tmp_path, monkeypatch):
    monkeypatch.setattr(gpg, "bot_state_dir", lambda _b: tmp_path)
    monkeypatch.setenv("GP_EXCLUDE_PAIRS", "GBP/JPY,BTC/USD")
    monkeypatch.setenv("GP_PROMOTE_GATE_MIN_SAMPLES", "20")
    monkeypatch.setenv("GP_PROMOTE_GATE_BAN", "-0.05")
    monkeypatch.setenv("GP_PROMOTE_GATE_UNBAN", "0.05")
    monkeypatch.setenv("GP_PROMOTE_GATE_COOLDOWN_S", "3600")
    monkeypatch.setenv("GP_PROMOTE_GATE_WINDOW", "100")
    monkeypatch.setenv("GP_PROMOTE_GATE_SHADOW_HORIZON_S", "100")
    return tmp_path


def test_env_seeds_initial_bans(gate_env):
    assert gpg.is_promote_allowed("forex", "BTC/USD") is False
    assert gpg.is_promote_allowed("forex", "GBP/JPY") is False
    assert gpg.is_promote_allowed("forex", "EUR/USD") is True
    st = gpg.load_state("forex")
    assert st["pairs"]["BTC/USD"]["seeded_from_env"] is True
    assert (gate_env / "gp_promote_gate.json").exists()


def test_ban_when_expectancy_bad(gate_env):
    # Fresh non-excluded pair → allowed until evidence says otherwise.
    assert gpg.is_promote_allowed("forex", "ETH/USD") is True
    out = gpg.refresh_from_pnls(
        "forex",
        "ETH/USD",
        [-0.2] * 25,
        now=1_000_000.0,
    )
    assert out["banned"] is True
    assert out["reason"] == "ban_expectancy"
    assert gpg.is_promote_allowed("forex", "ETH/USD") is False


def test_unban_when_expectancy_good(gate_env):
    # Seeded ban, then strong positive samples past min_n → unban.
    assert gpg.is_banned("forex", "BTC/USD") is True
    out = gpg.refresh_from_pnls(
        "forex",
        "BTC/USD",
        [0.25] * 25,
        now=1_000_000.0,
    )
    assert out["banned"] is False
    assert out["reason"] == "unban_expectancy"
    assert gpg.is_promote_allowed("forex", "BTC/USD") is True
    st = gpg.load_state("forex")
    assert st["pairs"]["BTC/USD"]["seeded_from_env"] is False


def test_hysteresis_dead_zone_holds_state(gate_env):
    # Ban first with clearly bad expectancy.
    gpg.refresh_from_pnls("forex", "ETH/USD", [-0.3] * 25, now=1_000.0)
    assert gpg.is_banned("forex", "ETH/USD") is True

    # After cooldown, expectancy in the dead zone (−0.05, +0.05) → stay banned.
    out = gpg.refresh_from_pnls(
        "forex",
        "ETH/USD",
        [0.0] * 25,
        now=1_000.0 + 10_000.0,
    )
    assert out["banned"] is True
    assert out["reason"] == "hold_banned"

    # Unban with strong positive, then dead-zone again → stay allowed.
    gpg.refresh_from_pnls(
        "forex",
        "ETH/USD",
        [0.3] * 25,
        now=20_000.0,
    )
    assert gpg.is_banned("forex", "ETH/USD") is False
    out2 = gpg.refresh_from_pnls(
        "forex",
        "ETH/USD",
        [0.0] * 25,
        now=30_000.0,
    )
    assert out2["banned"] is False
    assert out2["reason"] == "hold_allowed"


def test_min_samples_blocks_flip(gate_env):
    assert gpg.is_promote_allowed("forex", "AUD/USD") is True
    out = gpg.refresh_from_pnls(
        "forex",
        "AUD/USD",
        [-1.0] * 5,
        now=1_000.0,
    )
    assert out["n"] == 5
    assert out["banned"] is False
    assert out["reason"] == "insufficient_samples"
    assert gpg.is_promote_allowed("forex", "AUD/USD") is True

    # Seeded ban also refuses to unban on thin evidence.
    out2 = gpg.refresh_from_pnls(
        "forex",
        "GBP/JPY",
        [1.0] * 10,
        now=1_000.0,
    )
    assert out2["banned"] is True
    assert out2["reason"] == "insufficient_samples"


def test_cooldown_blocks_immediate_flip(gate_env):
    t0 = 5_000_000.0
    gpg.refresh_from_pnls("forex", "ETH/USD", [-0.4] * 25, now=t0)
    assert gpg.is_banned("forex", "ETH/USD") is True

    # Immediately after ban, even great expectancy cannot unban (cooldown).
    out = gpg.refresh_from_pnls(
        "forex",
        "ETH/USD",
        [0.5] * 25,
        now=t0 + 60.0,
    )
    assert out["banned"] is True
    assert out["reason"] == "cooldown"

    # After cooldown elapses → unban.
    out2 = gpg.refresh_from_pnls(
        "forex",
        "ETH/USD",
        [0.5] * 25,
        now=t0 + 4_000.0,
    )
    assert out2["banned"] is False
    assert out2["reason"] == "unban_expectancy"


def test_decide_pure_helpers():
    banned, reason = gpg.decide(False, -0.2, 5, min_n=20, ban_thr=-0.05, unban_thr=0.05)
    assert banned is False and reason == "insufficient_samples"

    banned, reason = gpg.decide(
        False,
        -0.2,
        30,
        now=100.0,
        last_change_ts=0.0,
        min_n=20,
        ban_thr=-0.05,
        unban_thr=0.05,
    )
    assert banned is True and reason == "ban_expectancy"

    banned, reason = gpg.decide(
        True,
        0.2,
        30,
        now=100.0,
        last_change_ts=0.0,
        min_n=20,
        ban_thr=-0.05,
        unban_thr=0.05,
    )
    assert banned is False and reason == "unban_expectancy"


def test_record_pnl_rolling_and_shadow_settle(gate_env, monkeypatch):
    monkeypatch.setenv("GP_PROMOTE_GATE_MIN_SAMPLES", "3")
    monkeypatch.setenv("GP_PROMOTE_GATE_COOLDOWN_S", "0")

    gpg.record_pnl("forex", "XAU/USD", -0.5, now=100.0)
    gpg.record_pnl("forex", "XAU/USD", -0.5, now=101.0)
    out = gpg.record_pnl("forex", "XAU/USD", -0.5, now=102.0)
    assert out["banned"] is True
    assert out["n"] == 3

    # Shadow observe: open pending, settle after horizon with adverse move.
    gpg.observe_shadow("forex", "XAG/USD", 30.0, direction=1, now=1_000.0)
    st = gpg.load_state("forex")
    assert st["pairs"]["XAG/USD"]["pending_shadow"]["direction"] == 1

    settled = gpg.observe_shadow(
        "forex",
        "XAG/USD",
        29.0,
        direction=None,
        now=1_000.0 + 200.0,
    )
    assert settled is not None
    assert settled["n"] >= 1
    # Long into a drop → negative sample.
    assert settled["expectancy"] < 0


def test_refresh_from_sim(gate_env, monkeypatch):
    monkeypatch.setenv("GP_PROMOTE_GATE_MIN_SAMPLES", "10")
    monkeypatch.setenv("GP_PROMOTE_GATE_COOLDOWN_S", "0")
    out = gpg.refresh_from_sim(
        "forex",
        "EUR/USD",
        {"trades": 20, "total_pnl": -4.0},  # mean −0.2
        now=9_000.0,
    )
    assert out["banned"] is True
    assert out["n"] == 20
    assert out["expectancy"] == pytest.approx(-0.2)
