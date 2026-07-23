"""Tests for L02/L03 market-data guards."""

from __future__ import annotations

from hermes_core.engines.guards import BB_BW_MIN, bb_bandwidth_guard, flat_price_guard
from hermes_core.indicators import compute_all


def test_l02_flat_unchanged_series():
    prices = [1.1] * 10
    ind = compute_all(prices)
    blocked, reason = flat_price_guard(ind, prices)
    assert blocked is True
    assert reason.startswith("flat_price:")


def test_l02_degenerate_indicators():
    ind = {"rsi": 0.0, "roc": 0.0, "adx": 0.0}
    blocked, reason = flat_price_guard(ind, [1.0, 1.01])
    assert blocked is True
    assert reason == "flat_price:degenerate_indicators"


def test_l02_allows_moving_market():
    prices = [1.0 + i * 0.01 for i in range(30)]
    ind = compute_all(prices)
    blocked, _ = flat_price_guard(ind, prices)
    assert blocked is False


def test_l03_bb_bandwidth_blocks_flat():
    bb = {"lower": 100.0, "middle": 100.0, "upper": 100.0}
    blocked, reason = bb_bandwidth_guard(bb)
    assert blocked is True
    assert reason.startswith("bb_bandwidth:")


def test_l03_bb_bandwidth_allows_volatile():
    bb = {"lower": 95.0, "middle": 100.0, "upper": 105.0}
    blocked, _ = bb_bandwidth_guard(bb)
    assert blocked is False


def test_l03_bb_bw_min_allows_live_fx_band():
    """Live FX tick BB often lands ~0.0004–0.0006 (was blocked by 0.001)."""
    assert BB_BW_MIN == 0.0003
    # bw = (100.02 - 99.98) / 100 = 0.0004
    bb = {"lower": 99.98, "middle": 100.0, "upper": 100.02}
    blocked, reason = bb_bandwidth_guard(bb)
    assert blocked is False, reason


def test_l03_bb_bw_still_blocks_near_zero():
    # bw = 0.0002 < 0.0003
    bb = {"lower": 99.99, "middle": 100.0, "upper": 100.01}
    blocked, reason = bb_bandwidth_guard(bb)
    assert blocked is True
    assert reason.startswith("bb_bandwidth:")
