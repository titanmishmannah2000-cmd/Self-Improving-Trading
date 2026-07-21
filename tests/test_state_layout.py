"""Unified state tree + strategy version baseline (audit items 11 + 14)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hermes_core.config import (
    ensure_strategy_seeded,
    load_strategy_for_pair,
    seed_strategy_path,
    strategy_yaml_path,
)


@pytest.fixture
def volume_root(tmp_path, monkeypatch):
    """Point HERMES_STATE_ROOT at an isolated temp volume."""
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("HERMES_STATE_ROOT", str(root))
    # Clear cached module-level assumptions; state_root reads env each call.
    yield root


def test_strategy_live_path_is_on_volume(volume_root):
    live = strategy_yaml_path("EUR/USD", "forex")
    assert live == volume_root / "forex" / "state" / "strategies" / "EUR_USD.yaml"
    seed = seed_strategy_path("EUR/USD", "forex")
    assert "bots" in seed.parts
    assert seed.name == "EUR_USD.yaml"
    assert live != seed


def test_ensure_strategy_seeded_copies_once(volume_root):
    live = ensure_strategy_seeded("EUR/USD", "forex")
    assert live.exists()
    data = yaml.safe_load(live.read_text(encoding="utf-8"))
    assert data.get("version") == "00"
    assert data.get("strategy_type") == "mean_reversion"

    # Mutate volume file; re-seed must NOT overwrite.
    data["stop_loss_pct"] = 1.2
    data["version"] = "01"
    live.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    ensure_strategy_seeded("EUR/USD", "forex")
    again = yaml.safe_load(live.read_text(encoding="utf-8"))
    assert again["stop_loss_pct"] == 1.2
    assert again["version"] == "01"


def test_load_strategy_prefers_volume(volume_root):
    ensure_strategy_seeded("EUR/USD", "forex")
    live = strategy_yaml_path("EUR/USD", "forex")
    patched = yaml.safe_load(live.read_text(encoding="utf-8"))
    patched["stop_loss_pct"] = 1.8
    patched["version"] = "03"
    live.write_text(yaml.safe_dump(patched, sort_keys=False), encoding="utf-8")

    loaded = load_strategy_for_pair("EUR/USD", "forex")
    assert loaded["stop_loss_pct"] == 1.8
    assert loaded["version"] == "03"


def test_all_seed_strategies_have_version():
    bots = Path(__file__).resolve().parent.parent / "bots"
    yamls = list(bots.glob("*/state/strategies/*.yaml"))
    assert len(yamls) >= 8
    for path in yamls:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "version" in data, f"missing version in {path}"
        assert str(data["version"]), f"empty version in {path}"


def test_load_strategy_returns_version_field(volume_root):
    s = load_strategy_for_pair("XAU/USD", "gold")
    assert s.get("version") == "00"
    assert strategy_yaml_path("XAU/USD", "gold").exists()
