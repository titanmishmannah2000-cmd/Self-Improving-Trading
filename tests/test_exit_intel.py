"""HIF exit intelligence — pair-tuned trail / BE / partial stamps."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_core.engines.exit import evaluate_exit
from hermes_core.engines.exit_intel import apply_exit_intel, exit_intel_enabled


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("EXIT_INTEL", raising=False)
    assert exit_intel_enabled() is False
    monkeypatch.setenv("EXIT_INTEL", "1")
    assert exit_intel_enabled() is True


def test_disabled_passthrough_yaml_partial():
    out = apply_exit_intel(
        enabled=False,
        pair="EUR/USD",
        entry_type="mean_reversion",
        strategy={"partial_enabled": True},
        cortex=None,
    )
    assert out["exit_intel_mode"] == "disabled"
    assert out["honor_current_stop"] is False
    assert out["partial_enabled"] is True
    assert out["trailing_atr_mult"] is None


def test_thin_evidence_passthrough():
    cortex = SimpleNamespace(
        edge_stats=lambda *_a, **_k: {"wins": 1, "losses": 1, "n": 2},
    )
    out = apply_exit_intel(
        enabled=True,
        pair="EUR/USD",
        entry_type="mean_reversion",
        strategy={},
        cortex=cortex,
    )
    assert out["exit_intel_mode"] == "passthrough"
    assert "thin_evidence" in out["exit_intel_reasons"]


def test_strong_edge_stamps_soft_knobs():
    cortex = SimpleNamespace(
        edge_stats=lambda *_a, **_k: {
            "wins": 12, "losses": 3, "n": 15,
            "avg_win": 2.0, "avg_loss": 1.0,
        },
    )
    out = apply_exit_intel(
        enabled=True,
        pair="EUR/USD",
        entry_type="mean_reversion",
        strategy={},
        cortex=cortex,
    )
    assert out["exit_intel_mode"] == "soft"
    assert out["honor_current_stop"] is True
    assert out["be_trigger_frac"] == pytest.approx(0.65)
    assert out["trailing_atr_mult"] == pytest.approx(1.8)
    assert out["partial_enabled"] is True


def test_be_trigger_frac_in_evaluate_exit():
    t = {
        "entry_price": 1.1,
        "profit_target_pct": 2.0,
        "breakeven_set": False,
        "partial_enabled": False,
        "be_trigger_frac": 0.35,
        "unrealised_pct": 0.8,  # 0.35 * 2.0 = 0.7 → fires
    }
    ex = evaluate_exit(t, 1.1088, None)
    assert ex is not None and ex.reason == "breakeven"


def test_honor_current_stop_hit():
    t = {
        "entry_price": 1.1,
        "stop_loss_pct": 5.0,  # hard SL far away
        "honor_current_stop": True,
        "current_stop": 1.1,
        "breakeven_set": True,
        "partial_enabled": False,
    }
    ex = evaluate_exit(t, 1.099, None)
    assert ex is not None and ex.reason == "stop_loss"


def test_honor_off_ignores_current_stop():
    t = {
        "entry_price": 1.1,
        "stop_loss_pct": 5.0,
        "honor_current_stop": False,
        "current_stop": 1.1,
        "breakeven_set": True,
        "partial_enabled": False,
    }
    assert evaluate_exit(t, 1.099, None) is None
