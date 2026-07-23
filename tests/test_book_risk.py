"""HIF book-level risk — soft cap + edge tilt."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_core.engines.book_risk import (
    BOOK_RISK_CAP,
    apply_book_risk,
    book_risk_enabled,
)


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("BOOK_RISK", raising=False)
    assert book_risk_enabled() is False


def test_disabled_passthrough():
    out = apply_book_risk(
        0.4,
        enabled=False,
        open_positions={"GBP/USD": {"size": 0.4}},
        pair="EUR/USD",
        entry_type="mean_reversion",
    )
    assert out["size"] == pytest.approx(0.4)
    assert out["book_mode"] == "disabled"


def test_cap_shrinks_when_book_full():
    open_pos = {
        "GBP/USD": {"size": 0.5, "entry_type": "mean_reversion"},
        "USD/JPY": {"size": 0.4, "entry_type": "mean_reversion"},
    }
    out = apply_book_risk(
        0.4,
        enabled=True,
        open_positions=open_pos,
        pair="EUR/USD",
        entry_type="mean_reversion",
        cortex=None,
        book_cap=1.0,
    )
    assert out["book_mode"] == "soft"
    assert out["book_used"] == pytest.approx(0.9)
    assert out["size"] == pytest.approx(0.1)
    assert "book_cap" in out["book_reasons"]


def test_tilt_toward_better_edge():
    def edge(pair, _et):
        if pair == "EUR/USD":
            return {"wins": 20, "losses": 5, "n": 25}
        return {"wins": 2, "losses": 18, "n": 20}

    cortex = SimpleNamespace(edge_stats=edge)
    open_pos = {
        "GBP/USD": {"size": 0.2, "entry_type": "mean_reversion"},
    }
    out = apply_book_risk(
        0.4,
        enabled=True,
        open_positions=open_pos,
        pair="EUR/USD",
        entry_type="mean_reversion",
        cortex=cortex,
        book_cap=BOOK_RISK_CAP,
    )
    assert out["book_tilt"] > 1.0
    assert out["size"] > 0.4 or out["size"] <= out["book_remaining"]


def test_fail_open_on_bad_positions():
    # Non-dict garbage should not raise
    out = apply_book_risk(
        0.3,
        enabled=True,
        open_positions={"X": "bad"},
        pair="EUR/USD",
        entry_type="mean_reversion",
    )
    assert out["size"] >= 0.0
    assert out["book_mode"] in ("soft", "passthrough")
