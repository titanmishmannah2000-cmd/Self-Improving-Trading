"""Phase 3 (reflection superiority) — live experiment control.

Auto-revert, KB quarantine, axis cooldown, champion status, safe mode, and the
dashboard experiment surface. Network-free; all state under a tmp
HERMES_STATE_ROOT so writes/reads hit the same files the loop uses.
"""

from __future__ import annotations

import json

import pytest

import hermes_core.engines.backtest as bt
import hermes_core.engines.decision_cortex as dc
from hermes_core.engines import experiment_control as ec
from hermes_core.engines.reflect import combined_reflect, reflection_health
from hermes_core.engines.soak_controls import append_trade
from hermes_core.state.paths import bot_state_dir

BOT = "forex"
PAIR = "EUR/USD"


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_BOT_NAME", BOT)
    monkeypatch.delenv("REFLECT_AUTO_DEPLOY", raising=False)
    monkeypatch.setattr(dc, "CORTEX_DIR", None)
    monkeypatch.setattr(bt, "KB_PATH", None)
    yield


def _close(pnl, version, pair=PAIR, reason="sl"):
    return {
        "id": f"{BOT}:{pair}:{version}:{pnl}",
        "bot": BOT,
        "pair": pair,
        "exit_reason": reason,
        "pnl_pct": pnl,
        "strategy_version": version,
    }


def _prior():
    return {"version": "00", "stop_loss_pct": 1.5, "profit_target_pct": 3.0}


def _deploy(**kw):
    ec.record_deployment(
        BOT,
        PAIR,
        variable="stop_loss_pct",
        old=1.5,
        new=1.2,
        version_from="00",
        version_to="01",
        prior_strategy=_prior(),
        closed_count=kw.get("closed_count", 0),
    )


# ── 3.1 deployment bookkeeping ──────────────────────────────────────────────
def test_record_deployment_opens_experiment_and_snapshots_champion():
    _deploy()
    exps = ec._load(BOT, ec._EXPERIMENTS)
    assert exps[PAIR]["status"] == "live"
    assert exps[PAIR]["version_to"] == "01"
    champs = ec._load(BOT, ec._CHAMPIONS)
    assert champs[PAIR]["status"] == "champion"
    assert champs[PAIR]["strategy"]["stop_loss_pct"] == 1.5


def test_evaluate_pending_until_enough_challenger_closes():
    _deploy()
    for _ in range(2):
        append_trade(BOT, _close(-0.2, "01"))
    ev = ec.evaluate_experiment(BOT, PAIR, k=3)
    assert ev["status"] == "pending"
    assert ev["have"] == 2 and ev["need"] == 3


# ── 3.1/3.2/3.3 auto-revert on a worse challenger ───────────────────────────
def test_auto_revert_restores_quarantines_cooldowns_and_flags():
    # Deploy the (worse) challenger v01, then simulate its live trading.
    _deploy()
    # Champion (v00) baseline was positive; challenger (v01) bleeds.
    for _ in range(4):
        append_trade(BOT, _close(0.5, "00"))
    for _ in range(3):
        append_trade(BOT, _close(-0.5, "01"))

    res = ec.maybe_auto_revert(BOT, PAIR, k=3)
    assert res["status"] == "reverted"
    assert res["restored"] is True

    # YAML restored to champion params/version.
    from hermes_core.config import load_strategy_for_pair

    live = load_strategy_for_pair(PAIR, BOT)
    assert float(live["stop_loss_pct"]) == 1.5
    assert str(live["version"]) == "00"

    # KB quarantine: the exact change is now a standing rejection.
    hit = bt._kb_hit(PAIR, "stop_loss_pct", 1.5, 1.2, bot=BOT)
    assert hit is not None and hit["approved"] is False
    assert "live_worse" in hit["reason"]

    # Axis cooldown forces a different lever now.
    assert "stop_loss_pct" in ec.blocked_axes(BOT, PAIR, len(ec._pair_closes(BOT, PAIR)))

    # Champion flagged underperforming; experiment closed to history.
    champs = ec._load(BOT, ec._CHAMPIONS)
    assert champs[PAIR]["status"] == "underperforming"
    assert champs[PAIR]["revert_count"] == 1
    exps = ec._load(BOT, ec._EXPERIMENTS)
    assert PAIR not in exps
    assert any(h["status"] == "reverted" for h in exps.get("_history", []))


def test_auto_revert_promotes_champion_when_improved():
    _deploy()
    # Write a live v01 YAML so the "improved" promotion can snapshot it.
    from hermes_core.engines.reflect import apply_strategy_change

    apply_strategy_change(
        PAIR, "stop_loss_pct", 1.2, bot=BOT, version="01",
        strategy={"version": "00", "stop_loss_pct": 1.5, "profit_target_pct": 3.0},
    )
    for _ in range(4):
        append_trade(BOT, _close(-0.5, "00"))
    for _ in range(3):
        append_trade(BOT, _close(0.8, "01"))

    res = ec.maybe_auto_revert(BOT, PAIR, k=3)
    assert res["status"] == "improved"
    champs = ec._load(BOT, ec._CHAMPIONS)
    assert champs[PAIR]["status"] == "champion"
    assert str(champs[PAIR]["version"]) == "01"
    # No quarantine on a winner.
    assert bt._kb_hit(PAIR, "stop_loss_pct", 1.5, 1.2, bot=BOT) is None


# ── 3.4 axis cooldown ───────────────────────────────────────────────────────
def test_axis_cooldown_measured_in_closed_trades():
    ec.set_axis_cooldown(BOT, PAIR, "stop_loss_pct", until_closed=30)
    assert ec.axis_in_cooldown(BOT, PAIR, "stop_loss_pct", closed_count=10) is True
    assert ec.axis_in_cooldown(BOT, PAIR, "stop_loss_pct", closed_count=30) is False
    assert ec.blocked_axes(BOT, PAIR, 29) == {"stop_loss_pct"}
    assert ec.blocked_axes(BOT, PAIR, 30) == set()


# ── 3.5 safe mode ───────────────────────────────────────────────────────────
def test_safe_mode_escalates_then_pauses():
    assert ec.pair_safe_mode(BOT, PAIR) is None
    r1 = ec.escalate_safe_mode(BOT, PAIR, "stuck")
    assert r1["mode"] == "size_down"
    r2 = ec.escalate_safe_mode(BOT, PAIR, "still stuck")
    assert r2["mode"] == "paused"
    assert ec.pair_safe_mode(BOT, PAIR)["mode"] == "paused"
    # Clearing returns to normal (no record).
    ec.set_safe_mode(BOT, PAIR, "normal", "recovered")
    assert ec.pair_safe_mode(BOT, PAIR) is None


# ── 3.4 integration: reflection skips a blocked axis + escalates safe mode ───
def test_combined_reflect_blocks_axis_and_enters_safe_mode():
    # Only the stop axis will be a candidate (low WR, no excursion signal).
    trades = [_close(0.2, "00")] + [_close(-0.1, "00") for _ in range(5)]
    for t in trades:
        append_trade(BOT, t)  # so total_closed matches the batch
    # Block the ONLY available axis.
    ec.set_axis_cooldown(BOT, PAIR, "stop_loss_pct", until_closed=999)

    strat = {
        "version": "00",
        "stop_loss_pct": 1.5,
        "profit_target_pct": 3.0,
        "strategy_type": "mean_reversion",
    }
    goal = {"max_drawdown": 10.0}
    out = combined_reflect(PAIR, trades, goal=goal, strategy=strat, bot=BOT)
    assert out == []  # blocked → no proposal
    assert ec.pair_safe_mode(BOT, PAIR)["mode"] == "size_down"


# ── 3.6 dashboard surface ───────────────────────────────────────────────────
def test_reflection_health_carries_experiment_fields():
    _deploy()
    goal = {"reflection_every": 5, "max_drawdown": 10.0}
    h = reflection_health(BOT, [PAIR], goal=goal)
    p = h["pairs"][PAIR]
    assert p["experiment"] is not None
    assert p["experiment"]["version_to"] == "01"
    assert p["champion_status"] == "champion"


def test_experiments_summary_lists_history_after_revert():
    _deploy()
    for _ in range(3):
        append_trade(BOT, _close(-0.5, "01"))
    ec.maybe_auto_revert(BOT, PAIR, k=3)
    summ = ec.experiments_summary(BOT, [PAIR])
    assert summ["pairs"][PAIR]["champion_status"] == "underperforming"
    assert "stop_loss_pct" in summ["pairs"][PAIR]["cooldown_axes"]
    assert any(h.get("status") == "reverted" for h in summ["history"])
