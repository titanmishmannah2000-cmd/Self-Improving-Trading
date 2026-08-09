"""Hold/entry improvements: probe soft-cut, fee floor, pullback clock, promote."""

from __future__ import annotations

from hermes_core.engines.exit import evaluate_exit, min_net_floor, effective_stall_bars
from hermes_core.engines.layered_hold import strategy_hold_knobs
from hermes_core.engines.sentient_entry import (
    _sleeve_promoted,
    meta_decision,
    sleeve_risk_overlays,
)
from hermes_core.engines.playbooks import update_playbook_on_close


def _trade(**kw):
    t = {
        "entry_price": 100.0,
        "stop_loss_pct": 1.5,
        "profit_target_pct": 2.0,
        "time_exit_cycles": 480,
        "early_reeval_cycles": 90,
        "time_exit_max_cycles": 720,
        "held_cycles": 0,
        "exit_haircut_pct": 0.11,
        "fees_pct_rt": 0.22,
        "min_bank_net_pct": 0.25,
        "min_bank_fee_mode": "round_trip",
        "peak_epsilon_pct": 0.05,
        "mfe_stall_bars": 1,
        "exit_bars_since_peak": 0,
        "exit_bars_held": 0,
        "honor_current_stop": True,
        "exit_tf_source": "live",
        "mfe_giveback_enabled": False,
        "trailing_atr_mult": None,
        "trailing_stop_pct": 0.0,
        "size_mode": "probe",
        "entry_type": "pullback",
        "probe_soft_cut_enabled": True,
        "probe_soft_cut_min_cycles": 120,
        "probe_soft_cut_mfe_pct": 0.40,
        "peak_mfe_pct": 0.0,
        "bank_score_ceiling": 0.45,
        "probe_ignore_patience_stall": True,
    }
    t.update(kw)
    return t


def test_probe_soft_cut_fires_on_dead_probe():
    t = _trade(held_cycles=150, peak_mfe_pct=0.1, size_mode="probe")
    ex = evaluate_exit(t, 99.9, None)
    assert ex is not None
    assert ex.reason == "probe_soft_cut"


def test_probe_soft_cut_skips_when_mfe_ok():
    t = _trade(held_cycles=150, peak_mfe_pct=0.55, size_mode="probe")
    ex = evaluate_exit(t, 100.2, None)
    assert ex is None or ex.reason != "probe_soft_cut"


def test_fee_aware_min_net_floor():
    t = _trade(min_bank_net_pct=0.10, fees_pct_rt=0.22, exit_haircut_pct=0.11)
    assert min_net_floor(t) >= 0.22


def test_pullback_time_exit_overlay():
    ov = sleeve_risk_overlays(
        {"pullback_stop_pct": 1.5, "pullback_tp_pct": 2.0, "pullback_time_exit_cycles": 240},
        "pullback",
    )
    assert ov["time_exit_cycles"] == 240
    assert ov["stop_loss_pct"] == 1.5


def test_strategy_hold_knobs_raise_bank_to_fees():
    kn = strategy_hold_knobs(
        {
            "min_bank_net_pct": 0.10,
            "min_bank_fee_mode": "round_trip",
            "probe_soft_cut_enabled": True,
            "bank_score_ceiling": 0.45,
        },
        entry_type="pullback",
        fees_rt=0.22,
    )
    assert kn["min_bank_net_pct"] >= 0.22
    assert kn["probe_soft_cut_enabled"] is True
    assert kn["bank_score_ceiling"] == 0.45


def test_sleeve_promote_fee_aware(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    st = update_playbook_on_close(
        bot="btc",
        pair="BTC/USDT",
        entry_type="pullback",
        d1="chop",
        pnl=0.05,
        mfe=0.2,
        capture=0.5,
        hold_cycles=100,
        fees_pct=0.22,
    )
    assert st["wins"] == 1
    assert st["fee_wins"] == 0
    assert st["fee_wr"] == 0.0
    assert not _sleeve_promoted(
        {"n": 8, "wr": 0.6, "fee_wr": 0.4},
        8,
        strategy={"sleeve_promote_fee_aware": True, "sleeve_promote_min_wr": 0.55},
    )
    assert _sleeve_promoted(
        {"n": 8, "wr": 0.4, "fee_wr": 0.6},
        8,
        strategy={"sleeve_promote_fee_aware": True, "sleeve_promote_min_wr": 0.55},
    )


def test_meta_force_probe_in_chop():
    dec = meta_decision(
        {
            "entry_type": "pullback",
            "conviction": 0.9,
            "force_probe": True,
            "playbook": {"n": 20, "wr": 0.7, "fee_wr": 0.7},
            "features": {},
        },
        {"sleeve_promote_n": 8, "entry_conviction_take": 0.55, "entry_conviction_probe": 0.4},
        {"n": 0},
    )
    assert dec == "probe"


def test_probe_stall_ignores_trend_patience():
    t = _trade(size_mode="probe", mfe_stall_bars=1, entry_regime="trend_up")
    assert effective_stall_bars(t) == 1
