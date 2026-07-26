"""Phase 1 — strict deploy proof: candidate must beat the last version on real data.

Network-free: prices injected, KB redirected to tmp. These assert the STRICT
gate is stricter than the permissive delta gate and fails closed on bad data.
"""

from __future__ import annotations

import random

import pytest

import hermes_core.engines.backtest as bt
from hermes_core.engines.backtest import _is_degenerate_prices, backtest_with_history


def _mr_friendly(n=300, start=1.10, dip=0.97, period=25, seed=1):
    rng = random.Random(seed)
    out = [start]
    for i in range(1, n):
        if i % period == 0:
            out.append(out[-1] * dip)
        elif i % period in (1, 2, 3):
            out.append(out[-1] * 1.012)
        else:
            out.append(out[-1] * (1 + rng.uniform(-0.0004, 0.0004)))
    return out


STRAT_MR = {
    "strategy_type": "mean_reversion",
    "session_filter": "24h",
    "entry": {"threshold": 30, "session_filter": "24h"},
    "stop_loss_pct": 1.5,
    "profit_target_pct": 3.0,
    "version": "03",
}


@pytest.fixture(autouse=True)
def _tmp_kb(tmp_path, monkeypatch):
    monkeypatch.setattr(bt, "KB_PATH", tmp_path / "hypotheses_kb.jsonl")
    yield tmp_path


def _gate(param, old, new, *, strict=False, prices=None, **kw):
    kw.setdefault("ensemble_consensus", "neutral")
    return backtest_with_history(
        "EUR/USD", param, old, new, strategy=STRAT_MR, prices=prices, strict=strict, **kw
    )


# ── 1.4 real-data-only guard ────────────────────────────────────────────────
def test_is_degenerate_prices():
    assert _is_degenerate_prices([]) is True
    assert _is_degenerate_prices([1.1]) is True
    assert _is_degenerate_prices([1.1] * 100) is True  # zero variance
    assert _is_degenerate_prices([1.1, float("nan")] * 50) is True
    assert _is_degenerate_prices(_mr_friendly()) is False


def test_strict_rejects_short_history():
    short = _mr_friendly(n=40)  # < STRICT_MIN_BARS
    res = _gate("stop_loss_pct", 1.5, 1.2, strict=True, prices=short)
    assert res["approved"] is False
    assert "insufficient real data" in res["reason"]
    assert res["data_bars"] == 40


def test_strict_rejects_degenerate_feed():
    flat = [1.10] * 120  # enough bars but zero variance
    res = _gate("stop_loss_pct", 1.5, 1.2, strict=True, prices=flat)
    assert res["approved"] is False
    assert "insufficient real data" in res["reason"]


# ── 1.1 / 1.2 strict superiority over the last version ──────────────────────
def test_noop_change_approved_permissive_but_rejected_strict():
    """A no-op (old==new) passes the permissive delta gate (0 > -0.1) but must
    FAIL strict: it does not strictly beat the previous version."""
    prices = _mr_friendly()
    permissive = _gate("stop_loss_pct", 1.5, 1.5, strict=False, prices=prices)
    assert permissive["approved"] is True  # existing lenient behavior preserved

    prices2 = _mr_friendly(seed=7)  # fresh series → avoid KB hit on same params
    strict = _gate("stop_loss_pct", 1.5, 1.5, strict=True, prices=prices2)
    assert strict["approved"] is False
    assert "STRICT FAIL" in strict["reason"]
    assert strict["phases"]["phase1b_strict"]["improved_full"] is False


def test_strict_requires_positive_oos_improvement():
    prices = _mr_friendly(seed=11)
    res = _gate("stop_loss_pct", 1.5, 1.5, strict=True, prices=prices)
    sb = res["phases"]["phase1b_strict"]
    # No-op → neither full nor OOS strictly improved.
    assert sb["improved_full"] is False
    assert sb["improved_oos"] is False
    assert sb["ok"] is False


# ── 1.3 absolute floor cannot be bypassed by a relative improvement ─────────
def test_strict_floor_min_entries_blocks(monkeypatch):
    """Even if improvement gates are satisfied, too few trades fails the floor."""
    # Make improvement always pass so we isolate the floor gate.
    monkeypatch.setattr(bt, "STRICT_IMPROVE_MARGIN", -999.0)
    monkeypatch.setattr(bt, "STRICT_OOS_IMPROVE_MARGIN", -999.0)
    monkeypatch.setattr(bt, "STRICT_FLOOR_MIN_ENTRIES", 999999)
    prices = _mr_friendly(seed=3)
    res = _gate("stop_loss_pct", 1.5, 1.2, strict=True, prices=prices)
    assert res["approved"] is False
    sb = res["phases"]["phase1b_strict"]
    assert sb["floor_ok"] is False
    assert "STRICT FAIL" in res["reason"]


def test_strict_floor_max_dd_blocks(monkeypatch):
    monkeypatch.setattr(bt, "STRICT_IMPROVE_MARGIN", -999.0)
    monkeypatch.setattr(bt, "STRICT_OOS_IMPROVE_MARGIN", -999.0)
    monkeypatch.setattr(bt, "STRICT_FLOOR_MIN_ENTRIES", 0)
    monkeypatch.setattr(bt, "STRICT_FLOOR_MAX_DD", -1.0)  # impossible → always fails
    prices = _mr_friendly(seed=4)
    res = _gate("stop_loss_pct", 1.5, 1.2, strict=True, prices=prices)
    assert res["approved"] is False
    assert res["phases"]["phase1b_strict"]["floor_ok"] is False


# ── 1.5 provenance ──────────────────────────────────────────────────────────
def test_verdict_carries_provenance():
    prices = _mr_friendly(seed=5)
    res = _gate("stop_loss_pct", 1.5, 1.2, strict=True, prices=prices)
    for key in (
        "strict",
        "improvement_full",
        "improvement_oos",
        "data_bars",
        "version_from",
        "version_to",
        "old_pnl",
        "new_pnl",
        "new_entries",
        "new_max_dd",
    ):
        assert key in res, f"missing provenance key {key}"
    assert res["strict"] is True
    assert res["version_from"] == "03"
    # version_to is set only on approval; None when rejected.
    if res["approved"]:
        assert res["version_to"] is not None


def test_non_strict_default_unchanged():
    """Default (strict=False) verdict still approves the MR-friendly no-op."""
    prices = _mr_friendly(seed=6)
    res = _gate("stop_loss_pct", 1.5, 1.5, strict=False, prices=prices)
    assert res["approved"] is True
    assert res["strict"] is False
