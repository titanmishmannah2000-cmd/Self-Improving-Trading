"""Session 13 / Phase 13 tests for the genetic programming discovery engine.

Network-free. Discovered indicators are redirected to a temp dir via an
autouse fixture so they never touch real state/discovered.

Blueprint exact names preserved:
  test_discover_oos, test_survives_restart, test_random_low_rate, test_redundancy_reject
plus fitness formula, novelty gate, and the D8 (no-crypto) import check.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

import hermes_core.engines.genetic as gp


@pytest.fixture(autouse=True)
def _tmp_discovered(tmp_path, monkeypatch):
    """Redirect discovered-indicator storage into an isolated temp dir."""
    monkeypatch.setattr(gp, "DISCOVERED_DIR", tmp_path / "discovered")
    # S10 backtest is integration-heavy; unit tests focus on GP gates + grammar.
    monkeypatch.setattr(
        "hermes_core.engines.backtest.backtest_gp_indicator",
        lambda *a, **k: {"approved": True, "reason": "test_stub", "oos_corr": 0.2},
    )
    yield


def _structured(n=400, start=1.10, seed=3):
    """A price series with real structure (sine + slow trend) so a useful
    indicator can actually be discovered."""
    import math

    rng = random.Random(seed)
    out = [start]
    for i in range(1, n):
        wave = 0.004 * math.sin(i / 7.0)
        out.append(out[-1] * (1 + wave + rng.uniform(-0.001, 0.001)))
    return out


def _random_walk(n=400, start=1.10, seed=9):
    """Near-random walk with no exploitable structure (for the low-rate gate)."""
    rng = random.Random(seed)
    out = [start]
    for _ in range(1, n):
        out.append(out[-1] * (1 + rng.uniform(-0.003, 0.003)))
    return out


# ── blueprint Phase 13 success criteria ───────────────────────────────────
def test_discover_oos():
    inds = gp.discover(
        "EUR/USD",
        _structured(500),
        generations=20,
        pop_size=24,
        seed=3,
        n_islands=1,
    )
    # at least one discovered indicator clears the OOS floor
    assert len(inds) >= 1
    assert any(i["fitness"] >= 0.15 for i in inds)
    assert all(i["oos_corr"] >= gp.OOS_FLOOR for i in inds)


def test_survives_restart():
    gp.discover("EUR/USD", _structured(500), generations=20, pop_size=24, n_islands=1)
    # simulate a restart: re-load from disk
    reloaded = gp.load_discovered_indicators("EUR/USD")
    assert len(reloaded) >= 1
    assert reloaded[0]["pair"] == "EUR/USD"
    assert "expr" in reloaded[0]


def test_random_low_rate():
    """False-discovery gate: <5% of random expressions are ADMITTED on a
    structureless random walk.

    NOTE: a single OOS |r|>=0.15 threshold alone is NOT a sufficient noise gate
    here -- signal and forward-return are derived from the same series, so
    spurious autocorrelation leaks through and ~22% of random expressions clear
    it. That is exactly why the deployed pipeline (discover()) REQUIRES the
    combination: OOS>=0.15 AND a permutation null-test (p<0.05). The
    permutation test is what genuinely rejects lucky-noise candidates; measured
    here it admits ~2% on pure noise, comfortably under the blueprint's <5%.
    """
    prices = _random_walk()
    admitted = 0
    for s in range(50):
        rng = random.Random(s)
        expr = gp._random_expr(rng, depth=2)
        sig = gp._signal_for_expr(expr, prices)
        # same two-gate check discover() applies in production
        if gp._oos_corr(sig, prices) >= 0.15:
            p, _c, _n = gp._permutation_pvalue(sig, prices, n_perm=200, seed=s)
            if p < 0.05:
                admitted += 1
    assert admitted < 3  # <5% -> the combined gate is real, not noise-passing


def test_redundancy_reject():
    # an indicator perfectly correlated with an existing one must be rejected
    base = _structured()
    sig_a = gp._signal_for_expr(("add", "sma5", "sma20"), base)
    sig_b = [x * 1.001 for x in sig_a]  # near-identical -> |r| ~ 1.0
    assert gp.redundancy_check(sig_b, [sig_a]) == "REJECTED"
    # an uncorrelated signal is fine
    sig_c = gp._signal_for_expr(("sub", "price", "sma5"), base)
    assert gp.redundancy_check(sig_c, [sig_a]) == "OK"


# ── fitness formula + novelty gate (unit) ──────────────────────────────────
def test_fitness_formula():
    prices = _structured()
    expr = ("add", "sma5", "sma20")
    sig = gp._signal_for_expr(expr, prices)
    corr = gp._compute_fitness(sig, prices)
    penalised = gp._fitness_with_penalty(expr, prices)
    # penalised = |corr| - 0.001*complexity (complexity >= 1 for any expr)
    assert penalised <= corr
    assert penalised == pytest.approx(corr - gp.COMPLEXITY_PENALTY * gp._complexity(expr))


def test_novelty_gate_rejects_duplicate():
    pop = [("add", "sma5", "sma20"), ("sub", "price", "sma5")]
    dup = ("add", "sma5", "sma20")  # exact clone -> distance 0
    assert gp._novelty_ok(dup, pop) is False
    fresh = ("mul", "rsi", "vol")  # new shape -> admitted
    assert gp._novelty_ok(fresh, pop) is True


def test_genetic_engine_wrapper():
    eng = gp.GeneticEngine()
    inds = eng.discover(
        "GBP/JPY",
        _structured(500, seed=11),
        generations=15,
        pop_size=20,
        n_islands=1,
    )
    assert isinstance(inds, list)
    assert eng.load("GBP/JPY") == inds or len(eng.load("GBP/JPY")) >= 1


def test_no_crypto_imports():
    """D8: this module must not import or reference crypto-specific signals.

    We scan for *code* references (import statements / module paths), not the
    descriptive prose in the docstring that merely names the prohibition.
    """
    import ast

    src = (gp.__file__ and Path(gp.__file__).read_text(encoding="utf-8")) or ""
    tree = ast.parse(src)
    code_tokens: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                code_tokens.update(n.name.lower().split("."))
        elif isinstance(node, ast.ImportFrom) and node.module:
            code_tokens.update(node.module.lower().split("."))
    for banned in ("onchain", "fng", "feargreed", "fear", "crypto", "btc"):
        assert banned not in code_tokens, f"crypto-linked import token: {banned}"
    # only market-data primitives are in the feature set
    assert all(f in gp.FEATURES for f in ("price", "ret", "sma5", "sma20", "rsi", "vol"))
