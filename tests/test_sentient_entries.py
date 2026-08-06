"""Layered sentient entries — soft map, D1 gates, conviction, arbiter."""

from __future__ import annotations

import pytest

from hermes_core.engines import btc_regime as br
from hermes_core.engines.entry import evaluate_entry_detailed
from hermes_core.engines import sentient_entry as se


def _donchian_strategy(**extra):
    s = {
        "strategy_type": "donchian_breakout",
        "regime_split": False,
        "position_size_r": 0.15,
        "stop_loss_pct": 2.5,
        "profit_target_pct": 1.5,
        "entry": {
            "donchian_period": 20,
            "session_filter": "24h",
            "breakout_buffer_pct": 0.4,
            "require_clean_chart": True,
            "breakout_confirm_bars": 1,
        },
        "vol_min_pct": 0.0,
        "vol_max_pct": 100.0,
        "vol_threshold_pct": 0.0,
        "adx_threshold": 0,
        "entry_conviction_take": 0.55,
        "entry_conviction_probe": 0.40,
        "entry_policy_min_n": 20,
        "pullback_max_dist_pct": 2.0,
        "sleeve_promote_n": 8,
        "max_alt_entries_per_day": 2,
        "pullback_stop_pct": 1.5,
        "pullback_tp_pct": 2.0,
        "idle_sleeve_cycles": 2,
    }
    s.update(extra)
    return s


def test_split_soft_reasons():
    hard, act = se.split_soft_reasons(["avoid", "wait_for_pullback", "downtrend"])
    assert hard == ["avoid", "downtrend"]
    assert act == ["wait_for_pullback"]


def test_parse_support_level():
    assert se.parse_support_level("SR: support at 62500, resistance at 64000") == 62500.0
    assert se.parse_support_level("") is None


def test_wait_for_pullback_does_not_veto_donchian(monkeypatch):
    monkeypatch.setattr(
        br,
        "classify_btc_regime",
        lambda pair, force=False: {
            "label": br.TREND_UP,
            "reason": "test",
            "pair": pair,
            "adx": 30.0,
        },
    )
    prices = [100.0] * 21 + [101.0]
    monkeypatch.setattr(
        "hermes_core.engines.entry.gp_invent_prices",
        lambda *a, **k: prices,
    )
    ctx = "trend: uptrend (conf=0.75). SR: support at 99, resistance at 105. Rec: wait for pullback"
    sig, reason = evaluate_entry_detailed(
        prices,
        _donchian_strategy(),
        pair="BTC/USDT",
        bot="btc",
        session_token="OTHER",
        context=ctx,
    )
    assert reason == ""
    assert sig is not None
    assert sig.meta["entry_type"] == "donchian_breakout"


def test_avoid_still_vetoes_clean_chart(monkeypatch):
    monkeypatch.setattr(
        br,
        "classify_btc_regime",
        lambda pair, force=False: {
            "label": br.TREND_UP,
            "reason": "test",
            "pair": pair,
            "adx": 30.0,
        },
    )
    prices = [100.0] * 21 + [101.0]
    monkeypatch.setattr(
        "hermes_core.engines.entry.gp_invent_prices",
        lambda *a, **k: prices,
    )
    sig, reason = evaluate_entry_detailed(
        prices,
        _donchian_strategy(),
        pair="BTC/USDT",
        bot="btc",
        session_token="OTHER",
        context="trend: sideways. Rec: avoid entirely",
    )
    assert sig is None
    assert reason.startswith("donchian:chart_soft:")
    assert "avoid" in reason


def test_hard_blocks_pullback_in_chop():
    assert br.hard_blocks_entry(br.CHOP, strategy_type="pullback") is True
    assert br.hard_blocks_entry(br.CHOP, strategy_type="mean_reversion") is True
    # v07: Donchian also blocked in chop (0% WR fee grind).
    assert br.hard_blocks_entry(br.CHOP, strategy_type="donchian_breakout") is True
    assert br.hard_blocks_entry(br.TREND_UP, strategy_type="pullback") is False
    assert br.hard_blocks_entry(br.TREND_UP, strategy_type="donchian_breakout") is False


def test_conviction_and_cold_policy_fail_open():
    conv = se.compute_entry_conviction(
        quality=0.6,
        world_mult=1.0,
        structure_mult=1.0,
        playbook_mult=1.0,
        cost_edge=1.0,
    )
    assert 0.4 <= conv <= 1.0
    pol = {"n": 0, "weights": [0.4, 0.35, 0.25]}
    assert se.predict_policy_mult({"conviction_raw": 0.9}, pol, min_n=20) == 1.0


def test_arbitrate_one_winner_prefers_donchian():
    strategy = _donchian_strategy()
    policy = {"n": 0}
    cands = [
        {
            "entry_type": "mean_reversion",
            "conviction": 0.7,
            "playbook": {},
            "features": {"conviction_raw": 0.7, "playbook_wr": 0.5, "world_mult": 1.0},
            "size_mult": 0.5,
        },
        {
            "entry_type": "donchian_breakout",
            "conviction": 0.7,
            "playbook": {},
            "features": {"conviction_raw": 0.7, "playbook_wr": 0.5, "world_mult": 1.0},
            "size_mult": 1.0,
            "signal": object(),
        },
    ]
    # Fake signal with meta
    class _S:
        meta = {}
        quality = 0.7
        size = 0.15

    cands[1]["signal"] = _S()
    w = se.arbitrate_entry(cands, strategy, policy)
    assert w is not None
    assert w["entry_type"] == "donchian_breakout"


def test_sleeve_risk_overlays_position_only():
    s = _donchian_strategy(pullback_stop_pct=1.5, pullback_tp_pct=1.2)
    ov = se.sleeve_risk_overlays(s, "pullback")
    assert ov["stop_loss_pct"] == 1.5
    assert ov["profit_target_pct"] == 1.2
    assert se.sleeve_risk_overlays(s, "donchian_breakout") == {}


def test_near_support_and_empty_chart_no_pullback(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTIENT_ENTRY", "1")
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        br,
        "classify_btc_regime",
        lambda pair, force=False: {
            "label": br.TREND_UP,
            "reason": "test",
            "pair": pair,
            "adx": 30.0,
        },
    )
    prices = [100.0] * 30
    # Empty chart → no pullback invent
    out = se.run_sentient_entry(
        bot="btc",
        pair="BTC/USDT",
        prices=prices,
        strategy=_donchian_strategy(),
        context="",
        trad_sig=None,
        trad_skip="donchian:no_breakout",
        current_cycle=10,
    )
    types = [c["entry_type"] for c in out.get("candidates") or []]
    assert "pullback" not in types


def test_chop_blocks_mr_but_allows_chart_pullback(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTIENT_ENTRY", "1")
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        br,
        "classify_btc_regime",
        lambda pair, force=False: {
            "label": br.CHOP,
            "reason": "test",
            "pair": pair,
            "adx": 12.0,
        },
    )
    monkeypatch.setattr(
        se,
        "build_context_bundle",
        lambda **kw: {
            "world": {},
            "world_mult": 1.0,
            "structure": {},
            "structure_mult": 1.0,
            "chart_missing": False,
            "support": 99.0,
            "event_hard_pause": False,
            "cost_rt": 0.22,
            "cost_stressed": 0.44,
            "donchian_mid": 100.0,
        },
    )
    prices = [100.0] * 25 + [99.2]
    ctx = "trend: uptrend. SR: support at 99. Rec: wait for pullback"
    out = se.run_sentient_entry(
        bot="btc",
        pair="BTC/USDT",
        prices=prices,
        strategy=_donchian_strategy(),
        context=ctx,
        trad_sig=None,
        trad_skip="donchian:no_breakout",
        current_cycle=100,
    )
    types = [c["entry_type"] for c in out.get("candidates") or []]
    assert "mean_reversion" not in types
    assert "pullback" in types or (
        out.get("signal") is not None
        and out["signal"].meta.get("entry_type") == "pullback"
    )


def test_chop_without_pullback_soft_stays_flat(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTIENT_ENTRY", "1")
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        br,
        "classify_btc_regime",
        lambda pair, force=False: {
            "label": br.CHOP,
            "reason": "test",
            "pair": pair,
            "adx": 12.0,
        },
    )
    prices = [100.0] * 25 + [99.2]
    out = se.run_sentient_entry(
        bot="btc",
        pair="BTC/USDT",
        prices=prices,
        strategy=_donchian_strategy(),
        context="trend: sideways. Rec: avoid entirely",
        trad_sig=None,
        trad_skip="donchian:no_breakout",
        current_cycle=100,
    )
    types = [c["entry_type"] for c in out.get("candidates") or []]
    assert "pullback" not in types
    assert "mean_reversion" not in types


def test_donchian_adx_soft_not_hard_skip(monkeypatch):
    """Weak ADX haircuts quality but no longer returns donchian:adx_weak."""
    monkeypatch.setattr(
        br,
        "classify_btc_regime",
        lambda pair, force=False: {
            "label": br.CHOP,
            "reason": "test",
            "pair": pair,
            "adx": 8.0,
        },
    )
    prices = [100.0] * 21 + [101.0]
    monkeypatch.setattr(
        "hermes_core.engines.entry.gp_invent_prices",
        lambda *a, **k: prices,
    )
    # Force low ADX on signal TF
    monkeypatch.setattr(
        "hermes_core.engines.entry.compute_all",
        lambda xs: {
            "rsi": 55.0,
            "adx": 10.0,
            "atr": 1.0,
            "bb": {"lower": 95.0, "mid": 100.0, "upper": 105.0},
            "regime": "range",
        },
    )
    strat = _donchian_strategy()
    strat["entry"]["require_clean_chart"] = False
    sig, reason = evaluate_entry_detailed(
        prices,
        strat,
        pair="BTC/USDT",
        bot="btc",
        session_token="OTHER",
        context="trend: uptrend. Rec: enter long",
    )
    assert reason != "donchian:adx_weak"
    # May still be no_breakout/confirm/vol — but ADX must not hard-block.
    if sig is not None:
        assert sig.meta.get("adx_soft") is True
        assert sig.meta.get("entry_type") == "donchian_breakout"


def test_confirm_bars_pending(monkeypatch):
    monkeypatch.setattr(
        br,
        "classify_btc_regime",
        lambda pair, force=False: {
            "label": br.TREND_UP,
            "reason": "test",
            "pair": pair,
            "adx": 30.0,
        },
    )
    # Last bar breaks out; prior bar still inside → confirm_bars=2 pending
    prices = [100.0] * 20 + [100.2, 101.0]
    monkeypatch.setattr(
        "hermes_core.engines.entry.gp_invent_prices",
        lambda *a, **k: prices,
    )
    strat = _donchian_strategy()
    strat["entry"]["breakout_confirm_bars"] = 2
    strat["entry"]["require_clean_chart"] = False
    sig, reason = evaluate_entry_detailed(
        prices,
        strat,
        pair="BTC/USDT",
        bot="btc",
        session_token="OTHER",
        context="trend: uptrend. Rec: enter long",
    )
    assert sig is None
    assert reason == "donchian:confirm_pending"


def test_pullback_opens_probe_in_trend_up(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTIENT_ENTRY", "1")
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        br,
        "classify_btc_regime",
        lambda pair, force=False: {
            "label": br.TREND_UP,
            "reason": "test",
            "pair": pair,
            "adx": 30.0,
        },
    )
    monkeypatch.setattr(
        se,
        "build_context_bundle",
        lambda **kw: {
            "world": {},
            "world_mult": 1.0,
            "structure": {},
            "structure_mult": 1.0,
            "chart_missing": False,
            "support": 99.0,
            "event_hard_pause": False,
            "cost_rt": 0.22,
            "cost_stressed": 0.44,
            "donchian_mid": 100.0,
        },
    )
    prices = [100.0] * 25 + [99.2]
    ctx = "trend: uptrend. SR: support at 99. Rec: wait for pullback"
    out = se.run_sentient_entry(
        bot="btc",
        pair="BTC/USDT",
        prices=prices,
        strategy=_donchian_strategy(),
        context=ctx,
        trad_sig=None,
        trad_skip="donchian:no_breakout",
        current_cycle=50,
    )
    assert out.get("signal") is not None
    assert out["signal"].meta.get("entry_type") == "pullback"
    assert out.get("decision") == "probe"
    assert out["signal"].meta.get("sleeve_risk", {}).get("stop_loss_pct") == 1.5
    assert out["signal"].meta.get("sleeve_risk", {}).get("profit_target_pct") == 2.0


def test_near_support_rejects_resistance_chase():
    # Above / at resistance → chase
    assert (
        se.near_support(
            63940.0,
            support=63000.0,
            donchian_mid=63450.0,
            max_dist_pct=2.0,
            resistance=63900.0,
        )
        is False
    )
    # Near support → ok
    assert (
        se.near_support(
            63100.0,
            support=63000.0,
            donchian_mid=63450.0,
            max_dist_pct=2.0,
            resistance=63900.0,
        )
        is True
    )
    # Tight SR band: mid-upper (~70% of span) must still be allowed when within
    # max_dist of support — absolute 0.35%-to-res was flipping every tick.
    assert (
        se.near_support(
            63992.0,
            support=63500.0,
            donchian_mid=63800.0,
            max_dist_pct=2.0,
            resistance=64200.0,
        )
        is True
    )
    # Upper quartile of SR band → chase
    assert se.resistance_chase(64100.0, support=63500.0, resistance=64200.0) is True
    assert (
        se.near_support(
            64100.0,
            support=63500.0,
            donchian_mid=63800.0,
            max_dist_pct=2.0,
            resistance=64200.0,
        )
        is False
    )


def test_event_hard_pause_blocks(monkeypatch, tmp_path):
    monkeypatch.setenv("SENTIENT_ENTRY", "1")
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(
        se,
        "build_context_bundle",
        lambda **kw: {
            "world": {},
            "world_mult": 1.0,
            "structure": {},
            "structure_mult": 1.0,
            "chart_missing": False,
            "support": 99.0,
            "event_hard_pause": True,
            "cost_rt": 0.22,
            "cost_stressed": 0.44,
        },
    )
    out = se.run_sentient_entry(
        bot="btc",
        pair="BTC/USDT",
        prices=[100.0] * 30,
        strategy=_donchian_strategy(),
        context="wait for pullback",
        trad_sig=None,
        trad_skip="donchian:no_breakout",
        current_cycle=1,
    )
    assert out.get("event_pause") is True
    assert out.get("skip") == "event:hard_pause"
    assert out.get("signal") is None


def test_entry_runtime_schema_clears_poisoned_alt_quota(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    path, _, _ = se._state_paths("btc")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"pairs": {}, "day": "2099-01-01", "alt_entries_today": 9}',
        encoding="utf-8",
    )
    rt = se.load_entry_runtime("btc")
    assert int(rt.get("alt_entries_today") or 0) == 0
    assert int(rt.get("schema") or 0) >= 2


def test_failed_breakout_cooldown_latches(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    se.note_failed_breakout_cooldown(
        "btc",
        "BTC/USDT",
        entry_type="donchian_breakout",
        current_cycle=100,
        strategy={"failed_breakout_cooldown_cycles": 60},
    )
    assert (
        se.failed_breakout_cooldown_active(
            "btc", "BTC/USDT", current_cycle=120, entry_type="donchian_breakout"
        )
        is True
    )
    assert (
        se.failed_breakout_cooldown_active(
            "btc", "BTC/USDT", current_cycle=160, entry_type="donchian_breakout"
        )
        is False
    )
    # Pullback sleeve not blocked by donchian FB cooldown
    assert (
        se.failed_breakout_cooldown_active(
            "btc", "BTC/USDT", current_cycle=120, entry_type="pullback"
        )
        is False
    )