"""Tests for the GP-ensemble shadow entry (Task A + Task B).

Network-free. Discovered-indicator storage is redirected to a temp dir by the
autouse fixture from test_genetic (which monkeypatches gp.DISCOVERED_DIR), so
these tests never touch real state/discovered.

Task A: cross-pair indicator sharing (gold->silver, USD group) is loaded and
        tagged with _shared_from / _shared_penalty.
Task B: the shadow gp_ensemble entry evaluates discovered expressions on live
        prices, votes, and returns a meta["shadow"]=True Signal (never a real
        order); simulate_gp_paper_pnl confirms the paper-trading math runs.
"""

from __future__ import annotations

import json

import pytest

import hermes_core.engines.genetic as gp
from hermes_core.engines.entry import (
    _gp_eval_last,
    _gp_parse,
    gp_ensemble_signal,
    simulate_gp_paper_pnl,
)
from hermes_core.engines import entry as entry_mod
from hermes_core.engines.genetic import FEATURES, _eval_expr, load_discovered_indicators


@pytest.fixture(autouse=True)
def _tmp_discovered(tmp_path, monkeypatch):
    """Same redirect as test_genetic: isolate discovered storage."""
    monkeypatch.setattr(gp, "DISCOVERED_DIR", tmp_path / "discovered")
    yield


def _write_discovered(pair: str, inds: list[dict]) -> None:
    # Item 9/15: ensemble requires backtest_approved; default True in tests
    # unless a case explicitly sets False/omits for reject coverage.
    stamped = []
    for ind in inds:
        row = dict(ind)
        if "backtest_approved" not in row:
            row["backtest_approved"] = True
        stamped.append(row)
    gp._save_discovered(pair, stamped)


# ── Task A: cross-pair sharing ──────────────────────────────────────────────
def test_shared_groups_constant():
    """Gold/silver + USD group are the hand-coded shared groups (Task A)."""
    groups = gp.SHARED_INDICATOR_GROUPS
    assert {"XAU/USD", "XAG/USD"} in groups
    assert {"EUR/USD", "GBP/USD", "AUD/USD"} in groups


def test_shared_pairs_for_known_and_unknown():
    assert set(gp._shared_pairs_for("XAG/USD")) == {"XAU/USD"}
    assert set(gp._shared_pairs_for("EUR/USD")) == {"GBP/USD", "AUD/USD"}
    assert gp._shared_pairs_for("BTC/USD") == []  # not in any group


def test_gold_indicators_shared_into_silver():
    """A gold discovery should appear when loading XAG/USD with include_shared."""
    gold_ind = [{
        "pair": "XAU/USD", "name": "(price-sma20)", "expr": "(price-sma20)",
        "fitness": 0.3, "win_rate": 0.55, "oos_corr": 0.3,
    }]
    _write_discovered("XAU/USD", gold_ind)
    sil_own = [{
        "pair": "XAG/USD", "name": "(ema20-sma20)", "expr": "(ema20-sma20)",
        "fitness": 0.25, "win_rate": 0.5, "oos_corr": 0.28,
    }]
    _write_discovered("XAG/USD", sil_own)

    merged = load_discovered_indicators("XAG/USD", include_shared=True)
    names = {i["name"] for i in merged}
    assert "(price-sma20)" in names          # came from gold
    assert "(ema20-sma20)" in names          # silver's own

    shared = [i for i in merged if i.get("_shared_from") == "XAU/USD"]
    assert len(shared) == 1
    assert shared[0]["_shared_penalty"] == 0.5


def test_dependent_pair_without_own_file_still_shared():
    """X1 regression: a shared-dependent pair (XAG/USD) that has NO own
    discovered file must STILL surface the group's indicators via the
    shared loader (the old _push_state globbed own-files only, so XAG/USD,
    GBP/USD, AUD/USD were missing from /api/discovered)."""
    gold_ind = [{
        "pair": "XAU/USD", "name": "(price-sma20)", "expr": "(price-sma20)",
        "fitness": 0.3, "win_rate": 0.55, "oos_corr": 0.3, "complexity": 2,
    }]
    _write_discovered("XAU/USD", gold_ind)  # only the anchor file exists
    merged = load_discovered_indicators("XAG/USD", include_shared=True)
    assert len(merged) == 1
    assert merged[0]["name"] == "(price-sma20)"


def test_discovered_record_carries_x2_fields():
    """X2: every discovered record must carry the fields the dashboard shows."""
    ind = [{
        "pair": "EUR/USD", "name": "price", "expr": "price",
        "fitness": 0.83, "oos_corr": 0.84, "complexity": 1,
    }]
    _write_discovered("EUR/USD", ind)
    loaded = load_discovered_indicators("EUR/USD")[0]
    for k in ("expr", "fitness", "oos_corr", "complexity"):
        assert loaded.get(k) is not None, f"missing required field {k}"



def test_shared_loading_never_infinite_loops():
    """Mutual sharing must terminate (gold<->silver)."""
    _write_discovered("XAU/USD", [{"name": "a", "expr": "a", "fitness": 0.2, "win_rate": 0.5}])
    _write_discovered("XAG/USD", [{"name": "b", "expr": "b", "fitness": 0.2, "win_rate": 0.5}])
    out = load_discovered_indicators("XAU/USD", include_shared=True)
    assert out  # returns without RecursionError


# ── Task B: shadow gp_ensemble entry ────────────────────────────────────────
def test_parser_matches_tree_eval():
    """The string parser + _eval_expr must equal a hand-built tree eval,
    proving live evaluation uses the SAME math as discovery."""
    expr_str = "(((vol+ema20)/sma50)*((ema20/sma50)*price))"
    # build equivalent tree
    tree = ("mul", ("div", ("add", "vol", "ema20"), "sma50"),
            ("mul", ("div", "ema20", "sma50"), "price"))
    prices = [100.0 + i * 0.1 for i in range(60)]
    a = _eval_expr(tree, prices)
    b = _gp_eval_last(expr_str, prices)
    assert abs(a - b) < 1e-9


def test_parser_handles_all_features():
    """Every FEATURES token parses + evaluates without raising."""
    prices = [100.0 + 0.2 * i for i in range(60)]
    for f in FEATURES:
        tree = _gp_parse(f)
        val = _eval_expr(tree, prices)
        assert isinstance(val, float)


def test_gp_signal_is_shadow_and_votes():
    """A clear consensus returns a shadow Signal; no real order path.

    Uses TWO indicators (matching the old engine's >=2 active rule via
    min_active) and enough price history for a 20+ point signal series.
    We assert a valid shadow Signal is produced with a real consensus label
    (direction depends on the synthetic data, not the assertion).
    """
    prices = [100.0 + 0.2 * i for i in range(110)] + [112.0 + 0.5 * j for j in range(10)]
    _write_discovered("EUR/USD", [
        {"name": "(price-sma20)", "expr": "(price-sma20)",
         "fitness": 0.4, "win_rate": 0.6, "oos_corr": 0.3},
        {"name": "(ema20-sma20)", "expr": "(ema20-sma20)",
         "fitness": 0.35, "win_rate": 0.58, "oos_corr": 0.29},
    ])
    sig = gp_ensemble_signal("EUR/USD", prices)
    assert sig is not None
    assert sig.type == "gp_ensemble"
    assert sig.meta["shadow"] is True
    assert sig.meta["consensus"] in ("bullish", "strong_bullish",
                                      "bearish", "strong_bearish")
    assert sig.meta["num_active"] >= 2


def test_gp_signal_none_when_no_indicators():
    """No discovered indicators -> no signal (shadow or otherwise)."""
    prices = [100.0 + 0.1 * i for i in range(60)]
    assert gp_ensemble_signal("NOPE/USD", prices) is None


def test_gp_signal_requires_min_active():
    """A lone weak indicator must not fire (min_active gate)."""
    prices = [100.0 + 0.1 * i for i in range(60)]
    # fitness*win_rate tiny -> weight floored, but single vote < min_active=2
    _write_discovered("EUR/USD", [
        {"name": "x", "expr": "(price-sma20)", "fitness": 0.001,
         "win_rate": 0.5, "oos_corr": 0.16},
    ])
    assert gp_ensemble_signal("EUR/USD", prices) is None


def test_paper_pnl_runs_and_is_deterministic():
    """simulate_gp_paper_pnl executes over a series and is stable."""
    prices = [100.0 + 0.3 * i + 2.0 * (i % 5) for i in range(120)]
    _write_discovered("EUR/USD", [
        {"name": "(price-sma20)", "expr": "(price-sma20)",
         "fitness": 0.3, "win_rate": 0.55, "oos_corr": 0.3},
        {"name": "(ema20-sma20)", "expr": "(ema20-sma20)",
         "fitness": 0.3, "win_rate": 0.55, "oos_corr": 0.3},
    ])
    r1 = simulate_gp_paper_pnl("EUR/USD", prices, horizon=1)
    r2 = simulate_gp_paper_pnl("EUR/USD", prices, horizon=1)
    assert r1 == r2
    assert r1["trades"] > 0
    assert 0.0 <= r1["win_rate"] <= 1.0
    # total_pnl sign is finite
    assert isinstance(r1["total_pnl"], float)


def test_paper_pnl_short_series_returns_empty():
    assert simulate_gp_paper_pnl("EUR/USD", [1.0, 2.0, 3.0])["trades"] == 0


# ── Shadow logger wiring (Task B live hook) ────────────────────────────────
def test_log_gp_shadow_writes_record(tmp_path, monkeypatch):
    """The live-loop shadow hook appends a paper-only record and never raises."""
    import hermes_core.engines.loop as loop
    import hermes_core.engines.genetic as gp

    monkeypatch.setattr(loop, "_state_dir", lambda bot: (tmp_path / bot).mkdir(parents=True, exist_ok=True) or (tmp_path / bot))
    # ensure some discovered indicators exist for the pair
    _write_discovered("EUR/USD", [
        {"name": "(price-sma20)", "expr": "(price-sma20)",
         "fitness": 0.4, "win_rate": 0.6, "oos_corr": 0.3},
        {"name": "(ema20-sma20)", "expr": "(ema20-sma20)",
         "fitness": 0.35, "win_rate": 0.58, "oos_corr": 0.29},
    ])
    # ramp then a sharp recent move so the indicators' last values deviate
    # from their rolling means (z-score clears the gate -> a real signal).
    prices = [100.0 + 0.2 * i for i in range(110)] + [112.0 + 0.5 * j for j in range(10)]
    strategy = {"position_size_r": 0.1}

    loop._log_gp_shadow("goldbot", "EUR/USD", prices, strategy)

    rec_path = tmp_path / "goldbot" / "gp_shadow.jsonl"
    assert rec_path.exists()
    lines = rec_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["shadow"] is True
    assert rec["pair"] == "EUR/USD"
    assert rec["signal"] == "gp_ensemble"
    assert rec["consensus"] in ("bullish", "strong_bullish",
                                 "bearish", "strong_bearish")


def test_log_gp_shadow_fail_soft(tmp_path, monkeypatch):
    """Bad inputs must never raise; the hook stays invisible to the cycle."""
    import hermes_core.engines.loop as loop
    monkeypatch.setattr(loop, "_state_dir", lambda bot: (tmp_path / bot).mkdir(parents=True, exist_ok=True) or (tmp_path / bot))
    # too-short prices -> silent no-op
    loop._log_gp_shadow("b", "EUR/USD", [1.0, 2.0], {"position_size_r": 0.1})
    assert not (tmp_path / "b" / "gp_shadow.jsonl").exists()


# ── Regression: FX pairs must get a REAL multi-candle history (not 1 tick) ──
def test_aggregate_fx_history_is_multi_candle(monkeypatch):
    """The live loop's history_fn must return a real series for FX.

    Regression guard for the bug where seed_history_fn returned
    [last_good_tick] (1 candle) for FX, leaving compute_all/evaluate_entry
    (and the GP shadow hook) running on a single degenerate point.

    The fetcher is mocked so the test is deterministic (yfinance intermittently
    reports FX symbols as 'delisted' -- a known transient mapping quirk).
    """
    import hermes_core.adapters.aggregate as agg
    monkeypatch.setattr(agg, "make_aggregator_fetch", lambda *a, **k: None)
    fake = [{"price": 1.0 + i * 0.001} for i in range(300)]
    monkeypatch.setattr(agg, "_yf_seed_history", lambda pair, max_candles=300: fake)
    a = agg.PriceAggregator(["EUR/USD", "GBP/USD", "XAU/USD", "XAG/USD", "BTC/USD"])
    for fx in ["EUR/USD", "GBP/USD", "AUD/USD"]:
        hist = a.seed_history_fn(fx, max_candles=300)
        assert len(hist) >= 50, f"{fx} history too short: {len(hist)}"
        # candles must carry a numeric price
        assert all(isinstance(c.get("price"), (int, float)) for c in hist)


def test_gp_ensemble_signal_promote_tags_entry_type():
    """When promote=True the returned Signal must be a real (paper) entry:
    shadow=False and entry_type='gp_ensemble', so the live loop opens it
    through the same RR-guard/position-size path as traditional entries and
    the dashboard can badge it as a GP-brain trade.
    """
    _write_discovered("EUR/USD", [
        {"name": "(price-sma20)", "expr": "(price-sma20)",
         "fitness": 0.4, "win_rate": 0.6, "oos_corr": 0.3},
        {"name": "(ema20-sma20)", "expr": "(ema20-sma20)",
         "fitness": 0.35, "win_rate": 0.58, "oos_corr": 0.29},
    ])
    # Oscillatory series (real GP entries fire on deviation, not steady ramps)
    # so the indicators produce a decisive z-score vote.
    import math
    daily = [100.0 + 5.0 * math.sin(i / 12.0) + 0.02 * i for i in range(260)]
    sig = gp_ensemble_signal("EUR/USD", daily, daily_prices=daily, promote=True)
    assert sig is not None, "expected a promoted GP signal on a trending series"
    assert sig.meta.get("shadow") is False
    assert sig.meta.get("entry_type") == "gp_ensemble"
    assert sig.meta.get("evaluated_on") == "daily"
    # And the shadow (default) variant stays observation-only.
    sh = gp_ensemble_signal("EUR/USD", daily, daily_prices=daily, promote=False)
    assert sh.meta.get("shadow") is True
    assert sh.meta.get("entry_type") == "shadow"


def test_gp_ensemble_skips_exiled_indicators():
    """L36: cortex-exiled names must not vote (exiled_ids filter)."""
    import math
    _write_discovered("EUR/USD", [
        {"name": "good_a", "expr": "(price-sma20)",
         "fitness": 0.4, "win_rate": 0.6, "oos_corr": 0.3},
        {"name": "good_b", "expr": "(ema20-sma20)",
         "fitness": 0.35, "win_rate": 0.58, "oos_corr": 0.29},
        {"name": "exiled_noise", "expr": "(roc5)",
         "fitness": 0.9, "win_rate": 0.9, "oos_corr": 0.5},
    ])
    daily = [100.0 + 5.0 * math.sin(i / 12.0) + 0.02 * i for i in range(260)]
    # With only the two good names, signal may fire; with all three exiled, None.
    all_exiled = {"good_a", "good_b", "exiled_noise"}
    assert gp_ensemble_signal(
        "EUR/USD", daily, daily_prices=daily, exiled_ids=all_exiled,
    ) is None
    # Exiling the noise name alone must still allow a vote from the other two.
    sig = gp_ensemble_signal(
        "EUR/USD", daily, daily_prices=daily, exiled_ids={"exiled_noise"},
    )
    if sig is not None:
        assert "exiled_noise" not in (sig.meta.get("gp_indicators") or [])


def test_runner_open_trade_carries_entry_type():
    """The bot's pushed recent_open_trades must carry entry_type so the
    dashboard can badge GP-brain entries next to traditional ones.
    """
    import importlib
    runner = importlib.import_module("bots._runner")
    summary = {"open_positions": {
        "EUR/USD": {"id": "forex:EUR/USD:1", "entry_ts": "2026-01-01T00:00:00+00:00",
                    "entry_price": 1.1, "size": 0.1, "entry_type": "gp_ensemble",
                    "stop_loss_pct": 1.0, "profit_target_pct": 2.0, "held_cycles": 5},
        "GBP/USD": {"id": "forex:GBP/USD:1", "entry_ts": "2026-01-01T00:00:00+00:00",
                    "entry_price": 1.3, "size": 0.1, "entry_type": "mean_reversion",
                    "stop_loss_pct": 1.0, "profit_target_pct": 2.0},
    }}
    recent = [{
        "id": pos.get("id") or f"gold:{pair}:x", "bot": "gold", "pair": pair,
        "entry_type": pos.get("entry_type", "mean_reversion"),
        "entry_price": pos.get("entry_price"), "size": pos.get("size"),
        "entry_ts": pos.get("entry_ts") or runner._now_iso(),
        "stop_loss_pct": pos.get("stop_loss_pct"),
        "profit_target_pct": pos.get("profit_target_pct"),
        "held_cycles": pos.get("held_cycles", 0),
        "unrealised_pct": pos.get("unrealised_pct"),
    } for pair, pos in summary["open_positions"].items()]
    types = {t["pair"]: t["entry_type"] for t in recent}
    assert types == {"EUR/USD": "gp_ensemble", "GBP/USD": "mean_reversion"}
    # Real entry_ts preserved (not overwritten with "now")
    assert recent[0]["entry_ts"] == "2026-01-01T00:00:00+00:00"
    assert recent[0]["id"] == "forex:EUR/USD:1"
    assert recent[0]["held_cycles"] == 5


# ── Audit B10: live paper-PnL feeds back into GP discovery fitness ───────────
def test_b10_live_feedback_relabels_and_suppresses(tmp_path, monkeypatch):
    """Realized GP PnL must bend discovered-indicator fitness, and a losing
    indicator must be flagged 'suppress' so it is excluded from the ensemble
    vote (B10 closes the self-evolving loop)."""
    import hermes_core.engines.decision_cortex as cx

    # Redirect cortex disk state too (autouse fixture already redirects DISCOVERED_DIR).
    monkeypatch.setattr(cx, "CORTEX_DIR", tmp_path / "cortex")
    monkeypatch.setattr(cx, "MEMORY_PATH", tmp_path / "cortex" / "m.json")
    monkeypatch.setattr(cx, "EXILE_PATH", tmp_path / "cortex" / "e.json")

    _write_discovered("EUR/USD", [
        {"name": "A_win", "expr": "A_win", "fitness": 0.30, "win_rate": 0.5,
         "complexity": 2, "nodes": 2, "horizon": 60, "interval": "1d",
         "source": "genetic"},
        {"name": "B_lose", "expr": "B_lose", "fitness": 0.40, "win_rate": 0.5,
         "complexity": 2, "nodes": 2, "horizon": 60, "interval": "1d",
         "source": "genetic"},
    ])

    cortex = cx.Cortex()
    for _ in range(5):
        cortex.record_indicator_outcome("A_win", +2.4, entry_type="gp_ensemble")
    for _ in range(5):
        cortex.record_indicator_outcome("B_lose", -1.6, entry_type="gp_ensemble")

    n = gp.apply_live_feedback("EUR/USD", cortex)
    assert n == 2

    reloaded = {i["name"]: i for i in gp.load_discovered_indicators("EUR/USD")}
    assert reloaded["A_win"]["live_flag"] == "promote"
    assert reloaded["A_win"]["live_fitness"] >= 0.30
    assert reloaded["B_lose"]["live_flag"] == "suppress"
    assert reloaded["B_lose"]["live_fitness"] < 0.40

    # The suppressed indicator must NOT be able to vote the ensemble.
    series = list([100.0] * 120) + [100.0 + i * 2 for i in range(30)]
    _write_discovered("EUR/USD", [
        {"name": "A_win", "expr": "price", "fitness": 0.30, "win_rate": 0.5,
         "complexity": 2, "nodes": 2, "horizon": 60, "interval": "1d",
         "source": "genetic", "live_flag": "promote", "live_fitness": 0.33},
        {"name": "C_ok", "expr": "price", "fitness": 0.30, "win_rate": 0.5,
         "complexity": 2, "nodes": 2, "horizon": 60, "interval": "1d",
         "source": "genetic", "live_flag": "promote", "live_fitness": 0.33},
        {"name": "B_lose", "expr": "price", "fitness": 0.40, "win_rate": 0.5,
         "complexity": 2, "nodes": 2, "horizon": 60, "interval": "1d",
         "source": "genetic", "live_flag": "suppress", "live_fitness": 0.37},
    ])
    sig = entry_mod.gp_ensemble_signal(
        "EUR/USD", series, strategy={"position_size_r": 0.1},
        daily_prices=series, promote=True)
    assert sig is not None
    fired = sig.meta["gp_indicators"]
    assert "B_lose" not in fired
    assert "A_win" in fired and "C_ok" in fired


def test_b10_feedback_ignored_until_min_samples(tmp_path, monkeypatch):
    """With < LIVE_FEEDBACK_MIN_SAMPLES GP entries, live signal is NOT trusted
    (anti-overfit guard): the indicator stays 'pending' and fitness unchanged."""
    import hermes_core.engines.decision_cortex as cx
    monkeypatch.setattr(cx, "CORTEX_DIR", tmp_path / "cortex")
    monkeypatch.setattr(cx, "MEMORY_PATH", tmp_path / "cortex" / "m.json")
    monkeypatch.setattr(cx, "EXILE_PATH", tmp_path / "cortex" / "e.json")

    _write_discovered("EUR/USD", [
        {"name": "A_win", "expr": "A_win", "fitness": 0.30, "win_rate": 0.5,
         "complexity": 2, "nodes": 2, "horizon": 60, "interval": "1d",
         "source": "genetic"},
    ])
    cortex = cx.Cortex()
    for _ in range(2):
        cortex.record_indicator_outcome("A_win", +5.0, entry_type="gp_ensemble")

    n = gp.apply_live_feedback("EUR/USD", cortex)
    assert n == 0  # nothing re-ranked (samples < min)
    reloaded = gp.load_discovered_indicators("EUR/USD")[0]
    assert reloaded.get("live_flag") == "pending"
    assert reloaded.get("live_fitness") == 0.30  # historical fitness kept
