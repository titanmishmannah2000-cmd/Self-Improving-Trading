"""Session 3 / Phase 3 acceptance tests for the indicator engine.

Mirrors the blueprint Phase 3 block (test_rsi_known_series, test_atr_flat_zero,
test_adx_range, test_bb_ordering) plus mutation-killing value checks and a purity
test (no I/O allowed inside any indicator function).

NOTE on RSI constant: the blueprint's test hardcodes 47.3 but explicitly flags it
"ASSUMED 47.3 exact — recompute at test time". An independent hand computation of
Wilder RSI(14) for the exact series below is 54.1667. We assert the CORRECT value
(54.2 within 0.1). Encoding the wrong 47.3 would be a dishonest constant; the
blueprint grants permission to recompute.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from hermes_core.indicators import (
    compute_adx,
    compute_all,
    compute_atr,
    compute_bb,
    compute_roc,
    compute_rsi,
)

INDICATORS_DIR = Path(__file__).resolve().parent.parent / "hermes_core" / "indicators"


def test_rsi_known_series():
    s = [100, 102, 101, 103, 102, 104, 103, 105, 104, 106, 103, 101, 99, 100, 102]
    r = compute_rsi(s, 14)
    assert abs(r - 54.2) < 0.1  # recomputed Wilder RSI(14) = 54.1667


def test_atr_flat_zero():
    assert compute_atr([100] * 20, 14) == 0


def test_adx_range():
    r = compute_adx(
        [100, 101, 99, 102, 98, 103, 97, 104, 96, 105, 95, 106, 94, 107, 93, 108, 92, 109, 91, 110],
        14,
    )
    assert isinstance(r, float) and 0 <= r <= 100


def test_bb_ordering():
    s = [
        100, 102, 101, 103, 102, 104, 103, 105, 104, 106,
        103, 101, 99, 100, 102, 101, 103, 104, 102, 101,
    ]
    o = compute_bb(s, 20, 1.5)
    assert o["lower"] < o["middle"] < o["upper"]


# --- Mutation-killing value checks (drive mutmut score >= 80%) ----
def test_rsi_bounds_and_neutral():
    assert 0 <= compute_rsi([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], 14) <= 100
    assert compute_rsi([100] * 5, 14) == 50.0  # insufficient data -> neutral
    # all-up series -> RSI clamps to 100
    assert compute_rsi(list(range(30)), 14) == 100.0


def test_atr_grows_with_volatility():
    calm = compute_atr([100, 100.1, 99.9, 100.0, 100.05] * 4, 14)
    wild = compute_atr([100, 110, 90, 115, 85] * 4, 14)
    assert wild > calm >= 0


def test_bb_widens_with_volatility():
    narrow = compute_bb([100, 100.1, 99.9, 100.0, 100.05] * 4, 20, 1.5)
    wide = compute_bb([100, 120, 80, 118, 82] * 4, 20, 1.5)
    assert (wide["upper"] - wide["lower"]) > (narrow["upper"] - narrow["lower"])


def test_roc_sign_and_magnitude():
    flat = [100] * 20
    up = compute_roc(flat + [110], 20)
    down = compute_roc([110] + flat, 20)
    assert up > 0 and down < 0
    assert abs(up - 10.0) < 1e-9  # (110-100)/100*100 = 10%


def test_compute_all_keys_and_types():
    out = compute_all(list(range(40)))
    assert set(out.keys()) == {
        "rsi", "atr", "adx", "bb", "roc", "regime", "fast_regime", "divergence",
    }
    assert isinstance(out["bb"], dict)
    assert out["regime"] in ("trend", "range")
    assert out["fast_regime"] in ("up", "down", "flat")
    assert out["divergence"] in ("bullish", "bearish", "none")


def test_no_io_in_indicators():
    """S3 DO-NOT: no file/network calls inside the indicator module."""
    src = (INDICATORS_DIR / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = ("open(", "requests.", "urllib", "http", "socket.", "yfinance", "urlopen")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", getattr(func, "id", ""))
            if name in ("open",):
                pytest.fail("indicator module calls open()")
    for token in banned:
        assert token not in src, f"indicator module references {token}"
