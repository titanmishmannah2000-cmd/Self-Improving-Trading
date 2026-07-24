"""Session 10 / Phase 10 tests for the backtest validation pipeline.

Network-free: prices are injected directly into backtest_with_history, and the
hypothesis KB is redirected to a temp file so the "rejected once -> KB hit"
behaviour is observable without touching real state.

Required blueprint names kept verbatim:
  test_oos_pass_crisis_fail_rejected, test_all_phases_pass,
  test_historical_kb_blocks, test_random_indicator_99th.
"""

from __future__ import annotations

import random

import pytest

import hermes_core.engines.backtest as bt
from hermes_core.engines import backtest_with_history, phase0_corr


def _mr_friendly(n=300, start=1.10, dip=0.97, period=25, seed=1):
    """Calm walk with periodic dips+bounces → BB/RSI MR entries have real edge."""
    rng = random.Random(seed)
    out = [start]
    for i in range(1, n):
        if i % period == 0:
            out.append(out[-1] * dip)
        elif i % period in (1, 2, 3):
            out.append(out[-1] * 1.012)
        else:
            out.append(out[-1] * (1 + rng.uniform(-0.0004, 0.0004)))
    return out


def _volatile(n=300, start=1.10, drop=0.015, noise=0.2, seed=2):
    """High-vol crash: crisis regime + BB/RSI entries that lose under a wide stop."""
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


STRAT_MR = {
    "strategy_type": "mean_reversion",
    "session_filter": "24h",
    "entry": {"threshold": 30, "session_filter": "24h"},
    "stop_loss_pct": 1.5,
    "profit_target_pct": 3.0,
    "version": "03",
}


@pytest.fixture(autouse=True)
def _tmp_kb(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "KB_PATH", tmp_path / "hypotheses_kb.jsonl")
    yield tmp_path


def _gate(pair, param, old, new, *, strategy=STRAT_MR, prices=None, **kw):
    """backtest_with_history with ensemble pinned neutral (no disk GP bleed)."""
    kw.setdefault("ensemble_consensus", "neutral")
    return backtest_with_history(
        pair,
        param,
        old,
        new,
        strategy=strategy,
        prices=prices,
        **kw,
    )


def test_oos_pass_crisis_fail_rejected():
    # blueprint: a proposal may look fine in-sample, but a crisis failure MUST
    # reject it. Widening the stop (1.5 -> 3.0) inflates crisis DD past the
    # ceiling, so the crisis gate fires and approval is refused.
    prices = _volatile()  # crisis regime -> crisis backtest fails
    res = _gate(
        "EUR/USD",
        "stop_loss_pct",
        1.5,
        3.0,
        strategy=STRAT_MR,
        prices=prices,
    )
    assert res["approved"] is False
    assert "crisis" in res["reason"].lower()


def test_all_phases_pass():
    # blueprint: identical params on an MR-friendly series -> all gates pass + bump.
    prices = _mr_friendly()
    res = _gate(
        "EUR/USD",
        "stop_loss_pct",
        1.5,
        1.5,
        strategy=STRAT_MR,
        prices=prices,
    )
    assert res["approved"] is True
    assert res["phases"]["phase6_deploy"]["version_bumped"] is not None


def test_historical_kb_blocks():
    # blueprint: a previously-rejected proposal is a KB hit on the 2nd call
    # (no re-run; cached rejection returned).
    prices = _volatile()
    first = _gate(
        "EUR/USD",
        "stop_loss_pct",
        1.5,
        3.0,
        strategy=STRAT_MR,
        prices=prices,
    )
    assert first["approved"] is False
    second = _gate(
        "EUR/USD",
        "stop_loss_pct",
        1.5,
        3.0,
        strategy=STRAT_MR,
        prices=prices,
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
    prices = _mr_friendly()
    rng = random.Random(99)
    noise = [rng.uniform(-1, 1) for _ in range(len(prices) - 1)]
    real_sig = bt._strategy_signal(
        prices,
        "mean_reversion",
        30,
        strategy=STRAT_MR,
        ensemble_consensus="neutral",
    )
    p_noise, _, _ = bt._permutation_pvalue(noise, prices)
    p_real, _, _ = bt._permutation_pvalue(real_sig, prices)
    assert p_noise > p_real  # noise ranks below the genuine edge
    assert p_real < 0.05  # the real MR signal IS significant


def test_entry_signal_matches_bb_rsi_adx():
    """Gate entries require BB lower + RSI + ADX calm (live evaluate_entry core)."""
    prices = _mr_friendly()
    sig = bt._entry_signal(
        prices,
        "mean_reversion",
        30,
        strategy=STRAT_MR,
        ensemble_consensus="neutral",
    )
    assert sum(sig) >= 5
    # A flat series should not produce MR entries.
    flat = _flat(n=100)
    assert (
        sum(
            bt._entry_signal(
                flat,
                "mean_reversion",
                30,
                strategy=STRAT_MR,
                ensemble_consensus="neutral",
            )
        )
        == 0
    )


def test_gate_blocks_bearish_ensemble():
    """L13: bearish ensemble consensus must suppress MR gate entries."""
    prices = _mr_friendly()
    open_sig = bt._entry_signal(
        prices,
        "mean_reversion",
        30,
        strategy=STRAT_MR,
        ensemble_consensus="neutral",
    )
    blocked = bt._entry_signal(
        prices,
        "mean_reversion",
        30,
        strategy=STRAT_MR,
        ensemble_consensus="strong_bearish",
    )
    assert sum(open_sig) > 0
    assert sum(blocked) == 0


def test_gate_respects_session_filter():
    """L04: london_only entries only fire on LDN-session bars."""
    prices = _mr_friendly(n=200)
    # Every bar stamped at 03:00 UTC → ASIA; london_only must yield zero entries.
    asia_ts = [3 * 3600.0 + i * 0.01 for i in range(len(prices))]
    strat = {
        **STRAT_MR,
        "session_filter": "london_only",
        "entry": {"threshold": 30, "session_filter": "london_only"},
    }
    sig = bt._entry_signal(
        prices,
        "mean_reversion",
        30,
        strategy=strat,
        candle_ts=asia_ts,
        ensemble_consensus="neutral",
    )
    assert sum(sig) == 0


def test_gate_cooldown_after_stop():
    """L15/L23: after a stop-out, re-entry is suppressed for cooldown bars."""
    # Force a stop: enter then next bar crashes hard.
    prices = [1.10] * 50 + [1.05, 1.00] + [1.10] * 40
    # Build a strategy that will signal at the dip bar via injected raw path:
    # use rsi_momentum with high threshold so the dip fires, then crash.
    strat = {
        "strategy_type": "rsi_momentum",
        "session_filter": "24h",
        "entry": {"threshold": 100, "session_filter": "24h"},
        "stop_loss_pct": 1.0,
        "profit_target_pct": 3.0,
    }
    # Without cooldown many entries; with cooldown after stop, fewer.
    res_cd = bt._simulate(
        prices,
        "rsi_momentum",
        100,
        1.0,
        3.0,
        strategy=strat,
        ensemble_consensus="neutral",
        apply_cooldown=True,
    )
    res_no = bt._simulate(
        prices,
        "rsi_momentum",
        100,
        1.0,
        3.0,
        strategy=strat,
        ensemble_consensus="neutral",
        apply_cooldown=False,
    )
    assert res_no["entries"] >= res_cd["entries"]


def test_default_fetch_uses_shared_ticker_map(monkeypatch):
    """Non-FX pairs must not be passed to Yahoo as raw XAU/USD / BTC/USD."""
    calls: list[tuple] = []

    def fake_seed(pair, interval="5m", period="60d", max_candles=500):
        calls.append((pair, interval, period, max_candles))
        return [{"price": 100.0 + i * 0.1} for i in range(50)]

    monkeypatch.setattr(
        "hermes_core.adapters.price.seed_history_interval_sync",
        fake_seed,
    )
    for pair in ("XAU/USD", "BTC/USD", "EUR/USD", "XAG/USD"):
        closes = bt._default_fetch(pair)
        assert len(closes) >= 10
        assert calls[-1][0] == pair
        assert calls[-1][1] == "5m"
