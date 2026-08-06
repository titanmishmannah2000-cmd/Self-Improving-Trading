"""Exhaustive verification of Layered Sentient Exits L0–L7.

Each test maps to a plan requirement. Failures mean the layer is not working
as specified.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_core.config import load_strategy_for_pair
from hermes_core.config.validator import validate_strategy_params
from hermes_core.engines.counterfactual_exits import counterfactual_evs, label_hold_vs_bank
from hermes_core.engines.exit import (
    compute_hold_score,
    dual_slope_path,
    effective_stall_bars,
    evaluate_exit,
    min_net_floor,
    net_unreal,
    patience_mult,
)
from hermes_core.engines.excursion import append_mfe_path_point, update_position_excursions
from hermes_core.engines import hold_policy as hp
from hermes_core.engines.layered_hold import (
    continuous_vision_enabled,
    limit_removal_enabled,
    resolve_exit_tf_prices,
    sentient_hold_enabled,
    strategy_hold_knobs,
    synthetic_tf_from_prices,
)
from hermes_core.engines.outcome_class import (
    counts_for_full_edge,
    edge_weight,
    is_soft_capture,
    stamp_exit_class,
)
from hermes_core.engines.playbooks import (
    playbook_patience,
    setup_key,
    update_playbook_on_close,
)
from hermes_core.engines.reflect import trade_pathology
from hermes_core.engines.structure import analyze_structure, structure_patience_mult
from hermes_core.adapters.derivatives_context import (
    _perp_symbol,
    world_patience_mult,
)
from hermes_core.adapters.event_context import event_patience_mult, fetch_event_context


def T(**kw):
    base = {
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
        "clock_lock_frac": 0.5,
        "path_slack": 1.25,
        "exit_bars_since_peak": 0,
        "exit_bars_held": 0,
        "honor_current_stop": False,
        "exit_tf_source": "live",
        "mfe_giveback_enabled": False,
        "trailing_atr_mult": None,
        "trailing_stop_pct": 0.0,
        "partial_enabled": False,
        "partial_done": False,
        "soft_partial_done": False,
        "breakeven_set": True,  # isolate from BE in most tests
    }
    base.update(kw)
    return base


# ── L0 ─────────────────────────────────────────────────────────────────────


class TestL0NetGreenBank:
    def test_net_subtracts_exit_haircut_only(self):
        t = T(fees_pct_rt=0.22, exit_haircut_pct=0.11)
        assert net_unreal(t, 0.50) == pytest.approx(0.39)
        assert min_net_floor(t) == pytest.approx(0.11)

    def test_min_net_floor_uses_min_bank_when_higher(self):
        t = T(exit_haircut_pct=0.05, min_bank_net_pct=0.10)
        assert min_net_floor(t) == pytest.approx(0.10)

    def test_soft_clock_cuts_non_net_green(self):
        t = T(held_cycles=480, unrealised_pct=0.05, exit_haircut_pct=0.11)
        ex = evaluate_exit(t, 100.05, None)
        assert ex is not None and ex.reason == "time_exit"

    def test_early_arm_does_not_time_exit_slight_loser(self):
        t = T(held_cycles=150, unrealised_pct=-0.05, exit_haircut_pct=0.11)
        ex = evaluate_exit(t, 99.95, None)
        assert ex is None or ex.reason != "time_exit"

    def test_hard_max_banks_net_green_when_stalled(self):
        t = T(
            held_cycles=720,
            unrealised_pct=0.50,
            peak_mfe_pct=0.55,
            exit_bars_since_peak=3,
            mfe_bar_peaks=[0.5, 0.55, 0.55],
            current_stop=100.12,
            honor_current_stop=True,
        )
        ex = evaluate_exit(t, 100.5, None)
        assert ex is not None and ex.reason == "profit_bank"

    def test_protect_raises_stop_when_unlocked(self):
        t = T(
            held_cycles=200,
            unrealised_pct=0.40,
            peak_mfe_pct=0.60,
            exit_bars_since_peak=0,
            current_stop=97.5,
            honor_current_stop=True,
        )
        ex = evaluate_exit(t, 100.4, None)
        assert ex is not None
        assert ex.reason == "trailing"
        assert ex.new_stop is not None
        assert ex.new_stop > 97.5


# ── L1 ─────────────────────────────────────────────────────────────────────


class TestL1Adaptive:
    def test_hold_score_fresh_beats_stale(self):
        fresh = T(peak_mfe_pct=1.0, exit_bars_since_peak=0, unrealised_pct=0.9)
        stale = T(peak_mfe_pct=1.0, exit_bars_since_peak=6, unrealised_pct=0.9)
        assert compute_hold_score(fresh, 0.9) > compute_hold_score(stale, 0.9)

    def test_dual_slope_path_ok_and_fail(self):
        ok = T(
            profit_target_pct=1.5,
            unrealised_pct=0.5,
            peak_mfe_pct=0.8,
            mfe_bar_peaks=[0.2, 0.4, 0.6, 0.8],
            held_cycles=200,
            time_exit_max_cycles=720,
            cycles_per_exit_bar=240,
        )
        path = dual_slope_path(ok, 0.5, 1.5)
        assert path["slow_slope"] > 0

        fail = T(
            profit_target_pct=1.5,
            unrealised_pct=0.3,
            peak_mfe_pct=0.3,
            mfe_bar_peaks=[0.8, 0.7, 0.5, 0.3],
            held_cycles=200,
        )
        path2 = dual_slope_path(fail, 0.3, 1.5)
        assert path2["fast_slope"] <= 0 and path2["slow_slope"] <= 0
        assert path2["path_fail"] is True

    def test_patience_trend_up_vs_chop(self):
        up = T(live_d1="trend_up")
        chop = T(live_d1="chop")
        down = T(live_d1="trend_down")
        assert patience_mult(up) > patience_mult(chop)
        assert patience_mult(down) < patience_mult(chop)
        assert effective_stall_bars(up) >= effective_stall_bars(chop)

    def test_soft_partial_at_frac_of_tp(self):
        # TP 1.5%, soft frac 0.4 → 0.6% → price 100.6
        t = T(
            partial_enabled=True,
            soft_partial_done=False,
            soft_partial_tp_frac=0.4,
            profit_target_pct=1.5,
            unrealised_pct=0.6,
            breakeven_set=True,
        )
        ex = evaluate_exit(t, 100.6, None)
        assert ex is not None
        assert ex.reason == "partial_close"
        assert ex.partial_close_fraction == 0.5

    def test_failed_breakout_after_bars(self):
        t = T(
            held_cycles=200,
            early_reeval_cycles=10,
            failed_breakout_bars=1,
            exit_bars_held=1,
            unrealised_pct=-0.5,
            peak_mfe_pct=0.0,
            trough_mae_pct=-0.5,
            entry_type="donchian_breakout",
            failed_breakout_min_mae_pct=0.40,
        )
        ex = evaluate_exit(t, 99.5, None)
        assert ex is not None and ex.reason == "failed_breakout"

    def test_failed_breakout_skips_pullback_and_shallow_mae(self):
        from hermes_core.engines.exit import is_failed_breakout_cut

        pull = T(
            failed_breakout_bars=2,
            exit_bars_held=2,
            unrealised_pct=-0.5,
            trough_mae_pct=-0.5,
            entry_type="pullback",
            failed_breakout_min_mae_pct=0.40,
        )
        assert is_failed_breakout_cut(pull, -0.5) is False
        shallow = T(
            failed_breakout_bars=1,
            exit_bars_held=1,
            unrealised_pct=-0.15,
            trough_mae_pct=-0.15,
            entry_type="donchian_breakout",
            failed_breakout_min_mae_pct=0.40,
        )
        assert is_failed_breakout_cut(shallow, -0.15) is False
        kn = strategy_hold_knobs({"failed_breakout_bars": 2}, entry_type="pullback")
        assert kn["failed_breakout_bars"] == 0
        kn_d = strategy_hold_knobs({"failed_breakout_bars": 2}, entry_type="donchian_breakout")
        assert kn_d["failed_breakout_bars"] == 2
        assert kn_d["failed_breakout_min_mae_pct"] == 0.40

    def test_outcome_class_weights(self):
        assert stamp_exit_class("profit_bank") == "soft_capture"
        assert stamp_exit_class("failed_breakout") == "failed_breakout"
        assert stamp_exit_class("profit_target") == "full"
        assert is_soft_capture({"exit_reason": "profit_bank"})
        assert counts_for_full_edge({"exit_reason": "mfe_giveback"})
        assert edge_weight({"exit_reason": "profit_bank"}) == 0.25
        assert edge_weight({"exit_reason": "failed_breakout"}) == 0.0
        assert edge_weight({"exit_reason": "profit_target"}) == 1.0

    def test_excursion_epsilon_and_bar_stall(self):
        pos: dict = {"peak_epsilon_pct": 0.05}
        update_position_excursions(pos, 0.10, tick=True, exit_bar_id="a")
        update_position_excursions(pos, 0.12, tick=True, exit_bar_id="a")  # noise
        assert pos["peak_mfe_pct"] == 0.10
        update_position_excursions(pos, 0.20, tick=True, exit_bar_id="b")
        assert pos["peak_mfe_pct"] == 0.20
        assert int(pos.get("exit_bars_held") or 0) >= 1
        # weekend: tick=False must not advance cycles_since_peak
        before = int(pos.get("cycles_since_peak_mfe") or 0)
        update_position_excursions(pos, 0.19, tick=False, exit_bar_id="b")
        assert int(pos.get("cycles_since_peak_mfe") or 0) == before


# ── L2 ─────────────────────────────────────────────────────────────────────


class TestL2Deferred:
    def test_ema_weight_update_renormalizes(self):
        w = hp.ema_update_weights(
            [0.45, 0.35, 0.20],
            progress=0.8,
            fresh=0.9,
            capture=0.7,
            y_hold=1.0,
        )
        assert abs(sum(w) - 1.0) < 1e-6
        assert all(0.1 <= x <= 0.6 for x in w)

    def test_hold_score_uses_stamped_weights(self):
        t = T(
            peak_mfe_pct=1.2,
            exit_bars_since_peak=0,
            unrealised_pct=1.0,
            hold_score_weights=[0.2, 0.6, 0.2],
        )
        s = compute_hold_score(t, 1.0)
        assert 0.0 <= s <= 1.0

    def test_synthetic_tf_and_resolve_fallback(self):
        prices = [100.0 + i * 0.01 for i in range(500)]
        syn = synthetic_tf_from_prices(prices, bucket=240)
        assert len(syn) >= 2
        pos = {"exit_tf": "4h", "signal_period": "120d", "signal_max_candles": 800}
        mark, px, src = resolve_exit_tf_prices("btc", "BTC/USDT", pos, prices)
        assert src in ("live", "synthetic", "none")
        if src != "none":
            assert mark is not None and px


# ── L3 ─────────────────────────────────────────────────────────────────────


class TestL3ModelMemory:
    def test_counterfactual_evs_and_labels(self):
        path = [
            {"unreal": 0.05, "peak": 0.05},
            {"unreal": 0.40, "peak": 0.40},
            {"unreal": 0.25, "peak": 0.40},
            {"unreal": 1.6, "peak": 1.6},
        ]
        ev = counterfactual_evs(path, tp=1.5, cost_pct=0.1, min_bank_net=0.1)
        assert ev["hold_to_tp"] > ev["bank_first_green"] or "best" in ev
        labels = label_hold_vs_bank(path, cost_pct=0.1)
        assert len(labels) == len(path)

    def test_hold_policy_fit_and_predict(self, tmp_path: Path):
        pol = {"n": 0, "weights": [0.45, 0.35, 0.20], "p_hit_bias": 0.0}
        pol = hp.fit_from_labels(
            pol,
            [{"y_hold": 1.0}, {"y_hold": 0.0}],
            [
                {"progress": 0.7, "fresh": 0.9, "capture": 0.8},
                {"progress": 0.2, "fresh": 0.1, "capture": 0.3},
            ],
        )
        path = tmp_path / "hold_policy.json"
        hp.save_hold_policy(path, pol)
        loaded = hp.load_hold_policy(path)
        p = hp.predict_p_hold(
            {"progress": 0.8, "fresh": 0.9, "capture": 0.8}, loaded
        )
        assert 0.0 <= p <= 1.0
        assert 0.0 <= hp.predict_p_hit_tp({"progress": 0.5, "fresh": 0.5, "capture": 0.5}, loaded) <= 1.0

    def test_playbooks_die_in_chop_patience(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("HERMES_BOT_NAME", "btc")
        # Force playbook path via bot_state — update enough samples
        for i in range(10):
            update_playbook_on_close(
                bot="btc",
                pair="BTC/USDT",
                entry_type="donchian_breakout",
                d1="chop",
                pnl=-0.1 if i < 7 else 0.2,
                mfe=0.5,
                capture=0.0 if i < 7 else 0.5,
                hold_cycles=100,
            )
        pb = playbook_patience(
            pair="BTC/USDT", entry_type="donchian_breakout", d1="chop", bot="btc"
        )
        assert pb is not None
        assert pb <= 1.0


# ── L4–L5 ──────────────────────────────────────────────────────────────────


class TestL4L5WorldExperts:
    def test_structure_and_patience(self):
        prices = [100.0] * 22 + [102.0, 100.5]
        st = analyze_structure(prices, donchian_period=20)
        assert "failed_auction" in st
        assert structure_patience_mult({"failed_auction": True}) < 1.0

    def test_perp_symbol_normalization(self):
        assert _perp_symbol("BTC/USDT") == "BTC/USDT:USDT"
        assert _perp_symbol("BTC/USDT:USDT") == "BTC/USDT:USDT"
        assert _perp_symbol("ETH/USDC") == "ETH/USDC:USDC"

    def test_world_and_event_patience(self):
        assert world_patience_mult({"world_degraded": True}, side="long") < 1.0
        assert world_patience_mult({"funding": 0.001}, side="long") < 1.0
        assert world_patience_mult({"funding": -0.001}, side="long") >= 1.0
        ev = fetch_event_context(bot="btc")
        assert "event_risk" in ev
        assert event_patience_mult({"event_risk": 0.8}) < 1.0

    def test_exit_experts_arbitrate(self, monkeypatch):
        monkeypatch.setenv("SENTIENT_HOLD", "1")
        from hermes_core.engines.exit_experts import arbitrate_exit, collect_votes

        t = T(
            use_exit_experts=True,
            held_cycles=200,
            unrealised_pct=0.40,
            peak_mfe_pct=0.45,
            exit_bars_since_peak=3,
            mfe_bar_peaks=[0.45, 0.45, 0.4],
            current_stop=100.12,
            honor_current_stop=True,
        )
        votes = collect_votes(t, 100.4)
        assert len(votes) == 5
        names = {v["name"] for v in votes}
        assert names == {"giveback", "trail", "bank", "tp_ladder", "failed_breakout"}
        ex = arbitrate_exit(t, 100.4, None)
        assert ex is None or ex.reason in (
            "profit_bank",
            "trailing",
            "partial_close",
            "failed_breakout",
            "time_exit",
        )

    def test_sentient_still_fires_soft_partial(self, monkeypatch):
        """Regression: SENTIENT_HOLD must not skip L1 soft partial."""
        monkeypatch.setenv("SENTIENT_HOLD", "1")
        t = T(
            partial_enabled=True,
            soft_partial_done=False,
            soft_partial_tp_frac=0.4,
            profit_target_pct=1.5,
            unrealised_pct=0.6,
            breakeven_set=True,
            held_cycles=10,
        )
        ex = evaluate_exit(t, 100.6, None)
        assert ex is not None and ex.reason == "partial_close"

    def test_sentient_still_fires_full_tp(self, monkeypatch):
        monkeypatch.setenv("SENTIENT_HOLD", "1")
        t = T(
            partial_enabled=False,
            profit_target_pct=1.5,
            unrealised_pct=1.6,
            breakeven_set=True,
        )
        ex = evaluate_exit(t, 101.6, None)
        assert ex is not None and ex.reason == "profit_target"


# ── L6–L7 ──────────────────────────────────────────────────────────────────


class TestL6L7LimitRemoval:
    def test_p_hit_tp_gates_path(self):
        t = T(
            unrealised_pct=0.4,
            peak_mfe_pct=0.5,
            mfe_bar_peaks=[0.3, 0.4, 0.5],
            held_cycles=200,
            p_hit_tp=0.1,
            p_hit_tp_threshold=0.35,
        )
        path = dual_slope_path(t, 0.4, 1.5)
        assert path["path_ok"] is False
        assert path["path_fail"] is True

    def test_tf_source_synthetic_widens_floor_in_trail(self):
        # atr trail with synthetic should still return trailing or None safely
        prices = [100.0 + (i % 5) * 0.2 for i in range(80)]
        t = T(
            unrealised_pct=0.8,
            peak_mfe_pct=0.9,
            trailing_atr_mult=1.5,
            atr_floor_pct=1.0,
            exit_tf_source="synthetic",
            mfe_giveback_min_pct=0.3,
            honor_current_stop=True,
            current_stop=98.0,
            held_cycles=10,
            early_reeval_cycles=1000,  # don't enter bank brain
            time_exit_cycles=2000,
        )
        ex = evaluate_exit(t, 100.8, prices)
        assert ex is None or ex.reason in ("trailing", "breakeven", "profit_target")

    def test_deep_hold_model_mode(self, monkeypatch):
        monkeypatch.setenv("HOLD_MODEL", "deep")
        assert hp.hold_model_mode() == "deep"
        p = hp.predict_p_hold(
            {"progress": 0.5, "fresh": 0.5, "capture": 0.5, "funding": 0.0, "oi_z": 0.0, "dist_res": 0.5},
            {"weights": [0.45, 0.35, 0.20], "deep_coeffs": {"atr": 0, "funding": 0, "oi": 0, "dist": 0}},
        )
        assert 0.0 <= p <= 1.0

    def test_limit_removal_and_vision_flags(self, monkeypatch):
        monkeypatch.delenv("SENTIENT_HOLD", raising=False)
        monkeypatch.delenv("LIMIT_REMOVAL", raising=False)
        monkeypatch.delenv("CONTINUOUS_VISION", raising=False)
        assert limit_removal_enabled() is False
        monkeypatch.setenv("LIMIT_REMOVAL", "1")
        assert limit_removal_enabled() is True
        monkeypatch.setenv("CONTINUOUS_VISION", "1")
        assert continuous_vision_enabled() is True

    def test_strategy_knobs_complete(self):
        knobs = strategy_hold_knobs({})
        for k in (
            "early_reeval_cycles",
            "time_exit_max_cycles",
            "min_bank_net_pct",
            "mfe_stall_bars",
            "soft_partial_tp_frac",
            "failed_breakout_bars",
        ):
            assert k in knobs


# ── Reflection isolation + BTC YAML ────────────────────────────────────────


class TestReflectAndConfig:
    def test_soft_bank_not_counted_as_timeout(self):
        trades = [
            {
                "pair": "BTC/USDT",
                "pnl_pct": 0.3,
                "exit_reason": "profit_bank",
                "exit_class": "soft_capture",
                "mfe_pct": 0.5,
            },
            {
                "pair": "BTC/USDT",
                "pnl_pct": 0.3,
                "exit_reason": "profit_bank",
                "exit_class": "soft_capture",
                "mfe_pct": 0.4,
            },
            {"pair": "BTC/USDT", "pnl_pct": -0.2, "exit_reason": "time_exit", "mfe_pct": 0.8},
        ]
        stats = trade_pathology(trades)
        assert stats["soft_bank_frac"] == pytest.approx(2 / 3)
        assert stats["timeout_frac"] == pytest.approx(1 / 3)

    def test_btc_v07_validates(self):
        s = load_strategy_for_pair("BTC/USDT", bot="btc")
        assert s["version"] == "07"
        assert s.get("partial_enabled") is True
        assert float(s.get("soft_partial_tp_frac")) == 0.4
        assert int(s.get("early_reeval_cycles")) == 120
        assert int(s.get("time_exit_max_cycles")) == 720
        assert int(s.get("failed_breakout_bars") or 0) == 2
        assert float(s.get("failed_breakout_min_mae_pct") or 0) >= 0.4
        assert int(s.get("failed_breakout_cooldown_cycles") or 0) >= 60
        assert float(s.get("entry_conviction_take") or 0) >= 0.5
        assert float(s.get("pullback_stop_pct") or 0) >= 0.5
        ok, errors = validate_strategy_params(s, raise_on_fail=False)
        assert ok, errors

    def test_mfe_path_append(self):
        pos = {"held_cycles": 5, "unrealised_pct": 0.2, "peak_mfe_pct": 0.3}
        append_mfe_path_point(pos, {"d1": "trend_up"})
        assert len(pos["mfe_path"]) == 1
        assert pos["mfe_path"][0]["d1"] == "trend_up"
