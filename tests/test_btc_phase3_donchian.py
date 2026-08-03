"""BTC Focus Phase 3 — Donchian Strategy B + invent freeze."""

from __future__ import annotations

from hermes_core.config import load_config, load_strategy_for_pair
from hermes_core.config.schema import ALLOWED_STRATEGY_TYPES
from hermes_core.config.validator import validate_strategy_params
from hermes_core.engines import btc_regime as br
from hermes_core.engines.entry import evaluate_entry_detailed
from hermes_core.indicators import compute_donchian


def test_donchian_in_allowed_strategy_types():
    assert "donchian_breakout" in ALLOWED_STRATEGY_TYPES


def test_btc_strategy_is_donchian_phase3():
    s = load_strategy_for_pair("BTC/USDT", bot="btc")
    assert s["strategy_type"] == "donchian_breakout"
    assert s.get("regime_split") is False
    assert int((s.get("entry") or {}).get("donchian_period") or 0) == 20
    assert float((s.get("entry") or {}).get("breakout_buffer_pct") or 0) >= 0.3
    assert (s.get("entry") or {}).get("require_clean_chart") is True
    assert int(s.get("time_exit_cycles") or 0) >= 300
    assert float(s.get("profit_target_pct") or 0) <= 2.0
    assert float(s.get("be_trigger_frac") or 1) <= 0.35
    assert int(s.get("early_reeval_cycles") or 0) >= 60
    assert str(s.get("exit_tf") or "") == "4h"
    valid, errors = validate_strategy_params(s, raise_on_fail=False)
    assert valid, errors


def test_btc_invent_frozen():
    cfg = load_config("btc")
    assert cfg["invent"].get("enabled") is False


def test_compute_donchian_prior_window_excludes_last():
    prices = [100.0] * 20 + [110.0]
    ch = compute_donchian(prices, period=20)
    assert ch["upper"] == 100.0
    assert prices[-1] > ch["upper"]


def test_donchian_fires_on_buffered_breakout(monkeypatch):
    monkeypatch.setattr(
        br,
        "classify_btc_regime",
        lambda pair, force=False: {
            "label": br.TREND_UP,
            "reason": "test_up",
            "pair": pair,
            "adx": 30.0,
        },
    )
    # 0.4% buffer: need > 100.4 from flat 100 channel.
    prices = [100.0] * 21 + [101.0]
    strategy = load_strategy_for_pair("BTC/USDT", bot="btc")
    sig, reason = evaluate_entry_detailed(
        prices,
        strategy,
        pair="",
        bot="btc",
        session_token="OTHER",
        context="trend: uptrend (conf=0.80). Rec: enter long",
    )
    assert reason == ""
    assert sig is not None
    assert sig.meta["entry_type"] == "donchian_breakout"


def test_donchian_skips_weak_breakout_inside_buffer():
    prices = [100.0] * 21 + [100.2]  # +0.2% < 0.4% buffer
    strategy = load_strategy_for_pair("BTC/USDT", bot="btc")
    sig, reason = evaluate_entry_detailed(
        prices,
        strategy,
        pair="",
        bot="btc",
        session_token="OTHER",
        context="trend: uptrend (conf=0.80). Rec: enter long",
    )
    assert sig is None
    assert reason == "donchian:no_breakout"


def test_donchian_skips_when_no_breakout():
    prices = [100.0] * 25
    strategy = {
        "strategy_type": "donchian_breakout",
        "regime_split": False,
        "position_size_r": 0.2,
        "entry": {
            "donchian_period": 20,
            "session_filter": "24h",
            "breakout_buffer_pct": 0.4,
            "require_clean_chart": False,
        },
        "vol_min_pct": 0.0,
        "vol_max_pct": 100.0,
        "vol_threshold_pct": 0.0,
    }
    sig, reason = evaluate_entry_detailed(
        prices,
        strategy,
        pair="",
        bot="btc",
        session_token="OTHER",
    )
    assert sig is None
    assert reason == "donchian:no_breakout"


def test_donchian_require_clean_chart_rejects_avoid(monkeypatch):
    from hermes_core.engines.chart_vision import chart_hard_blocks_strategy

    ctx = "trend: sideways (conf=0.85). Rec: avoid entirely"
    # Capital veto still soft for donchian, but clean-chart entry rejects it.
    assert chart_hard_blocks_strategy(ctx, strategy_type="donchian_breakout") is False

    monkeypatch.setattr(
        br,
        "classify_btc_regime",
        lambda pair, force=False: {
            "label": br.CHOP,
            "reason": "test_chop",
            "pair": pair,
            "adx": 10.0,
        },
    )
    prices = [100.0] * 21 + [101.0]
    strategy = load_strategy_for_pair("BTC/USDT", bot="btc")
    monkeypatch.setattr(
        "hermes_core.engines.entry.gp_invent_prices",
        lambda *a, **k: prices,
    )
    sig, reason = evaluate_entry_detailed(
        prices,
        strategy,
        pair="BTC/USDT",
        bot="btc",
        session_token="OTHER",
        context=ctx,
    )
    assert sig is None
    assert reason.startswith("donchian:chart_soft:")
