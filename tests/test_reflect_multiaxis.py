"""Phase 2 — multi-axis, Cortex-aware L1 with dynamic confidence.

Verifies pathology extraction, axis selection/priority (one variable only),
dynamic confidence calibration, and that the classic stop rules are preserved.
"""

from __future__ import annotations

import pytest

from hermes_core.engines.reflect import (
    dynamic_confidence,
    layer1_rule_based,
    trade_pathology,
)

GOAL = {"max_drawdown": 10.0}
STRAT = {
    "strategy_type": "mean_reversion",
    "stop_loss_pct": 1.5,
    "profit_target_pct": 3.0,
    "trailing_stop_pct": 0.0,
    "time_exit_cycles": 288,
    "position_size_r": 0.4,
    "entry": {"threshold": 30},
    "version": "00",
}


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    # Keep Cortex reads deterministic (empty) so confidence stability == 0.
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    yield


def _t(pnl, reason="tp", **extra):
    r = {"pair": "EUR/USD", "pnl_pct": pnl, "exit_reason": reason}
    r.update(extra)
    return r


# ── 2.1 pathology extraction ────────────────────────────────────────────────
def test_trade_pathology_fractions_and_excursion():
    trades = [
        _t(1.0, "tp", mfe_pct=2.0, mfe_capture=0.4, giveback_frac=0.6),
        _t(-0.5, "sl"),
        _t(0.2, "time_exit", mfe_pct=1.5),
        _t(0.3, "time_exit", mfe_pct=1.0),
    ]
    p = trade_pathology(trades)
    assert p["count"] == 4
    assert p["stop_frac"] == 0.25
    assert p["timeout_frac"] == 0.5
    assert p["avg_capture"] == pytest.approx(0.4)
    assert p["avg_time_mfe"] == pytest.approx(1.25)


def test_trade_pathology_absent_fields_are_none():
    p = trade_pathology([_t(1.0), _t(-1.0, "sl")])
    assert p["avg_capture"] is None
    assert p["avg_giveback"] is None
    assert p["avg_time_mfe"] is None


# ── 2.3 dynamic confidence ──────────────────────────────────────────────────
def test_dynamic_confidence_thin_sample_below_l2_bar():
    assert dynamic_confidence(5, 1.0, 0.0) < 0.65


def test_dynamic_confidence_strong_evidence_high():
    assert dynamic_confidence(20, 1.0, 1.0) > 0.65


def test_dynamic_confidence_monotonic():
    base = dynamic_confidence(10, 0.5, 0.5)
    assert dynamic_confidence(20, 0.5, 0.5) >= base
    assert dynamic_confidence(10, 0.9, 0.5) >= base
    assert dynamic_confidence(10, 0.5, 0.9) >= base


# ── 2.2 / 2.4 multi-axis selection + priority ───────────────────────────────
def test_dd_breach_still_tightens_stop():
    losing = [_t(-12.0, "sl") for _ in range(5)]
    out = layer1_rule_based("EUR/USD", losing, GOAL, STRAT)
    assert out is not None
    assert out[0] == "stop_loss_pct"
    assert float(out[2]) == pytest.approx(float(out[1]) - 0.3)


def test_giveback_proposes_trailing_stop():
    winners = [_t(1.0, "tp", mfe_pct=2.5, mfe_capture=0.4) for _ in range(6)]
    out = layer1_rule_based("EUR/USD", winners, GOAL, STRAT)
    assert out is not None
    assert out[0] == "trailing_stop_pct"
    assert float(out[2]) > float(out[1])  # trailing added/raised


def test_timeout_proposes_lower_profit_target():
    timeouts = [_t(0.2, "time_exit", mfe_pct=1.5) for _ in range(6)]
    out = layer1_rule_based("EUR/USD", timeouts, GOAL, STRAT)
    assert out is not None
    assert out[0] == "profit_target_pct"
    assert float(out[2]) < float(out[1])  # take profit sooner


def test_sustained_loss_decent_wr_shrinks_size():
    trades = [_t(0.5, "tp") for _ in range(4)] + [_t(-0.4, "sl") for _ in range(8)]
    out = layer1_rule_based("EUR/USD", trades, GOAL, STRAT)
    assert out is not None
    assert out[0] == "position_size_r"
    assert float(out[2]) < float(out[1])  # de-risk


def test_priority_dd_beats_giveback():
    # Both DD breach AND giveback present → drawdown (P1) wins (one variable).
    trades = [_t(-12.0, "sl", mfe_pct=0.1, mfe_capture=0.4) for _ in range(3)] + [
        _t(1.0, "tp", mfe_pct=2.5, mfe_capture=0.4) for _ in range(3)
    ]
    out = layer1_rule_based("EUR/USD", trades, GOAL, STRAT)
    assert out is not None
    assert out[0] == "stop_loss_pct"  # DD tighten wins


def test_giveback_beats_low_wr():
    # Low WR (P4) AND giveback (P2) both present → giveback wins (higher priority).
    trades = [_t(1.0, "tp", mfe_pct=2.5, mfe_capture=0.4) for _ in range(3)] + [
        _t(-0.4, "sl") for _ in range(8)
    ]
    # WR = 3/11 = 0.27 (< 0.3, so P4 would fire) but P2 giveback outranks it.
    out = layer1_rule_based("EUR/USD", trades, GOAL, STRAT)
    assert out is not None
    assert out[0] == "trailing_stop_pct"


def test_one_variable_only_single_tuple():
    trades = [_t(1.0, "tp", mfe_pct=2.5, mfe_capture=0.4) for _ in range(6)]
    out = layer1_rule_based("EUR/USD", trades, GOAL, STRAT)
    assert out is not None and len(out) == 5  # (variable, old, new, reason, conf)


def test_min_sample_guard():
    assert layer1_rule_based("EUR/USD", [_t(-12.0, "sl")] * 4, GOAL, STRAT) is None
