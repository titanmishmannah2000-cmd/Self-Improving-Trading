"""Pollution detector for trades.jsonl."""

from __future__ import annotations

from tools.clean_trades import is_polluted


def test_fixture_stub_is_polluted():
    assert is_polluted({
        "id": "t_forex_1", "pair": "EUR/USD", "pnl_pct": 1.2,
        "exit_reason": "tp", "entry_price": 1.08, "exit_price": 1.093,
    })


def test_impossible_fx_price_is_polluted():
    assert is_polluted({
        "pair": "EUR/USD", "pnl_pct": -3.2, "reason": "stop_loss",
        "entry_price": 0.32975, "exit_price": 0.31918, "size": 0.24,
    })


def test_replay_pnl_fingerprint_is_polluted():
    assert is_polluted({
        "id": "forex:GBP/USD:1784642936", "pair": "GBP/USD",
        "pnl_pct": -5.26730846950878, "reason": "stop_loss",
        "entry_price": 1.0015951622793189, "exit_price": 0.9488380554663901,
        "entry_ts": "2026-07-21T14:08:56.608260+00:00",
        "exit_ts": "2026-07-21T14:08:56.665969+00:00",
    })


def test_instant_close_is_polluted():
    assert is_polluted({
        "id": "forex:EUR/USD:1", "pair": "EUR/USD", "pnl_pct": 1.5,
        "entry_price": 1.1, "exit_price": 1.1166,
        "entry_ts": "2026-07-20T00:00:00Z",
        "exit_ts": "2026-07-20T00:00:30Z",
    })


def test_real_trade_not_polluted():
    assert not is_polluted({
        "id": "forex:EUR/USD:1784700000",
        "bot": "forex",
        "pair": "EUR/USD",
        "reason": "take_profit",
        "exit_reason": "take_profit",
        "entry_type": "mean_reversion",
        "entry_price": 1.1410,
        "exit_price": 1.1485,
        "entry_ts": "2026-07-22T10:00:00+00:00",
        "exit_ts": "2026-07-22T14:30:00+00:00",
        "pnl_pct": 0.66,
        "size": 0.1,
        "hold_cycles": 12,
    })
