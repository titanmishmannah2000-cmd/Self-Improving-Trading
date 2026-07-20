"""Tests for the trade-close pipeline fix (audit #1).

Before this fix, EVERY exit event (including breakeven/trailing stop
adjustments) was written to the closed-trades log, and the record used the
key `reason` while the dashboard reads `exit_reason` -> every "closed trade"
had exit_reason=None and entry_price==exit_price, which zeroed the closed
counter, cumulative chart, Activity feed and Reports.
"""
import pytest

from hermes_core.engines import loop as L
from hermes_core.engines.exit import evaluate_exit, Exit


class _CortexStub:
    def __init__(self):
        self.outcomes = []
        self.ind_credit = []

    def record_outcome(self, pair, entry_type, pnl):
        self.outcomes.append((pair, entry_type, pnl))

    def record_indicator_outcome(self, ind_id, pnl, entry_type=None):
        self.ind_credit.append(ind_id)

    def record_entry(self, pair, entry_type):
        pass


def _make_pos(entry_type="gp_ensemble"):
    return {
        "id": "forex:EUR/USD:1",
        "entry_ts": "2026-07-19T00:00:00+00:00",
        "entry_price": 1.0,
        "size": 0.1,
        "stop_loss_pct": 1.5,
        "profit_target_pct": 3.0,
        "time_exit_cycles": 288,
        "held_cycles": 0,
        "breakeven_set": False,
        "partial_done": False,
        "partial_enabled": False,
        "current_stop": 0.985,
        "atr": 0.01,
        "entry_type": entry_type,
    }


# Price that makes evaluate_exit return the intended reason, plus any
# extra pos/prices needed for that reason to win the check-ordering.
def _cases(reason):
    if reason == "stop_loss":
        return 0.98, {}, None
    if reason == "profit_target":
        return 1.04, {}, None
    if reason == "breakeven":
        return 1.02, {}, None
    if reason == "trailing":
        # unreal in (0, 1.5%) so breakeven doesn't fire; vary prices for real ATR
        return 1.01, {"trailing_atr_mult": 2.0, "current_stop": 0.5}, \
               [1.0 + 0.003 * ((i % 2) * 2 - 1) for i in range(50)]


def _eval(reason):
    price, extra, prices = _cases(reason)
    pos = _make_pos()
    pos.update(extra)
    ex = evaluate_exit(pos, price, prices)
    assert ex is not None and ex.reason == reason, f"{reason} -> {ex}"
    return ex, price, prices


def _run(reason):
    ex, price, prices = _eval(reason)
    pos = _make_pos()
    pos.update(_cases(reason)[1])
    # The live loop recomputes unrealised_pct each cycle before evaluate_exit.
    pos["unrealised_pct"] = (price - pos["entry_price"]) / pos["entry_price"] * 100.0
    logged = []
    L._log_trade = lambda bot, rec: logged.append(rec)
    L._discovered_indicator_ids = lambda bot, pair: []
    cortex = _CortexStub()
    open_positions = {"EUR/USD": pos}
    summary = {"exits": []}
    reentry = {}
    L._process_exit(
        "forex", "EUR/USD", 10, pos, price, ex,
        cortex=cortex, reentry=reentry,
        open_positions=open_positions, summary=summary, alert_fn=None,
    )
    return {
        "logged": logged,
        "outcomes": cortex.outcomes,
        "pos_deleted": "EUR/USD" not in open_positions,
        "summary_exits": summary["exits"],
        "pos": pos,
        "price": price,
    }


@pytest.mark.parametrize("reason", ["breakeven", "trailing"])
def test_stop_adjustments_do_not_close(reason):
    res = _run(reason)
    assert res["logged"] == [], f"{reason} must NOT be logged as a close"
    assert res["pos_deleted"] is False, f"{reason}: position must stay open"
    assert res["outcomes"] == [], f"{reason}: no outcome recorded"
    # stop must have been moved (not a no-op)
    assert res["pos"]["current_stop"] is not None


@pytest.mark.parametrize("reason", ["stop_loss", "profit_target"])
def test_real_close_is_logged_with_correct_keys(reason):
    res = _run(reason)
    assert res["pos_deleted"] is True, "real close must delete the position"
    assert len(res["logged"]) == 1
    rec = res["logged"][0]
    for k in ("id", "exit_reason", "entry_ts", "exit_ts", "entry_type"):
        assert k in rec, f"missing key {k}"
    assert rec["exit_reason"] == reason
    assert rec["entry_ts"] == res["pos"]["entry_ts"]
    assert rec["exit_ts"] is not None and "T" in rec["exit_ts"]
    # exit_ts must be strictly after entry_ts (a real close happens later)
    assert rec["exit_ts"] > rec["entry_ts"]
    assert rec["entry_price"] != rec["exit_price"], "entry must differ from exit"
    assert res["outcomes"] == [("EUR/USD", "gp_ensemble", rec["pnl_pct"])]


def test_partial_close_is_logged_as_real_close():
    """partial_close returns new_stop but is still a genuine close of 50%."""
    pos = _make_pos()
    pos["partial_enabled"] = True
    price = 1.04  # >= entry*(1+tp)
    ex = evaluate_exit(pos, price, None)
    assert ex is not None and ex.reason == "partial_close"
    res = _run("profit_target")  # plumbing sanity (real close path)
    assert res["pos_deleted"] is True
    # Now drive partial directly:
    pos2 = _make_pos(); pos2["partial_enabled"] = True
    pos2["unrealised_pct"] = (price - 1.0) / 1.0 * 100.0
    logged = []
    L._log_trade = lambda b, r: logged.append(r)
    L._discovered_indicator_ids = lambda b, p: []
    cortex = _CortexStub()
    op = {"EUR/USD": pos2}
    L._process_exit("forex", "EUR/USD", 10, pos2, price, ex,
                    cortex=cortex, reentry={}, open_positions=op,
                    summary={"exits": []}, alert_fn=None)
    assert "EUR/USD" not in op, "partial_close closes the position"
    assert len(logged) == 1 and logged[0]["exit_reason"] == "partial_close"
    assert logged[0]["entry_type"] == "gp_ensemble"


def test_gp_close_credits_only_firing_indicators():
    """B9: a GP close credits ONLY the indicators that fired on that trade
    (carried on pos['gp_indicators']), never every discovered indicator for the
    pair. A non-GP close credits none."""
    # GP close: position carries exactly two firing indicator ids
    pos = _make_pos("gp_ensemble")
    pos["gp_indicators"] = ["EUR_roc20", "EUR_rsi14"]
    pos["unrealised_pct"] = 1.5
    ex = evaluate_exit(pos, 0.98, None)  # stop_loss reason
    assert ex is not None and ex.reason == "stop_loss"
    cortex = _CortexStub()
    op = {"EUR/USD": pos}
    L._process_exit("forex", "EUR/USD", 10, pos, 0.98, ex,
                    cortex=cortex, reentry={}, open_positions=op,
                    summary={"exits": []}, alert_fn=None)
    assert cortex.ind_credit == ["EUR_roc20", "EUR_rsi14"], \
        f"only firing indicators should be credited, got {cortex.ind_credit}"

    # mean_reversion close: must credit NO indicators
    pos2 = _make_pos("mean_reversion")
    pos2["gp_indicators"] = []
    pos2["unrealised_pct"] = -0.5
    ex2 = evaluate_exit(pos2, 0.98, None)
    assert ex2 is not None and ex2.reason == "stop_loss"
    cortex2 = _CortexStub()
    op2 = {"GBP/USD": pos2}
    L._process_exit("forex", "GBP/USD", 11, pos2, 0.98, ex2,
                    cortex=cortex2, reentry={}, open_positions=op2,
                    summary={"exits": []}, alert_fn=None)
    assert cortex2.ind_credit == [], \
        "non-GP close must credit no indicators"


def test_gp_close_persists_to_cortex_and_surfaces_in_summary(tmp_path, monkeypatch):
    """End-to-end X2 pipeline (no live trade needed):

    A GP shadow close must (a) credit its firing indicators via
    record_indicator_outcome, (b) PERSIST that to the cortex memory file on
    disk (the /data volume in prod — was previously ephemeral /app, so it was
    wiped every redeploy and live feedback never accumulated), and (c) show up
    in Cortex().summary()['indicators'] with a gp_ensemble sub-block — exactly
    what /api/cortex reads to populate the GP-Entry column.
    """
    import hermes_core.engines.decision_cortex as cx
    monkeypatch.setattr(cx, "CORTEX_DIR", tmp_path / "cortex")
    monkeypatch.setattr(cx, "MEMORY_PATH", tmp_path / "cortex" / "m.json")
    monkeypatch.setattr(cx, "EXILE_PATH", tmp_path / "cortex" / "e.json")

    from hermes_core.engines.decision_cortex import Cortex

    # Real persisted cortex (mirrors what the bot + push both use).
    cortex = Cortex()
    pos = _make_pos("shadow")  # GP in shadow mode (not yet promoted)
    pos["gp_indicators"] = ["EUR_roc20", "EUR_rsi14"]
    pos["unrealised_pct"] = 2.3
    ex = evaluate_exit(pos, 0.98, None)
    assert ex is not None and ex.reason == "stop_loss"
    op = {"EUR/USD": pos}
    L._process_exit("forex", "EUR/USD", 10, pos, 0.98, ex,
                    cortex=cortex, reentry={}, open_positions=op,
                    summary={"exits": []}, alert_fn=None)

    # (b) file actually written to disk
    assert cx.MEMORY_PATH.exists(), "cortex memory must persist to disk"

    # (c) a FRESH Cortex (what the push / apply_live_feedback read) sees it
    reloaded = Cortex().summary()
    inds = reloaded.get("indicators", {})
    assert "EUR_roc20" in inds, f"indicator missing from summary: {list(inds)}"
    gp_block = inds["EUR_roc20"].get("by_type", {}).get("gp_ensemble")
    assert gp_block, f"GP-entry sub-block missing: {inds['EUR_roc20']}"
    assert gp_block["entries"] == 1 and gp_block["wins"] == 1
    # The outcome is also recorded under gp_ensemble (shadow GP is real evidence)
    assert reloaded["by_entry_type"]["gp_ensemble"]["n"] == 1


def test_mean_reversion_close_does_not_credit_indicators(tmp_path, monkeypatch):
    """Sanity: a non-GP close must not create any indicator stats."""
    import hermes_core.engines.decision_cortex as cx
    monkeypatch.setattr(cx, "CORTEX_DIR", tmp_path / "cortex")
    monkeypatch.setattr(cx, "MEMORY_PATH", tmp_path / "cortex" / "m.json")
    monkeypatch.setattr(cx, "EXILE_PATH", tmp_path / "cortex" / "e.json")

    from hermes_core.engines.decision_cortex import Cortex

    cortex = Cortex()
    pos = _make_pos("mean_reversion")
    pos["gp_indicators"] = []
    pos["unrealised_pct"] = -0.5
    ex = evaluate_exit(pos, 0.98, None)
    op = {"GBP/USD": pos}
    L._process_exit("forex", "GBP/USD", 11, pos, 0.98, ex,
                    cortex=cortex, reentry={}, open_positions=op,
                    summary={"exits": []}, alert_fn=None)
    assert Cortex().summary()["indicators"] == {}
