"""Session 4 / Phase 4 acceptance + guard-regression tests for the entry engine.

Mirrors the blueprint Phase-4 test block (test_eur_mr_fires, test_eur_mr_session_block,
test_eur_mr_chart_block, test_eur_mr_cooldown, test_aud_rsi_fires, test_aud_rsi_confluence)
plus the explicit L13 regression fixture that reproduces the v06->v07 cliff's exact
precondition (strong recent WR + regime shift to bearish ensemble consensus) and asserts
the MR long is blocked.
"""

from __future__ import annotations

import pytest

from hermes_core.engines import evaluate_entry

# A price series whose last close sits at/below the Bollinger lower band so the
# mean-reversion band check passes (flat 100s then a sharp drop to 70 -> RSI 0,
# ADX 0, last 70 <= bb_lower ~88.7). Verified against compute_all().
prices_at_bb_lower = [100] * 20 + [70]
# AUD/USD momentum series: sustained downtrend so RSI is oversold (<= threshold).
p = [110 - i * 1.0 for i in range(40)]  # 110 down to 71, RSI -> 0


def mr_strategy(**kw):
    base = {"strategy_type": "mean_reversion", "session_filter": "london_only",
            "entry": {"threshold": 38}, "position_size_r": 0.2}
    base.update(kw)
    return base


def rsi_mom_strategy(**kw):
    base = {"strategy_type": "rsi_momentum", "session_filter": "asian_only",
            "entry": {"threshold": 41}, "position_size_r": 0.2}
    base.update(kw)
    return base


def test_eur_mr_fires():
    sig = evaluate_entry(
        "EUR/USD", prices_at_bb_lower, mr_strategy(rsi=38, session="LDN", adx=20),
        "", "neutral", 0, False, {}, 100, "LDN",
    )
    assert sig is not None and sig.type == "mean_reversion"


def test_eur_mr_session_block():
    sig = evaluate_entry(
        "EUR/USD", prices_at_bb_lower, mr_strategy(rsi=38, session="OTHER", adx=20),
        "", "neutral", 0, False, {}, 100, "OTHER",
    )
    assert sig is None  # L04: outside LDN window


def test_eur_mr_chart_block():
    sig = evaluate_entry(
        "EUR/USD", prices_at_bb_lower, mr_strategy(rsi=38, session="LDN", adx=20),
        "avoid entirely", "neutral", 0, False, {}, 100, "LDN",
    )
    assert sig is None  # hard block from chart vision


def test_eur_mr_cooldown():
    reentry = {"EUR/USD": {"last_exit_cycle": 85}}  # stopped out 15 cycles ago (100-15)
    sig = evaluate_entry(
        "EUR/USD", prices_at_bb_lower, mr_strategy(rsi=38, session="LDN", adx=20),
        "", "neutral", 0, False, reentry, 100, "LDN",
    )
    assert sig is None  # L15/L23: re-entry cooldown < 30 cycles


def test_aud_rsi_fires():
    strat = rsi_mom_strategy(session="ASIA", adx=22, vol_above=True, oversold_pairs=2)
    sig = evaluate_entry("AUD/USD", p, strat, "", "neutral", 2, True, {}, 100, "ASIA")
    assert sig is not None and sig.type == "rsi_momentum"


def test_aud_rsi_confluence():
    strat = rsi_mom_strategy(session="ASIA", adx=22, vol_above=True, oversold_pairs=1)
    sig = evaluate_entry("AUD/USD", p, strat, "", "neutral", 1, True, {}, 100, "ASIA")
    assert sig is None  # L18: confluence requires >=2 oversold pairs


# --- Explicit L13 regression fixture: the v06->v07 cliff -------------------
def test_l13_ensemble_bearish_blocks_mr_long():
    # Precondition of the v06->v07 cliff: strong recent WR, regime flipped to a
    # bearish discovered-indicator ensemble consensus. An MR long here is exactly
    # the trade that blew up v06; L13 must block it.
    sig = evaluate_entry(
        "EUR/USD", prices_at_bb_lower, mr_strategy(rsi=38, session="LDN", adx=20),
        "", "strong_bearish", 0, False, {}, 100, "LDN",
    )
    assert sig is None
    # Sanity: the identical setup with neutral consensus DOES fire (guard is the
    # only thing standing between this and a blocked trade).
    ok = evaluate_entry(
        "EUR/USD", prices_at_bb_lower, mr_strategy(rsi=38, session="LDN", adx=20),
        "", "neutral", 0, False, {}, 100, "LDN",
    )
    assert ok is not None and ok.type == "mean_reversion"


def test_session_filter_under_entry_key():
    """Production YAMLs nest session_filter under entry.*; engine must honor it."""
    strat = {
        "strategy_type": "mean_reversion",
        "entry": {"threshold": 38, "session_filter": "london_only"},
        "position_size_r": 0.2,
    }
    assert evaluate_entry(
        "EUR/USD", prices_at_bb_lower, strat, "", "neutral", 0, False, {}, 100, "LDN",
    ) is not None
    assert evaluate_entry(
        "EUR/USD", prices_at_bb_lower, strat, "", "neutral", 0, False, {}, 100, "NY",
    ) is None


def test_mr_entry_rsi_used_when_threshold_absent():
    """Forex YAML uses mr_entry_rsi, not threshold — must not silently default to 50."""
    from hermes_core.engines.entry import _entry_rsi_threshold

    assert _entry_rsi_threshold({"entry": {"mr_entry_rsi": 30}}) == 30.0
    assert _entry_rsi_threshold({"entry": {"threshold": 41}}) == 41.0
    assert _entry_rsi_threshold({"entry": {"threshold": 55, "mr_entry_rsi": 30}}) == 55.0
    assert _entry_rsi_threshold({"entry": {}}) == 50.0

    # Live path: mr_entry_rsi-only strategy must still fire on deep oversold + BB.
    strat = {
        "strategy_type": "mean_reversion",
        "entry": {"mr_entry_rsi": 30, "session_filter": "24h"},
        "position_size_r": 0.2,
    }
    sig = evaluate_entry(
        "EUR/USD", prices_at_bb_lower, strat, "", "neutral", 0, False, {}, 100, "LDN",
    )
    assert sig is not None
    assert sig.meta.get("rsi_threshold") == 30.0


def test_traditional_signal_tags_entry_type():
    mr = evaluate_entry(
        "EUR/USD", prices_at_bb_lower, mr_strategy(),
        "", "neutral", 0, False, {}, 100, "LDN",
    )
    assert mr is not None
    assert mr.meta.get("entry_type") == "mean_reversion"

    mom = evaluate_entry(
        "AUD/USD", p, rsi_mom_strategy(), "", "neutral", 2, True, {}, 100, "ASIA",
    )
    assert mom is not None
    assert mom.meta.get("entry_type") == "rsi_momentum"


def test_momentum_atr_vol_proxy_when_vol_above_false():
    """Live runner passes vol_above=False; ATR% vs YAML vol_* must unlock momentum."""
    strat = {
        "strategy_type": "rsi_momentum",
        "session_filter": "asian_only",
        "entry": {"threshold": 41},
        "position_size_r": 0.2,
        "vol_threshold_pct": 0.5,
        "vol_min_pct": 0.1,
        "vol_max_pct": 10.0,
    }
    # Explicit True still works
    assert evaluate_entry(
        "AUD/USD", p, strat, "", "neutral", 2, True, {}, 100, "ASIA",
    ) is not None
    # False + elevated ATR on the downtrend series should still fire via proxy
    sig = evaluate_entry(
        "AUD/USD", p, strat, "", "neutral", 2, False, {}, 100, "ASIA",
    )
    assert sig is not None and sig.type == "rsi_momentum"
    # Flat series -> ATR ~0 -> proxy fails
    flat = [100.0] * 40
    assert evaluate_entry(
        "AUD/USD", flat, strat, "", "neutral", 2, False, {}, 100, "ASIA",
    ) is None


def test_no_io_in_entry_engine():
    """S4 DO-NOT: entry engine must stay pure (no network/file calls)."""
    import ast
    from pathlib import Path

    engines = Path(__file__).resolve().parent.parent / "hermes_core" / "engines"
    src = (engines / "entry.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name == "open":
                pytest.fail("entry engine calls open()")
    for banned in ("requests.", "urllib", "yfinance", "urlopen", "socket."):
        assert banned not in src
