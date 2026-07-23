"""HIF Phase-4 skip + GP-shadow learning (shadow hypotheses only)."""

from __future__ import annotations

import json

import hermes_core.engines.skip_shadow_learn as ssl
from hermes_core.engines.skip_shadow_learn import (
    analyze_skip_shadow,
    format_skip_shadow_context,
    maybe_skip_shadow_learn,
    propose_skip_shadow_notes,
)


def test_analyze_dominant_reason():
    skips = [{"pair": "EUR/USD", "reason": "no_signal"} for _ in range(18)]
    skips += [{"pair": "EUR/USD", "reason": "rr_guard"} for _ in range(2)]
    shadows = [
        {"pair": "EUR/USD", "consensus": "bullish", "signal": "gp", "gp_strength": 0.4}
        for _ in range(6)
    ]
    a = analyze_skip_shadow(skips, shadows)
    assert a["skip_count"] == 20
    assert a["top_reason"] == "no_signal"
    assert a["top_share"] >= 0.55
    assert a["shadow_signals"] == 6
    ctx = format_skip_shadow_context(a)
    assert "no_signal" in ctx


def test_propose_rr_guard_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(ssl, "bot_state_dir", lambda bot: tmp_path)
    analysis = {
        "skip_count": 30,
        "top_reason": "rr_guard",
        "top_share": 0.7,
        "shadow_count": 0,
        "shadow_signals": 0,
        "shadow_consensus": {},
        "avg_gp_strength": None,
        "top_reasons": [{"reason": "rr_guard", "n": 21}],
    }
    notes = propose_skip_shadow_notes(
        "EUR/USD", "forex", analysis,
        strategy={"stop_loss_pct": 1.5, "profit_target_pct": 1.0},
    )
    assert any(n.get("variable") == "profit_target_pct" for n in notes)
    prop = next(n for n in notes if n.get("variable") == "profit_target_pct")
    assert prop["new"] == 1.5
    assert prop["deployable"] is False
    assert prop["status"] == "skip_shadow_proposed"


def test_maybe_disabled_no_fire(tmp_path, monkeypatch):
    monkeypatch.setattr(ssl, "bot_state_dir", lambda bot: tmp_path)
    monkeypatch.setenv("SKIP_SHADOW_REFLECT", "0")
    out = maybe_skip_shadow_learn("forex", ["EUR/USD"])
    assert out["enabled"] is False
    assert out["fired"] == []


def test_maybe_fires_and_latches(tmp_path, monkeypatch):
    monkeypatch.setattr(ssl, "bot_state_dir", lambda bot: tmp_path)
    monkeypatch.setenv("SKIP_SHADOW_REFLECT", "1")
    skips_path = tmp_path / "skips.jsonl"
    rows = [
        {"ts": i, "pair": "EUR/USD", "cycle": i, "reason": "no_signal",
         "reason_skipped": "no_signal"}
        for i in range(55)
    ]
    skips_path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8",
    )
    (tmp_path / "gp_shadow.jsonl").write_text("", encoding="utf-8")

    logged: list[dict] = []
    monkeypatch.setattr(
        "hermes_core.engines.reflect._log_hypothesis",
        lambda rec: logged.append(rec),
    )

    first = maybe_skip_shadow_learn("forex", ["EUR/USD"], strategies={})
    assert first["enabled"] is True
    assert any(f["pair"] == "EUR/USD" for f in first["fired"])
    assert logged  # notes written

    logged.clear()
    second = maybe_skip_shadow_learn("forex", ["EUR/USD"], strategies={})
    # Same bucket → latched, no re-fire
    assert second["fired"] == []
    assert logged == []


def test_combined_reflect_attaches_skip_ctx(tmp_path, monkeypatch):
    import hermes_core.engines.reflect as rf

    monkeypatch.setattr(rf, "hypotheses_path", lambda bot=None: tmp_path / "h.jsonl")
    losing = [
        {"pnl_pct": -1.0, "exit_price": 1.0, "entry_price": 1.01} for _ in range(6)
    ]
    goal = {"max_drawdown": 0.5}
    strat = {"stop_loss_pct": 1.5, "profit_target_pct": 3.0}
    recs = rf.combined_reflect(
        "EUR/USD", losing, goal=goal, strategy=strat, bot="forex",
        skipped_json="skips=40; top=no_signal(80%)",
    )
    assert recs
    assert "skip_ctx" in recs[0]["reason"]
    assert recs[0].get("skip_context")
