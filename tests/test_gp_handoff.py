"""Phase 4 (reflection superiority) — GP handoff + shared failure memory.

Covers:
  4.1 underperforming + quarantined axes → priority_discovery handoff
  4.2 GP admits stay on the signal path (no strategy version bump)
  4.3 Cortex surfaces param_quarantine + indicator_exile separately
  4.4 YAML revert never touches exile; exile never touches strategy YAML
  4.5 GP admit on a handoff pair schedules a reflection risk retune
"""

from __future__ import annotations

import pytest

import hermes_core.engines.backtest as bt
import hermes_core.engines.decision_cortex as dc
from hermes_core.engines import experiment_control as ec
from hermes_core.engines.policy_engine import PolicyEngine
from hermes_core.engines.soak_controls import append_trade
from hermes_core.state.paths import cortex_dir, strategies_dir

BOT = "forex"
PAIR = "EUR/USD"


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_BOT_NAME", BOT)
    monkeypatch.setattr(dc, "CORTEX_DIR", None)
    monkeypatch.setattr(dc, "EXILE_PATH", None)
    monkeypatch.setattr(dc, "MEMORY_PATH", None)
    monkeypatch.setattr(bt, "KB_PATH", None)
    yield


def _close(pnl, version="00"):
    return {
        "id": f"{BOT}:{PAIR}:{version}:{pnl}",
        "bot": BOT,
        "pair": PAIR,
        "exit_reason": "sl",
        "pnl_pct": pnl,
        "strategy_version": version,
    }


def _prior():
    return {"version": "00", "stop_loss_pct": 1.5, "profit_target_pct": 3.0}


def _deploy_and_revert_worse():
    """Deploy a challenger then feed losing closes → auto-revert + handoff."""
    ec.record_deployment(
        BOT,
        PAIR,
        variable="stop_loss_pct",
        old=1.5,
        new=1.2,
        version_from="00",
        version_to="01",
        prior_strategy=_prior(),
        closed_count=0,
    )
    for _ in range(4):
        append_trade(BOT, _close(0.5, "00"))
    for _ in range(3):
        append_trade(BOT, _close(-0.5, "01"))
    return ec.maybe_auto_revert(BOT, PAIR, k=3)


# ── 4.1 explicit GP handoff ─────────────────────────────────────────────────
def test_revert_requests_gp_handoff_and_policy_priority():
    res = _deploy_and_revert_worse()
    assert res["status"] == "reverted"
    assert ec.needs_gp_handoff(BOT, PAIR) is True
    assert PAIR in ec.gp_handoff_pairs(BOT)

    # Policy engine OR's the handoff into priority_discovery.
    cx = dc.Cortex(bot=BOT)
    pol = PolicyEngine().evaluate(0, [PAIR], cortex=cx)
    assert pol.priority_discovery is True
    assert PAIR in pol.priority_discovery_pairs


# ── 4.2 GP remains signal path (no strategy version bump) ───────────────────
def test_on_gp_admit_does_not_bump_strategy_version(tmp_path):
    # Seed a live strategy YAML at v00.
    from hermes_core.config import load_strategy_for_pair
    from hermes_core.engines.reflect import apply_strategy_change

    apply_strategy_change(
        PAIR, "stop_loss_pct", 1.5, bot=BOT, version="00", strategy=_prior()
    )
    v_before = str(load_strategy_for_pair(PAIR, BOT).get("version"))

    ec.request_gp_handoff(BOT, PAIR, reason="test", variable="stop_loss_pct")
    retune = ec.on_gp_admit(BOT, PAIR, admitted=2)
    assert retune is not None
    assert retune["active"] is True

    v_after = str(load_strategy_for_pair(PAIR, BOT).get("version"))
    assert v_after == v_before == "00"
    # Handoff cleared; retune pending.
    assert ec.needs_gp_handoff(BOT, PAIR) is False
    assert ec.pending_reflection_retune(BOT, PAIR) is True


def test_on_gp_admit_noop_without_handoff():
    assert ec.on_gp_admit(BOT, PAIR, admitted=3) is None
    assert ec.pending_reflection_retune(BOT, PAIR) is False


# ── 4.3 shared failure memory in Cortex summary ─────────────────────────────
def test_cortex_summary_exposes_param_quarantine_and_exile_separately():
    # Param side: live_worse ban via revert.
    _deploy_and_revert_worse()
    # Indicator side: exile a formula.
    cx = dc.Cortex(bot=BOT)
    cx.exile_indicator("gp_foo_expr")

    summ = cx.summary()
    assert "gp_foo_expr" in summ["exiled"]
    pq = summ["param_quarantine"]
    assert any(q.get("live_worse") and q.get("variable") == "stop_loss_pct" for q in pq)
    assert PAIR in summ["gp_handoff_pairs"]
    assert "param_quarantine" in summ["gates"]


# ── 4.4 no cross-contamination ──────────────────────────────────────────────
def test_yaml_revert_does_not_touch_indicator_exile():
    cx = dc.Cortex(bot=BOT)
    cx.exile_indicator("keep_me_exiled")
    exile_before = set(cx.get_exiled_indicators())

    _deploy_and_revert_worse()

    cx2 = dc.Cortex(bot=BOT)
    assert "keep_me_exiled" in cx2.get_exiled_indicators()
    assert set(cx2.get_exiled_indicators()) >= exile_before
    # Exile file exists and is distinct from strategy YAML.
    exile_path = cortex_dir(BOT) / "indicator_exile.json"
    assert exile_path.exists()
    strat_files = list(strategies_dir(BOT).glob("*.yaml"))
    assert strat_files
    assert exile_path not in strat_files


def test_exile_does_not_mutate_strategy_yaml():
    from hermes_core.config import load_strategy_for_pair
    from hermes_core.engines.reflect import apply_strategy_change

    apply_strategy_change(
        PAIR, "stop_loss_pct", 1.5, bot=BOT, version="00", strategy=_prior()
    )
    before = load_strategy_for_pair(PAIR, BOT)

    cx = dc.Cortex(bot=BOT)
    cx.exile_indicator("some_expr")
    cx.exile_indicator("other_expr")

    after = load_strategy_for_pair(PAIR, BOT)
    assert after == before
    assert str(after.get("version")) == "00"
    assert float(after["stop_loss_pct"]) == 1.5


# ── 4.5 reflection retune after GP admit ────────────────────────────────────
def test_retune_clears_cooldowns_and_unpauses():
    ec.set_axis_cooldown(BOT, PAIR, "stop_loss_pct", until_closed=999)
    ec.set_safe_mode(BOT, PAIR, "paused", "stuck")
    ec.request_gp_handoff(BOT, PAIR, reason="test")

    ec.on_gp_admit(BOT, PAIR, admitted=1)

    assert ec.blocked_axes(BOT, PAIR, closed_count=0) == set()
    sm = ec.pair_safe_mode(BOT, PAIR)
    assert sm is not None and sm["mode"] == "size_down"  # paused → size_down
    assert ec.pending_reflection_retune(BOT, PAIR) is True
    consumed = ec.consume_reflection_retune(BOT, PAIR)
    assert consumed and consumed["active"] is True
    assert ec.pending_reflection_retune(BOT, PAIR) is False
