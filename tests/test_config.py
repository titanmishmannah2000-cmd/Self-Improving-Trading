"""Session 1 / Phase 1 acceptance tests for the config system.

These four tests ARE the blueprint Phase-1 gate (blueprint Section 7, lines
1068-1082). They must pass for S1 to be complete:

    test_load_valid_forex_config   -> forex pairs list is exact
    test_load_stop_loss_below_min  -> stop_loss_pct 0.3 (< 0.5 min) raises ValidationError
    test_load_unknown_session_filter -> session_filter 'tokyo_only' raises ValidationError
    test_load_gold_config_momentum -> XAU/USD strategy_type == 'rsi_momentum'

Note: the blueprint test block asserts ``pytest.raises(ValidationError)`` while
the build-target prose says ``raises ValueError``. ValidationError subclasses
ValueError (see hermes_core/config/schema.py), so both assertions hold.
"""

from __future__ import annotations

import pytest

from hermes_core.config import (
    ValidationError,
    load_config,
    load_strategy_for_pair,
    validate_strategy_params,
)


def test_load_valid_forex_config():
    cfg = load_config("forex")
    assert cfg["pairs"] == ["EUR/USD", "GBP/USD", "GBP/JPY", "AUD/USD"]


def test_load_stop_loss_below_min():
    with pytest.raises(ValidationError):
        validate_strategy_params({"stop_loss_pct": 0.3})  # 0.3 < 0.5 minimum


def test_load_unknown_session_filter():
    with pytest.raises(ValidationError):
        validate_strategy_params({"entry": {"session_filter": "tokyo_only"}})


def test_load_gold_config_momentum():
    gold = load_strategy_for_pair("XAU/USD")
    assert gold["strategy_type"] == "rsi_momentum"  # not "mean_reversion"


# --- Discipline extras (beyond the blueprint's 4 tests) ----------------------
# The blueprint's 4 tests are the gate; these lock the rule-protecting invariants
# the S1 EXIT GATE calls out (gold never MR; per-pair types; range upper bounds;
# reflection cadence == 5 per the user override).


def test_gold_silver_are_momentum_not_mr():
    for pair in ("XAU/USD", "XAG/USD"):
        s = load_strategy_for_pair(pair)
        assert s["strategy_type"] == "rsi_momentum"
        assert s["strategy_type"] != "mean_reversion"


def test_forex_mr_pairs_load_with_correct_type():
    for pair in ("EUR/USD", "GBP/USD", "GBP/JPY"):
        s = load_strategy_for_pair(pair)
        assert s["strategy_type"] == "mean_reversion"
    aud = load_strategy_for_pair("AUD/USD")
    assert aud["strategy_type"] == "rsi_momentum"


def test_crypto_pairs_are_momentum_not_mr():
    """Crypto rethink: BTC/ETH use rsi_momentum (trend), not FX mean-reversion."""
    for pair in ("BTC/USD", "ETH/USD"):
        s = load_strategy_for_pair(pair, bot="crypto")
        assert s["strategy_type"] == "rsi_momentum"
        assert s["strategy_type"] != "mean_reversion"
        assert float(s["stop_loss_pct"]) >= 2.0
        assert float(s["position_size_r"]) <= 0.2


def test_reflection_every_is_five_per_user_override():
    cfg = load_config("forex")
    assert cfg["goal"]["reflection_every"] == 5
    gold = load_config("gold")
    assert gold["goal"]["reflection_every"] == 5


def test_valid_strategy_passes_validation():
    s = load_strategy_for_pair("EUR/USD")
    valid, errors = validate_strategy_params(s, raise_on_fail=False)
    assert valid, errors
    assert errors == []
