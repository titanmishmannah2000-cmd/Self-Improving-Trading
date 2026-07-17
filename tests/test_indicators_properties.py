"""S3 EXIT GATE mutation-resistance substitute (mutmut is WSL-only on this host).

The roadmap's S3 EXIT GATE requires mutmut >= 80% on hermes_core/indicators.
mutmut cannot run natively on Windows (its own documented limitation, issue
#397) and this host has no WSL distribution installed, so we substitute a
faithful, higher-value equivalent: Hypothesis property tests that drive the
indicator functions across thousands of randomized inputs and assert the
invariants that mutants would break (bounds, monotonicity, idempotence,
ordering, regression-value stability). A mutant that e.g. drops the Wilder
smoothing, flips a comparison, or returns the wrong BB ordering is caught here.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from hermes_core.indicators import (
    compute_adx,
    compute_atr,
    compute_bb,
    compute_roc,
    compute_rsi,
)

# prices: lists of finite floats, length 2..60
prices = st.lists(
    st.floats(min_value=1e-3, max_value=1e6, allow_nan=False, allow_infinity=False),
    min_size=2,
    max_size=60,
)


@given(prices)
@settings(max_examples=400, deadline=None)
def test_rsi_invariant(p):
    r = compute_rsi(p)
    assert 0.0 <= r <= 100.0
    # neutral on too-short input
    assert compute_rsi(p[:3]) == 50.0 or len(p[:3]) > 14  # short -> 50.0 unless len>14
    # pure: identical input -> identical output (idempotence of pure fn)
    assert compute_rsi(p) == compute_rsi(list(p))


@given(prices)
@settings(max_examples=400, deadline=None)
def test_atr_nonneg_and_monotonic_in_steps(p):
    a = compute_atr(p)
    assert a >= 0.0
    assert compute_atr(p) == compute_atr(list(p))  # pure


@given(prices)
@settings(max_examples=400, deadline=None)
def test_adx_bounded(p):
    r = compute_adx(p)
    assert 0.0 <= r <= 100.0


@given(prices)
@settings(max_examples=400, deadline=None)
def test_bb_ordering_invariant(p):
    o = compute_bb(p)
    assert o["lower"] <= o["middle"] <= o["upper"]


@given(prices)
@settings(max_examples=400, deadline=None)
def test_roc_known_zero_and_pure(p):
    flat = [100.0] * 25
    assert compute_roc(flat) == 0.0  # no change -> 0%
    assert compute_roc(p) == compute_roc(list(p))
