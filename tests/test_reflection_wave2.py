"""Wave 2 — cadence, transfer, I/O cache, plans, L2 trust, explore/shadow."""

from __future__ import annotations

import time

import pytest

import hermes_core.engines.backtest as bt
import hermes_core.engines.decision_cortex as dc
from hermes_core.engines import adaptive as ad
from hermes_core.engines import experiment_control as ec
from hermes_core.engines import trades_cache as tc
from hermes_core.engines.reflect import (
    call_llm_consensus,
    layer1_rule_based,
    maybe_reflect_pair,
)
from hermes_core.engines.soak_controls import append_trade

BOT = "forex"
PAIR = "EUR/USD"
PAIR2 = "GBP/USD"


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_BOT_NAME", BOT)
    monkeypatch.setattr(dc, "CORTEX_DIR", None)
    monkeypatch.setattr(bt, "KB_PATH", None)
    tc.invalidate()
    yield
    tc.invalidate()


def _close(pnl, version="00", *, pair=PAIR, regime=None):
    rec = {
        "id": f"{BOT}:{pair}:{version}:{pnl}:{time.time()}",
        "bot": BOT,
        "pair": pair,
        "exit_reason": "sl",
        "pnl_pct": pnl,
        "strategy_version": version,
    }
    if regime:
        rec["entry_regime"] = regime
    return rec


def _hist(entries):
    data = ec._load(BOT, ec._EXPERIMENTS)
    data["_history"] = entries
    ec._save(BOT, ec._EXPERIMENTS, data)


# ── #2 cadence ──────────────────────────────────────────────────────────────
def test_adaptive_reflection_every_equals_prior_without_evidence():
    assert ad.adaptive_reflection_every(BOT, PAIR, 5) == 5


def test_adaptive_reflection_every_grows_after_reverts():
    _hist(
        [
            {"pair": PAIR, "variable": "stop_loss_pct", "status": "reverted"}
            for _ in range(6)
        ]
    )
    assert ad.adaptive_reflection_every(BOT, PAIR, 5) > 5


def test_adaptive_l2_thresholds_harden_when_l2_fails_live():
    # Simulate L2 approvals that then reverted.
    for _ in range(6):
        ec.record_l2_votes(
            BOT, PAIR, votes={"deepseek": True, "gemini": True, "groq": True}, decision=True
        )
        ec.record_l2_outcome(BOT, PAIR, {"variable": "x"}, outcome="reverted")
    mn, uni = ad.adaptive_l2_thresholds(BOT, PAIR)
    assert mn > 65.0
    assert uni > 75.0


def test_adaptive_deploy_cooldown_lengthens_after_reverts():
    _hist([{"pair": PAIR, "status": "reverted"} for _ in range(5)])
    assert ad.adaptive_deploy_cooldown_s(BOT, PAIR, 86400) > 86400


# ── #7 cross-pair transfer ──────────────────────────────────────────────────
def test_cold_pair_inherits_weak_prior_from_fleet():
    _hist(
        [
            {
                "pair": PAIR,
                "variable": "trailing_stop_pct",
                "status": "improved",
                "verdict": {"challenger_avg": 0.5, "baseline": 0.0},
            }
            for _ in range(8)
        ]
    )
    # PAIR2 has no history → should be pulled toward PAIR's success, but not equal.
    cold = ad.axis_reliability(BOT, PAIR2, "trailing_stop_pct")
    warm = ad.axis_reliability(BOT, PAIR, "trailing_stop_pct")
    assert cold > 0.5
    assert cold < warm


# ── #8 trades cache ─────────────────────────────────────────────────────────
def test_trades_cache_returns_same_as_direct_and_invalidates():
    for _ in range(3):
        append_trade(BOT, _close(0.1))
    a = tc.closed_trades(BOT, PAIR)
    b = tc.closed_trades(BOT, PAIR)
    assert len(a) == 3 and a == b
    append_trade(BOT, _close(-0.2))
    c = tc.closed_trades(BOT, PAIR)
    assert len(c) == 4


# ── #9 planned chains ───────────────────────────────────────────────────────
def test_plan_saved_and_preferred_on_next_l1():
    # Batch with BOTH giveback (trail) and low WR (widen stop) signals.
    # 3 winners + 8 losers → WR ≈ 0.27 < 0.30 so P4 also fires.
    trades = [
        {"pair": PAIR, "exit_reason": "tp", "pnl_pct": 1.0, "mfe_pct": 3.0, "mfe_capture": 0.2}
        for _ in range(3)
    ] + [{"pair": PAIR, "exit_reason": "sl", "pnl_pct": -0.5} for _ in range(8)]
    for t in trades:
        append_trade(BOT, t)
    out = layer1_rule_based(
        PAIR,
        trades,
        {"max_drawdown": 10.0},
        {"stop_loss_pct": 1.5, "profit_target_pct": 3.0, "trailing_stop_pct": 0.0},
        bot=BOT,
    )
    assert out is not None
    plan = ec.peek_plan(BOT, PAIR)
    assert plan is not None
    assert plan["steps"]
    nxt = ec.next_plan_step(BOT, PAIR)
    assert nxt and nxt.get("variable")


def test_advance_plan_after_improve():
    ec.save_plan(
        BOT,
        PAIR,
        [
            {"variable": "trailing_stop_pct", "old": 0.0, "new": 0.4},
            {"variable": "position_size_r", "old": 0.4, "new": 0.3},
        ],
    )
    ec.advance_plan(BOT, PAIR, consumed_variable="trailing_stop_pct")
    step = ec.next_plan_step(BOT, PAIR)
    assert step["variable"] == "position_size_r"


# ── #10 L2 trust ────────────────────────────────────────────────────────────
def test_l2_weights_rise_for_accurate_models():
    for _ in range(5):
        ec.record_l2_votes(
            BOT,
            PAIR,
            votes={"deepseek": True, "gemini": False, "groq": True},
            decision=True,
        )
        # improved → deepseek/groq correct (yes), gemini correct (no)? 
        # agreed = (voted_yes == good). good=True → yes voters correct.
        ec.record_l2_outcome(BOT, PAIR, {}, outcome="improved")
    assert ec.l2_model_weight(BOT, "deepseek") > 1.0
    assert ec.l2_model_weight(BOT, "gemini") < 1.0


def test_weighted_consensus_matches_classic_with_default_weights():
    callers = {
        "deepseek": lambda p: "YES",
        "gemini": lambda p: "YES",
        "groq": lambda p: "NO",
    }
    cons = call_llm_consensus(
        {"variable": "stop_loss_pct", "old": 1.5, "new": 1.2, "pair": PAIR, "reason": "t"},
        score=70,
        confidence=0.7,
        callers=callers,
        bot=BOT,
    )
    assert cons.decision is True
    assert cons.votes_yes == 2


# ── #11 explore + shadow ────────────────────────────────────────────────────
def test_revert_enters_explore_size_down():
    ec.record_deployment(
        BOT,
        PAIR,
        variable="stop_loss_pct",
        old=1.5,
        new=1.2,
        version_from="00",
        version_to="01",
        prior_strategy={"version": "00", "stop_loss_pct": 1.5, "profit_target_pct": 3.0},
        closed_count=0,
    )
    for _ in range(4):
        append_trade(BOT, _close(0.5, "00"))
    for _ in range(3):
        append_trade(BOT, _close(-0.5, "01"))
    res = ec.maybe_auto_revert(BOT, PAIR, k=3)
    assert res["status"] == "reverted"
    assert ec.in_explore(BOT, PAIR) is True
    assert ec.pair_safe_mode(BOT, PAIR)["mode"] == "size_down"


def test_improve_clears_explore():
    ec.enter_explore(BOT, PAIR, reason="explore_underperforming")
    assert ec.in_explore(BOT, PAIR)
    ec.record_deployment(
        BOT,
        PAIR,
        variable="stop_loss_pct",
        old=1.5,
        new=1.2,
        version_from="00",
        version_to="01",
        prior_strategy={"version": "00", "stop_loss_pct": 1.5, "profit_target_pct": 3.0},
        closed_count=0,
    )
    from hermes_core.engines.reflect import apply_strategy_change

    apply_strategy_change(
        PAIR,
        "stop_loss_pct",
        1.2,
        bot=BOT,
        version="01",
        strategy={"version": "00", "stop_loss_pct": 1.5, "profit_target_pct": 3.0},
    )
    for _ in range(4):
        append_trade(BOT, _close(-0.5, "00"))
    for _ in range(3):
        append_trade(BOT, _close(0.8, "01"))
    res = ec.maybe_auto_revert(BOT, PAIR, k=3)
    assert res["status"] == "improved"
    assert ec.in_explore(BOT, PAIR) is False


def test_shadow_challenger_recorded():
    ec.record_shadow_challenger(
        BOT,
        PAIR,
        variable="trailing_stop_pct",
        old=0.0,
        new=0.4,
        reason="auto_deploy_off",
        backtest={"approved": True, "improvement_full": 0.5},
        version="02",
    )
    sh = ec.shadow_challenger(BOT, PAIR)
    assert sh["variable"] == "trailing_stop_pct"
    assert sh["status"] == "shadow"
    assert sh["version"] == "02"
    pending = ec.list_pending_deploys(BOT, [PAIR])
    assert len(pending) == 1
    assert pending[0]["status"] == "approved_pending_deploy"
    assert pending[0]["deployable"] is True


def test_approve_pending_deploy_writes_yaml(monkeypatch):
    applied = {}

    def _fake_load(pair, bot=None):
        return {
            "pair": pair,
            "version": "01",
            "stop_loss_pct": 1.5,
            "profit_target_pct": 3.0,
            "trailing_stop_pct": 0.0,
        }

    def _fake_apply(pair, variable, new_val, *, bot="forex", version=None, strategy=None):
        applied.update(
            {"pair": pair, "variable": variable, "new": new_val, "version": version, "bot": bot}
        )
        out = dict(strategy or {})
        out[variable] = new_val
        if version is not None:
            out["version"] = str(version)
        return out

    monkeypatch.setattr("hermes_core.config.load_strategy_for_pair", _fake_load)
    monkeypatch.setattr("hermes_core.engines.reflect.apply_strategy_change", _fake_apply)
    monkeypatch.setattr(ec, "deploy_blocked", lambda *a, **k: None)

    ec.record_shadow_challenger(
        BOT,
        PAIR,
        variable="trailing_stop_pct",
        old=0.0,
        new=0.4,
        reason="auto_deploy_off",
        version="02",
    )
    out = ec.approve_pending_deploy(BOT, PAIR, source="test_approve")
    assert out.get("ok") is True
    assert out.get("status") == "deployed"
    assert applied["variable"] == "trailing_stop_pct"
    assert applied["new"] == 0.4
    assert ec.shadow_challenger(BOT, PAIR) is None


def test_maybe_reflect_uses_adaptive_every_without_breaking_latch():
    goal = {"reflection_every": 5, "max_drawdown": 10.0}
    for _ in range(4):
        append_trade(BOT, _close(-0.2))
    # Prior every=5 must not fire early just because the book is quiet.
    assert maybe_reflect_pair(BOT, PAIR, goal=goal, auto_deploy=False) is None
    append_trade(BOT, _close(-0.2))
    out = maybe_reflect_pair(BOT, PAIR, goal=goal, auto_deploy=False)
    assert out is None or isinstance(out, dict)
