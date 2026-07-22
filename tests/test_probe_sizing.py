"""HIF Phase-1 probe sizing — never blocks; shrinks only when enabled + thin evidence."""

from __future__ import annotations

import pytest

from hermes_core.engines.risk import (
    MAX_POSITION_SIZE,
    PROBE_EVIDENCE_MIN,
    PROBE_SIZE_FRACTION,
    apply_probe_sizing,
    compute_position_size,
    evidence_state_for,
)


def test_flag_off_identical_to_base():
    base = compute_position_size("BULL", 0.05, 0, {"position_size_r": 0.4})
    out = apply_probe_sizing(base, enabled=False, evidence_n=0)
    assert out["size_mode"] == "full"
    assert out["evidence_state"] == "disabled"
    assert out["size"] == pytest.approx(base)


def test_thin_evidence_probes():
    base = 0.40
    out = apply_probe_sizing(base, enabled=True, evidence_n=0)
    assert out["size_mode"] == "probe"
    assert out["evidence_state"] == "thin"
    assert out["size"] == pytest.approx(base * PROBE_SIZE_FRACTION)
    assert out["base_size"] == pytest.approx(base)


def test_enough_evidence_full():
    base = 0.40
    out = apply_probe_sizing(
        base, enabled=True, evidence_n=PROBE_EVIDENCE_MIN,
    )
    assert out["size_mode"] == "full"
    assert out["evidence_state"] == "ok"
    assert out["size"] == pytest.approx(base)


def test_unknown_evidence_fail_open_full():
    """Cortex missing / unreadable → full size (same as today)."""
    base = 0.30
    out = apply_probe_sizing(base, enabled=True, evidence_n=None)
    assert out["size_mode"] == "full"
    assert out["evidence_state"] == "unknown"
    assert out["size"] == pytest.approx(base)


def test_probe_never_exceeds_cap():
    out = apply_probe_sizing(10.0, enabled=True, evidence_n=0)
    assert out["size"] <= MAX_POSITION_SIZE
    assert out["base_size"] <= MAX_POSITION_SIZE


def test_evidence_state_labels():
    assert evidence_state_for(None, enabled=False) == "disabled"
    assert evidence_state_for(None, enabled=True) == "unknown"
    assert evidence_state_for(2, enabled=True) == "thin"
    assert evidence_state_for(5, enabled=True) == "ok"


def test_cortex_evidence_n(tmp_path, monkeypatch):
    import hermes_core.engines.decision_cortex as dc

    cortex_dir = tmp_path / "cortex"
    monkeypatch.setattr(dc, "CORTEX_DIR", cortex_dir)
    monkeypatch.setattr(dc, "EXILE_PATH", cortex_dir / "indicator_exile.json")
    monkeypatch.setattr(dc, "MEMORY_PATH", cortex_dir / "cortex_memory.json")
    c = dc.Cortex(bot="forex")
    assert c.evidence_n("EUR/USD", "mean_reversion") == 0
    c.record_entry("EUR/USD", "mean_reversion")  # open — does not count
    assert c.evidence_n("EUR/USD", "mean_reversion") == 0
    c.record_outcome("EUR/USD", "mean_reversion", 1.0)
    c.record_outcome("EUR/USD", "mean_reversion", -0.5)
    c.record_outcome("GBP/USD", "mean_reversion", 1.0)
    assert c.evidence_n("EUR/USD", "mean_reversion") == 2
    assert c.evidence_n("EUR/USD", "gp_ensemble") == 0


def test_cortex_summary_probe_evidence(tmp_path, monkeypatch):
    import hermes_core.engines.decision_cortex as dc

    cortex_dir = tmp_path / "cortex"
    monkeypatch.setattr(dc, "CORTEX_DIR", cortex_dir)
    monkeypatch.setattr(dc, "EXILE_PATH", cortex_dir / "indicator_exile.json")
    monkeypatch.setattr(dc, "MEMORY_PATH", cortex_dir / "cortex_memory.json")
    c = dc.Cortex(bot="forex")
    for _ in range(3):
        c.record_outcome("EUR/USD", "mean_reversion", 0.5)
    summary = c.summary()
    pe = summary["probe_evidence"]
    assert pe["threshold"] == PROBE_EVIDENCE_MIN
    key = "EUR/USD|mean_reversion"
    assert key in pe["by_key"]
    assert pe["by_key"][key]["evidence_n"] == 3
    assert pe["by_key"][key]["evidence_state"] == "thin"
    assert pe["by_key"][key]["size_mode_if_enabled"] == "probe"
    assert "probe" in summary["gates"]
