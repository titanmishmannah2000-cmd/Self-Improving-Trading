"""Phase 6 — adaptive reflection: priors that learn from outcomes.

The contract under test:
  * with NO evidence every adaptive value equals the old hand-set constant
  * with evidence, steps / axis order / cooldown / thresholds / confidence move
  * hard risk guards (stop floor, schema bounds) never move
"""

from __future__ import annotations

import pytest

import hermes_core.engines.backtest as bt
import hermes_core.engines.decision_cortex as dc
from hermes_core.engines import adaptive as ad
from hermes_core.engines import experiment_control as ec
from hermes_core.engines.reflect import (
    GIVEBACK_CAPTURE_LO,
    LOW_WR,
    STOP_FLOOR,
    STOP_TIGHTEN,
    STOP_WIDEN,
    _axis_candidates,
    adaptive_bars,
    aggregate_trades,
    dynamic_confidence,
    layer1_rule_based,
    trade_pathology,
)
from hermes_core.engines.soak_controls import append_trade

BOT = "forex"
PAIR = "EUR/USD"
GOAL = {"max_drawdown": 10.0}
STRAT = {"version": "00", "stop_loss_pct": 1.5, "profit_target_pct": 3.0}


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_BOT_NAME", BOT)
    monkeypatch.setattr(dc, "CORTEX_DIR", None)
    monkeypatch.setattr(bt, "KB_PATH", None)
    yield


def _hist(entries):
    """Seed closed-experiment history."""
    data = ec._load(BOT, ec._EXPERIMENTS)
    data["_history"] = entries
    ec._save(BOT, ec._EXPERIMENTS, data)


def _exp(variable, status, chal=0.5, base=0.0, pair=PAIR):
    return {
        "pair": pair,
        "variable": variable,
        "status": status,
        "verdict": {"challenger_avg": chal, "champion_avg": base, "baseline": base},
    }


def _t(pnl, reason="sl", **kw):
    rec = {"pair": PAIR, "exit_reason": reason, "pnl_pct": pnl}
    rec.update(kw)
    return rec


# ── zero-evidence equivalence (no behaviour change until it learns) ─────────
def test_no_evidence_returns_priors():
    assert ad.axis_reliability(BOT, PAIR, "stop_loss_pct") == 0.5
    assert ad.step_scale(BOT, PAIR, "stop_loss_pct") == pytest.approx(1.0)
    assert ad.adaptive_step(BOT, PAIR, "stop_loss_pct", STOP_WIDEN) == pytest.approx(STOP_WIDEN)
    assert ad.adaptive_cooldown(BOT, PAIR, "stop_loss_pct", 30) == 30
    assert ad.confidence_weights(BOT) == ad.DEFAULT_CONF_WEIGHTS


def test_adaptive_bars_equal_priors_without_history():
    bars = adaptive_bars(BOT, PAIR, [])
    assert bars["low_wr"] == LOW_WR
    assert bars["giveback_capture_lo"] == GIVEBACK_CAPTURE_LO


def test_axis_order_unchanged_without_evidence():
    trades = [_t(-2.0) for _ in range(6)]
    agg = aggregate_trades(trades)
    path = trade_pathology(trades)
    prior = _axis_candidates(STRAT, agg, path, {"max_drawdown": 0.5})
    learned = _axis_candidates(STRAT, agg, path, {"max_drawdown": 0.5}, bot=BOT, pair=PAIR)
    assert [c[1] for c in prior] == [c[1] for c in learned]


# ── reliability learning ────────────────────────────────────────────────────
def test_reliability_rises_with_wins_falls_with_losses():
    _hist([_exp("trailing_stop_pct", "improved") for _ in range(4)])
    assert ad.axis_reliability(BOT, PAIR, "trailing_stop_pct") > 0.7

    _hist([_exp("stop_loss_pct", "reverted") for _ in range(4)])
    assert ad.axis_reliability(BOT, PAIR, "stop_loss_pct") < 0.3


def test_fail_streak_resets_on_success():
    _hist(
        [
            _exp("stop_loss_pct", "reverted"),
            _exp("stop_loss_pct", "reverted"),
            _exp("stop_loss_pct", "improved"),
        ]
    )
    st = ad.axis_outcomes(BOT, PAIR)["stop_loss_pct"]
    assert st["attempts"] == 3
    assert st["streak"] == 0  # a win clears the backoff


# ── adaptive step size ──────────────────────────────────────────────────────
def test_step_backs_off_after_consecutive_failures():
    _hist([_exp("stop_loss_pct", "reverted") for _ in range(3)])
    scaled = ad.adaptive_step(BOT, PAIR, "stop_loss_pct", STOP_WIDEN)
    assert scaled < STOP_WIDEN  # nudge, don't yank


def test_step_grows_with_severity_and_reliability():
    _hist([_exp("trailing_stop_pct", "improved") for _ in range(5)])
    mild = ad.adaptive_step(BOT, PAIR, "trailing_stop_pct", 0.4, effect=0.0)
    severe = ad.adaptive_step(BOT, PAIR, "trailing_stop_pct", 0.4, effect=1.0)
    assert severe > mild > 0.4


def test_step_scale_is_bounded():
    _hist([_exp("trailing_stop_pct", "improved") for _ in range(50)])
    assert ad.step_scale(BOT, PAIR, "trailing_stop_pct", effect=1.0) <= ad.STEP_SCALE_MAX
    _hist([_exp("stop_loss_pct", "reverted") for _ in range(50)])
    assert ad.step_scale(BOT, PAIR, "stop_loss_pct") >= ad.STEP_SCALE_MIN


# ── adaptive axis order ─────────────────────────────────────────────────────
def test_reliable_low_priority_axis_can_outrank_failing_high_priority():
    # stop_loss_pct (P4) keeps failing; trailing (P2) keeps working.
    _hist(
        [_exp("stop_loss_pct", "reverted") for _ in range(6)]
        + [_exp("trailing_stop_pct", "improved") for _ in range(6)]
    )
    cands = [
        (2, "trailing_stop_pct", 0.0, 0.4, "why", 0.5),
        (4, "stop_loss_pct", 1.5, 1.8, "why", 0.5),
    ]
    ordered = ad.sort_candidates(BOT, PAIR, cands)
    assert ordered[0][1] == "trailing_stop_pct"
    # And the failing axis is pushed further down than its prior priority.
    assert ad.axis_order_key(BOT, PAIR, 4, "stop_loss_pct") > 4.0
    assert ad.axis_order_key(BOT, PAIR, 2, "trailing_stop_pct") < 2.0


# ── adaptive thresholds ─────────────────────────────────────────────────────
def test_threshold_prior_holds_on_thin_sample():
    assert ad.adaptive_threshold(0.3, [0.9, 0.9], q=0.25, floor=0.1, cap=0.5) == 0.3


def test_threshold_moves_toward_pair_distribution():
    # A pair that habitually runs high win-rates should have a HIGHER "low" bar.
    high = [0.8] * 20
    bar = ad.adaptive_threshold(0.3, high, q=0.25, floor=0.1, cap=0.5)
    assert bar > 0.3
    assert bar <= 0.5  # clamped


def test_threshold_respects_clamps():
    assert ad.adaptive_threshold(0.3, [0.0] * 30, q=0.25, floor=0.1, cap=0.5) >= 0.1
    assert ad.adaptive_threshold(0.3, [1.0] * 30, q=0.25, floor=0.1, cap=0.5) <= 0.5


def test_adaptive_bars_learn_from_pair_history():
    for _ in range(30):
        append_trade(BOT, {"pair": PAIR, "exit_reason": "tp", "pnl_pct": 1.0})
    bars = adaptive_bars(BOT, PAIR, [])
    # Every close a winner → the pair's "low win-rate" bar rises above the prior.
    assert bars["low_wr"] > LOW_WR


# ── adaptive cooldown ───────────────────────────────────────────────────────
def test_cooldown_backs_off_exponentially():
    _hist([_exp("stop_loss_pct", "reverted")])
    one = ad.adaptive_cooldown(BOT, PAIR, "stop_loss_pct", 30)
    _hist([_exp("stop_loss_pct", "reverted") for _ in range(3)])
    three = ad.adaptive_cooldown(BOT, PAIR, "stop_loss_pct", 30)
    assert three > one


def test_cooldown_shorter_for_reliable_axis():
    _hist([_exp("trailing_stop_pct", "improved") for _ in range(6)])
    assert ad.adaptive_cooldown(BOT, PAIR, "trailing_stop_pct", 30) < 30


# ── adaptive evaluation window ──────────────────────────────────────────────
def test_eval_window_prior_on_thin_sample():
    assert ad.adaptive_eval_closes(10, [0.1, 0.2]) == 10


def test_eval_window_grows_with_noise():
    quiet = [0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
    noisy = [5.0, -5.0, 4.0, -4.5, 6.0, -6.0, 3.0, -3.5]
    assert ad.adaptive_eval_closes(10, noisy) > ad.adaptive_eval_closes(10, quiet)


def test_eval_window_is_bounded():
    wild = [100.0, -100.0] * 20
    n = ad.adaptive_eval_closes(10, wild)
    assert 3 <= n <= 60


# ── adaptive confidence ─────────────────────────────────────────────────────
def test_confidence_intercept_drops_when_proposals_keep_failing():
    _hist([_exp("stop_loss_pct", "reverted") for _ in range(10)])
    w = ad.confidence_weights(BOT)
    assert w["intercept"] < ad.DEFAULT_CONF_WEIGHTS["intercept"]
    # Lower intercept ⇒ lower confidence for the same evidence.
    assert dynamic_confidence(10, 0.5, 0.0, weights=w) < dynamic_confidence(10, 0.5, 0.0)


def test_confidence_intercept_rises_when_proposals_work():
    _hist([_exp("trailing_stop_pct", "improved") for _ in range(10)])
    w = ad.confidence_weights(BOT)
    assert w["intercept"] > ad.DEFAULT_CONF_WEIGHTS["intercept"]


# ── hard guards stay hard ───────────────────────────────────────────────────
def test_stop_floor_never_adapts():
    # Heavy evidence that bigger stop-tighten steps "work" must not breach floor.
    _hist([_exp("stop_loss_pct", "improved") for _ in range(50)])
    out = layer1_rule_based(
        PAIR,
        [_t(-5.0) for _ in range(6)],
        {"max_drawdown": 0.5},
        {"stop_loss_pct": STOP_FLOOR, "profit_target_pct": 3.0},
        bot=BOT,
    )
    assert out is not None
    assert float(out[2]) >= STOP_FLOOR


def test_adaptive_proposal_stays_in_schema_range():
    from hermes_core.config import STRATEGY_PARAM_RANGES

    _hist([_exp("trailing_stop_pct", "improved") for _ in range(40)])
    trades = [_t(1.0, "tp", mfe_pct=3.0, mfe_capture=0.1) for _ in range(4)] + [
        _t(-0.2) for _ in range(2)
    ]
    out = layer1_rule_based(PAIR, trades, GOAL, dict(STRAT), bot=BOT)
    assert out is not None
    lo, hi = STRATEGY_PARAM_RANGES[out[0]]
    assert lo <= float(out[2]) <= hi


def test_summary_reports_what_was_learned():
    _hist(
        [_exp("stop_loss_pct", "reverted") for _ in range(2)]
        + [_exp("trailing_stop_pct", "improved") for _ in range(3)]
    )
    s = ad.summary(BOT, PAIR)
    assert s["axes"]["stop_loss_pct"]["reverted"] == 2
    assert s["axes"]["trailing_stop_pct"]["improved"] == 3
    assert s["axes"]["trailing_stop_pct"]["reliability"] > s["axes"]["stop_loss_pct"]["reliability"]
