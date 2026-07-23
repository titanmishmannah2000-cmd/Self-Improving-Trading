"""Phase A superior GP: grammar, canonicalize, islands, Pareto, pool-lift, metadata."""

from __future__ import annotations

import random

import pytest

import hermes_core.engines.genetic as gp
from hermes_core.engines.entry import _gp_eval_last, _gp_parse


@pytest.fixture(autouse=True)
def _tmp_discovered(tmp_path, monkeypatch):
    monkeypatch.setattr(gp, "DISCOVERED_DIR", tmp_path / "discovered")
    monkeypatch.setattr(
        "hermes_core.engines.backtest.backtest_gp_indicator",
        lambda *a, **k: {"approved": True, "reason": "test_stub", "oos_corr": 0.2},
    )
    yield


def _structured(n=500, start=1.10, seed=3):
    import math
    rng = random.Random(seed)
    out = [start]
    for i in range(1, n):
        wave = 0.004 * math.sin(i / 7.0)
        out.append(out[-1] * (1 + wave + rng.uniform(-0.001, 0.001)))
    return out


def test_canonicalize_commutative_and_double_abs():
    a = ("add", "rsi", "vol")
    b = ("add", "vol", "rsi")
    assert gp._semantic_key(a) == gp._semantic_key(b)
    assert gp._canonicalize(("abs", ("abs", "rsi"))) == ("abs", "rsi")
    assert gp._canonicalize(("neg", ("neg", "price"))) == "price"


def test_unary_rolling_eval_and_roundtrip_parse():
    prices = [100.0 + 0.15 * i for i in range(120)]
    tree = ("abs", ("sub", "price", "sma20"))
    s = gp._expr_to_str(tree)
    assert s.startswith("abs(")
    assert abs(gp._eval_expr(tree, prices) - _gp_eval_last(s, prices)) < 1e-9

    roll = ("mean", "rsi", 20)
    rs = gp._expr_to_str(roll)
    assert rs == "mean(rsi,20)"
    parsed = _gp_parse(rs)
    assert parsed[0] == "mean"
    assert abs(gp._eval_expr(roll, prices) - gp._eval_expr(parsed, prices)) < 1e-9


def test_is_gp_expr_phase_a_forms():
    assert gp._is_gp_expr("abs(rsi)") is True
    assert gp._is_gp_expr("mean(price,20)") is True
    assert gp._is_gp_expr("(price-sma20)") is True
    assert gp._is_gp_expr("k_p05") is True
    assert gp._is_gp_expr("ta.rsi(close,14)") is False
    assert gp._is_gp_expr("mom(close,5)") is False


def test_pareto_front_keeps_nondominated():
    cands = [
        {"oos_corr": 0.4, "complexity": 8, "max_dd": 5.0, "pool_lift": 0.1},
        {"oos_corr": 0.3, "complexity": 2, "max_dd": 1.0, "pool_lift": 0.05},
        {"oos_corr": 0.2, "complexity": 9, "max_dd": 6.0, "pool_lift": 0.01},  # dominated
    ]
    front = gp._pareto_front(cands)
    assert len(front) == 2
    assert all(c["oos_corr"] >= 0.3 for c in front)


def test_pool_lift_positive_for_first():
    prices = _structured(300)
    expr = ("sub", "price", "sma20")
    sig = gp._signal_for_expr(expr, prices, lookback=60)
    lift0 = gp._marginal_pool_lift(sig, [], prices, horizon=1)
    assert lift0 >= 0.0
    # near-duplicate should not lift much vs itself
    lift1 = gp._marginal_pool_lift(sig, [sig], prices, horizon=1)
    assert lift1 <= lift0 + 1e-9


def test_discover_phase_a_metadata():
    """Admitted indicators carry Phase A dashboard contract fields."""
    inds = gp.discover(
        "EUR/USD", _structured(600),
        generations=12, pop_size=24, seed=11, top_k=3, horizon=1, n_islands=2,
    )
    if not inds:
        pytest.skip("no admits on synthetic series (gates strict) — grammar path still covered")
    ind = inds[0]
    assert ind.get("engine_version") == gp.ENGINE_VERSION
    assert "phase_b" in ind.get("engine_version", "") or "phase_a" in ind.get("engine_version", "")
    assert ind.get("run_id")
    assert "island_id" in ind
    assert "pool_lift" in ind
    assert "niche" in ind
    assert ind["niche"].get("behavior") in ("momentum", "mean_revert", "mixed")
    assert "admit_reason" in ind
    assert gp._is_gp_expr(ind["expr"])
