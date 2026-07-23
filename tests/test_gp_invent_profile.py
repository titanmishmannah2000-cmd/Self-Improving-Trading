"""Per-bot invent profiles + same-regime (interval, horizon) voting."""

from __future__ import annotations

import json

import pytest

import hermes_core.engines.genetic as gp
from hermes_core.engines import entry as entry_mod
from hermes_core.engines.gp_invent_profile import (
    has_votable_for_regime,
    indicator_matches_regime,
    invent_profile,
)


@pytest.fixture(autouse=True)
def _tmp_discovered(tmp_path, monkeypatch):
    monkeypatch.setattr(gp, "DISCOVERED_DIR", tmp_path / "discovered")
    yield


def test_invent_profiles_per_bot():
    fx = invent_profile("forex")
    assert fx["interval"] == "1d"
    assert fx["horizon"] == 10
    assert fx["timeout_s"] >= 240

    gd = invent_profile("gold")
    assert gd["interval"] == "1d"
    assert gd["horizon"] == 20
    assert gd["timeout_s"] >= 240

    cr = invent_profile("crypto")
    assert cr["interval"] == "1h"
    assert cr["horizon"] == 12
    assert cr["timeout_s"] >= 180
    assert cr["generations"] <= 24
    assert cr["pop_size"] <= 30
    assert cr["n_islands"] == 1


def test_invent_profile_config_override(tmp_path, monkeypatch):
    cfg = tmp_path / "bots" / "crypto"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(
        "bot:\n  name: crypto\npairs: [BTC/USD]\n"
        "invent:\n  horizon: 8\n  timeout_s: 240\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "hermes_core.config.loader.repo_root", lambda: tmp_path,
    )
    # invent_profile imports load_config from hermes_core.config
    monkeypatch.setattr(
        "hermes_core.config.load_config",
        lambda bot: {
            "invent": {"horizon": 8, "timeout_s": 240},
            "pairs": ["BTC/USD"],
        },
    )
    cr = invent_profile("crypto")
    assert cr["horizon"] == 8
    assert cr["timeout_s"] == 240
    # Untouched keys keep crypto defaults.
    assert cr["interval"] == "1h"


def test_indicator_matches_regime():
    assert indicator_matches_regime(
        {"interval": "1h", "horizon": 12}, interval="1h", horizon=12,
    )
    assert not indicator_matches_regime(
        {"interval": "1d", "horizon": 60}, interval="1h", horizon=12,
    )
    assert not indicator_matches_regime(
        {"interval": "1h", "horizon": 60}, interval="1h", horizon=12,
    )


def test_has_votable_for_regime_ignores_wrong_horizon():
    inds = [
        {
            "name": "(price-sma20)", "expr": "(price-sma20)",
            "backtest_approved": True, "interval": "1d", "horizon": 60,
        },
    ]
    assert has_votable_for_regime(inds, interval="1d", horizon=10) is False
    inds[0]["horizon"] = 10
    assert has_votable_for_regime(inds, interval="1d", horizon=10) is True


def test_ensemble_only_same_type_formulas_vote(tmp_path):
    """Legacy daily/h60 must not vote with active forex invent (1d/h10)."""
    import math

    path = gp._discovered_path("EUR/USD")
    path.parent.mkdir(parents=True, exist_ok=True)
    inds = [
        {
            "pair": "EUR/USD", "name": "legacy_h60_a", "expr": "(price-sma20)",
            "fitness": 0.5, "win_rate": 0.6, "oos_corr": 0.4,
            "backtest_approved": True, "interval": "1d", "horizon": 60,
        },
        {
            "pair": "EUR/USD", "name": "legacy_h60_b", "expr": "(ema20-sma20)",
            "fitness": 0.5, "win_rate": 0.6, "oos_corr": 0.4,
            "backtest_approved": True, "interval": "1d", "horizon": 60,
        },
        {
            "pair": "EUR/USD", "name": "new_h10_a", "expr": "(price-sma20)",
            "fitness": 0.5, "win_rate": 0.6, "oos_corr": 0.4,
            "backtest_approved": True, "interval": "1d", "horizon": 10,
        },
        {
            "pair": "EUR/USD", "name": "new_h10_b", "expr": "(ema20-sma20)",
            "fitness": 0.5, "win_rate": 0.6, "oos_corr": 0.4,
            "backtest_approved": True, "interval": "1d", "horizon": 10,
        },
    ]
    path.write_text(json.dumps(inds), encoding="utf-8")
    daily = [100.0 + 5.0 * math.sin(i / 12.0) + 0.02 * i for i in range(260)]

    # Default forex profile = 1d/h10 → only new formulas vote.
    sig = entry_mod.gp_ensemble_signal(
        "EUR/USD", daily, daily_prices=daily, promote=False,
        invent_interval="1d", invent_horizon=10,
    )
    assert sig is not None
    fired = set(sig.meta["gp_indicators"])
    assert "new_h10_a" in fired and "new_h10_b" in fired
    assert "legacy_h60_a" not in fired

    # Asking for h60 regime excludes the new formulas.
    sig_old = entry_mod.gp_ensemble_signal(
        "EUR/USD", daily, daily_prices=daily, promote=False,
        invent_interval="1d", invent_horizon=60,
    )
    assert sig_old is not None
    fired_old = set(sig_old.meta["gp_indicators"])
    assert "legacy_h60_a" in fired_old
    assert "new_h10_a" not in fired_old


def test_discover_tags_interval_and_horizon(monkeypatch):
    monkeypatch.setattr(
        "hermes_core.engines.backtest.backtest_gp_indicator",
        lambda *a, **k: {"approved": True, "reason": "test_stub", "oos_corr": 0.2},
    )
    prices = [100.0 + 0.15 * i for i in range(220)]
    # Tiny search — may admit 0; we only assert tags when something lands.
    inds = gp.discover(
        "BTC/USD", prices,
        generations=2, pop_size=6, top_k=2, horizon=12,
        n_islands=1, interval="1h", seed=3,
    )
    for ind in inds:
        assert ind.get("interval") == "1h"
        assert int(ind.get("horizon")) == 12
