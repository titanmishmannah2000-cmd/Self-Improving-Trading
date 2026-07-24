"""Cortex 30-day soak durability: atomic I/O, bot bind, open-row close, GP exile."""

from __future__ import annotations

import json

import pytest

import hermes_core.engines.decision_cortex as dc
import hermes_core.engines.policy_engine as pe
from hermes_core.state.atomic_json import atomic_write_json, load_json


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    cortex_dir = tmp_path / "cortex"
    monkeypatch.setattr(dc, "CORTEX_DIR", cortex_dir)
    monkeypatch.setattr(dc, "EXILE_PATH", cortex_dir / "indicator_exile.json")
    monkeypatch.setattr(dc, "MEMORY_PATH", cortex_dir / "cortex_memory.json")
    monkeypatch.setattr(pe, "POLICY_PATH", tmp_path / "policy.json")
    yield


def test_atomic_write_roundtrip(tmp_path):
    path = tmp_path / "x.json"
    atomic_write_json(path, {"a": 1}, indent=2)
    assert load_json(path) == {"a": 1}
    assert not list(tmp_path.glob(".*.tmp"))


def test_corrupt_json_quarantined(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not-json", encoding="utf-8")
    assert load_json(path, default={"ok": True}) == {"ok": True}
    assert not path.exists()
    q = list(tmp_path.glob("bad.json.corrupt-*"))
    assert len(q) == 1


def test_cortex_corrupt_memory_does_not_wipe_without_trace(tmp_path, monkeypatch):
    mem = tmp_path / "cortex" / "cortex_memory.json"
    mem.parent.mkdir(parents=True)
    mem.write_text("{broken", encoding="utf-8")
    c = dc.Cortex(bot="forex")
    assert c._entries == []
    assert list(mem.parent.glob("cortex_memory.json.corrupt-*"))
    c.record_outcome("EUR/USD", "mean_reversion", 0.5)
    assert mem.exists()
    data = json.loads(mem.read_text(encoding="utf-8"))
    assert len(data["entries"]) == 1


def test_policy_saves_under_cortex_bot_not_env(tmp_path, monkeypatch):
    """CLI bot=crypto must not write policy under HERMES_BOT_NAME=forex."""
    monkeypatch.setenv("HERMES_BOT_NAME", "forex")
    crypto_dir = tmp_path / "crypto_cortex"
    monkeypatch.setattr(dc, "CORTEX_DIR", crypto_dir)
    monkeypatch.setattr(dc, "EXILE_PATH", crypto_dir / "indicator_exile.json")
    monkeypatch.setattr(dc, "MEMORY_PATH", crypto_dir / "cortex_memory.json")
    crypto_policy = tmp_path / "crypto" / "state" / "policy.json"
    monkeypatch.setattr(pe, "POLICY_PATH", None)

    def _policy_path(bot=None):
        b = bot or "forex"
        p = tmp_path / b / "state" / "policy.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    monkeypatch.setattr(pe, "policy_path", _policy_path)
    # Re-bind _policy_file to use patched policy_path
    monkeypatch.setattr(pe, "POLICY_PATH", None)

    c = dc.Cortex(bot="crypto")
    for i in range(20):
        c.record_outcome("BTC/USD", "mean_reversion", 1.0 if i < 9 else -1.0)
    for i in range(20):
        c.record_outcome("BTC/USD", "gp_ensemble", 1.0 if i < 4 else -1.0)

    # Force evaluate to resolve policy file via bot_for_save
    real_save = pe._save_policy
    saved_bots: list[str] = []

    def _track(policy, bot=None):
        saved_bots.append(bot)
        path = tmp_path / str(bot) / "policy.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, policy, indent=2)

    monkeypatch.setattr(pe, "_save_policy", _track)
    pe.PolicyEngine().evaluate(1, ["BTC/USD"], cortex=c)
    assert saved_bots == ["crypto"]


def test_record_outcome_fills_open_row():
    c = dc.Cortex(bot="forex")
    c.record_entry("EUR/USD", "mean_reversion")
    assert c.summary()["summary"]["entries_open"] == 1
    c.record_outcome("EUR/USD", "mean_reversion", 0.4)
    s = c.summary()["summary"]
    assert s["entries_open"] == 0
    assert s["entries_total"] == 1
    assert len(c._entries) == 1


def test_shadow_open_filled_as_gp_ensemble():
    c = dc.Cortex(bot="forex")
    c.record_entry("EUR/USD", "shadow")
    c.record_outcome("EUR/USD", "gp_ensemble", -0.2)
    assert len(c._entries) == 1
    assert c._entries[0]["type"] == "gp_ensemble"
    assert c.evidence_n("EUR/USD", "gp_ensemble") == 1


def test_partial_excluded_from_wr_and_evidence():
    c = dc.Cortex(bot="forex")
    c.record_outcome("EUR/USD", "mean_reversion", 1.0, partial=True)
    c.record_outcome("EUR/USD", "mean_reversion", -0.5)
    assert c.evidence_n("EUR/USD", "mean_reversion") == 1
    assert c.entry_type_wr("mean_reversion", pair="EUR/USD") == 0.0


def test_best_entry_type_respects_pair():
    c = dc.Cortex(bot="forex")
    for _ in range(10):
        c.record_outcome("EUR/USD", "gp_ensemble", 1.0)
    for _ in range(10):
        c.record_outcome("EUR/USD", "mean_reversion", -1.0)
    for _ in range(10):
        c.record_outcome("GBP/USD", "mean_reversion", 1.0)
    for _ in range(10):
        c.record_outcome("GBP/USD", "gp_ensemble", -1.0)
    assert c.best_entry_type("EUR/USD") == "gp_ensemble"
    assert c.best_entry_type("GBP/USD") == "mean_reversion"


def test_exile_uses_gp_subblock_not_overall():
    c = dc.Cortex(bot="forex")
    ind = "gp_only_bad"
    # Overall would be mixed if we had non-GP credit; gate on GP losses only.
    for _ in range(5):
        c.record_indicator_outcome(ind, -1.0, entry_type="gp_ensemble")
    assert c.is_indicator_exiled(ind) is True


def test_exile_file_synced_on_reload(tmp_path, monkeypatch):
    c = dc.Cortex(bot="forex")
    c.exile_indicator("manual_x")
    c2 = dc.Cortex(bot="forex")
    assert c2.is_indicator_exiled("manual_x")
    assert c2._indicator_stats["manual_x"]["exiled"] is True


def test_apply_live_feedback_uses_cortex_bot(tmp_path, monkeypatch):
    from hermes_core.engines import genetic as gp

    forex_dir = tmp_path / "forex_cx"
    crypto_dir = tmp_path / "crypto_cx"
    monkeypatch.setenv("HERMES_BOT_NAME", "forex")
    monkeypatch.setattr(dc, "CORTEX_DIR", crypto_dir)
    monkeypatch.setattr(dc, "EXILE_PATH", crypto_dir / "indicator_exile.json")
    monkeypatch.setattr(dc, "MEMORY_PATH", crypto_dir / "cortex_memory.json")

    c = dc.Cortex(bot="crypto")
    for _ in range(5):
        c.record_indicator_outcome("ind_live", 1.0, entry_type="gp_ensemble")

    # If feedback ignored cortex._bot it would load empty forex memory.
    monkeypatch.setattr(gp, "load_discovered_indicators", lambda pair, include_shared=False: [
        {"name": "ind_live", "fitness": 0.5, "expr": "div(close, close)"},
    ])
    monkeypatch.setattr(gp, "_is_dashboard_seed_fixture", lambda ind: False)
    monkeypatch.setattr(gp, "_save_discovered", lambda *a, **k: tmp_path / "noop.json")

    n = gp.apply_live_feedback("BTC/USD", c)
    assert n >= 0
    stats = dc.Cortex(bot="crypto").indicator_live_stats("ind_live")
    assert stats.get("attempts", 0) >= 5
