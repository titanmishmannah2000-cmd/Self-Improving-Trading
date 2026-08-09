"""Reflection upgrade: truth recovery, path replay, sleeve batch, layered axes."""

from __future__ import annotations

import json

import pytest

from hermes_core.engines.path_replay import replay_prove
from hermes_core.engines.reflect import (
    same_sleeve_batch,
    trade_pathology,
    _axis_candidates,
    apply_strategy_change,
    aggregate_trades,
)
from hermes_core.engines.reflect_verifier import (
    PROFITABILITY_TUNABLES,
    verify_reflection_candidate,
)
from hermes_core.engines.size_stamp import (
    infer_closed_size_fields,
    synthesize_mfe_path,
)
from hermes_core.engines.trade_truth import (
    enrich_closed_trade,
    join_probe_open_from_shadow,
)


def test_same_sleeve_batch_prefers_pullback():
    trades = [
        {"entry_type": "donchian_breakout", "pnl_pct": -0.2},
        {"entry_type": "pullback", "pnl_pct": 0.1},
        {"entry_type": "pullback", "pnl_pct": 0.2},
        {"entry_type": "pullback", "pnl_pct": -0.1},
        {"entry_type": "pullback", "pnl_pct": 0.05},
        {"entry_type": "donchian_breakout", "pnl_pct": -0.3},
    ]
    batch, sleeve = same_sleeve_batch(trades, 4)
    assert sleeve == "pullback"
    assert all(t["entry_type"] == "pullback" for t in batch)


def test_pathology_failed_breakout_and_probe_known():
    trades = [
        {
            "exit_reason": "failed_breakout",
            "pnl_pct": -0.3,
            "entry_type": "donchian_breakout",
            "entry_decision": "take",
            "decision_source": "stamped",
            "fees_pct": 0.22,
            "mae_pct": -0.5,
        },
        {
            "exit_reason": "failed_breakout",
            "pnl_pct": -0.2,
            "entry_type": "donchian_breakout",
            "entry_decision": "take",
            "decision_source": "stamped",
            "fees_pct": 0.22,
            "mae_pct": -0.4,
        },
        {
            "exit_reason": "profit_bank",
            "pnl_pct": 0.15,
            "soft_bank": True,
            "entry_type": "pullback",
            "entry_decision": "probe",
            "decision_source": "stamped",
            "fees_pct": 0.22,
            "mfe_capture": 0.6,
        },
        {
            "exit_reason": "profit_bank",
            "pnl_pct": 0.12,
            "soft_bank": True,
            "entry_type": "pullback",
            "size_mode": "probe",
            "decision_source": "unknown",
            "fees_pct": 0.22,
        },
        {
            "exit_reason": "stop_loss",
            "pnl_pct": -0.1,
            "entry_type": "donchian_breakout",
            "entry_decision": "take",
            "decision_source": "stamped",
            "fees_pct": 0.22,
        },
    ]
    p = trade_pathology(trades)
    assert p["failed_breakout_frac"] == pytest.approx(0.4)
    assert p["soft_bank_frac"] == pytest.approx(0.4)
    assert p["dominant_sleeve"] in {"donchian_breakout", "pullback"}
    assert p["probe_frac"] is not None
    assert p["unknown_decision_frac"] > 0


def test_infer_closed_size_never_invents_take():
    t = infer_closed_size_fields(
        {"size": 0.075, "entry_type": "pullback"},
        strategy={"position_size_r": 0.15},
        pair_max_size=0.15,
    )
    assert t["size_mode"] == "probe"
    assert t.get("entry_decision") in (None, "")
    assert t["decision_source"] == "unknown"
    assert t["size_stamp_inferred"] is True


def test_shadow_join_sets_probe_decision():
    trade = {
        "pair": "BTC/USDT",
        "entry_type": "pullback",
        "entry_mid": 65000.0,
        "entry_ts": 1_700_000_000.0,
    }
    shadow = [
        {
            "reason": "probe_open",
            "pair": "BTC/USDT",
            "entry_type": "pullback",
            "mark": 65010.0,
            "ts": 1_700_000_100.0,
        }
    ]
    out = join_probe_open_from_shadow(trade, shadow)
    assert out["entry_decision"] == "probe"
    assert out["decision_source"] == "shadow_join"


def test_synthesize_mfe_path_length():
    path = synthesize_mfe_path(
        {"hold_cycles": 12, "mae_pct": -0.3, "mfe_pct": 0.5, "pnl_pct": 0.1}
    )
    assert len(path) >= 3
    assert path[-1]["unreal"] == pytest.approx(0.1)


def test_pullback_batch_suppresses_stop_axis():
    trades = [
        {
            "pnl_pct": -2.0,
            "exit_reason": "stop_loss",
            "entry_type": "pullback",
            "mfe_capture": 0.3,
        }
        for _ in range(6)
    ]
    # Worst loss drawdown high
    agg = aggregate_trades(trades)
    path = trade_pathology(trades)
    strategy = {
        "stop_loss_pct": 1.5,
        "profit_target_pct": 2.0,
        "trailing_stop_pct": 0.3,
        "position_size_r": 0.15,
        "min_bank_net_pct": 0.1,
        "mfe_giveback_frac": 0.4,
        "failed_breakout_min_mae_pct": 0.4,
    }
    goal = {"max_drawdown": 0.5}
    cands = _axis_candidates(strategy, agg, path, goal)
    vars_ = [c[1] for c in cands]
    assert "stop_loss_pct" not in vars_


def test_donchian_fb_proposes_mae_floor():
    trades = [
        {
            "pnl_pct": -0.3,
            "exit_reason": "failed_breakout",
            "entry_type": "donchian_breakout",
            "mae_pct": -0.5,
            "fees_pct": 0.22,
        }
        for _ in range(6)
    ]
    agg = aggregate_trades(trades)
    path = trade_pathology(trades)
    strategy = {
        "stop_loss_pct": 2.5,
        "failed_breakout_min_mae_pct": 0.40,
        "profit_target_pct": 2.5,
        "trailing_stop_pct": 0.3,
        "position_size_r": 0.15,
        "mfe_giveback_frac": 0.4,
        "min_bank_net_pct": 0.1,
    }
    cands = _axis_candidates(strategy, agg, path, {"max_drawdown": 10.0})
    assert any(c[1] == "failed_breakout_min_mae_pct" for c in cands)


def test_verifier_book_n_and_position_size_allowlist():
    assert "position_size_r" in PROFITABILITY_TUNABLES
    assert "min_bank_net_pct" in PROFITABILITY_TUNABLES
    v = verify_reflection_candidate(
        pair="BTC/USDT",
        proposal={"variable": "min_bank_net_pct", "old": 0.1, "new": 0.12},
        verdict={"approved": True, "improvement_oos": 0.01},
        trades=[{"pnl_pct": 0.1}] * 8,
        book_n=15,
    )
    assert v["ok"] is True


def test_path_replay_prove_improves(tmp_path):
    trades = []
    for _ in range(6):
        # Path peaks then gives back — bank_first_green beats final
        path = [
            {"unreal": 0.05, "peak": 0.05},
            {"unreal": 0.20, "peak": 0.20},
            {"unreal": 0.35, "peak": 0.35},
            {"unreal": 0.10, "peak": 0.35},
        ]
        trades.append(
            {
                "pnl_pct": 0.05,
                "fees_pct": 0.1,
                "mfe_path": path,
                "profit_target_pct": 1.5,
            }
        )
    strategy = {"min_bank_net_pct": 0.05, "profit_target_pct": 1.5}
    prop = {"variable": "min_bank_net_pct", "old": 0.05, "new": 0.12}
    out = replay_prove(trades, strategy=strategy, proposal=prop, min_paths=5)
    assert out["approved"] is True
    assert out["method"] == "path_replay"


def test_frozen_key_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    strat = {
        "pair": "BTC/USDT",
        "strategy_type": "donchian_breakout",
        "version": "08",
        "stop_loss_pct": 2.5,
        "profit_target_pct": 2.5,
        "pullback_stop_pct": 1.5,
        "position_size_r": 0.15,
    }
    # Write seed so apply can load
    d = tmp_path / "btc" / "state" / "strategies"
    d.mkdir(parents=True)
    import yaml

    (d / "BTC_USDT.yaml").write_text(yaml.safe_dump(strat), encoding="utf-8")
    with pytest.raises(ValueError, match="frozen_reflect_key"):
        apply_strategy_change(
            "BTC/USDT",
            "pullback_stop_pct",
            1.2,
            bot="btc",
            strategy=strat,
        )


def test_enrich_closed_trade_pipeline():
    t = enrich_closed_trade(
        {
            "pair": "BTC/USDT",
            "entry_type": "pullback",
            "size": 0.075,
            "hold_cycles": 10,
            "mae_pct": -0.2,
            "mfe_pct": 0.1,
            "pnl_pct": -0.05,
        },
        strategy={"position_size_r": 0.15},
        pair_max_size=0.15,
        shadow_rows=[],
    )
    assert t["size_mode"] == "probe"
    assert len(t["mfe_path"]) >= 3
    assert t.get("mfe_path_synthetic") is True


def test_cortex_valid_entry_types_include_sleeves():
    from hermes_core.engines.decision_cortex import VALID_ENTRY_TYPES

    assert "donchian_breakout" in VALID_ENTRY_TYPES
    assert "pullback" in VALID_ENTRY_TYPES


def test_deploy_stage_defaults_to_prove(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("REFLECT_DEPLOY_STAGE", raising=False)
    from hermes_core.engines.experiment_control import get_deploy_stage

    assert get_deploy_stage("btc") == "prove"


def test_pathology_l2_brief_includes_sleeve():
    from hermes_core.engines.reflect import _pathology_l2_brief

    brief = _pathology_l2_brief(
        {
            "dominant_sleeve": "pullback",
            "failed_breakout_frac": 0.2,
            "avg_soft_bank_net": 0.12,
            "fee_cover_rate": 0.5,
            "probe_frac": 0.4,
        }
    )
    assert "sleeve=pullback" in brief
    assert "FB=" in brief
    assert "known_probe=" in brief


def test_pullback_entry_surrogate_fires():
    from hermes_core.engines.backtest import _entry_signal

    # Need ≥ min_lookback (40) bars; dip near support → pullback long.
    prices = [100.0 + (i % 3) * 0.05 for i in range(50)] + [98.8, 98.9, 99.0]
    sig = _entry_signal(
        prices,
        "pullback",
        30.0,
        strategy={
            "pullback_max_dist_pct": 3.0,
            "donchian_period": 20,
            "entry": {"session_filter": "24h"},
            "session_filter": "24h",
        },
        apply_ensemble=False,
    )
    assert any(x > 0 for x in sig)


def test_path_replay_rejects_worse_challenger():
    trades = []
    for _ in range(6):
        path = [
            {"unreal": 0.05, "peak": 0.05},
            {"unreal": 0.25, "peak": 0.25},
            {"unreal": 0.22, "peak": 0.25},
        ]
        trades.append({"pnl_pct": 0.22, "fees_pct": 0.1, "mfe_path": path})
    # Raising bank floor above path peaks should not beat realized hold
    out = replay_prove(
        trades,
        strategy={"min_bank_net_pct": 0.05, "profit_target_pct": 1.5},
        proposal={"variable": "position_size_r", "old": 0.15, "new": 0.05},
        min_paths=5,
    )
    assert out["approved"] is False


def test_path_replay_fb_mae_not_false_reject():
    """Trade-15 style batch: raising FB floor must not false-reject via fee/CF bias."""
    from hermes_core.engines.size_stamp import synthesize_mfe_path

    batch = [
        {
            "pnl_pct": -0.128494,
            "fees_pct": 0.22,
            "exit_reason": "stop_loss",
            "mae_pct": -0.1175,
            "mfe_pct": 0.0129,
            "hold_cycles": 8,
        },
        {
            "pnl_pct": -0.432625,
            "fees_pct": 0.22,
            "exit_reason": "failed_breakout",
            "mae_pct": -0.3406,
            "mfe_pct": 0.0,
            "hold_cycles": 30,
        },
        {
            "pnl_pct": -0.25354,
            "fees_pct": 0.22,
            "exit_reason": "failed_breakout",
            "mae_pct": -0.2015,
            "mfe_pct": 0.0,
            "hold_cycles": 30,
        },
        {
            "pnl_pct": -0.290181,
            "fees_pct": 0.22,
            "exit_reason": "failed_breakout",
            "mae_pct": -0.2326,
            "mfe_pct": 0.0,
            "hold_cycles": 30,
        },
        {
            "pnl_pct": -0.243862,
            "fees_pct": 0.22,
            "exit_reason": "failed_breakout",
            "mae_pct": -0.134,
            "mfe_pct": 0.0,
            "hold_cycles": 30,
        },
    ]
    for t in batch:
        t["mfe_path"] = synthesize_mfe_path(t)
        t["mfe_path_synthetic"] = True
    out = replay_prove(
        batch,
        strategy={
            "failed_breakout_min_mae_pct": 0.4,
            "failed_breakout_bars": 2,
            "min_bank_net_pct": 0.1,
            "profit_target_pct": 2.5,
        },
        proposal={"variable": "failed_breakout_min_mae_pct", "old": 0.4, "new": 0.45},
        min_paths=5,
    )
    assert out["approved"] is True
    assert out["reason"] in {"path_replay_ok", "path_replay_neutral_ok"}


def test_path_replay_fb_deeper_floor_can_improve():
    """Deeper MAE floor avoids early knife-cut when path recovers."""
    path = [
        {"unreal": -0.10, "peak": 0.0},
        {"unreal": -0.42, "peak": 0.0},  # hits 0.40 floor
        {"unreal": -0.20, "peak": 0.0},
        {"unreal": 0.15, "peak": 0.15},
    ]
    trades = [
        {
            "pnl_pct": 0.15,
            "fees_pct": 0.22,
            "mfe_path": path,
            "mfe_path_synthetic": False,
        }
        for _ in range(5)
    ]
    out = replay_prove(
        trades,
        strategy={"failed_breakout_min_mae_pct": 0.40, "failed_breakout_bars": 3},
        proposal={"variable": "failed_breakout_min_mae_pct", "old": 0.40, "new": 0.50},
        min_paths=5,
    )
    assert out["approved"] is True
    assert float(out.get("improvement") or 0) > 0
