"""HIF Phase-5 Bayesian fractional Kelly sizing."""

from __future__ import annotations

import pytest

from hermes_core.engines.kelly_sizing import (
    MIN_MULT,
    apply_kelly_sizing,
    bayesian_p,
    kelly_f,
    kelly_size_mult,
)


def test_disabled_passthrough():
    out = apply_kelly_sizing(0.4, enabled=False, wins=10, losses=10)
    assert out["size"] == pytest.approx(0.4)
    assert out["kelly_mode"] == "disabled"


def test_no_evidence_passthrough():
    out = apply_kelly_sizing(0.4, enabled=True, wins=0, losses=0)
    assert out["size"] == pytest.approx(0.4)
    assert out["kelly_mult"] == 1.0
    assert "no_evidence_passthrough" in out["reasons"]


def test_bayesian_p_shrinks_to_prior():
    assert bayesian_p(0, 0) == pytest.approx(0.5)
    assert bayesian_p(9, 1) > 0.7


def test_positive_edge_near_full_mult():
    # Strong WR + 2:1 RR → healthy Kelly → mult near 1
    info = kelly_size_mult(wins=20, losses=5, rr_b=2.0)
    assert info["p_bayes"] > 0.7
    assert info["kelly_mult"] >= 0.5
    assert info["ci_low"] is not None


def test_negative_edge_hits_floor():
    info = kelly_size_mult(wins=2, losses=20, rr_b=1.0)
    assert info["kelly_f"] == pytest.approx(0.0) or info["kelly_f"] < 0.05
    assert info["kelly_mult"] == pytest.approx(MIN_MULT)


def test_apply_scales():
    out = apply_kelly_sizing(
        0.40,
        enabled=True,
        wins=2,
        losses=20,
        rr_b=1.0,
    )
    assert out["size"] == pytest.approx(0.40 * MIN_MULT)
    assert out["kelly_mode"] == "soft"


def test_kelly_f_formula():
    # p=0.6, b=2 → full=(1.2-0.4)/2=0.4 → quarter=0.1
    assert kelly_f(0.6, 2.0, fraction=0.25) == pytest.approx(0.1)


def test_cortex_edge_stats(tmp_path, monkeypatch):
    import hermes_core.engines.decision_cortex as dc

    cortex_dir = tmp_path / "cortex"
    monkeypatch.setattr(dc, "CORTEX_DIR", cortex_dir)
    monkeypatch.setattr(dc, "EXILE_PATH", cortex_dir / "indicator_exile.json")
    monkeypatch.setattr(dc, "MEMORY_PATH", cortex_dir / "cortex_memory.json")
    c = dc.Cortex(bot="forex")
    c.record_outcome("EUR/USD", "mean_reversion", 2.0)
    c.record_outcome("EUR/USD", "mean_reversion", 1.0)
    c.record_outcome("EUR/USD", "mean_reversion", -1.0)
    st = c.edge_stats("EUR/USD", "mean_reversion")
    assert st["wins"] == 2
    assert st["losses"] == 1
    assert st["avg_win"] == pytest.approx(1.5)
    assert st["avg_loss"] == pytest.approx(1.0)
