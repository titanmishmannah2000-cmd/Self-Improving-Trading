"""Session 15 / Phase 15 tests for decision cortex + policy engine.

Network-free. Cortex exile state and policy state are redirected to a temp dir
via an autouse fixture so tests never touch real state (D2: survives restart).

Blueprint exact names preserved:
  test_suppress_gp, test_suppress_mr, test_priority_discovery, test_cortex_best_updates
plus exile/reinstate, persistence, and the bidirectional suppression contract.
"""

from __future__ import annotations

import pytest

import hermes_core.engines.decision_cortex as dc
import hermes_core.engines.policy_engine as pe


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    """Redirect cortex exile + policy state into an isolated temp dir."""
    cortex_dir = tmp_path / "cortex"
    monkeypatch.setattr(dc, "CORTEX_DIR", cortex_dir)
    monkeypatch.setattr(dc, "EXILE_PATH", cortex_dir / "indicator_exile.json")
    monkeypatch.setattr(dc, "MEMORY_PATH", cortex_dir / "cortex_memory.json")
    monkeypatch.setattr(pe, "POLICY_PATH", tmp_path / "policy.json")
    yield


def _cortex_with_wrs(mr_wr=None, gp_wr=None, mr_trades=0):
    """Build a Cortex whose recorded outcomes yield the requested WRs."""
    c = dc.Cortex()
    # mean_reversion outcomes
    if mr_wr is not None:
        wins = round(mr_wr * 20)
        for i in range(20):
            c.record_outcome("EUR/USD", "mean_reversion", 1.0 if i < wins else -1.0)
    # gp_ensemble outcomes
    if gp_wr is not None:
        wins = round(gp_wr * 20)
        for i in range(20):
            c.record_outcome("EUR/USD", "gp_ensemble", 1.0 if i < wins else -1.0)
    return c


# ── blueprint Phase 15 success criteria ───────────────────────────────────
def test_suppress_gp():
    # MR strong (45%), GP weak (25%) -> GP suppressed
    c = _cortex_with_wrs(mr_wr=0.45, gp_wr=0.25)
    eng = pe.PolicyEngine()
    pol = eng.evaluate(10, ["EUR/USD"], c)
    assert pol.is_suppressed("EUR/USD", "gp_ensemble") is True
    assert pol.is_suppressed("EUR/USD", "mean_reversion") is False


def test_suppress_mr():
    # GP strong (55%) -> MR suppressed (even if MR also decent)
    c = _cortex_with_wrs(mr_wr=0.50, gp_wr=0.55)
    eng = pe.PolicyEngine()
    pol = eng.evaluate(10, ["EUR/USD"], c)
    assert pol.is_suppressed("EUR/USD", "mean_reversion") is True


def test_priority_discovery():
    c = dc.Cortex()
    c.exile_indicator("ind_a")
    c.exile_indicator("ind_b")  # >=2 exiled fleet-wide
    eng = pe.PolicyEngine()
    pol = eng.evaluate(10, ["EUR/USD"], c)
    assert pol.priority_discovery is True


def test_no_priority_discovery_single():
    # own fresh tmp dir: exactly 1 exiled -> no priority discovery
    c = dc.Cortex()
    c.exile_indicator("ind_a")
    eng = pe.PolicyEngine()
    pol = eng.evaluate(10, ["EUR/USD"], c)
    assert pol.priority_discovery is False


def test_cortex_best_updates():
    c = dc.Cortex()
    assert c.best_entry_type() == "mean_reversion"  # no data -> safe default
    c.record_outcome("EUR/USD", "gp_ensemble", 1.0)
    c.record_outcome("EUR/USD", "gp_ensemble", 1.0)
    c.record_outcome("EUR/USD", "mean_reversion", -1.0)
    # GP now known and winning -> best switches to gp_ensemble
    assert c.best_entry_type() == "gp_ensemble"
    assert c.best_entry_type() in dc.VALID_ENTRY_TYPES


# ── exile / reinstate (L36) ────────────────────────────────────────────────
def test_exile_after_5_attempts_low_wr():
    c = dc.Cortex()
    ind = "bad_ind"
    # 5 attempts, all losses -> WR 0.0 < 0.30 -> exiled
    for _ in range(5):
        c.record_indicator_outcome(ind, -1.0)
    assert c.is_indicator_exiled(ind) is True
    assert ind in c.get_exiled_indicators()


def test_exile_not_before_5_attempts():
    c = dc.Cortex()
    ind = "slow_ind"
    for _ in range(4):  # only 4 attempts -> gate not reached
        c.record_indicator_outcome(ind, -1.0)
    assert c.is_indicator_exiled(ind) is False


def test_reinstate_after_recovery():
    c = dc.Cortex()
    ind = "recover_ind"
    for _ in range(5):
        c.record_indicator_outcome(ind, -1.0)  # exiled at WR 0.0
    assert c.is_indicator_exiled(ind) is True
    # pile on wins up to a decay checkpoint (attempt 100) with WR >= 40%
    for _ in range(95):
        c.record_indicator_outcome(ind, 1.0)  # attempts=100, wins=95 -> WR 0.95
    assert c.is_indicator_exiled(ind) is False


# ── persistence (D2: survives restart) ─────────────────────────────────────
def test_cortex_persists_across_restart():
    c = dc.Cortex()
    c.exile_indicator("persist_ind")
    # simulate restart: a brand-new Cortex reads the same on-disk state
    c2 = dc.Cortex()
    assert c2.is_indicator_exiled("persist_ind") is True


def test_policy_persists_and_readable():
    c = _cortex_with_wrs(mr_wr=0.45, gp_wr=0.25)
    eng = pe.PolicyEngine()
    eng.evaluate(10, ["EUR/USD"], c)
    # a fresh engine can read the persisted policy
    reloaded = pe.PolicyEngine().get_policy()
    assert reloaded is not None
    assert reloaded.is_suppressed("EUR/USD", "gp_ensemble") is True


def test_both_directions_independent(tmp_path, monkeypatch):
    # GP suppression requires MR strong AND GP weak; flipping GP to strong
    # must drop GP suppression AND (if >=50%) suppress MR instead.
    cortex_dir = tmp_path / "cortex_a"
    monkeypatch.setattr(dc, "CORTEX_DIR", cortex_dir)
    monkeypatch.setattr(dc, "EXILE_PATH", cortex_dir / "indicator_exile.json")
    monkeypatch.setattr(dc, "MEMORY_PATH", cortex_dir / "cortex_memory.json")
    weak = _cortex_with_wrs(mr_wr=0.45, gp_wr=0.25)
    pol_weak = pe.PolicyEngine().evaluate(10, ["EUR/USD"], weak)
    assert pol_weak.is_suppressed("EUR/USD", "gp_ensemble")
    assert not pol_weak.is_suppressed("EUR/USD", "mean_reversion")

    # Fresh cortex memory — a second Cortex() in the same dir would load the
    # weak scenario's persisted entries and blend WRs (D2 persistence).
    cortex_dir2 = tmp_path / "cortex_b"
    monkeypatch.setattr(dc, "CORTEX_DIR", cortex_dir2)
    monkeypatch.setattr(dc, "EXILE_PATH", cortex_dir2 / "indicator_exile.json")
    monkeypatch.setattr(dc, "MEMORY_PATH", cortex_dir2 / "cortex_memory.json")
    strong = _cortex_with_wrs(mr_wr=0.45, gp_wr=0.55)
    pol_strong = pe.PolicyEngine().evaluate(10, ["EUR/USD"], strong)
    assert not pol_strong.is_suppressed("EUR/USD", "gp_ensemble")
    assert pol_strong.is_suppressed("EUR/USD", "mean_reversion")


def test_policy_suppressions_are_per_pair():
    """A bleeding EUR must not bench GP on a healthy GBP."""
    c = dc.Cortex()
    # EUR: MR WR 45%, GP WR 20% -> suppress GP on EUR only
    for i in range(20):
        c.record_outcome("EUR/USD", "mean_reversion", 1.0 if i < 9 else -1.0)
    for i in range(20):
        c.record_outcome("EUR/USD", "gp_ensemble", 1.0 if i < 4 else -1.0)
    # GBP: GP WR 60%, MR WR 40% -> suppress MR on GBP; GP stays allowed
    for i in range(20):
        c.record_outcome("GBP/USD", "gp_ensemble", 1.0 if i < 12 else -1.0)
    for i in range(20):
        c.record_outcome("GBP/USD", "mean_reversion", 1.0 if i < 8 else -1.0)

    pol = pe.PolicyEngine().evaluate(10, ["EUR/USD", "GBP/USD"], c)
    assert pol.is_suppressed("EUR/USD", "gp_ensemble") is True
    assert pol.is_suppressed("GBP/USD", "gp_ensemble") is False
    assert pol.is_suppressed("GBP/USD", "mean_reversion") is True
    assert pol.is_suppressed("EUR/USD", "mean_reversion") is False


def test_entry_type_wr_pair_scope():
    c = dc.Cortex()
    for _ in range(10):
        c.record_outcome("EUR/USD", "gp_ensemble", 1.0)
    for _ in range(10):
        c.record_outcome("GBP/USD", "gp_ensemble", -1.0)
    assert c.entry_type_wr("gp_ensemble", pair="EUR/USD") == 1.0
    assert c.entry_type_wr("gp_ensemble", pair="GBP/USD") == 0.0
    assert abs(c.entry_type_wr("gp_ensemble") - 0.5) < 1e-9
