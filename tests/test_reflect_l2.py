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
        "pair": "EUR/USD",
        "variable": "stop_loss_pct",
        "old": 1.5,
        "new": 1.2,
        "reason": "DD breach",
        "confidence": conf,
    }


def _yes(_prompt: str) -> str:
    return "YES, apply this change."


def _no(_prompt: str) -> str:
    return "NO, do not apply."


# ── corrected gate: 65 is the standard, 75 is unanimous ───────────────────
@pytest.mark.parametrize(
    "score,expected_decision",
    [
        (55, False),  # blueprint's old 55 gate is a REGRESSION -> rejected here
        (64, False),  # just below 65 -> L2 not invoked
        (65, False),  # exactly 65 needs 2/3; with no votes it fails
        (70, False),  # 65-74 needs 2/3; default fakes below
    ],
)
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
    assert res.decision is False  # 2/3 is NOT enough at >=75


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
    res = call_llm_consensus(_proposal(80, conf=0.30), score=80, confidence=0.30, callers=callers)
    assert res.votes_yes == 3
    assert res.decision is False  # confidence 0.30 < 0.40 -> blocked
    assert any("confidence" in r for r in res.reasons)


# ── httpx callers (no openai / google.generativeai SDKs) ─────────────────
class _FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._payload


def test_call_deepseek_httpx(monkeypatch):
    from hermes_core.engines import reflect as rf
    import httpx as hx

    calls = {}

    def _post(url, *, json=None, headers=None, timeout=None, params=None):
        calls["url"] = url
        calls["json"] = json
        calls["headers"] = headers
        return _FakeResp({"choices": [{"message": {"content": "YES"}}]})

    monkeypatch.setattr(hx, "post", _post)
    out = rf.call_deepseek("should we apply?", api_key="sk-test")
    assert out == "YES"
    assert "deepseek.com" in calls["url"]
    assert calls["json"]["model"] == rf.DEFAULT_DEEPSEEK_MODEL
    assert calls["headers"]["Authorization"] == "Bearer sk-test"


def test_default_l2_model_ids(monkeypatch):
    from hermes_core.engines import reflect as rf
    import httpx as hx

    assert rf.DEFAULT_DEEPSEEK_MODEL == "deepseek-v4-pro"
    assert rf.DEFAULT_GEMINI_MODEL == "gemini-2.5-flash"
    assert rf.DEFAULT_GROQ_MODEL == "llama-3.1-8b-instant"
    monkeypatch.delenv("DEEPSEEK_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    seen = {}

    def _post(url, *, json=None, headers=None, timeout=None, params=None):
        if "googleapis" in url:
            seen["gemini_url"] = url
            return _FakeResp({"candidates": [{"content": {"parts": [{"text": "YES"}]}}]})
        if "deepseek.com" in url:
            seen["deepseek_model"] = (json or {}).get("model")
            return _FakeResp({"choices": [{"message": {"content": "YES"}}]})
        seen["groq_model"] = (json or {}).get("model")
        return _FakeResp({"choices": [{"message": {"content": "YES"}}]})

    monkeypatch.setattr(hx, "post", _post)
    rf.call_deepseek("?", api_key="k")
    rf.call_gemini("?", api_key="k")
    rf.call_groq("?", api_key="k")
    assert seen["deepseek_model"] == "deepseek-v4-pro"
    assert "gemini-2.5-flash" in seen["gemini_url"]
    assert seen["groq_model"] == "llama-3.1-8b-instant"


def test_call_gemini_httpx(monkeypatch):
    from hermes_core.engines import reflect as rf
    import httpx as hx

    def _post(url, *, json=None, headers=None, timeout=None, params=None):
        assert "generativelanguage.googleapis.com" in url
        assert params and params.get("key") == "gem-key"
        return _FakeResp(
            {"candidates": [{"content": {"parts": [{"text": "NO"}]}}]}
        )

    monkeypatch.setattr(hx, "post", _post)
    assert rf.call_gemini("vote?", api_key="gem-key") == "NO"


def test_call_groq_httpx(monkeypatch):
    from hermes_core.engines import reflect as rf
    import httpx as hx

    def _post(url, *, json=None, headers=None, timeout=None, params=None):
        assert "api.groq.com" in url
        return _FakeResp(
            {"choices": [{"message": {"content": "APPROVE"}}]}
        )

    monkeypatch.setattr(hx, "post", _post)
    assert rf.call_groq("vote?", api_key="g-key") == "APPROVE"


def test_callers_missing_key_raises(monkeypatch):
    from hermes_core.engines import reflect as rf

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY missing"):
        rf.call_deepseek("x")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY missing"):
        rf.call_gemini("x")
    with pytest.raises(RuntimeError, match="GROQ_API_KEY missing"):
        rf.call_groq("x")


def test_l2_keys_read_at_call_time(monkeypatch):
    """Keys must resolve after env is set (not frozen empty at import)."""
    from hermes_core.engines import reflect as rf

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    st = rf.l2_keys_status()
    assert st == {"deepseek": False, "gemini": False, "groq": False}

    monkeypatch.setenv("GEMINI_API_KEY", " late-gemini ")
    monkeypatch.setenv("GROQ_API_KEY", "late-groq")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "late-deepseek")
    st2 = rf.l2_keys_status()
    assert st2 == {"deepseek": True, "gemini": True, "groq": True}
    # Strip whitespace so Railway/paste mistakes still work.
    assert rf._env("GEMINI_API_KEY") == "late-gemini"


def test_default_callers_no_sdk_import(monkeypatch):
    """Production path must not require openai / google.generativeai packages."""
    from hermes_core.engines import reflect as rf
    import httpx as hx
    import builtins

    real_import = builtins.__import__

    def _block_sdks(name, *a, **k):
        if name in ("openai", "google.generativeai") or name.startswith("google.generativeai"):
            raise ModuleNotFoundError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _block_sdks)

    def _post(url, *, json=None, headers=None, timeout=None, params=None):
        if "googleapis" in url:
            return _FakeResp({"candidates": [{"content": {"parts": [{"text": "YES"}]}}]})
        return _FakeResp({"choices": [{"message": {"content": "YES"}}]})

    monkeypatch.setattr(hx, "post", _post)
    assert rf.call_deepseek("?", api_key="k") == "YES"
    assert rf.call_gemini("?", api_key="k") == "YES"
    assert rf.call_groq("?", api_key="k") == "YES"


def test_consensus_uses_httpx_callers_end_to_end(monkeypatch):
    """With httpx fakes, real DEFAULT callers can produce a real APPLY.

    At score=70 (required 2/3), trust-ordered cascade skips the 3rd model when
    the top-2 already agree YES — so votes_yes is 2, not 3.
    """
    from hermes_core.engines import reflect as rf
    import httpx as hx

    def _post(url, *, json=None, headers=None, timeout=None, params=None):
        if "googleapis" in url:
            return _FakeResp({"candidates": [{"content": {"parts": [{"text": "YES"}]}}]})
        return _FakeResp({"choices": [{"message": {"content": "YES"}}]})

    monkeypatch.setattr(hx, "post", _post)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "a")
    monkeypatch.setenv("GEMINI_API_KEY", "b")
    monkeypatch.setenv("GROQ_API_KEY", "c")
    res = call_llm_consensus(_proposal(70), score=70)  # default _MODEL_CALLERS
    assert res.votes_yes == 2
    assert res.votes_total == 2
    assert res.decision is True
    assert any("cascade_skip:groq" in r for r in res.reasons)
    assert not any("ModuleNotFoundError" in r for r in res.reasons)


# ── adaptive prompt / context / cascade / parse ─────────────────────────────
def test_parse_vote_prefers_last_line_vote_contract():
    from hermes_core.engines import reflect as rf

    assert rf._parse_vote("Looks fine.\nVOTE: YES") is True
    assert rf._parse_vote("Risky oscillation.\nVOTE: NO") is False
    assert rf._parse_vote("YES, apply this change.") is True
    assert rf._parse_vote("APPROVE") is True
    assert rf._parse_vote("should this be applied?") is False
    assert rf._parse_vote("") is False
    assert rf._parse_vote("maybe") is False


def test_build_l2_prompt_full_vs_micro():
    from hermes_core.engines import reflect as rf

    prop = _proposal(70)
    ctx = "CHART: trend up | CORTEX: type_wr={'mr':0.4} | SKIPS: session_filter=3"
    full = rf.build_l2_prompt(prop, ctx, tier="full")
    micro = rf.build_l2_prompt(prop, ctx, tier="micro")
    assert "senior trading-risk reviewer" in full
    assert "EVIDENCE:" in full
    assert "VOTE: YES or VOTE: NO" in full
    assert "fast trading-risk sanity voter" in micro
    assert "FACTS:" in micro
    assert "EVIDENCE:" not in micro
    assert len(micro) < len(full)
    assert "VOTE: YES or VOTE: NO" in micro


def test_build_l2_context_omits_skips_for_trail_includes_for_rsi():
    from hermes_core.engines import reflect as rf

    chart = "x" * 250
    skips = "session_filter=9," + ("y" * 200)
    trail_ctx = rf._build_l2_context(
        "EUR/USD",
        "forex",
        chart_context=chart,
        skipped_json=skips,
        variable="trailing_stop_pct",
    )
    assert "SKIPS:" not in trail_ctx
    assert "CHART:" in trail_ctx
    # trail chart cap is 120
    chart_part = [p for p in trail_ctx.split(" | ") if p.startswith("CHART:")][0]
    assert len(chart_part) <= len("CHART: ") + 120

    entry_ctx = rf._build_l2_context(
        "EUR/USD",
        "forex",
        chart_context=chart,
        skipped_json=skips,
        variable="entry.threshold",
    )
    assert "SKIPS:" in entry_ctx
    skip_part = [p for p in entry_ctx.split(" | ") if p.startswith("SKIPS:")][0]
    assert len(skip_part) <= len("SKIPS: ") + 120


def test_cascade_skips_third_when_top_two_agree_yes():
    calls = {"deepseek": 0, "gemini": 0, "groq": 0}

    def _count(name):
        def _fn(_p):
            calls[name] += 1
            return "VOTE: YES"

        return _fn

    callers = {n: _count(n) for n in ("deepseek", "gemini", "groq")}
    res = call_llm_consensus(_proposal(70), score=70, callers=callers)
    assert res.decision is True
    assert res.votes_yes == 2
    assert calls["deepseek"] == 1
    assert calls["gemini"] == 1
    assert calls["groq"] == 0
    assert any("cascade_skip:groq" in r for r in res.reasons)


def test_cascade_calls_third_on_split_votes():
    calls = {"deepseek": 0, "gemini": 0, "groq": 0}

    def _yes_count(name):
        def _fn(_p):
            calls[name] += 1
            return "VOTE: YES"

        return _fn

    def _no_count(name):
        def _fn(_p):
            calls[name] += 1
            return "VOTE: NO"

        return _fn

    callers = {
        "deepseek": _yes_count("deepseek"),
        "gemini": _no_count("gemini"),
        "groq": _yes_count("groq"),
    }
    res = call_llm_consensus(_proposal(70), score=70, callers=callers)
    assert calls["deepseek"] == 1
    assert calls["gemini"] == 1
    assert calls["groq"] == 1
    assert res.votes_yes == 2
    assert res.decision is True
    assert not any(r.startswith("cascade_skip:") for r in res.reasons)


def test_cascade_skips_third_when_top_two_both_no():
    calls = {"deepseek": 0, "gemini": 0, "groq": 0}

    def _no_count(name):
        def _fn(_p):
            calls[name] += 1
            return "VOTE: NO"

        return _fn

    callers = {n: _no_count(n) for n in ("deepseek", "gemini", "groq")}
    res = call_llm_consensus(_proposal(70), score=70, callers=callers)
    assert res.decision is False
    assert res.votes_yes == 0
    assert calls["groq"] == 0
    assert any("cascade_skip:groq" in r for r in res.reasons)


def test_unanimous_still_calls_third():
    calls = {"deepseek": 0, "gemini": 0, "groq": 0}

    def _yes_count(name):
        def _fn(_p):
            calls[name] += 1
            return "VOTE: YES"

        return _fn

    callers = {n: _yes_count(n) for n in ("deepseek", "gemini", "groq")}
    res = call_llm_consensus(_proposal(80), score=80, callers=callers)
    assert res.required == 3
    assert calls["groq"] == 1
    assert res.votes_yes == 3
    assert res.decision is True


def test_micro_prompt_sent_to_groq_only():
    seen: dict[str, str] = {}

    def _deep(prompt: str):
        seen["deepseek"] = prompt
        return "VOTE: YES"

    def _gem(prompt: str):
        seen["gemini"] = prompt
        return "VOTE: NO"

    def _groq(prompt: str):
        seen["groq"] = prompt
        return "VOTE: YES"

    call_llm_consensus(
        _proposal(70),
        score=70,
        callers={"deepseek": _deep, "gemini": _gem, "groq": _groq},
    )
    assert "senior trading-risk reviewer" in seen["deepseek"]
    assert "fast trading-risk sanity voter" in seen["groq"]
    assert "FACTS:" in seen["groq"]
    assert "EVIDENCE:" in seen["deepseek"]


def test_l2_max_tokens_bumped():
    from hermes_core.engines import reflect as rf

    assert rf._L2_MAX_TOKENS == 96

