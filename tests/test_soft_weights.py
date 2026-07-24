"""HIF Phase-2 soft expert weights — size shrink, never hard-ban when enabled."""

from __future__ import annotations

import pytest

from hermes_core.engines.expert_weights import (
    EXPLORE_FLOOR,
    SOFT_SUPPRESS_MULT,
    apply_expert_weight,
    expert_weight,
    pair_expert_weights,
)


def test_disabled_is_full_weight():
    info = expert_weight(enabled=False, suppressed=True, wr=0.1, evidence_n=0)
    assert info["weight"] == 1.0
    assert info["mode"] == "disabled"


def test_soft_suppress_shrinks_not_zero():
    info = expert_weight(enabled=True, suppressed=True, wr=None, evidence_n=10)
    assert info["suppressed_soft"] is True
    assert info["weight"] == pytest.approx(SOFT_SUPPRESS_MULT)
    assert info["weight"] > 0


def test_explore_floor_on_thin_evidence():
    # Extremely low WR would push weight down; thin evidence lifts to explore floor.
    info = expert_weight(enabled=True, suppressed=False, wr=0.0, evidence_n=1)
    assert info["weight"] >= EXPLORE_FLOOR
    assert "explore_floor" in info["reasons"] or info["weight"] >= EXPLORE_FLOOR


def test_apply_scales_size():
    info = expert_weight(enabled=True, suppressed=True, evidence_n=20)
    out = apply_expert_weight(0.40, info)
    assert out["size"] == pytest.approx(0.40 * SOFT_SUPPRESS_MULT)
    assert out["expert_weight"] == pytest.approx(SOFT_SUPPRESS_MULT)


def test_pair_expert_weights_marks_suppressed(tmp_path, monkeypatch):
    import hermes_core.engines.decision_cortex as dc

    cortex_dir = tmp_path / "cortex"
    monkeypatch.setattr(dc, "CORTEX_DIR", cortex_dir)
    monkeypatch.setattr(dc, "EXILE_PATH", cortex_dir / "indicator_exile.json")
    monkeypatch.setattr(dc, "MEMORY_PATH", cortex_dir / "cortex_memory.json")
    c = dc.Cortex(bot="forex")
    for _ in range(6):
        c.record_outcome("EUR/USD", "mean_reversion", 1.0)
    for _ in range(6):
        c.record_outcome("EUR/USD", "gp_ensemble", -1.0)

    weights = pair_expert_weights(
        "EUR/USD",
        c,
        {"gp_ensemble"},
        enabled=True,
    )
    assert weights["gp_ensemble"]["suppressed_soft"] is True
    assert weights["gp_ensemble"]["weight"] < weights["mean_reversion"]["weight"]
    assert weights["mean_reversion"]["weight"] <= 1.0


def test_policy_allocation_when_soft_on(tmp_path, monkeypatch):
    import hermes_core.engines.decision_cortex as dc
    import hermes_core.engines.policy_engine as pe

    cortex_dir = tmp_path / "cortex"
    monkeypatch.setattr(dc, "CORTEX_DIR", cortex_dir)
    monkeypatch.setattr(dc, "EXILE_PATH", cortex_dir / "indicator_exile.json")
    monkeypatch.setattr(dc, "MEMORY_PATH", cortex_dir / "cortex_memory.json")
    monkeypatch.setattr(pe, "POLICY_PATH", tmp_path / "policy.json")
    monkeypatch.setenv("SOFT_WEIGHTS", "1")

    c = dc.Cortex(bot="forex")
    # MR 45%, GP 25% → L35 would suppress GP
    for i in range(20):
        c.record_outcome("EUR/USD", "mean_reversion", 1.0 if i < 9 else -1.0)
    for i in range(20):
        c.record_outcome("EUR/USD", "gp_ensemble", 1.0 if i < 5 else -1.0)

    pol = pe.PolicyEngine().evaluate(1, ["EUR/USD"], cortex=c)
    assert pol.is_suppressed("EUR/USD", "gp_ensemble") is True
    assert pol.soft_weights is True
    alloc = pol.allocation["EUR/USD"]["gp_ensemble"]
    assert alloc["suppressed_soft"] is True
    assert alloc["weight"] < 1.0
    d = pol.to_dict()
    assert "allocation" in d and d["soft_weights"] is True
