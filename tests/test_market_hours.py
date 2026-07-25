"""Tests for FX/metals weekend detection and flat-book backup."""

from __future__ import annotations

from datetime import UTC, datetime

from hermes_core.engines.market_hours import (
    is_bot_market_closed,
    is_fx_market_closed,
    is_metals_market_closed,
    live_book_is_flat,
)


def _ts(y, m, d, hh, mm=0) -> float:
    return datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp()


def test_fx_closed_friday_after_22utc():
    # 2026-07-24 was a Friday
    assert is_fx_market_closed(_ts(2026, 7, 24, 21, 59)) is False
    assert is_fx_market_closed(_ts(2026, 7, 24, 22, 0)) is True


def test_fx_closed_saturday_and_sunday_before_22():
    assert is_fx_market_closed(_ts(2026, 7, 25, 12, 0)) is True  # Sat
    assert is_fx_market_closed(_ts(2026, 7, 26, 21, 0)) is True  # Sun before 22
    assert is_fx_market_closed(_ts(2026, 7, 26, 22, 0)) is False  # Sun open


def test_bot_market_closed_crypto_always_open():
    assert is_bot_market_closed("crypto", _ts(2026, 7, 25, 12, 0)) is False
    assert is_bot_market_closed("forex", _ts(2026, 7, 25, 12, 0)) is True
    assert is_metals_market_closed(_ts(2026, 7, 25, 12, 0)) is True
    assert is_bot_market_closed("gold", _ts(2026, 7, 25, 12, 0)) is True


def test_live_book_is_flat():
    assert live_book_is_flat({"XAG/USD": [58.2] * 8}) is True
    assert live_book_is_flat({"XAG/USD": [58.2, 58.3, 58.4, 58.5, 58.6]}) is False
    assert live_book_is_flat({"A": [1, 1, 1], "B": [2, 2, 2]}, flat_tail=3) is True
    assert live_book_is_flat({}) is False
