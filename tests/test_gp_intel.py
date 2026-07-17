"""Session 14 / Phase 14 tests for the GP intelligence layer.

Network-free. GP governance state (lockout/cull) is redirected to a temp file
via an autouse fixture so tests never touch real state.

Blueprint exact names preserved:
  test_strong_bullish_label, test_default_zero_after_fix, test_consecutive_lockout,
  test_cull_low_wr
plus score bounds, suppression reason, and the Problem-4 regime-mismatch
separation (degradation-cull vs weight-penalty).
"""

from __future__ import annotations

import pytest

import hermes_core.engines.gp_intelligence as gp


@pytest.fixture(autouse=True)
def _tmp_gp_state(tmp_path, monkeypatch):
    """Redirect GP governance state into an isolated temp file."""
    monkeypatch.setattr(gp, "GP_STATE", tmp_path / "gp_intelligence.json")
    yield


def _ind(signal, wr=0.5, fitness=0.6, trained=("BULL",)):
    return {"id": "i", "signal": signal, "wr": wr, "fitness": fitness,
            "trained_regimes": list(trained)}


# ── blueprint Phase 14 success criteria ───────────────────────────────────
def test_strong_bullish_label():
    inds = [_ind(0.9), _ind(0.7), _ind(0.8)]   # all signal>0.2, wr>0.5
    assert gp.get_label(inds) == "strong_bullish"
    # a 2/3 bullish mix is still bullish (not conflict): score~0.33, agree~0.67
    assert gp.get_label([_ind(0.9), _ind(-0.8), _ind(0.5)]) == "bullish"
    assert gp.get_label([]) == "conflict"


def test_default_zero_after_fix():
    # PROBLEM 3: the original -0.3 default deadlocked new indicators because
    # the entry gate requires score >= 0. A fresh pair must start at NEUTRAL 0.0
    # so its first entries can fire and accumulate the data that earns a real
    # score. -0.3 would mean "never enters -> never learns -> never rises".
    assert gp.DEFAULT_GP_SCORE == 0.0
    assert gp.gp_entry_score("EUR/USD_BRAND_NEW") == 0.0   # not -0.3
    # and it is NOT suppressed at neutral (gate is >= 0)
    suppressed, _reason = gp.should_suppress("EUR/USD_BRAND_NEW")
    assert suppressed is False


def test_consecutive_lockout():
    pair = "EUR/USD"
    assert gp.is_locked(pair) is False
    gp.record_loss(pair)
    gp.record_loss(pair)
    assert gp.is_locked(pair) is False          # 2 not enough
    gp.record_loss(pair)
    assert gp.is_locked(pair) is True           # >=3 -> locked
    # a win resets the counter
    gp.record_win(pair)
    assert gp.is_locked(pair) is False


def test_cull_low_wr():
    # genuine degradation: same-regime WR 0.35 over 50 signals -> culled.
    reg = [{"id": "x", "fitness": 0.7, "trained_regimes": ["BULL"],
            "by_regime": {"BULL": {"wins": 0, "signals": 0}}}]
    # feed 50 outcomes, 30% wins (0.30 WR < 0.40 cull threshold)
    for k in range(50):
        outcome = 1.0 if k % 10 < 3 else -1.0   # 3/10 = 0.30 win rate
        reg = gp._update_indicator(reg, "x", outcome, "BULL")
    culled = [i for i in reg if i["id"] == "x"][0]
    assert culled["culled"] is True
    assert culled["cull_reason"] is not None


# ── score bounds + suppression reason ──────────────────────────────────────
def test_score_bounded():
    # after enough winning data the score stays within [-1, 1]
    gp.record_win("GBP/JPY")   # reset any lockout
    s = gp.gp_entry_score("GBP/JPY")
    assert -1.0 <= s <= 1.0


def test_suppress_gives_reason():
    pair = "AUD/USD"
    suppressed, reason = gp.should_suppress(pair)
    if suppressed:
        assert isinstance(reason, str) and reason
    # lockout path yields a concrete reason
    gp.record_loss(pair)
    gp.record_loss(pair)
    gp.record_loss(pair)
    suppressed, reason = gp.should_suppress(pair)
    assert suppressed is True
    assert "locked" in reason.lower()


# ── Problem 4 separation: regime mismatch is penalty, NOT cull ──────────────
def test_regime_mismatch_weight_penalty_not_cull():
    # indicator trained in BULL, evaluated in NEUTRAL -> weight penalized, kept
    ind = _ind(0.5, fitness=0.8, trained=("BULL",))
    assert gp.weight_for(ind, "BULL") == 0.8
    assert gp.weight_for(ind, "NEUTRAL") == pytest.approx(0.8 * gp.REGIME_MISMATCH_PENALTY)
    assert ind.get("culled") is None          # never culled for mismatch
    # but a same-regime low-WR indicator IS culled (degradation), not just penalized
    reg = [{"id": "y", "fitness": 0.8, "trained_regimes": ["BULL"],
            "by_regime": {"BULL": {"wins": 0, "signals": 0}}}]
    for k in range(50):
        outcome = 1.0 if k % 10 < 3 else -1.0
        reg = gp._update_indicator(reg, "y", outcome, "BULL")
    degraded = [i for i in reg if i["id"] == "y"][0]
    assert degraded["culled"] is True
    assert gp.weight_for(degraded, "BULL") == 0.0   # culled -> zero weight


def test_gp_intelligence_wrapper():
    eng = gp.GPIntelligence()
    assert eng.score("EUR/USD") == 0.0
    eng.record_loss("EUR/USD")
    eng.record_loss("EUR/USD")
    eng.record_loss("EUR/USD")
    assert eng.is_locked("EUR/USD") is True
    suppressed, reason = eng.should_suppress("EUR/USD")
    assert suppressed and reason
