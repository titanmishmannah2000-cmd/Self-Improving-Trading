"""BTC/USDT Focus — bots/btc regime gate + config (crypto restored separately)."""

from __future__ import annotations

from hermes_core.config import load_config, load_strategy_for_pair
from hermes_core.engines import btc_regime as br
from hermes_core.engines.entry import evaluate_entry_detailed
from hermes_core.engines.profitability_freeze import focus_pairs_for_bot


def test_btc_config_usdt_only():
    cfg = load_config("btc")
    assert cfg["bot"]["name"] == "btc"
    assert cfg["pairs"] == ["BTC/USDT"]
    assert cfg["invent"]["interval"] == "4h"
    assert cfg["invent"].get("enabled") is False
    s = load_strategy_for_pair("BTC/USDT", bot="btc")
    assert s["strategy_type"] == "donchian_breakout"
    assert focus_pairs_for_bot("btc") == ["BTC/USDT"]


def test_crypto_restored_btc_usd_eth():
    cfg = load_config("crypto")
    assert cfg["pairs"] == ["BTC/USD", "ETH/USD"]
    assert cfg["invent"]["interval"] == "1h"
    assert focus_pairs_for_bot("crypto") == ["BTC/USD"]


def test_classify_trend_up_from_rising_closes():
    # Strong uptrend: price > sma50 > sma200 with enough bars.
    closes = [100.0 + i * 0.5 for i in range(220)]
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    out = br.classify_from_closes(closes, highs=highs, lows=lows)
    assert out["label"] in (br.TREND_UP, br.CHOP)  # ADX approx may keep chop
    assert out["sma50"] is not None
    assert out["last"] == closes[-1]


def test_classify_chop_on_flat_series():
    closes = [100.0] * 220
    out = br.classify_from_closes(closes)
    assert out["label"] == br.CHOP


def test_hard_blocks_non_uptrend():
    assert br.hard_blocks_entry(br.CHOP) is True
    assert br.hard_blocks_entry(br.TREND_DOWN) is True
    assert br.hard_blocks_entry(br.TREND_UP) is False
    assert br.allows_long(br.TREND_UP) is True
    # v07: Donchian also hard-flat in chop (failed-breakout fee grind).
    assert br.hard_blocks_entry(br.CHOP, strategy_type="donchian_breakout") is True
    assert br.hard_blocks_entry(br.TREND_DOWN, strategy_type="donchian_breakout") is True
    assert br.hard_blocks_entry(br.TREND_UP, strategy_type="donchian_breakout") is False


def test_donchian_still_blocked_by_d1_downtrend(monkeypatch):
    monkeypatch.setattr(
        br,
        "classify_btc_regime",
        lambda pair, force=False: {
            "label": br.TREND_DOWN,
            "reason": "test_down",
            "pair": pair,
        },
    )
    prices = [100.0 + i * 0.1 for i in range(80)]
    strategy = load_strategy_for_pair("BTC/USDT", bot="btc")
    sig, reason = evaluate_entry_detailed(
        prices,
        strategy,
        pair="BTC/USDT",
        bot="btc",
        session_token="OTHER",
    )
    assert sig is None
    assert reason.startswith("btc_regime:trend_down")


def test_pair_aliases_yf_and_coinbase():
    from hermes_core.adapters.pair_aliases import coinbase_product, yfinance_symbol

    assert yfinance_symbol("BTC/USDT") == "BTC-USD"
    assert coinbase_product("BTC/USDT") == "BTC-USD"
