"""Session 9 / Phase 9 tests for the L1 reflection engine.

Network-free: combined_reflect is given an injected goal/strategy and the
hypotheses log is redirected to a temp file so the blueprint's "hypothesis
logged on 5 trades" assertion runs without touching real state.

Required blueprint names kept verbatim:
  test_l1_logs_hypothesis, test_l1_tightens_on_dd, test_l1_widens_on_low_wr,
  test_one_variable_only, test_floor_enforced.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import hermes_core.engines.reflect as rf
from hermes_core.engines import combined_reflect, layer1_rule_based

GOAL = {"max_drawdown": 10.0}
STRAT = {"stop_loss_pct": 1.5, "profit_target_pct": 3.0}
STRAT_FLOOR = {"stop_loss_pct": 0.5, "profit_target_pct": 3.0}


def _trades(pnls, base=1.1000):
    out = []
    for i, p in enumerate(pnls):
        out.append(
            {
                "pair": "EUR/USD",
                "cycle": i,
                "reason": "tp" if p > 0 else "sl",
                "entry_price": base,
                "exit_price": base * (1 + p / 100.0),
                "pnl_pct": p,
            }
        )
    return out


@pytest.fixture(autouse=True)
def _tmp_hypotheses(tmp_path, monkeypatch):
    log = tmp_path / "forex" / "state" / "hypotheses.jsonl"
    monkeypatch.setattr(
        rf,
        "hypotheses_path",
        lambda bot=None: log,
    )
    yield log


def _read_hypotheses(path: Path) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines if ln.strip()]


def test_l1_logs_hypothesis(_tmp_hypotheses):
    # blueprint: feed 5 trades that breach a rule -> a hypothesis is logged
    losing = _trades([-12.0, -13.0, -11.0, -14.0, -12.0])  # DD breach
    changes = combined_reflect("EUR/USD", losing, goal=GOAL, strategy=STRAT)
    assert len(changes) == 1
    hyps = _read_hypotheses(_tmp_hypotheses)
    assert len([h for h in hyps if h["pair"] == "EUR/USD"]) >= 1


def test_l1_tightens_on_dd():
    # blueprint: DD > max_dd -> stop_loss_pct tightened by 0.3
    losing = _trades([-12.0, -13.0, -11.0, -14.0, -12.0])  # worst_loss ~ -14% > 10%
    out = layer1_rule_based("EUR/USD", losing, GOAL, STRAT)
    assert out is not None
    assert out[0] == "stop_loss_pct"
    assert float(out[2]) == float(out[1]) - 0.3


def test_l1_widens_on_low_wr():
    # blueprint: WR < 0.3 -> widen (new > old)
    bad_wr = _trades([-1.0, -2.0, -1.5, 0.5, -1.0])  # 1 win / 5 = 0.2
    out = layer1_rule_based("EUR/USD", bad_wr, GOAL, STRAT)
    assert out is not None
    assert out[2] > out[1]  # widen


def test_one_variable_only():
    # blueprint: combined_reflect changes exactly one variable per call
    losing = _trades([-12.0, -13.0, -11.0, -14.0, -12.0])
    changes = combined_reflect("EUR/USD", losing, goal=GOAL, strategy=STRAT)
    assert len(changes) == 1
    for ch in changes:
        assert ch["variable"] == "stop_loss_pct"  # exactly one variable


def test_floor_enforced():
    # blueprint: stop at floor 0.5 with DD breach must not go below 0.5
    losing = _trades([-12.0, -13.0, -11.0, -14.0, -12.0])
    out = layer1_rule_based("EUR/USD", losing, GOAL, STRAT_FLOOR)
    assert out is not None
    assert float(out[2]) >= 0.5


def test_no_change_on_quiet_batch():
    # discipline: no rule fired -> no proposal, no log spam
    quiet = _trades([0.3, -0.2, 0.4, -0.1, 0.2])  # small moves, WR 0.6, no DD
    out = layer1_rule_based("EUR/USD", quiet, GOAL, STRAT)
    assert out is None
    changes = combined_reflect("EUR/USD", quiet, goal=GOAL, strategy=STRAT)
    assert changes == []


def test_shadow_only_no_mutation():
    # discipline S9: combined_reflect must NOT modify the passed strategy dict
    losing = _trades([-12.0, -13.0, -11.0, -14.0, -12.0])
    before = dict(STRAT)
    combined_reflect("EUR/USD", losing, goal=GOAL, strategy=STRAT)
    assert before == STRAT  # unchanged
