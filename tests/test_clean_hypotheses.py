"""Pollution detector for hypotheses.jsonl (audit item 12)."""

from __future__ import annotations

from tools.clean_hypotheses import is_polluted


def test_fixture_max_dd_2_is_polluted():
    assert is_polluted({
        "reason": "tighten stop on drawdown breach; drawdown 3.00% > max_dd 2.00%",
        "status": "proposed",
    })


def test_rsi_period_seed_stub_is_polluted():
    assert is_polluted({
        "pair": "EUR/USD", "variable": "rsi_period", "old": "14", "new": "12",
        "reasoning": "improve WR", "mode": "shadow",
    })


def test_real_proposal_not_polluted():
    assert not is_polluted({
        "pair": "EUR/USD", "variable": "stop_loss_pct", "old": 1.5, "new": 1.2,
        "reason": "tighten stop on drawdown breach; drawdown 12.00% > max_dd 10.00%",
        "status": "proposed", "confidence": 0.4,
        "stats": {"count": 5, "drawdown": 12.0},
    })
