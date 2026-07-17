"""Session 11 / Phase 11 tests for the L2 three-model consensus gate.

Network-free: model calls are injected as fakes. The corrected, tiered score
gate (65 standard / 75 unanimous) is asserted directly at all three boundary
values (55/65/75) so the behavior is explicit, not implicit.

Required blueprint names / scenarios kept:
  * score-gate parameterized at 55, 65, 75
  * 3-model cascade (DeepSeek -> Gemini -> Groq) fallback tested
  * sub-threshold vote count -> no change applied, fail-closed
  * EXIT GATE: score=77, 2/3-only -> REJECTED (the GBP/JPY trap the gate fixes)
"""

from __future__ import annotations

import pytest

from hermes_core.engines import call_llm_consensus


def _proposal(score: float, conf: float = 0.5) -> dict:
    return {
        "pair": "EUR/USD", "variable": "stop_loss_pct", "old": 1.5, "new": 1.2,
        "reason": "DD breach", "confidence": conf,
    }


def _yes(_prompt: str) -> str:
    return "YES, apply this change."


def _no(_prompt: str) -> str:
    return "NO, do not apply."


# ── corrected gate: 65 is the standard, 75 is unanimous ───────────────────
@pytest.mark.parametrize("score,expected_decision", [
    (55, False),   # blueprint's old 55 gate is a REGRESSION -> rejected here
    (64, False),   # just below 65 -> L2 not invoked
    (65, False),   # exactly 65 needs 2/3; with no votes it fails
    (70, False),   # 65-74 needs 2/3; default fakes below
])
def test_score_gate_boundaries(score, expected_decision):
    # no model callers -> zero votes -> fail-closed at every boundary
    res = call_llm_consensus(_proposal(score), score=score, callers={})
    assert res.decision is expected_decision
    assert res.votes_yes == 0


def test_score_65_needs_2_of_3():
    callers = {"deepseek": _yes, "gemini": _yes, "groq": _no}
    res = call_llm_consensus(_proposal(70), score=70, callers=callers)
    assert res.required == 2
    assert res.votes_yes == 2
    assert res.decision is True


def test_score_75_needs_unanimous_3_of_3():
    callers = {"deepseek": _yes, "gemini": _yes, "groq": _no}
    res = call_llm_consensus(_proposal(77), score=77, callers=callers)
    assert res.required == 3
    assert res.votes_yes == 2
    assert res.decision is False   # 2/3 is NOT enough at >=75


def test_exit_gate_score77_2of3_rejected():
    # the exact GBP/JPY case (original score 77) the corrected gate protects against
    callers = {"deepseek": _yes, "gemini": _yes, "groq": _no}
    res = call_llm_consensus(_proposal(77), score=77, callers=callers)
    assert res.score == 77
    assert res.votes_yes == 2
    assert res.decision is False
    assert "REJECT" in res.reasons[-1]


def test_score75_unanimous_passes():
    callers = {"deepseek": _yes, "gemini": _yes, "groq": _yes}
    res = call_llm_consensus(_proposal(80), score=80, callers=callers)
    assert res.required == 3
    assert res.votes_yes == 3
    assert res.decision is True


def test_below_65_l2_never_called():
    # even if all models would say YES, score<65 means they are not consulted
    callers = {"deepseek": _yes, "gemini": _yes, "groq": _yes}
    res = call_llm_consensus(_proposal(50), score=50, callers=callers)
    assert res.votes_total == 0
    assert res.votes_yes == 0
    assert res.decision is False
    assert "L2 not invoked" in res.reasons[0]


def test_cascade_fallback_deepseek_fails():
    # DeepSeek raises, Gemini + Groq answer -> 2/3 reached via fallback
    def _boom(_p):
        raise RuntimeError("deepseek down")

    callers = {"deepseek": _boom, "gemini": _yes, "groq": _yes}
    res = call_llm_consensus(_proposal(70), score=70, callers=callers)
    assert "deepseek:RuntimeError" in res.reasons[1]
    assert res.votes_yes == 2
    assert res.decision is True


def test_all_models_fail_fail_closed():
    def _boom(_p):
        raise RuntimeError("x")

    callers = {"deepseek": _boom, "gemini": _boom, "groq": _boom}
    res = call_llm_consensus(_proposal(70), score=70, callers=callers)
    assert res.votes_yes == 0
    assert res.decision is False


def test_confidence_below_040_blocks_apply():
    callers = {"deepseek": _yes, "gemini": _yes, "groq": _yes}
    res = call_llm_consensus(_proposal(80, conf=0.30), score=80,
                             confidence=0.30, callers=callers)
    assert res.votes_yes == 3
    assert res.decision is False   # confidence 0.30 < 0.40 -> blocked
    assert any("confidence" in r for r in res.reasons)
