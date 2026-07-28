"""Tests for momentum range / confluence guard (Jul 23 gold lesson)."""

from __future__ import annotations

import pytest

from hermes_core.engines.mom_range_guard import (
    apply_mom_range_guard,
    chart_downtrend_hostile,
    count_oversold,
    gp_agree_bullish,
    mom_range_guard_enabled,
)
from hermes_core.engines.risk import PROBE_SIZE_FRACTION


def test_flag_explicit(monkeypatch):
    monkeypatch.setenv("MOM_RANGE_GUARD", "1")
    assert mom_range_guard_enabled(bot="forex") is True
    monkeypatch.setenv("MOM_RANGE_GUARD", "0")
    assert mom_range_guard_enabled(bot="gold") is False


def test_unset_auto_gold_and_crypto(monkeypatch):
    monkeypatch.delenv("MOM_RANGE_GUARD", raising=False)
    assert mom_range_guard_enabled(bot="gold") is True
    assert mom_range_guard_enabled(bot="crypto") is True
    assert mom_range_guard_enabled(bot="forex") is False


def test_gp_agree_bullish():
    assert gp_agree_bullish("bullish") is True
    assert gp_agree_bullish("neutral") is False
    assert gp_agree_bullish("bearish") is False
    assert gp_agree_bullish(None, gp_strength=0.4) is True
    assert gp_agree_bullish(None, gp_strength=0.0) is False


def test_count_oversold():
    rows = [
        {"rsi": 40, "threshold": 55},
        {"rsi": 60, "threshold": 55},
        {"rsi": 50, "threshold": 55},
    ]
    assert count_oversold(rows) == 2


def test_chart_downtrend_hostile():
    assert chart_downtrend_hostile("trend: downtrend (conf=0.9). Rec: wait for pullback")
    assert chart_downtrend_hostile(None, chart_soft_reasons=["downtrend", "wait_for_pullback"])
    assert not chart_downtrend_hostile("trend: uptrend. Rec: enter long")
    assert not chart_downtrend_hostile(None, chart_soft_reasons=["wait_for_pullback"])


def test_disabled_passthrough():
    out = apply_mom_range_guard(
        0.4,
        enabled=False,
        entry_type="rsi_momentum",
        regime="range",
        oversold_count=1,
        gp_agree=False,
    )
    assert out["mom_guard_action"] == "disabled"
    assert out["size"] == pytest.approx(0.4)


def test_non_momentum_passthrough():
    out = apply_mom_range_guard(
        0.4,
        enabled=True,
        entry_type="mean_reversion",
        regime="range",
        oversold_count=1,
        gp_agree=False,
    )
    assert out["mom_guard_action"] == "full"
    assert out["size"] == pytest.approx(0.4)


def test_range_benches_unconfirmed_momentum():
    out = apply_mom_range_guard(
        0.4,
        enabled=True,
        entry_type="rsi_momentum",
        regime="range",
        oversold_count=1,
        gp_agree=False,
    )
    assert out["mom_guard_action"] == "bench"
    assert out["size"] == 0.0


def test_chart_downtrend_benches_unconfirmed_momentum():
    out = apply_mom_range_guard(
        0.4,
        enabled=True,
        entry_type="rsi_momentum",
        regime="trend",
        oversold_count=1,
        gp_agree=False,
        chart_context="trend: downtrend (conf=0.9). Rec: wait for pullback",
    )
    assert out["mom_guard_action"] == "bench"
    assert out["size"] == 0.0
    assert "chart_downtrend_bench" in out["mom_guard_reasons"]


def test_chart_downtrend_allows_confluence():
    out = apply_mom_range_guard(
        0.4,
        enabled=True,
        entry_type="rsi_momentum",
        regime="trend",
        oversold_count=2,
        gp_agree=False,
        chart_soft_reasons=["downtrend"],
    )
    assert out["mom_guard_action"] == "full"
    assert out["mom_guard_confirmed"] is True


def test_gp_ensemble_probes_on_chart_downtrend():
    out = apply_mom_range_guard(
        0.4,
        enabled=True,
        entry_type="gp_ensemble",
        regime="trend",
        oversold_count=0,
        gp_agree=False,
        chart_context="trend: downtrend. Rec: wait for pullback",
    )
    assert out["mom_guard_action"] == "probe"
    assert out["size"] == pytest.approx(0.4 * PROBE_SIZE_FRACTION)
    assert "gp_downtrend_probe" in out["mom_guard_reasons"]


def test_gp_ensemble_full_without_downtrend():
    out = apply_mom_range_guard(
        0.4,
        enabled=True,
        entry_type="gp_ensemble",
        regime="trend",
        oversold_count=0,
        gp_agree=False,
        chart_context="trend: uptrend. Rec: enter long",
    )
    assert out["mom_guard_action"] == "full"
    assert out["size"] == pytest.approx(0.4)


def test_range_allows_dual_metal_confluence():
    out = apply_mom_range_guard(
        0.4,
        enabled=True,
        entry_type="rsi_momentum",
        regime="range",
        oversold_count=2,
        gp_agree=False,
    )
    assert out["mom_guard_action"] == "full"
    assert out["mom_guard_confirmed"] is True
    assert out["size"] == pytest.approx(0.4)


def test_range_allows_gp_agree():
    out = apply_mom_range_guard(
        0.4,
        enabled=True,
        entry_type="rsi_momentum",
        regime="range",
        oversold_count=1,
        gp_agree=True,
    )
    assert out["mom_guard_action"] == "full"


def test_trend_unconfirmed_probes():
    out = apply_mom_range_guard(
        0.4,
        enabled=True,
        entry_type="rsi_momentum",
        regime="trend",
        oversold_count=1,
        gp_agree=False,
    )
    assert out["mom_guard_action"] == "probe"
    assert out["size"] == pytest.approx(0.4 * PROBE_SIZE_FRACTION)
