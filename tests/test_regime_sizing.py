"""HIF Phase-3 soft regime size multiplier."""

from __future__ import annotations

import pytest

from hermes_core.engines.regime_sizing import (
    MULT_RANGE,
    MULT_TREND_DOWN,
    MULT_TREND_UP,
    apply_regime_sizing,
    regime_size_mult,
)


def test_disabled_passthrough():
    out = apply_regime_sizing(
        0.4, enabled=False, regime="trend", fast_regime="down",
    )
    assert out["size"] == pytest.approx(0.4)
    assert out["regime_mult"] == 1.0
    assert out["regime_mode"] == "disabled"


def test_trend_up_full():
    info = regime_size_mult("trend", "up")
    assert info["mult"] == pytest.approx(MULT_TREND_UP)
    assert info["label"] == "trend_up"


def test_trend_down_cuts():
    info = regime_size_mult("trend", "down")
    assert info["mult"] == pytest.approx(MULT_TREND_DOWN)
    out = apply_regime_sizing(0.4, enabled=True, regime="trend", fast_regime="down")
    assert out["size"] == pytest.approx(0.4 * MULT_TREND_DOWN)
    assert out["regime_mode"] == "soft"


def test_range_moderate():
    info = regime_size_mult("range", "flat")
    assert info["mult"] == pytest.approx(MULT_RANGE)


def test_unknown_fail_open():
    info = regime_size_mult("weird", "up")
    assert info["mult"] == pytest.approx(1.0)


def test_adx_blend_between_range_and_trend():
    soft = regime_size_mult("trend", "down", adx=15)
    hard = regime_size_mult("trend", "down", adx=40)
    # Low ADX → closer to range mult; high ADX → closer to trend_down
    assert soft["mult"] > hard["mult"]
    assert soft["mult"] <= MULT_RANGE + 0.01
