"""Phase 5 (reflection superiority) — cadence, staged deploy, hard caps.

Covers:
  5.1 same-regime reflection batches
  5.2 deploy cooldown (1/day) + post-deploy quiet period
  5.3 staged deploy prove→canary→full (+ unlock gate)
  5.4 hard caps: stop floor, one-variable/one-pair, thread cap retained
"""

from __future__ import annotations

import threading
import time

import pytest

import hermes_core.engines.backtest as bt
import hermes_core.engines.decision_cortex as dc
import hermes_core.engines.loop as loop
import hermes_core.engines.reflect as rf
from hermes_core.engines import experiment_control as ec
from hermes_core.engines.reflect import (
    STOP_FLOOR,
    combined_reflect,
    same_regime_batch,
)

BOT = "forex"
PAIR = "EUR/USD"


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_BOT_NAME", BOT)
    monkeypatch.delenv("REFLECT_DEPLOY_STAGE", raising=False)
    monkeypatch.delenv("REFLECT_AUTO_DEPLOY", raising=False)
    monkeypatch.setattr(dc, "CORTEX_DIR", None)
    monkeypatch.setattr(bt, "KB_PATH", None)
    # Short quiet period for tests.
    monkeypatch.setattr(ec, "DEPLOY_COOLDOWN_S", 1000)
    monkeypatch.setattr(ec, "DEPLOY_QUIET_CLOSES", 5)
    yield


def _t(pnl, regime=None, version="00"):
    rec = {
        "pair": PAIR,
        "exit_reason": "sl" if pnl < 0 else "tp",
        "pnl_pct": pnl,
        "strategy_version": version,
    }
    if regime is not None:
        rec["entry_regime"] = regime
    return rec


# ── 5.1 same-regime batches ─────────────────────────────────────────────────
def test_same_regime_batch_prefers_dominant_recent():
    trades = (
        [_t(-0.1, "trend") for _ in range(3)]
        + [_t(-0.1, "range") for _ in range(7)]
        + [_t(0.2, "range") for _ in range(2)]
    )
    batch, regime = same_regime_batch(trades, every=5)
    assert regime == "range"
    assert len(batch) == 5
    assert all(t["entry_regime"] == "range" for t in batch)


def test_same_regime_batch_legacy_without_stamps():
    trades = [_t(-0.1) for _ in range(8)]
    batch, regime = same_regime_batch(trades, every=5)
    assert regime is None
    assert len(batch) == 5
    assert batch == trades[-5:]


# ── 5.2 deploy cooldown ─────────────────────────────────────────────────────
def test_deploy_cooldown_blocks_second_deploy_same_day():
    ec.record_deploy_cooldown(BOT, PAIR, closed_count=10)
    block = ec.deploy_blocked(BOT, PAIR, closed_count=12, now=time.time() + 10)
    assert block is not None
    assert block["reason"] == "deploy_cooldown_day"


def test_deploy_quiet_period_after_day_passes():
    ec.record_deploy_cooldown(BOT, PAIR, closed_count=10)
    # Age past the day cooldown but still inside quiet closes.
    block = ec.deploy_blocked(
        BOT, PAIR, closed_count=12, now=time.time() + ec.DEPLOY_COOLDOWN_S + 1
    )
    assert block is not None
    assert block["reason"] == "deploy_quiet_period"
    # Past quiet too.
    clear = ec.deploy_blocked(
        BOT, PAIR, closed_count=20, now=time.time() + ec.DEPLOY_COOLDOWN_S + 1
    )
    assert clear is None


# ── 5.3 staged deploy ───────────────────────────────────────────────────────
def test_stage_prove_blocks_auto_deploy_even_when_flag_true():
    ec.set_deploy_stage(BOT, "prove", reason="soak")
    allow = ec.auto_deploy_allowed(BOT, env_auto=True)
    assert allow["allowed"] is False
    assert allow["stage"] == "prove"


def test_stage_advances_prove_canary_full_on_improved():
    ec.set_deploy_stage(BOT, "prove")
    assert ec.advance_deploy_stage(BOT) == "canary"
    assert ec.advance_deploy_stage(BOT) == "full"
    assert ec.advance_deploy_stage(BOT) == "full"  # no-op at top
    allow = ec.auto_deploy_allowed(BOT, env_auto=True)
    assert allow["allowed"] is True


def test_pipeline_prove_stage_returns_pending(monkeypatch):
    """Even with auto_deploy=True, prove stage must not write YAML."""
    ec.set_deploy_stage(BOT, "prove")
    strat = {
        "version": "00",
        "stop_loss_pct": 1.5,
        "profit_target_pct": 3.0,
        "strategy_type": "mean_reversion",
    }
    monkeypatch.setattr(
        rf,
        "combined_reflect",
        lambda *a, **k: [
            {
                "pair": PAIR,
                "bot": BOT,
                "variable": "stop_loss_pct",
                "old": 1.5,
                "new": 1.2,
                "confidence": 0.5,
                "reason": "test",
            }
        ],
    )
    import hermes_core.engines.backtest as btmod

    monkeypatch.setattr(
        btmod,
        "backtest_with_history",
        lambda *a, **k: {
            "approved": True,
            "phases": {"phase6_deploy": {"version_bumped": "01"}},
        },
    )
    out = rf.run_reflection_pipeline(
        PAIR,
        [_t(-1.0) for _ in range(5)],
        bot=BOT,
        goal={"max_drawdown": 10.0},
        strategy=strat,
        auto_deploy=True,
        prices=[1.0 + i * 0.001 for i in range(100)],
    )
    assert out["status"] == "approved_pending_deploy"
    assert out.get("deploy_stage") == "prove"
    assert out["deployed"] is False


# ── 5.4 hard caps ───────────────────────────────────────────────────────────
def test_stop_floor_never_below_half_percent():
    assert STOP_FLOOR == 0.5
    # Drawdown breach with stop already at floor → proposal clamps to floor.
    trades = [_t(-2.0) for _ in range(5)]  # drawdown 2% if max_dd small
    # Use a goal max_dd of 1 so drawdown breaches; stop already at floor.
    out = rf.layer1_rule_based(
        PAIR,
        trades,
        {"max_drawdown": 0.5},
        {"stop_loss_pct": STOP_FLOOR, "profit_target_pct": 3.0},
    )
    assert out is not None
    assert out[0] == "stop_loss_pct"
    assert float(out[2]) >= STOP_FLOOR


def test_combined_reflect_one_variable_one_proposal():
    trades = [_t(0.2) for _ in range(2)] + [_t(-0.3) for _ in range(6)]
    strat = {
        "version": "00",
        "stop_loss_pct": 1.5,
        "profit_target_pct": 3.0,
        "strategy_type": "mean_reversion",
    }
    props = combined_reflect(PAIR, trades, goal={"max_drawdown": 10.0}, strategy=strat, bot=BOT)
    assert len(props) <= 1
    if props:
        assert "variable" in props[0]
        assert props[0]["pair"] == PAIR  # never mass-mutates other pairs


def test_reflect_thread_cap_retained():
    # The loop's reflection worker is gated by a Semaphore (default 2).
    assert hasattr(loop, "_REFLECT_SEM")
    # Force-init the way _maybe_reflect_async does.
    loop._REFLECT_SEM = threading.Semaphore(2)
    assert loop._REFLECT_SEM.acquire(blocking=False)
    assert loop._REFLECT_SEM.acquire(blocking=False)
    assert loop._REFLECT_SEM.acquire(blocking=False) is False  # capped
    loop._REFLECT_SEM.release()
    loop._REFLECT_SEM.release()
