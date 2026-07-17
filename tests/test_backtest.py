"""Session 10 / Phase 10 tests for the backtest validation pipeline.

Network-free: prices are injected directly into backtest_with_history, and the
hypothesis KB is redirected to a temp file so the "rejected once -> KB hit"
behaviour is observable without touching real state.

Required blueprint names kept verbatim:
  test_oos_pass_crisis_fail_rejected, test_all_phases_pass,
  test_historical_kb_blocks, test_random_indicator_99th.
"""

from __future__ import annotations

import math
import random

import pytest

import hermes_core.engines.backtest as bt
from hermes_core.engines import backtest_with_history, phase0_corr


def _sine(n=200, start=1.10, amp=0.01, period=20, seed=1):
    """Oscillating series -> mean-reversion strategy has real edge (pass case)."""
    rng = random.Random(seed)
    return [start + amp * math.sin(2 * math.pi * i / period)
            + rng.uniform(-0.0003, 0.0003) for i in range(n)]


def _volatile(n=200, start=1.10, drop=0.01, noise=0.18, seed=7):
    """Sustained crash: steady -drop/bar plus +-noise. Regime=crisis (realized
    vol > 0.4) AND mean-reversion longs net-LOSE, so the crisis backtest fails."""
    rng = random.Random(seed)
    out = [start]
    for _ in range(1, n):
        out.append(out[-1] * (1 - drop + rng.uniform(-noise, noise)))
    return out


def _flat(n=500, start=1.10, amp=0.0003, seed=5):
    """Near-flat random walk -> random signals almost never correlate (>=0.15).
    n=500 makes the null corr std ~0.045, so 0.15 is ~3.3sigma -> <0.1% exceed."""
    rng = random.Random(seed)
    out = [start]
    for _ in range(1, n):
        out.append(out[-1] * (1 + rng.uniform(-amp, amp)))
    return out


STRAT_MR = {"strategy_type": "mean_reversion", "entry": {"threshold": 30},
            "stop_loss_pct": 1.5, "profit_target_pct": 3.0, "version": "03"}


@pytest.fixture(autouse=True)
def _tmp_kb(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "KB_PATH", tmp_path / "hypotheses_kb.jsonl")
    yield tmp_path


def test_oos_pass_crisis_fail_rejected():
    # blueprint: a proposal may look fine in-sample, but a crisis failure MUST
    # reject it. Widening the stop (1.5 -> 3.0) raises per-trade crisis losses
    # past the DD ceiling, so the crisis gate fires and approval is refused.
    prices = _volatile()  # crisis regime -> crisis backtest fails
    res = backtest_with_history(
        "EUR/USD", "stop_loss_pct", 1.5, 3.0,
        strategy=STRAT_MR, prices=prices,
    )
    assert res["approved"] is False
    assert "crisis" in res["reason"].lower()


def test_all_phases_pass():
    # blueprint: identical params on a sine (MR edge) -> all gates pass + bump.
    prices = _sine()
    res = backtest_with_history(
        "EUR/USD", "stop_loss_pct", 1.5, 1.5,
        strategy=STRAT_MR, prices=prices,
    )
    assert res["approved"] is True
    assert res["phases"]["phase6_deploy"]["version_bumped"] is not None


def test_historical_kb_blocks():
    # blueprint: a previously-rejected proposal is a KB hit on the 2nd call
    # (no re-run; cached rejection returned).
    prices = _volatile()
    first = backtest_with_history(
        "EUR/USD", "stop_loss_pct", 1.5, 3.0,
        strategy=STRAT_MR, prices=prices,
    )
    assert first["approved"] is False
    second = backtest_with_history(
        "EUR/USD", "stop_loss_pct", 1.5, 3.0,
        strategy=STRAT_MR, prices=prices,
    )
    assert second["kb_hit"] is True
    assert second["approved"] is False


def test_random_indicator_99th():
    # blueprint: >=19/20 random (white-noise) signals must FAIL OOS (corr < 0.15),
    # validating 0.15 == 99th percentile gate.
    prices = _flat(n=500)
    rng = random.Random(123)
    fails = 0
    for _ in range(20):
        random_signal = [rng.uniform(-1, 1) for _ in range(len(prices) - 1)]
        if phase0_corr(random_signal, prices) < 0.15:
            fails += 1
    assert fails >= 19


def test_permutation_flags_noise():
    # discipline: a white-noise signal must be LESS significant than the real
    # strategy edge on the same market. (Asserting a hard p>=0.05 is flaky at
    # large n because the null p is ~uniform, so we rank noise below real edge.)
    prices = _sine()
    rng = random.Random(99)
    noise = [rng.uniform(-1, 1) for _ in range(len(prices) - 1)]
    real_sig = bt._strategy_signal(prices, "mean_reversion", 30)
    p_noise, _, _ = bt._permutation_pvalue(noise, prices)
    p_real, _, _ = bt._permutation_pvalue(real_sig, prices)
    assert p_noise > p_real          # noise ranks below the genuine edge
    assert p_real < 0.05             # the real MR signal IS significant
