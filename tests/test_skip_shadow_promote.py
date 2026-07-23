"""HIF skip-shadow promote — backtest-gated, never blind."""

from __future__ import annotations

import json

import pytest

from hermes_core.engines import skip_shadow_learn as ssl


def test_promote_flag_default_off(monkeypatch):
    monkeypatch.delenv("SKIP_SHADOW_PROMOTE", raising=False)
    assert ssl.skip_shadow_promote_enabled() is False


def test_maybe_promote_noop_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("SKIP_SHADOW_PROMOTE", "0")
    monkeypatch.setattr(ssl, "bot_state_dir", lambda _b: tmp_path)
    out = ssl.maybe_promote_skip_shadow("forex")
    assert out["enabled"] is False
    assert out["attempted"] == []


def test_promote_rejects_without_backtest_pass(monkeypatch, tmp_path):
    monkeypatch.setenv("SKIP_SHADOW_PROMOTE", "1")
    monkeypatch.setenv("REFLECT_AUTO_DEPLOY", "1")
    monkeypatch.setattr(ssl, "bot_state_dir", lambda _b: tmp_path)
    (tmp_path / "hypotheses.jsonl").write_text(
        json.dumps({
            "pair": "EUR/USD",
            "bot": "forex",
            "status": "skip_shadow_proposed",
            "variable": "profit_target_pct",
            "old": 1.0,
            "new": 1.5,
            "ts": 1.0,
            "deployable": False,
        }) + "\n",
        encoding="utf-8",
    )

    def fake_bt(*_a, **_k):
        return {"approved": False, "reason": "fail", "phases": {}}

    monkeypatch.setattr(
        "hermes_core.engines.backtest.backtest_with_history", fake_bt,
    )
    # Also patch via promote's import path by injecting backtest_fn
    rec = {
        "pair": "EUR/USD", "bot": "forex",
        "variable": "profit_target_pct", "old": 1.0, "new": 1.5, "ts": 1.0,
    }
    logged = []
    monkeypatch.setattr(
        "hermes_core.engines.reflect._log_hypothesis",
        lambda r: logged.append(r),
    )
    monkeypatch.setattr(
        "hermes_core.engines.reflect.apply_strategy_change",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not deploy")),
    )
    result = ssl.promote_skip_shadow_proposal(
        rec, bot="forex", strategy={"profit_target_pct": 1.0, "stop_loss_pct": 1.5},
        auto_deploy=True, backtest_fn=fake_bt,
    )
    assert result["deployed"] is False
    assert result["status"] == "backtest_reject"
    assert any(r.get("status") == "backtest_rejected" for r in logged)


def test_promote_pending_when_auto_deploy_off(monkeypatch):
    monkeypatch.setenv("REFLECT_AUTO_DEPLOY", "0")
    logged = []
    monkeypatch.setattr(
        "hermes_core.engines.reflect._log_hypothesis",
        lambda r: logged.append(r),
    )
    deployed = []
    monkeypatch.setattr(
        "hermes_core.engines.reflect.apply_strategy_change",
        lambda *a, **k: deployed.append(1) or {"version": "x"},
    )

    def fake_bt(*_a, **_k):
        return {
            "approved": True,
            "reason": "ok",
            "phases": {"phase6_deploy": {"version_bumped": "v2"}},
        }

    result = ssl.promote_skip_shadow_proposal(
        {
            "pair": "EUR/USD", "bot": "forex",
            "variable": "profit_target_pct", "old": 1.0, "new": 1.5,
        },
        strategy={"profit_target_pct": 1.0, "stop_loss_pct": 1.5},
        auto_deploy=False,
        backtest_fn=fake_bt,
    )
    assert result["status"] == "approved_pending_deploy"
    assert result["deployed"] is False
    assert not deployed


def test_backtest_profit_target_param_differs(monkeypatch):
    """profit_target_pct old/new must feed different sim targets."""
    from hermes_core.engines import backtest as bt

    calls = []

    def fake_sim(prices, strat_type, threshold, stop, target, **kw):
        calls.append({"stop": stop, "target": target})
        return {"pnl": 1.0 if target > 2 else 0.0, "wr": 0.5, "entries": 1, "max_dd": 0}

    monkeypatch.setattr(bt, "_simulate", fake_sim)
    monkeypatch.setattr(bt, "_kb_hit", lambda *a, **k: None)
    monkeypatch.setattr(bt, "_kb_record", lambda *a, **k: None)
    monkeypatch.setattr(bt, "phase0_corr", lambda *a, **k: 0.5)
    monkeypatch.setattr(bt, "_permutation_pvalue", lambda *a, **k: (0.01, 0.2, 0.0))
    monkeypatch.setattr(bt, "_crisis_backtest", lambda *a, **k: {"approved": True})
    monkeypatch.setattr(bt, "_classify_regime", lambda *a, **k: "range")
    monkeypatch.setattr(bt, "_bump_version", lambda *a, **k: "01")
    monkeypatch.setattr(bt, "_strategy_signal", lambda *a, **k: [0.0] * 20)

    prices = [1.0 + i * 0.001 for i in range(40)]
    strategy = {"strategy_type": "mean_reversion", "stop_loss_pct": 1.5,
                "profit_target_pct": 1.0, "rsi_threshold": 30}
    bt.backtest_with_history(
        "EUR/USD", "profit_target_pct", 1.0, 3.0,
        strategy=strategy, prices=prices, bot="forex",
    )
    targets = {c["target"] for c in calls}
    assert 1.0 in targets and 3.0 in targets
