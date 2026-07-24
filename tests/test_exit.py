"""Session 5 / Phase 5 acceptance + guard tests for the exit engine.

Mirrors the blueprint Phase-5 test block (test_stop_loss, test_profit_target,
test_time_exit, test_breakeven, test_partial_close) plus the [GUARD L24] circuit
breaker predicate and a Hypothesis property test proving evaluate_exit never
returns zero or multiple reasons — exactly one action per evaluation, per the
roadmap S5 DO-NOT.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hermes_core.engines import Exit, evaluate_exit, should_circuit_break


def trade(entry, **kw):
    """Build an exit-engine trade dict (mirrors blueprint trade() shorthand).

    The blueprint's Phase-5 tests use shorthand keys (sl=, tp=, target=, held=);
    the engine reads canonical keys, so we bridge them here to keep the original
    test calls intact.
    """
    d = {
        "entry_price": entry,
        "held_cycles": 0,
        "breakeven_set": False,
        "partial_done": False,
        "partial_enabled": False,
    }
    if "sl" in kw:
        d["stop_loss_pct"] = kw.pop("sl")
    if "tp" in kw:
        d["profit_target_pct"] = kw.pop("tp")
    if "target" in kw:
        d["profit_target_pct"] = kw.pop("target")
    if "held" in kw:
        d["held_cycles"] = kw.pop("held")
    d.update(kw)
    if "sl_moved" in kw:  # blueprint uses sl_moved=False to mean breakeven not yet set
        d["breakeven_set"] = kw["sl_moved"]
        del d["sl_moved"]
    return d


def test_stop_loss():
    ex = evaluate_exit(trade(1.1000, sl=1.5), 1.0834, None)
    assert ex is not None and ex.reason == "stop_loss"  # 1.1000*0.985=1.0835; 1.0834<=1.0835


def test_profit_target():
    ex = evaluate_exit(trade(1.1000, tp=3.0), 1.1331, None)
    assert ex is not None and ex.reason == "profit_target"  # 1.1000*1.03=1.1330; 1.1331>=1.1330


def test_time_exit():
    ex = evaluate_exit(trade(1.1000, held_cycles=361, time_exit_cycles=360), 1.1000, None)
    assert ex is not None and ex.reason == "time_exit"


def test_mfe_giveback_locks_winner():
    """Peak 0.8% then give back ≥50% → hard exit (not time_exit)."""
    t = trade(
        1.1000,
        peak_mfe_pct=0.8,
        unrealised_pct=0.3,  # giveback 0.5 → frac 0.625
        mfe_giveback_min_pct=0.4,
        mfe_giveback_frac=0.5,
        time_exit_cycles=288,
        held=10,
        tp=3.0,
        sl=1.5,
    )
    # price consistent with ~0.3% unrealised
    ex = evaluate_exit(t, 1.1033, None)
    assert ex is not None and ex.reason == "mfe_giveback"


def test_mfe_giveback_below_min_mfe_holds():
    t = trade(
        1.1000,
        peak_mfe_pct=0.3,  # below default min 0.4
        unrealised_pct=0.05,
        mfe_giveback_min_pct=0.4,
        mfe_giveback_frac=0.5,
        time_exit_cycles=288,
        held=10,
        tp=3.0,
        sl=1.5,
    )
    ex = evaluate_exit(t, 1.10055, None)
    assert ex is None


def test_mfe_giveback_disabled():
    t = trade(
        1.1000,
        peak_mfe_pct=1.0,
        unrealised_pct=0.2,
        mfe_giveback_enabled=False,
        time_exit_cycles=288,
        held=10,
        tp=3.0,
        sl=1.5,
    )
    ex = evaluate_exit(t, 1.1022, None)
    assert ex is None


def test_trail_fires_before_time_exit():
    """Held at time limit but trail can still arm on earlier cycles;
    at the deadline, giveback/TP still beat time_exit; with no giveback,
    time_exit remains last resort. Trail must win while held < te.
    """
    prices = [1.0 + 0.003 * ((i % 2) * 2 - 1) for i in range(50)]
    t = trade(
        1.0,
        held=100,
        time_exit_cycles=150,
        tp=3.0,
        sl=1.5,
        unrealised_pct=1.0,
        trailing_atr_mult=2.0,
        current_stop=0.5,
        peak_mfe_pct=1.0,
        mfe_giveback_enabled=False,  # isolate trail vs giveback
    )
    ex = evaluate_exit(t, 1.01, prices)
    assert ex is not None and ex.reason == "trailing"


def test_time_exit_is_last_resort():
    """At the clock with no TP/giveback/trail action → time_exit."""
    t = trade(
        1.1000,
        held=150,
        time_exit_cycles=150,
        tp=3.0,
        sl=1.5,
        unrealised_pct=0.1,
        peak_mfe_pct=0.15,  # below min → no giveback
        mfe_giveback_min_pct=0.4,
        trailing_atr_mult=None,
    )
    ex = evaluate_exit(t, 1.1011, None)
    assert ex is not None and ex.reason == "time_exit"


def test_breakeven():
    t = trade(1.1000, unrealised_pct=1.5, profit_target_pct=3.0, sl_moved=False)
    ex = evaluate_exit(t, 1.1165, None)
    assert ex is not None and ex.new_stop == 1.1000  # breakeven at target*0.5


def test_partial_close():
    t = trade(
        1.1000, held_cycles=101, profit_target_pct=3.0, partial_enabled=True, partial_done=False
    )
    ex = evaluate_exit(t, 1.1331, None)
    # 50% off at FULL target 3.0% (->1.1330, NOT target/2); stop to breakeven
    assert ex is not None
    assert ex.partial_close_fraction == 0.5
    assert ex.new_stop == 1.1000


# --- [GUARD L24] circuit breaker -------------------------------------------
def test_circuit_breaker():
    assert should_circuit_break(4) is False
    assert should_circuit_break(5) is True
    assert should_circuit_break(7) is True


def test_no_io_in_exit_engine():
    import ast
    from pathlib import Path

    engines = Path(__file__).resolve().parent.parent / "hermes_core" / "engines"
    src = (engines / "exit.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name == "open":
                pytest.fail("exit engine calls open()")
    for banned in ("requests.", "urllib", "yfinance", "urlopen", "socket."):
        assert banned not in src


# --- Hypothesis: exactly one reason, never zero/multiple --------------------
prices = st.lists(
    st.floats(min_value=1e-3, max_value=1e6, allow_nan=False, allow_infinity=False),
    min_size=2,
    max_size=40,
)


@given(
    entry=st.floats(min_value=1e-3, max_value=1e6, allow_nan=False, allow_infinity=False),
    price=st.floats(min_value=1e-3, max_value=1e6, allow_nan=False, allow_infinity=False),
    sl=st.floats(min_value=0.0, max_value=20.0),
    tp=st.floats(min_value=0.0, max_value=20.0),
    te=st.integers(min_value=1, max_value=1000),
    held=st.integers(min_value=0, max_value=2000),
    partial=st.booleans(),
    pr=st.floats(min_value=1e-3, max_value=1e6, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=300, deadline=None)
def test_exactly_one_reason(entry, price, sl, tp, te, held, partial, pr):
    # Build a valid trade; never pass contradictory state. evaluate_exit must
    # return either None or a single Exit with one reason — never 2 actions.
    t = {
        "entry_price": entry,
        "stop_loss_pct": sl,
        "profit_target_pct": tp,
        "time_exit_cycles": te,
        "held_cycles": held,
        "partial_enabled": partial,
        "partial_done": False,
        "breakeven_set": False,
        "unrealised_pct": (price - entry) / entry * 100.0 if entry else 0.0,
        "mfe_giveback_enabled": False,  # isolate classic reasons in property test
    }
    try:
        result = evaluate_exit(t, price, [pr] * 5)
    except Exception as exc:  # pure fn must never raise on valid numeric input
        raise AssertionError(f"evaluate_exit raised: {exc}") from None
    assert result is None or isinstance(result, Exit)
    if result is not None:
        assert result.reason in {
            "stop_loss",
            "profit_target",
            "partial_close",
            "time_exit",
            "breakeven",
            "trailing",
            "mfe_giveback",
        }
        # exactly one action: no reason can also imply another simultaneously
        assert (result.reason == "partial_close") == (result.partial_close_fraction == 0.5)
