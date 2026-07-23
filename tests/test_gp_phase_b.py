"""Phase B superior GP: ε-lexicase, MAP-Elites, constant polish, discovery pulse."""

from __future__ import annotations

import random

import pytest

import hermes_core.engines.genetic as gp


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


def test_regime_slices_and_lexicase_selects():
    prices = _structured(400)
    slices = gp._regime_slices(prices, 4)
    assert len(slices) >= 2
    assert all(len(s) >= 40 for s in slices)

    pop = [
        ("sub", "price", "sma20"),
        ("add", "rsi", "vol"),
        ("mul", "mom10", "k_p05"),
    ]
    mat = gp._case_fitness_matrix(pop, prices, horizon=1, n_cases=3)
    assert len(mat) == 3
    assert all(len(row) >= 1 for row in mat)
    rng = random.Random(0)
    parent = gp._epsilon_lexicase_select(pop, mat, rng)
    assert parent in pop


def test_constant_polish_can_improve_or_keep():
    prices = _structured(300)
    expr = ("mul", "rsi", "k_p01")
    rng = random.Random(1)
    out = gp._polish_constants(expr, prices, horizon=1, rng=rng, tries=5)
    # Still a valid tree; polish is best-effort.
    assert isinstance(out, (tuple, str))
    assert gp._complexity(out) >= 1


def test_map_elites_insert_keeps_best_per_cell():
    archive = {}
    weak = {
        "oos_corr": 0.2, "complexity": 2, "behavior": "momentum",
        "horizon": 60, "expr_str": "a", "niche_key": "momentum|1-3|h_long",
    }
    strong = {
        "oos_corr": 0.5, "complexity": 2, "behavior": "momentum",
        "horizon": 60, "expr_str": "b", "niche_key": "momentum|1-3|h_long",
    }
    gp._map_elites_insert(archive, weak)
    gp._map_elites_insert(archive, strong)
    assert len(archive) == 1
    assert archive["momentum|1-3|h_long"]["expr_str"] == "b"
    cov = gp._map_elites_coverage(archive)
    assert cov["filled"] == 1
    assert cov["total_cells"] == 27


def test_prefer_niche_diverse_round_robin():
    inds = [
        {"name": "a", "fitness": 0.9, "niche_key": "momentum|1-3|h_long"},
        {"name": "b", "fitness": 0.8, "niche_key": "momentum|1-3|h_long"},
        {"name": "c", "fitness": 0.7, "niche_key": "mean_revert|4-6|h_long"},
        {"name": "d", "fitness": 0.6, "niche_key": "mixed|7+|h_med"},
    ]
    out = gp.prefer_niche_diverse(inds, max_per_niche=1)
    # First three should cover three niches (order by niche key sort + rr).
    first = {i["name"] for i in out[:3]}
    assert "a" in first  # best in momentum niche
    assert "c" in first
    assert "d" in first
    assert "b" in {i["name"] for i in out}  # leftover still present


def test_discovery_pulse_persisted():
    inds = gp.discover(
        "EUR/USD", _structured(600),
        generations=8, pop_size=16, seed=5, top_k=2, horizon=1, n_islands=1,
    )
    pulse = gp.load_discovery_pulse("EUR/USD")
    assert pulse is not None
    assert pulse.get("engine_version") == gp.ENGINE_VERSION
    assert "phase_b" in pulse["engine_version"]
    assert "candidates_evaluated" in pulse
    assert "map_elites" in pulse
    assert "admitted" in pulse
    assert pulse["admitted"] == len(inds)
    if inds:
        assert inds[0].get("engine_version") == gp.ENGINE_VERSION
        assert inds[0].get("niche_key") or inds[0].get("niche")


def test_niche_map_from_indicators():
    inds = [
        {"complexity": 2, "horizon": 60, "niche": {"behavior": "momentum", "complexity_bin": "1-3", "horizon_bin": "h_long"}},
        {"complexity": 5, "horizon": 60, "niche_key": "mean_revert|4-6|h_long"},
    ]
    nm = gp.niche_map_from_indicators(inds)
    assert nm["filled"] >= 1
    assert nm["total_cells"] == 27
    assert sum(nm["counts"].values()) == 2
