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
    valid, errors = validate_strategy_params(s, raise_on_fail=False)
    assert valid, errors


def test_btc_invent_frozen():
    cfg = load_config("btc")
    assert cfg["invent"].get("enabled") is False


def test_compute_donchian_prior_window_excludes_last():
    # Flat then spike: prior max is 100, last is 110 → upper 100.
    prices = [100.0] * 20 + [110.0]
    ch = compute_donchian(prices, period=20)
    assert ch["upper"] == 100.0
    assert ch["lower"] == 100.0
    assert prices[-1] > ch["upper"]


def test_donchian_fires_on_fresh_breakout(monkeypatch):
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
    # 20 bars at 100, then break to 101 (fresh).
    prices = [100.0] * 21 + [101.0]
    strategy = load_strategy_for_pair("BTC/USDT", bot="btc")
    # Force local series (skip 4h fetch) by clearing pair for the signal TF path
    # while still exercising D1 gate via a BTC pair on a second call.
    sig, reason = evaluate_entry_detailed(
        prices,
        strategy,
        pair="",  # no network invent fetch
        bot="btc",
        session_token="OTHER",
    )
    assert reason == ""
    assert sig is not None
    assert sig.meta["entry_type"] == "donchian_breakout"


def test_donchian_skips_when_no_breakout():
    prices = [100.0] * 25  # flat — never above prior channel
    strategy = {
        "strategy_type": "donchian_breakout",
        "regime_split": False,
        "position_size_r": 0.2,
        "entry": {"donchian_period": 20, "session_filter": "24h"},
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


def test_donchian_chart_avoid_is_soft_not_hard(monkeypatch):
    """Vision 'avoid entirely' must not capital-veto Donchian (BTC Phase 3)."""
    from hermes_core.engines.chart_vision import chart_hard_blocks_strategy

    ctx = "trend: sideways (conf=0.85). Rec: avoid entirely"
    assert chart_hard_blocks_strategy(ctx, strategy_type="donchian_breakout") is False
    assert chart_hard_blocks_strategy(ctx, strategy_type="rsi_momentum") is True

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
    assert reason == ""
    assert sig is not None
    assert "avoid" in (sig.meta.get("chart_soft_reasons") or [])
