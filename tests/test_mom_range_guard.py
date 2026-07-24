"""Tests for momentum range / confluence guard (Jul 23 gold lesson)."""

from __future__ import annotations

import pytest

from hermes_core.engines.mom_range_guard import (
    apply_mom_range_guard,
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


def test_unset_auto_gold_only(monkeypatch):
    monkeypatch.delenv("MOM_RANGE_GUARD", raising=False)
    assert mom_range_guard_enabled(bot="gold") is True
    assert mom_range_guard_enabled(bot="forex") is False
    assert mom_range_guard_enabled(bot="crypto") is False


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
        entry_type="gp_ensemble",
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
