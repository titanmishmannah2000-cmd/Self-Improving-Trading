"""Tests for shared tick bucketing + flat-series detection."""

from __future__ import annotations

from hermes_core.adapters.tick_history import (
    append_bucketed_tick,
    series_is_flat,
)


def test_append_identical_ticks_refresh_tip_only():
    hist: list[dict] = []
    for i in range(20):
        append_bucketed_tick(
            hist,
            {"price": 100.0, "ts": 1_000_000.0 + i},  # same price, <45s apart
            sample_min_s=45.0,
        )
    assert len(hist) == 1
    assert hist[0]["price"] == 100.0


def test_append_moves_or_ages_create_bars():
    hist: list[dict] = []
    append_bucketed_tick(hist, {"price": 100.0, "ts": 1_000.0})
    append_bucketed_tick(hist, {"price": 100.0, "ts": 1_010.0})  # no move, young
    assert len(hist) == 1
    append_bucketed_tick(hist, {"price": 100.02, "ts": 1_011.0})  # moved
    assert len(hist) == 2
    append_bucketed_tick(hist, {"price": 100.02, "ts": 1_011.0 + 50})  # aged
    assert len(hist) == 3


def test_series_is_flat_identical_closes():
    flat = [{"price": 65000.0, "ts": float(i)} for i in range(80)]
    assert series_is_flat(flat) is True


def test_series_is_flat_varied():
    varied = [{"price": 65000.0 + i * 5, "ts": float(i)} for i in range(80)]
    assert series_is_flat(varied) is False


def test_crypto_flat_ws_falls_through_to_yahoo(monkeypatch):
    """Flat Coinbase buffer must not win over varied Yahoo history."""
    import hermes_core.adapters.aggregate as agg

    flat_ws = [{"price": 65_000.0, "ts": float(i)} for i in range(80)]
    yahoo = [{"price": 65_000.0 + i * 10, "ts": float(i)} for i in range(80)]
    monkeypatch.setattr(
        agg,
        "_external_history",
        lambda pair, max_candles=300: yahoo if pair == "BTC/USD" else [],
    )
    a = agg.PriceAggregator(["BTC/USD"], sources=[])
    # Inject flat WS history as if Coinbase had been spamming identical ticks.
    a._crypto._history["BTC/USD"] = list(flat_ws)
    hist = a.seed_history_fn("BTC/USD", max_candles=300)
    assert len(hist) >= 50
    assert hist[0]["price"] < hist[-1]["price"]
    assert not agg.series_is_flat(hist)
