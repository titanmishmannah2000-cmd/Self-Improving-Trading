"""GP indicators must clear the same 7-phase S10 backtest gate (items 9/15)."""

from __future__ import annotations

import math
import random

import pytest

import hermes_core.engines.backtest as bt
import hermes_core.engines.genetic as gp
from hermes_core.engines import backtest_gp_indicator
from hermes_core.engines.entry import gp_ensemble_signal


@pytest.fixture(autouse=True)
def _tmp_kb_and_discovered(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "KB_PATH", tmp_path / "hypotheses_kb.jsonl")
    monkeypatch.setattr(gp, "DISCOVERED_DIR", tmp_path / "discovered")
    yield


def _sine(n=300, start=1.10, amp=0.01, period=20, seed=1):
    rng = random.Random(seed)
    return [start + amp * math.sin(2 * math.pi * i / period)
            + rng.uniform(-0.0003, 0.0003) for i in range(n)]


def _flat(n=300, start=1.10, amp=0.0003, seed=5):
    rng = random.Random(seed)
    out = [start]
    for _ in range(1, n):
        out.append(out[-1] * (1 + rng.uniform(-amp, amp)))
    return out


STRAT = {"stop_loss_pct": 1.5, "profit_target_pct": 3.0, "version": "00"}


def test_gp_backtest_rejects_noise():
    prices = _flat()
    # Random-ish expression unlikely to clear OOS+perm on flat noise
    res = backtest_gp_indicator(
        "EUR/USD", "(vol*stdev20)", strategy=STRAT, prices=prices,
    )
    assert res["approved"] is False
    assert res.get("kb_hit") is not True or res["approved"] is False


def test_gp_backtest_kb_blocks_rerun():
    prices = _flat()
    first = backtest_gp_indicator(
        "EUR/USD", "(vol*stdev20)", strategy=STRAT, prices=prices,
    )
    assert first["approved"] is False
    second = backtest_gp_indicator(
        "EUR/USD", "(vol*stdev20)", strategy=STRAT, prices=prices,
    )
    assert second.get("kb_hit") is True
    assert second["approved"] is False


def test_ensemble_skips_unapproved_indicators():
    prices = [100.0 + 0.2 * i for i in range(120)]
    gp._save_discovered("EUR/USD", [
        {"name": "(price-sma20)", "expr": "(price-sma20)",
         "fitness": 0.4, "win_rate": 0.6, "backtest_approved": False,
         "interval": "1d", "horizon": 10},
        {"name": "(ema20-sma20)", "expr": "(ema20-sma20)",
         "fitness": 0.35, "win_rate": 0.55,
         "interval": "1d", "horizon": 10},  # missing flag == not approved
    ])
    assert gp_ensemble_signal("EUR/USD", prices, promote=False) is None


def test_ensemble_votes_only_approved():
    prices = [100.0 + 0.2 * i for i in range(110)] + [112.0 + 0.5 * j for j in range(10)]
    gp._save_discovered("EUR/USD", [
        {"name": "(price-sma20)", "expr": "(price-sma20)",
         "fitness": 0.4, "win_rate": 0.6, "backtest_approved": True,
         "interval": "1d", "horizon": 10},
        {"name": "(ema20-sma20)", "expr": "(ema20-sma20)",
         "fitness": 0.35, "win_rate": 0.55, "backtest_approved": True,
         "interval": "1d", "horizon": 10},
    ])
    sig = gp_ensemble_signal("EUR/USD", prices, promote=False)
    assert sig is not None
    assert sig.meta.get("num_active", 0) >= 2


def test_discover_marks_backtest_approved():
    rng = random.Random(3)
    prices = [1.10]
    for i in range(1, 400):
        wave = 0.004 * math.sin(i / 7.0)
        prices.append(prices[-1] * (1 + wave + rng.uniform(-0.001, 0.001)))
    inds = gp.discover(
        "EUR/USD", prices, generations=40, pop_size=40, top_k=3, horizon=10,
        interval="1d",
    )
    assert len(inds) >= 1
    for ind in inds:
        assert ind.get("backtest_approved") is True
        assert "backtest_reason" in ind
        assert ind.get("interval") == "1d"
        assert int(ind.get("horizon")) == 10
