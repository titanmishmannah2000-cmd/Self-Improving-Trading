"""Tests for forex regime split (MR vs trend_follow, sleeve-aware chart avoid)."""

from __future__ import annotations

from hermes_core.engines.entry import evaluate_entry, evaluate_entry_detailed
from hermes_core.engines.regime_split import (
    chart_blocks_sleeve,
    classify_market,
    cost_aware_ok,
    pick_sleeve,
    regime_split_enabled,
)

# Flat then dump → BB lower + oversold + calm ADX (range)
prices_at_bb_lower = [100] * 20 + [70]
# Rising series for trend strength (RSI high, ADX elevated)
prices_uptrend = [70 + i * 1.5 for i in range(40)]


def mr_strategy(**kw):
    base = {
        "strategy_type": "mean_reversion",
        "session_filter": "24h",
        "entry": {"threshold": 38, "session_filter": "24h"},
        "position_size_r": 0.2,
        "adx_threshold": 20,
        "vol_min_pct": 0.005,
        "vol_threshold_pct": 0.01,
        "vol_max_pct": 5.0,
    }
    base.update(kw)
    return base


def test_regime_split_default_forex_only(monkeypatch):
    monkeypatch.delenv("REGIME_SPLIT", raising=False)
    assert regime_split_enabled(bot="forex") is True
    assert regime_split_enabled(bot="gold") is False
    assert regime_split_enabled(bot="crypto") is False


def test_classify_range_vs_trend():
    assert classify_market(adx=10, regime="range", context="") == "range"
    assert (
        classify_market(
            adx=30,
            regime="trend",
            context="trend: uptrend (conf=0.80). Rec: enter long",
        )
        == "trend_up"
    )
    assert (
        classify_market(
            adx=30,
            regime="trend",
            context="trend: downtrend (conf=0.85). Rec: avoid entirely",
        )
        == "trend_down"
    )


def test_chart_avoid_allows_mr_in_range_only():
    ctx = "trend: downtrend (conf=0.85). Rec: avoid entirely"
    assert chart_blocks_sleeve(ctx, sleeve="mean_reversion", market="range") is False
    assert chart_blocks_sleeve(ctx, sleeve="mean_reversion", market="trend_up") is True
    assert chart_blocks_sleeve(ctx, sleeve="mean_reversion", market="trend_down") is True
    assert chart_blocks_sleeve(ctx, sleeve="trend_follow", market="trend_down") is True
    assert (
        chart_blocks_sleeve(
            "trend: uptrend (conf=0.8). Rec: enter long",
            sleeve="trend_follow",
            market="trend_up",
        )
        is False
    )


def test_pick_sleeve():
    assert pick_sleeve("range") == "mean_reversion"
    assert pick_sleeve("trend_up") == "trend_follow"
    assert pick_sleeve("trend_down") is None


def test_legacy_chart_block_without_bot():
    """Without bot=forex, REGIME_SPLIT stays off → legacy avoid veto."""
    sig = evaluate_entry(
        "EUR/USD",
        prices_at_bb_lower,
        mr_strategy(),
        "avoid entirely",
        "neutral",
        0,
        False,
        {},
        100,
        "LDN",
    )
    assert sig is None


def test_forex_range_mr_survives_chart_avoid(monkeypatch):
    monkeypatch.setenv("REGIME_SPLIT", "1")
    monkeypatch.setenv("SCORECARD_COST_PCT", "0")  # ignore cost gate in unit fixture
    sig, reason = evaluate_entry_detailed(
        prices_at_bb_lower,
        mr_strategy(),
        pair="GBP/USD",
        context="trend: downtrend (conf=0.85). Rec: avoid entirely",
        session_token="LDN",
        regime="range",
        bot="forex",
        cost_pct=0.0,
    )
    assert reason == ""
    assert sig is not None
    assert sig.type == "mean_reversion"
    assert sig.meta.get("regime_split") is True
    assert sig.meta.get("market_class") == "range"


def test_forex_trend_down_skips(monkeypatch):
    monkeypatch.setenv("REGIME_SPLIT", "1")
    sig, reason = evaluate_entry_detailed(
        prices_uptrend,
        mr_strategy(),
        pair="GBP/USD",
        context="trend: downtrend (conf=0.85). Rec: avoid entirely",
        session_token="LDN",
        regime="trend",
        bot="forex",
        cost_pct=0.0,
    )
    assert sig is None
    assert reason in ("regime:trend_down", "chart:hard_block", "trend:rsi_weak", "trend:adx_weak", "vol", "trend:below_mid")


def test_cost_aware_gate():
    assert cost_aware_ok(1.0, 100.0, cost_pct=0.05) is True  # atr% = 1.0 >= 0.1
    assert cost_aware_ok(0.01, 100.0, cost_pct=0.05) is False  # atr% = 0.01 < 0.1
