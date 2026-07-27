"""Phase 0 (reflection superiority) — trade-book truth, health, taxonomy, cortex wiring.

Network-free. All state is redirected under a tmp HERMES_STATE_ROOT so the
health view and the live cadence read the exact same trades.jsonl the loop
writes via ``append_trade``.
"""

from __future__ import annotations

import json

import pytest

import hermes_core.engines.decision_cortex as dc
import hermes_core.engines.reflect as rf
from hermes_core.engines.reflect import (
    _closed_trades_for_pair,
    is_soak_success,
    reflection_health,
    status_class,
)
from hermes_core.engines.soak_controls import append_trade
from hermes_core.state.paths import bot_state_dir


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("REFLECT_AUTO_DEPLOY", raising=False)
    # decision_cortex module-level overrides must be off so cortex_dir() is used.
    monkeypatch.setattr(dc, "CORTEX_DIR", None)
    yield


def _close(pair="EUR/USD", pnl=-1.0, reason="sl"):
    return {
        "id": f"forex:{pair}:{pnl}",
        "bot": "forex",
        "pair": pair,
        "exit_reason": reason,
        "pnl_pct": pnl,
        "strategy_version": "00",
    }


# ── 0.1 canonical close book: write path == reflection read path ────────────
def test_append_trade_and_reflection_read_same_file():
    # The loop appends via append_trade; reflection counts via _closed_trades_for_pair.
    for i in range(3):
        assert append_trade("forex", _close(pnl=-1.0 - i))
    # Same physical file.
    assert (bot_state_dir("forex") / "trades.jsonl").exists()
    closed = _closed_trades_for_pair("forex", "EUR/USD")
    assert len(closed) == 3


def test_reflection_read_filters_pair_and_orphans():
    append_trade("forex", _close(pair="EUR/USD", pnl=-1.0))
    append_trade("forex", _close(pair="GBP/USD", pnl=-2.0))
    orphan = _close(pair="EUR/USD", pnl=0.0, reason="restart_orphan")
    orphan["orphan"] = True
    append_trade("forex", orphan)
    closed = _closed_trades_for_pair("forex", "EUR/USD")
    assert len(closed) == 1  # GBP filtered by pair, orphan excluded


# ── 0.3 status taxonomy ─────────────────────────────────────────────────────
def test_status_taxonomy_classes():
    assert status_class("no_proposal") == "no_proposal"
    assert status_class("approved_pending_deploy") == "proven"
    assert status_class("deployed") == "deployed"
    assert status_class("l2_reject") == "rejected"
    assert status_class("backtest_reject") == "rejected"
    assert status_class(None) == "unknown"


def test_soak_success_includes_pending_deploy():
    assert is_soak_success("approved_pending_deploy") is True
    assert is_soak_success("deployed") is True
    assert is_soak_success("no_proposal") is False
    assert is_soak_success("l2_reject") is False


# ── 0.2 reflection health snapshot ──────────────────────────────────────────
def test_health_next_fire_and_counts():
    goal = {"reflection_every": 5, "max_drawdown": 10.0}
    for _ in range(3):
        append_trade("forex", _close(pnl=-1.0))
    h = reflection_health("forex", ["EUR/USD"], goal=goal)
    assert h["reflection_every"] == 5
    assert h["auto_deploy"] is False
    p = h["pairs"]["EUR/USD"]
    assert p["closed"] == 3
    assert p["next_fire_at"] == 5
    assert p["trades_until_next"] == 2
    assert p["last_status"] is None


def test_health_due_now_at_multiple():
    goal = {"reflection_every": 5, "max_drawdown": 10.0}
    for _ in range(5):
        append_trade("forex", _close(pnl=-1.0))
    p = reflection_health("forex", ["EUR/USD"], goal=goal)["pairs"]["EUR/USD"]
    assert p["closed"] == 5
    assert p["next_fire_at"] == 5  # due now (not latched yet)
    assert p["trades_until_next"] == 0


def test_health_reflects_last_status_from_hypotheses():
    goal = {"reflection_every": 5, "max_drawdown": 10.0}
    for _ in range(5):
        append_trade("forex", _close(pnl=-1.0))
    # Reflection logs a hypothesis via the normal path.
    rf._log_hypothesis(
        {
            "pair": "EUR/USD",
            "bot": "forex",
            "variable": "stop_loss_pct",
            "old": 1.5,
            "new": 1.2,
            "reason": "drawdown breach",
            "status": "approved_pending_deploy",
            "ts": 1.0,
        }
    )
    p = reflection_health("forex", ["EUR/USD"], goal=goal)["pairs"]["EUR/USD"]
    assert p["last_status"] == "approved_pending_deploy"
    assert p["last_status_class"] == "proven"
    assert p["proven"] is True


def test_health_includes_pending_deploys():
    goal = {"reflection_every": 5, "max_drawdown": 10.0}
    from hermes_core.engines import experiment_control as ec

    ec.record_shadow_challenger(
        "forex",
        "EUR/USD",
        variable="trailing_stop_pct",
        old=0.0,
        new=0.4,
        reason="auto_deploy_off",
        version="02",
    )
    health = reflection_health("forex", ["EUR/USD"], goal=goal)
    assert health["pending_deploys"]
    assert health["pending_deploys"][0]["pair"] == "EUR/USD"
    assert health["pending_deploys"][0]["variable"] == "trailing_stop_pct"
    assert health["pairs"]["EUR/USD"]["shadow"]["variable"] == "trailing_stop_pct"


def test_health_reflects_latch_after_fire():
    goal = {"reflection_every": 5, "max_drawdown": 10.0}
    for _ in range(5):
        append_trade("forex", _close(pnl=-1.0))
    rf._mark_reflection_done("EUR/USD", 5, "forex")
    p = reflection_health("forex", ["EUR/USD"], goal=goal)["pairs"]["EUR/USD"]
    assert p["latched_at"] == 5
    # Already reflected at 5 → next fire moves to 10.
    assert p["next_fire_at"] == 10


# ── 0.4 cortex ← reflection wiring (append-only, race-safe channel) ─────────
def test_log_hypothesis_mirrors_into_cortex_channel():
    rf._log_hypothesis(
        {
            "pair": "EUR/USD",
            "bot": "forex",
            "variable": "stop_loss_pct",
            "old": 1.5,
            "new": 1.2,
            "reason": "drawdown breach",
            "status": "proposed",
            "ts": 2.0,
        }
    )
    notes = dc.read_reflection_notes("forex", pair="EUR/USD")
    assert notes, "reflection note not mirrored into cortex channel"
    last = notes[-1]
    assert last["variable"] == "stop_loss_pct"
    assert last["status"] == "proposed"
    assert last["new"] == 1.2


def test_cortex_record_hypothesis_and_recent_read():
    cx = dc.Cortex(bot="forex")
    cx.record_hypothesis(
        "GBP/USD",
        "widen stop",
        status="proposed",
        variable="stop_loss_pct",
        old=1.5,
        new=1.8,
    )
    seen = cx.recent_hypotheses(pair="GBP/USD")
    assert len(seen) == 1
    assert seen[0]["text"] == "widen stop"
    assert seen[0]["new"] == 1.8


def test_cortex_hypothesis_does_not_touch_memory_blob():
    """record_hypothesis must not append to the entries memory (race-safe)."""
    cx = dc.Cortex(bot="forex")
    before = len(cx._entries)
    cx.record_hypothesis("EUR/USD", "note", status="proposed")
    assert len(cx._entries) == before  # no memory mutation
    # And it is retrievable from the append-only channel.
    assert cx.recent_hypotheses(pair="EUR/USD")


def test_reflection_log_is_jsonl_appended_not_rewritten():
    dc.append_reflection_note("forex", {"pair": "EUR/USD", "status": "proposed", "ts": 1})
    dc.append_reflection_note("forex", {"pair": "EUR/USD", "status": "deployed", "ts": 2})
    path = dc.reflection_log_path("forex")
    lines = [x for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["status"] == "proposed"
    assert json.loads(lines[1])["status"] == "deployed"
