"""Unit tests for hermes_core.engines.cost_model (BTC/USDT Focus Phase 1)."""

from __future__ import annotations

from hermes_core.engines import cost_model as cm


def test_btc_round_trip_uses_taker_and_floor_slippage(monkeypatch):
    monkeypatch.setenv("BTC_TAKER_FEE_PCT", "0.1")
    monkeypatch.setenv("BTC_SLIPPAGE_FLOOR_BPS", "1")  # 0.01%
    monkeypatch.setenv("BTC_SLIPPAGE_ATR_K", "0.05")
    monkeypatch.setenv("COST_STRESS_MULT", "2")
    b = cm.estimate("BTC/USDT", atr_pct=None)
    assert b.model == "btc_venue"
    assert abs(b.fee_pct_one_way - 0.1) < 1e-9
    assert abs(b.slippage_pct_one_way - 0.01) < 1e-9
    assert abs(b.round_trip_pct - 0.22) < 1e-9
    assert abs(b.stressed_round_trip_pct - 0.44) < 1e-9


def test_btc_slippage_scales_with_atr(monkeypatch):
    monkeypatch.setenv("BTC_TAKER_FEE_PCT", "0.1")
    monkeypatch.setenv("BTC_SLIPPAGE_FLOOR_BPS", "1")
    monkeypatch.setenv("BTC_SLIPPAGE_ATR_K", "0.05")
    b = cm.estimate("BTC/USDT", atr_pct=2.0)  # 0.05*2=0.1% > floor 0.01%
    assert abs(b.slippage_pct_one_way - 0.1) < 1e-9
    assert abs(b.round_trip_pct - 0.4) < 1e-9  # 2*(0.1+0.1)


def test_fx_uses_flat_fallback(monkeypatch):
    monkeypatch.setenv("SCORECARD_COST_PCT", "0.05")
    b = cm.estimate("EUR/USD")
    assert b.model == "flat"
    assert abs(b.round_trip_pct - 0.05) < 1e-9


def test_entry_exit_haircuts_adverse():
    # Long: buy higher, sell lower
    assert cm.apply_entry_fill(100.0, "long", 1.0) == 101.0
    assert cm.apply_exit_fill(100.0, "long", 1.0) == 99.0
    # Short: sell lower, buy higher
    assert cm.apply_entry_fill(100.0, "short", 1.0) == 99.0
    assert cm.apply_exit_fill(100.0, "short", 1.0) == 101.0


def test_net_pnl_subtracts_round_trip():
    assert abs(cm.net_pnl_pct(1.0, 0.22) - 0.78) < 1e-9


def test_is_btc_pair_aliases():
    assert cm.is_btc_pair("BTC/USDT")
    assert cm.is_btc_pair("BTC/USD")
    assert not cm.is_btc_pair("ETH/USD")
    assert not cm.is_btc_pair("EUR/USD")
