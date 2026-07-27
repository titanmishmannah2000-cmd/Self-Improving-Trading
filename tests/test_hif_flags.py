"""HIF dormant flags — registry honesty + activation when env=1."""

from __future__ import annotations

import hermes_core.engines.hif_flags as hf


def test_snapshot_all_dormant_by_default(monkeypatch):
    for key, _ in hf.DORMANT_FLAGS:
        monkeypatch.delenv(key, raising=False)
    snap = hf.snapshot()
    assert snap["n_enabled"] == 0
    assert snap["n_dormant"] == snap["n_total"] == len(hf.DORMANT_FLAGS)
    assert all(v is False for v in snap["flags"].values())


def test_each_dormant_flag_activates_when_set(monkeypatch):
    for key, _ in hf.DORMANT_FLAGS:
        monkeypatch.delenv(key, raising=False)
    for key, _ in hf.DORMANT_FLAGS:
        monkeypatch.setenv(key, "1")
        assert hf.flag_on(key) is True
        snap = hf.snapshot()
        assert snap["flags"][key] is True
        assert key in snap["enabled"]
        monkeypatch.setenv(key, "0")
        assert hf.flag_on(key) is False


def test_helpers_match_registry(monkeypatch):
    monkeypatch.setenv("PROBE_SIZING", "1")
    monkeypatch.setenv("CRISIS_RECOMMEND", "1")
    monkeypatch.setenv("GP_PROMOTE", "1")
    assert hf.probe_sizing_enabled() is True
    assert hf.crisis_recommend_enabled() is True
    assert hf.gp_promote_enabled() is True
    monkeypatch.setenv("PROBE_SIZING", "0")
    monkeypatch.setenv("CRISIS_RECOMMEND", "0")
    monkeypatch.setenv("GP_PROMOTE", "0")
    assert hf.probe_sizing_enabled() is False
    assert hf.crisis_recommend_enabled() is False
    assert hf.gp_promote_enabled() is False


def test_write_heartbeat_includes_hif_flags(tmp_path, monkeypatch):
    from hermes_core.engines import loop as loop_mod

    monkeypatch.setattr(loop_mod, "_state_dir", lambda _bot: tmp_path)
    for key, _ in hf.DORMANT_FLAGS:
        monkeypatch.delenv(key, raising=False)
    snap = hf.snapshot()
    data = loop_mod.write_heartbeat(
        "forex",
        1,
        0,
        1.1,
        hif_flags=snap,
    )
    assert data["hif_flags"]["n_dormant"] == len(hf.DORMANT_FLAGS)
    assert (tmp_path / "heartbeat.json").exists()


def test_existing_enabled_helpers_still_default_off(monkeypatch):
    """Cross-check module-local *_enabled() stay soak-dormant."""
    from hermes_core.engines.book_risk import book_risk_enabled
    from hermes_core.engines.entry_ranking import entry_ranking_enabled
    from hermes_core.engines.exit_intel import exit_intel_enabled
    from hermes_core.engines.kelly_sizing import kelly_sizing_enabled
    from hermes_core.engines.policy_engine import soft_weights_enabled
    from hermes_core.engines.regime_sizing import regime_sizing_enabled
    from hermes_core.engines.skip_shadow_learn import (
        skip_shadow_promote_enabled,
        skip_shadow_reflect_enabled,
    )

    for key, _ in hf.DORMANT_FLAGS:
        monkeypatch.delenv(key, raising=False)

    assert soft_weights_enabled() is False
    assert regime_sizing_enabled() is False
    assert kelly_sizing_enabled() is False
    assert entry_ranking_enabled() is False
    assert exit_intel_enabled() is False
    assert book_risk_enabled() is False
    assert skip_shadow_reflect_enabled() is False
    assert skip_shadow_promote_enabled() is False
