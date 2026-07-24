"""Session 6 / Phase 6 acceptance + guard tests for the risk engine.

Mirrors the blueprint Phase-6 test block (test_size_bull, test_size_neutral,
test_size_neutral_two_open, test_rr_guard_blocks, test_atr_stop_floor) plus the
[GUARD L40] param-range gate and a Hypothesis property test that size() is always
in (0, 0.5].
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hermes_core.engines import (
    MAX_POSITION_SIZE,
    check_rr_guard,
    compute_atr_stop,
    compute_position_size,
    param_range_gate,
    size,
    size_regime_from_market,
)


def cfg(**kw):
    base = {"position_size_r": 0.25}
    base.update(kw)
    return base


def test_size_bull():
    assert compute_position_size("BULL", 0.05, 0, cfg()) == 0.25


def test_size_neutral():
    # NEUTRAL -> *0.6
    assert compute_position_size("NEUTRAL", 0.05, 0, cfg()) == pytest.approx(0.15)


def test_size_neutral_two_open():
    s = compute_position_size("NEUTRAL", 0.05, 2, cfg())
    assert s == pytest.approx(0.15 * 0.7)  # extra 30% (2*15%) reduction


def test_session_token_is_not_bull():
    """LDN/NY must not accidentally size as unknown→NEUTRAL… wait they DID.
    Explicit: session tokens map to NEUTRAL sizing, never full BULL."""
    assert compute_position_size("LDN", 0.05, 0, cfg()) == pytest.approx(0.15)
    assert compute_position_size("NY", 0.05, 0, cfg()) == pytest.approx(0.15)
    assert compute_position_size("ASIA", 0.05, 0, cfg()) == pytest.approx(0.15)


def test_size_regime_from_market_trend_up_is_bull():
    assert size_regime_from_market("trend", "up") == "BULL"
    assert size_regime_from_market("trend", "down") == "BEAR"
    assert size_regime_from_market("trend", "flat") == "NEUTRAL"
    assert size_regime_from_market("range", "up") == "NEUTRAL"
    assert size_regime_from_market("range", "down") == "BEAR"


def test_size_regime_rejects_session_tokens():
    assert size_regime_from_market("LDN") == "NEUTRAL"
    assert size_regime_from_market("NY", "up") == "NEUTRAL"


def test_market_mapped_size_matches_bull_full():
    """trend+up → BULL → full position_size_r (not session NEUTRAL 0.6×)."""
    label = size_regime_from_market("trend", "up")
    assert label == "BULL"
    assert compute_position_size(label, 0.05, 0, cfg()) == 0.25
    # Old bug path would have used session token:
    assert compute_position_size("LDN", 0.05, 0, cfg()) == pytest.approx(0.15)


def test_rr_guard_blocks():
    # R:R = 1.0/1.5 < 1.0 -> rejected
    assert check_rr_guard(1.5, 1.0) is False
    # equal -> passes
    assert check_rr_guard(1.0, 1.0) is True
    # 2:1 -> passes
    assert check_rr_guard(1.0, 2.0) is True


def test_atr_stop_floor():
    # Floor not binding: ATR distance (0.0035*1.5=0.00525) > floor (0.0008)
    stp = compute_atr_stop(1.1000, 0.0035, 1.5, 0.0008)
    assert stp == pytest.approx(1.1000 - 0.0035 * 1.5)
    # Floor binding: tiny ATR distance, floor forces a wider (safer) stop
    bound = compute_atr_stop(1.1000, 0.0001, 1.5, 0.0008)
    assert bound == pytest.approx(1.1000 - 0.0008)  # floor wins
    assert bound >= 1.1000 - 0.0008  # never tighter than floor


# --- [GUARD L40] param-range gate ------------------------------------------
def test_param_gate_pass():
    ok, reason = param_range_gate(
        {
            "stop_loss_pct": 1.5,
            "profit_target_pct": 3.0,
            "position_size_r": 0.2,
            "entry_threshold": 40,
        }
    )
    assert ok is True and reason is None


def test_param_gate_out_of_range():
    ok, reason = param_range_gate({"position_size_r": 0.9})  # within range -> ok
    assert ok is True
    ok, reason = param_range_gate({"stop_loss_pct": 0.1})  # below 0.5 -> reject
    assert ok is False and reason is not None
    ok, reason = param_range_gate({"profit_target_pct": 50.0})  # above 20 -> reject
    assert ok is False


# --- Hypothesis: size always within (0, 0.5] -------------------------------
regimes = st.sampled_from(["BULL", "NEUTRAL", "BEAR", "SOMETHING_ELSE"])
vols = st.floats(min_value=0.0, max_value=5.0, allow_nan=False, allow_infinity=False)
bases = st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False)
opens = st.integers(min_value=0, max_value=20)


@given(regime=regimes, vol=vols, base=bases, open_bullish=opens)
@settings(max_examples=400, deadline=None)
def test_size_bounds(regime, vol, base, open_bullish):
    s = compute_position_size(regime, vol, open_bullish, {"position_size_r": base})
    assert 0.0 <= s <= MAX_POSITION_SIZE
    # never negative, never above the hard cap
    assert s >= 0.0
    assert s <= MAX_POSITION_SIZE
    # roadmap alias route produces the same result
    s2 = size({"position_size_r": base}, regime, vol, {"open_bullish_count": open_bullish})
    assert s2 == s
