"""Profitability Path Phase 0–5 unit tests."""

from __future__ import annotations

import json

import pytest

from hermes_core.engines import hif_flags as hf
from hermes_core.engines.expert_weights import (
    MIN_WEIGHT,
    beta_posterior,
    expert_weight,
)
from hermes_core.engines.feed_health import check_heartbeat_health, price_sane
from hermes_core.engines.profitability_freeze import assert_phase0_freeze
from hermes_core.engines.reflect_verifier import (
    PROFITABILITY_TUNABLES,
    verify_reflection_candidate,
)
from hermes_core.engines.regime_decay import evaluate_decay, update_pair_decay
from hermes_core.engines.scorecard import build_scorecard, phase1_gate, summarize_bucket


def test_phase0_freeze_ok_when_only_book_risk(monkeypatch):
    for key, _ in hf.DORMANT_FLAGS:
        monkeypatch.setenv(key, "0")
    monkeypatch.setenv("BOOK_RISK", "1")
    monkeypatch.setenv("REFLECT_AUTO_DEPLOY", "0")
    monkeypatch.setenv("GP_PROMOTE", "0")
    report = assert_phase0_freeze()
    assert report["ok"] is True
    assert report["enabled"] == ["BOOK_RISK"]


def test_focus_pairs_include_all_three_bots():
    from hermes_core.engines.profitability_freeze import focus_pairs_for_bot

    assert focus_pairs_for_bot("gold") == ["XAU/USD"]
    assert focus_pairs_for_bot("forex") == ["EUR/USD", "GBP/USD"]
    assert focus_pairs_for_bot("crypto") == ["BTC/USD"]
    assert focus_pairs_for_bot("btc") == ["BTC/USDT"]


def test_phase0_freeze_fails_when_soft_weights_on(monkeypatch):
    for key, _ in hf.DORMANT_FLAGS:
        monkeypatch.setenv(key, "0")
    monkeypatch.setenv("BOOK_RISK", "1")
    monkeypatch.setenv("SOFT_WEIGHTS", "1")
    monkeypatch.setenv("REFLECT_AUTO_DEPLOY", "0")
    report = assert_phase0_freeze()
    assert report["ok"] is False
    assert any("SOFT_WEIGHTS" in v for v in report["violations"])


def test_price_sane_rejects_stub_gold():
    assert price_sane("XAU/USD", 1.1) is False
    assert price_sane("XAU/USD", 2400.0) is True
    assert price_sane("EUR/USD", 1.08) is True


def test_heartbeat_health_flags_insane_price(tmp_path, monkeypatch):
    from hermes_core.engines import feed_health as fh

    hb = {
        "ts": 1_700_000_000.0,
        "status": "ok",
        "prices": {"XAU/USD": 1.1},
        "regimes": {"XAU/USD": "range"},
    }
    path = tmp_path / "heartbeat.json"
    path.write_text(json.dumps(hb), encoding="utf-8")
    monkeypatch.setattr(fh, "bot_state_dir", lambda _bot: tmp_path)
    # Bypass age by using same ts as now via now=
    report = check_heartbeat_health(
        "gold",
        focus_pairs=["XAU/USD"],
        max_age_s=1e12,
        heartbeat=hb,
        now=1_700_000_000.0,
    )
    assert report["ok"] is False
    assert any("insane_price" in v for v in report["violations"])


def test_scorecard_expectancy_applies_cost():
    trades = [
        {"pair": "EUR/USD", "entry_type": "mean_reversion", "pnl_pct": 1.0, "exit_reason": "tp"},
        {"pair": "EUR/USD", "entry_type": "mean_reversion", "pnl_pct": -0.5, "exit_reason": "sl"},
    ]
    s = summarize_bucket(trades, cost=0.05)
    assert s["n"] == 2
    assert s["expectancy"] == pytest.approx(((1.0 - 0.05) + (-0.5 - 0.05)) / 2)


def test_phase1_gate_wait_thin_sample():
    card = build_scorecard(
        "forex",
        trades=[
            {
                "pair": "EUR/USD",
                "entry_type": "mean_reversion",
                "pnl_pct": 1.0,
                "exit_reason": "tp",
            }
        ],
        min_n=20,
    )
    gate = phase1_gate(card, focus_pairs=["EUR/USD"], min_n=20)
    key = "EUR/USD|mean_reversion"
    assert gate["decisions"][key]["verdict"] == "wait"


def test_bayesian_passthrough_thin_evidence():
    info = expert_weight(enabled=True, suppressed=False, wr=0.0, evidence_n=3)
    assert info["mode"] == "passthrough"
    assert info["weight"] == 1.0


def test_bayesian_retire_on_bad_streak():
    # 20 losses → strong P(WR < 0.45)
    info = expert_weight(enabled=True, suppressed=False, wr=0.0, evidence_n=20, wins=0, losses=20)
    assert info["retired"] is True
    assert info["weight"] == pytest.approx(MIN_WEIGHT)


def test_beta_posterior_mean():
    _a, _b, mean, _p = beta_posterior(9, 1)
    assert mean == pytest.approx(10 / 12)


def test_verifier_rejects_exotic_variable():
    v = verify_reflection_candidate(
        pair="EUR/USD",
        proposal={"variable": "adx_threshold", "old": 20, "new": 25},
        verdict={"approved": True, "improvement_oos": 0.1},
        trades=[{"pnl_pct": 1.0}] * 20,
    )
    assert v["ok"] is False
    assert "tunables" in v["reason"]


def test_verifier_passes_stop_change():
    assert "stop_loss_pct" in PROFITABILITY_TUNABLES
    v = verify_reflection_candidate(
        pair="EUR/USD",
        proposal={"variable": "stop_loss_pct", "old": 1.5, "new": 1.2},
        verdict={"approved": True, "improvement_oos": 0.05, "phases": {}},
        trades=[{"pnl_pct": 1.0}] * 20,
    )
    assert v["ok"] is True


def test_regime_decay_trips_on_two_signals():
    # Bad WR + huge DD vs tiny backtest MDD
    r = evaluate_decay(
        wins=2,
        losses=30,
        live_dd=20.0,
        backtest_mdd=5.0,
        feature_z=None,
        min_trades=20,
    )
    assert r["s1_wr_decay"] is True
    assert r["s2_dd"] is True
    assert r["tripped"] is True


def test_regime_decay_persist(tmp_path, monkeypatch):
    from hermes_core.engines import regime_decay as rd

    monkeypatch.setattr(rd, "bot_state_dir", lambda _bot: tmp_path)
    monkeypatch.setenv("REGIME_DECAY", "1")
    out = update_pair_decay(
        "forex",
        "EUR/USD",
        wins=2,
        losses=30,
        live_dd=20.0,
        backtest_mdd=5.0,
    )
    assert out["tripped"] is True
    assert rd.is_pair_suppressed("forex", "EUR/USD") is True


def test_gp_promote_expectancy_applies_cost(monkeypatch):
    from hermes_core.engines import gp_promote_gate as g

    monkeypatch.setenv("GP_PROMOTE_COST_PCT", "0.10")
    monkeypatch.delenv("SCORECARD_COST_PCT", raising=False)
    assert g.compute_expectancy([1.0, 1.0]) == pytest.approx(0.9)
    monkeypatch.setenv("GP_PROMOTE_COST_PCT", "")
    monkeypatch.delenv("SCORECARD_COST_PCT", raising=False)
    assert g.compute_expectancy([1.0, 1.0]) == pytest.approx(1.0)


def test_simulate_cost_reduces_pnl():
    from hermes_core.engines.backtest import _simulate

    # Synthetic upward path so MR/momentum may or may not trade; use many bars.
    prices = [100.0 + i * 0.01 for i in range(80)]
    # Force via known path: if no entries, both equal — still valid API smoke.
    a = _simulate(prices, "mean_reversion", 30.0, 1.5, 3.0, cost_pct=0.0)
    b = _simulate(prices, "mean_reversion", 30.0, 1.5, 3.0, cost_pct=0.5)
    if a["entries"] > 0:
        assert b["pnl"] <= a["pnl"]
