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

Profitability Path Phase 0 narrowed live pairs; parked pair strategy seeds are
still validated via explicit ``bot=``.
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
    # Profitability Path Phase 0: EUR + GBP only (GBP/JPY, AUD parked).
    assert cfg["pairs"] == ["EUR/USD", "GBP/USD"]


def test_load_stop_loss_below_min():
    with pytest.raises(ValidationError):
        validate_strategy_params({"stop_loss_pct": 0.3})  # 0.3 < 0.5 minimum


def test_load_unknown_session_filter():
    with pytest.raises(ValidationError):
        validate_strategy_params({"entry": {"session_filter": "tokyo_only"}})


def test_load_gold_config_momentum():
    gold = load_strategy_for_pair("XAU/USD")
    assert gold["strategy_type"] == "rsi_momentum"  # not "mean_reversion"


def test_gold_silver_are_momentum_not_mr():
    # XAG strategy seed remains momentum even while parked from gold pairs list.
    assert load_strategy_for_pair("XAU/USD")["strategy_type"] == "rsi_momentum"
    assert load_strategy_for_pair("XAG/USD", bot="gold")["strategy_type"] == "rsi_momentum"


def test_forex_mr_pairs_load_with_correct_type():
    for pair in ("EUR/USD", "GBP/USD"):
        s = load_strategy_for_pair(pair)
        assert s["strategy_type"] == "mean_reversion"
    # Parked seeds still load via explicit bot.
    assert load_strategy_for_pair("GBP/JPY", bot="forex")["strategy_type"] == "mean_reversion"
    assert load_strategy_for_pair("AUD/USD", bot="forex")["strategy_type"] == "rsi_momentum"


def test_crypto_pairs_are_momentum_not_mr():
    """Crypto BTC/USD + ETH/USD use rsi_momentum; BTC bot uses Donchian (Phase 3)."""
    s = load_strategy_for_pair("BTC/USDT", bot="btc")
    assert s["strategy_type"] == "donchian_breakout"
    assert float(s["stop_loss_pct"]) >= 2.0
    assert float(s["position_size_r"]) <= 0.2
    legacy = load_strategy_for_pair("BTC/USD", bot="crypto")
    assert legacy["strategy_type"] == "rsi_momentum"


def test_reflection_every_is_fifteen_profitability_path():
    cfg = load_config("forex")
    assert cfg["goal"]["reflection_every"] == 15
    gold = load_config("gold")
    assert gold["goal"]["reflection_every"] == 15
    assert gold["pairs"] == ["XAU/USD"]
    crypto = load_config("crypto")
    assert crypto["goal"]["reflection_every"] == 15
    assert crypto["pairs"] == ["BTC/USD", "ETH/USD"]
    assert crypto["invent"]["interval"] == "1h"
    btc = load_config("btc")
    assert btc["goal"]["reflection_every"] == 10
    assert btc["pairs"] == ["BTC/USDT"]
    assert btc["invent"]["interval"] == "4h"


def test_valid_strategy_passes_validation():
    s = load_strategy_for_pair("EUR/USD")
    valid, errors = validate_strategy_params(s, raise_on_fail=False)
    assert valid, errors
    assert errors == []
    assert "version" in s


def test_strategy_seeds_declare_version():
    """Item 14: every image seed strategy ships with a baseline version."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parent.parent / "bots"
    for path in root.glob("*/state/strategies/*.yaml"):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data.get("version") is not None, path.name
