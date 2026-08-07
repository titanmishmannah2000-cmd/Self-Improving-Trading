"""Layered sentient exits — core unit tests (L0–L2)."""

from __future__ import annotations

from hermes_core.engines.exit import (
    compute_hold_score,
    evaluate_exit,
    min_net_floor,
    net_unreal,
)
from hermes_core.engines.excursion import update_position_excursions
from hermes_core.engines.outcome_class import edge_weight, stamp_exit_class
from hermes_core.engines.structure import analyze_structure
from hermes_core.engines.counterfactual_exits import counterfactual_evs
from hermes_core.engines import hold_policy as hp


def _trade(**kw):
    t = {
        "entry_price": 100.0,
        "stop_loss_pct": 2.5,
        "profit_target_pct": 1.5,
        "time_exit_cycles": 480,
        "early_reeval_cycles": 120,
        "time_exit_max_cycles": 720,
        "held_cycles": 0,
        "exit_haircut_pct": 0.11,
        "min_bank_net_pct": 0.10,
        "peak_epsilon_pct": 0.05,
        "mfe_stall_bars": 1,
        "exit_bars_since_peak": 0,
        "exit_bars_held": 0,
        "honor_current_stop": True,
        "exit_tf_source": "live",
        "mfe_giveback_enabled": False,
        "trailing_atr_mult": None,
        "trailing_stop_pct": 0.0,
    }
    t.update(kw)
    return t


def test_net_floor_uses_exit_haircut_not_full_rt():
    t = _trade(fees_pct_rt=0.22, exit_haircut_pct=0.11, unrealised_pct=0.20)
    assert abs(net_unreal(t, 0.20) - 0.09) < 1e-9
    assert min_net_floor(t) == 0.11


def test_time_exit_underwater_at_soft_clock():
    t = _trade(held_cycles=480, unrealised_pct=-0.5, exit_haircut_pct=0.11)
    ex = evaluate_exit(t, 99.5, None)
    assert ex is not None and ex.reason == "time_exit"


def test_profit_bank_when_stalled_green_past_early():
    t = _trade(
        held_cycles=200,
        unrealised_pct=0.40,
        peak_mfe_pct=0.45,
        exit_bars_since_peak=2,
        exit_bars_held=2,
        mfe_bar_peaks=[0.4, 0.45, 0.45],
        exit_haircut_pct=0.11,
        current_stop=100.12,  # already fee-locked
    )
    ex = evaluate_exit(t, 100.4, None)
    assert ex is not None and ex.reason == "profit_bank"


def test_protect_before_bank_when_stop_unlocked():
    t = _trade(
        held_cycles=200,
        unrealised_pct=0.40,
        peak_mfe_pct=0.50,
        exit_bars_since_peak=0,
        exit_haircut_pct=0.11,
        current_stop=97.5,
        honor_current_stop=True,
    )
    ex = evaluate_exit(t, 100.4, None)
    assert ex is not None and ex.reason == "trailing" and ex.new_stop is not None


def test_hold_score_prefers_fresh_peak():
    t = _trade(peak_mfe_pct=1.0, profit_target_pct=1.5, exit_bars_since_peak=0, unrealised_pct=0.9)
    s1 = compute_hold_score(t, 0.9)
    t2 = dict(t)
    t2["exit_bars_since_peak"] = 5
    s2 = compute_hold_score(t2, 0.9)
    assert s1 > s2


def test_excursion_epsilon_and_stall_bars():
    pos: dict = {"peak_epsilon_pct": 0.05}
    update_position_excursions(pos, 0.10, tick=True, exit_bar_id="b1")
    assert pos["peak_mfe_pct"] == 0.10
    update_position_excursions(pos, 0.12, tick=True, exit_bar_id="b1")  # < epsilon
    assert pos["peak_mfe_pct"] == 0.10
    update_position_excursions(pos, 0.20, tick=True, exit_bar_id="b2")
    assert pos["peak_mfe_pct"] == 0.20
    assert pos.get("exit_bars_held", 0) >= 1


def test_outcome_class_soft_bank():
    assert stamp_exit_class("profit_bank") == "soft_capture"
    assert edge_weight({"exit_reason": "profit_bank"}) == 0.25
    assert edge_weight({"exit_reason": "profit_target"}) == 1.0


def test_structure_failed_auction():
    # flat then spike above then back
    prices = [100.0] * 25 + [101.0, 100.2]
    st = analyze_structure(prices, donchian_period=20)
    assert "failed_auction" in st


def test_counterfactual_and_hold_policy_fit():
    path = [
        {"unreal": 0.1, "peak": 0.1},
        {"unreal": 0.3, "peak": 0.3},
        {"unreal": 0.2, "peak": 0.3},
    ]
    ev = counterfactual_evs(path, cost_pct=0.1, min_bank_net=0.1)
    assert "best" in ev
    pol = hp.fit_from_labels(
        {"n": 0, "weights": [0.45, 0.35, 0.20]},
        [{"y_hold": 1.0}],
        [{"progress": 0.5, "fresh": 0.8, "capture": 0.7}],
    )
    assert abs(sum(pol["weights"]) - 1.0) < 1e-6


def test_btc_v05_strategy_knobs():
    from hermes_core.config import load_strategy_for_pair

    s = load_strategy_for_pair("BTC/USDT", bot="btc")
    # Seed version may bump (v05 layered knobs → v08+); require layered fields only.
    assert str(s.get("version") or "") >= "05"
    assert int(s.get("early_reeval_cycles") or 0) == 120
    assert float(s.get("soft_partial_tp_frac") or 0) == 0.4
    assert float(s.get("entry_conviction_take") or 0) >= 0.5
    assert float(s.get("pullback_stop_pct") or 0) >= 0.5
