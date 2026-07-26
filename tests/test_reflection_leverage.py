"""High-leverage reflection gaps — richer verdict, regime credit, soft quarantine.

Covers:
  #1+#5 richer + sequential live verdict (early-abort losers only)
  #3 regime-conditioned axis reliability
  #4 pipeline negative evidence
  #6 soft direction / near-duplicate quarantine
"""

from __future__ import annotations

import pytest

import hermes_core.engines.backtest as bt
import hermes_core.engines.decision_cortex as dc
from hermes_core.engines import adaptive as ad
from hermes_core.engines import experiment_control as ec
from hermes_core.engines.live_verdict import (
    change_direction,
    dominant_regime,
    judge_live,
    trade_stats,
)
from hermes_core.engines.reflect import layer1_rule_based
from hermes_core.engines.soak_controls import append_trade

BOT = "forex"
PAIR = "EUR/USD"


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_BOT_NAME", BOT)
    monkeypatch.setattr(dc, "CORTEX_DIR", None)
    monkeypatch.setattr(bt, "KB_PATH", None)
    yield


def _close(pnl, version="01", *, regime=None, reason="sl"):
    rec = {
        "id": f"{BOT}:{PAIR}:{version}:{pnl}:{regime}",
        "bot": BOT,
        "pair": PAIR,
        "exit_reason": reason,
        "pnl_pct": pnl,
        "strategy_version": version,
    }
    if regime:
        rec["entry_regime"] = regime
    return rec


def _deploy(**kw):
    ec.record_deployment(
        BOT,
        PAIR,
        variable=kw.get("variable", "stop_loss_pct"),
        old=kw.get("old", 1.5),
        new=kw.get("new", 1.2),
        version_from="00",
        version_to="01",
        prior_strategy={"version": "00", "stop_loss_pct": 1.5, "profit_target_pct": 3.0},
        closed_count=kw.get("closed_count", 0),
    )


def _hist(entries):
    data = ec._load(BOT, ec._EXPERIMENTS)
    data["_history"] = entries
    ec._save(BOT, ec._EXPERIMENTS, data)


# ── live_verdict primitives ─────────────────────────────────────────────────
def test_trade_stats_mean_wr_and_drawdown():
    rows = [_close(1.0), _close(-0.5), _close(0.5), _close(-2.0)]
    st = trade_stats(rows)
    assert st["n"] == 4
    assert st["mean"] == pytest.approx(-0.25)
    assert st["win_rate"] == 0.5
    assert st["max_dd"] >= 2.0


def test_change_direction():
    assert change_direction(1.5, 1.8) == "up"
    assert change_direction(1.5, 1.2) == "down"
    assert change_direction(1.5, 1.5) is None
    assert change_direction("a", "b") == "set"


def test_dominant_regime_picks_mode():
    rows = [
        _close(0.1, regime="range"),
        _close(0.1, regime="range"),
        _close(0.1, regime="trend"),
    ]
    assert dominant_regime(rows) == "range"


def test_judge_pending_before_early_min():
    chal = [_close(-1.0), _close(-1.0)]
    champ = [_close(0.5, "00") for _ in range(4)]
    v = judge_live(chal, champ, need=10)
    assert v["status"] == "pending"
    assert v["have"] == 2


def test_judge_early_aborts_clear_loser():
    # Full window is 10, but 3 catastrophic closes should abort now.
    chal = [_close(-1.0), _close(-1.0), _close(-1.0)]
    champ = [_close(0.5, "00") for _ in range(4)]
    v = judge_live(chal, champ, need=10)
    assert v["status"] == "worsened"
    assert v.get("early_abort") is True


def test_judge_does_not_early_promote():
    # Strong early winner still waits for the full window.
    chal = [_close(1.0), _close(1.0), _close(1.0)]
    champ = [_close(-0.5, "00") for _ in range(4)]
    v = judge_live(chal, champ, need=10)
    assert v["status"] == "pending"
    assert not v.get("early_abort")


def test_judge_promotes_clear_winner_at_full_window():
    chal = [_close(0.8) for _ in range(5)]
    champ = [_close(-0.5, "00") for _ in range(4)]
    v = judge_live(chal, champ, need=5)
    assert v["status"] == "improved"
    assert v["dd_ok"] is True


def test_judge_rejects_noisy_thin_edge_with_bad_dd():
    # Mean barely positive but equity DD blows past champion → worsened.
    chal = [
        _close(3.0),
        _close(-4.0),
        _close(3.0),
        _close(-4.0),
        _close(2.5),
    ]
    champ = [_close(0.1, "00") for _ in range(5)]  # quiet champion, tiny DD
    v = judge_live(chal, champ, need=5, margin=0.0)
    # Mean of chal ≈ 0.1, equal-ish to champ — with bad DD should not promote.
    assert v["status"] == "worsened" or v["dd_ok"] is False


# ── wired into experiment_control ───────────────────────────────────────────
def test_evaluate_early_abort_via_maybe_auto_revert():
    _deploy()
    for _ in range(4):
        append_trade(BOT, _close(0.5, "00"))
    for _ in range(3):
        append_trade(BOT, _close(-0.8, "01"))
    # k=10 would have been pending under the old thin gate; sequential abort fires.
    res = ec.maybe_auto_revert(BOT, PAIR, k=10)
    assert res["status"] == "reverted"
    assert res["detail"].get("early_abort") is True


def test_evaluate_still_pending_when_mixed_mid_window():
    _deploy()
    for _ in range(4):
        append_trade(BOT, _close(0.2, "00"))
    # Mixed / not clearly doomed mid-window.
    append_trade(BOT, _close(0.1, "01"))
    append_trade(BOT, _close(-0.1, "01"))
    append_trade(BOT, _close(0.05, "01"))
    ev = ec.evaluate_experiment(BOT, PAIR, k=10)
    assert ev["status"] == "pending"


# ── #3 regime-conditioned credit ────────────────────────────────────────────
def test_regime_credit_does_not_average_across_regimes():
    _hist(
        [
            {
                "pair": PAIR,
                "variable": "trailing_stop_pct",
                "status": "improved",
                "regime": "range",
                "verdict": {"challenger_avg": 0.5, "baseline": 0.0, "regime": "range"},
            }
            for _ in range(5)
        ]
        + [
            {
                "pair": PAIR,
                "variable": "trailing_stop_pct",
                "status": "reverted",
                "regime": "trend",
                "verdict": {"challenger_avg": -0.5, "baseline": 0.0, "regime": "trend"},
            }
            for _ in range(5)
        ]
    )
    rel_range = ad.axis_reliability(BOT, PAIR, "trailing_stop_pct", regime="range")
    rel_trend = ad.axis_reliability(BOT, PAIR, "trailing_stop_pct", regime="trend")
    assert rel_range > 0.6
    assert rel_trend < 0.4
    # Global (no regime) sits between the two extremes.
    rel_all = ad.axis_reliability(BOT, PAIR, "trailing_stop_pct")
    assert rel_trend < rel_all < rel_range


def test_regime_reorders_axes_for_current_batch():
    _hist(
        [
            {
                "pair": PAIR,
                "variable": "stop_loss_pct",
                "status": "reverted",
                "regime": "range",
                "verdict": {"challenger_avg": -1.0, "baseline": 0.0},
            }
            for _ in range(6)
        ]
        + [
            {
                "pair": PAIR,
                "variable": "trailing_stop_pct",
                "status": "improved",
                "regime": "range",
                "verdict": {"challenger_avg": 0.8, "baseline": 0.0},
            }
            for _ in range(6)
        ]
    )
    cands = [
        (2, "trailing_stop_pct", 0.0, 0.4, "why", 0.5),
        (4, "stop_loss_pct", 1.5, 1.8, "why", 0.5),
    ]
    ordered = ad.sort_candidates(BOT, PAIR, cands, regime="range")
    assert ordered[0][1] == "trailing_stop_pct"


# ── #4 pipeline negative evidence ───────────────────────────────────────────
def test_pipeline_reject_soft_penalises_reliability():
    base = ad.axis_reliability(BOT, PAIR, "profit_target_pct")
    assert base == 0.5
    for _ in range(6):
        ec.record_pipeline_outcome(
            BOT,
            PAIR,
            variable="profit_target_pct",
            status="backtest_reject",
            old=3.0,
            new=2.5,
            regime="range",
        )
    rel = ad.axis_reliability(BOT, PAIR, "profit_target_pct", regime="range")
    assert rel < base
    st = ad.axis_outcomes(BOT, PAIR, regime="range")["profit_target_pct"]
    assert st["pipeline_rejects"] == 6


def test_pipeline_reject_sets_direction_cooldown():
    ec.record_pipeline_outcome(
        BOT,
        PAIR,
        variable="stop_loss_pct",
        status="backtest_reject",
        old=1.5,
        new=1.8,  # up
    )
    # Same direction blocked.
    ban = ec.direction_blocked(BOT, PAIR, "stop_loss_pct", 1.5, 1.7, closed_count=0)
    assert ban is not None and ban["reason"] == "direction_cooldown"
    # Opposite direction still allowed.
    assert ec.direction_blocked(BOT, PAIR, "stop_loss_pct", 1.5, 1.2, closed_count=0) is None


# ── #6 soft direction quarantine ────────────────────────────────────────────
def test_soft_quarantine_blocks_near_duplicate_widen():
    ec.set_direction_cooldown(
        BOT, PAIR, "stop_loss_pct", 1.5, 1.8, until_closed=50, reason="live_worse"
    )
    # Near-dupe in the same direction (1.5→1.7) is blocked.
    assert ec.soft_quarantined(BOT, PAIR, "stop_loss_pct", 1.5, 1.7, 10) is not None
    # After cooldown clears, allowed again.
    assert ec.soft_quarantined(BOT, PAIR, "stop_loss_pct", 1.5, 1.7, 50) is None


def test_layer1_skips_direction_quarantined_axis():
    # Low-WR batch would normally widen stop (P4). Quarantine that direction.
    ec.set_direction_cooldown(
        BOT, PAIR, "stop_loss_pct", 1.5, 1.8, until_closed=999, reason="test"
    )
    trades = [_close(-0.2) for _ in range(6)]
    for t in trades:
        append_trade(BOT, t)
    out = layer1_rule_based(
        PAIR,
        trades,
        {"max_drawdown": 10.0},
        {"stop_loss_pct": 1.5, "profit_target_pct": 3.0},
        bot=BOT,
    )
    # Widening stop is the only classic candidate → no proposal.
    assert out is None or out[0] != "stop_loss_pct" or float(out[2]) <= 1.5


def test_live_revert_also_sets_direction_cooldown():
    _deploy(old=1.5, new=1.2)  # down (tighten)
    for _ in range(4):
        append_trade(BOT, _close(0.5, "00"))
    for _ in range(3):
        append_trade(BOT, _close(-0.5, "01"))
    ec.maybe_auto_revert(BOT, PAIR, k=3)
    # Further tighten attempts blocked; widen allowed.
    closed = len(ec._pair_closes(BOT, PAIR))
    assert ec.direction_blocked(BOT, PAIR, "stop_loss_pct", 1.5, 1.0, closed) is not None
    assert ec.direction_blocked(BOT, PAIR, "stop_loss_pct", 1.5, 1.8, closed) is None


def test_kb_near_duplicate_soft_quarantine():
    bt._kb_record(PAIR, "trailing_stop_pct", 0.0, 0.4, False, "backtest flop", bot=BOT)
    hit = ec.soft_quarantined(BOT, PAIR, "trailing_stop_pct", 0.0, 0.35, closed_count=0)
    assert hit is not None
    assert hit["reason"] in ("kb_near_duplicate", "pipeline_near_duplicate", "direction_cooldown")
