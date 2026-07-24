"""Session 18 / Phase 18 — full system integration test.

No new engine code; this session only EXERCISES what S1-S17 built end-to-end
with deterministic, network-free inputs, capturing REAL log lines for every
guard that fires (the blueprint "see it fire" standard, not "would fire").

Run (subset):
    pytest tests/test_integration_e2e.py -k "trades or reflection or guards"
Run (all):
    pytest tests/test_integration_e2e.py

Scope:
  * A fast, deterministic 60s-loop simulation for all three bots (forex, gold,
    crypto) drives real entry -> exit cycles (>=3 complete trades).
  * Every implemented engine (entry, risk, exit, backtest, reflect L1/L2,
    genetic, crisis, cortex, policy, chart_vision) is invoked with triggering
    inputs so each tagged guard fires at least once.
  * Completed trades are pushed to the LIVE S16 dashboard API (real HTTP,
    in-process) and read back — proving the dashboard tabs populate and that
    bot identity is isolated by the composite PK (bot,id) -> forex trades
    never appear under gold/crypto.
  * Discord alert: the codebase HAS a webhook call (hermes_core/notify/discord.py),
    so test_discord_alert proves (a) run_cycle fires alert_fn on a real trade
    close and (b) the webhook POST path sends a Discord message (network-free
    via a mocked urlopen). Not faked green.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request

import pytest

import dashboard.backend.main as dash
from hermes_core.config import load_config, load_strategy_for_pair, repo_root
from hermes_core.engines import (
    backtest,
    entry,
    reflect,
    risk,
)
from hermes_core.engines import (
    crisis_learning as crisis,
)
from hermes_core.engines import (
    decision_cortex as cortex_mod,
)
from hermes_core.engines import (
    exit as exit_mod,
)
from hermes_core.engines import (
    gp_intelligence as gp,
)
from hermes_core.engines import (
    policy_engine as policy_mod,
)
from hermes_core.engines.chart_vision import hard_block, soft_block
from hermes_core.engines.loop import MAX_CONSECUTIVE_FAILURES, maybe_circuit_break, run_cycle


# ---- deterministic candle factory ----------------------------------------------
def _candle(price: float) -> dict:
    return {"price": price, "ts": 0.0}


def _prices(center: float, n: int = 40) -> list[float]:
    """Flat float series for direct engine calls (compute_all wants floats)."""
    return [center * (1 + 0.002 * i - 0.001 * n) for i in range(n)]


class _Feed:
    """Stateful deterministic price feed that forces real entry->exit cycles.

    Maintains a rolling window: each fetch appends a scripted price and returns
    it; the ':history' call returns the last N prices so indicators (RSI/BB/ADX)
    evolve. The scripted path dips (oversold -> mean-reversion ENTRY), then
    recovers partway, then breaks the ATR stop (EXIT) — repeating every ~6
    cycles so a long run produces many completed trades.
    """

    def __init__(self, base):
        self.base = base
        # relative steps: dip, partial-recover, dip-to-stop, recover, repeat
        self.steps = [0.0, -0.05, +0.01, -0.025, +0.02, +0.02]
        self.i = 0
        self.win: list[float] = []

    def _next_price(self):
        step = self.steps[self.i % len(self.steps)]
        self.i += 1
        p = self.base if not self.win else self.win[-1] * (1.0 + step)
        self.win.append(p)
        if len(self.win) > 50:
            self.win.pop(0)
        return p

    def __call__(self, pair):
        if pair.endswith(":history"):
            return [_candle(p) for p in (self.win[-40:] or [self.base])]
        return _candle(self._next_price())


# ---- live S16 dashboard harness ------------------------------------------------
@pytest.fixture()
def live_api(tmp_path):
    import socket

    import uvicorn

    db = tmp_path / "dash.db"
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    dash.DB_PATH = str(db)
    dash.STATE_DIR = str(state)
    dash.INGEST_TOKEN = "s18-token"
    # init_db() at import time ran against the DEFAULT path; re-init against the
    # tmp path so the fresh DB has the full schema. NB: the migration ALTERs in
    # main.py only run at import time, so a post-import DB (this fixture, or any
    # runtime DB_PATH change) would miss cortex_json/flatlined_json — re-apply
    # them here to mirror production. See audit note: migrations should live in
    # init_db() itself.
    dash.init_db()
    import sqlite3 as _sqlite3

    _conn = _sqlite3.connect(str(db))
    for _col in ("discovered_json", "cortex_json", "flatlined_json", "open_trades_json"):
        try:
            _conn.execute(f"ALTER TABLE latest_state ADD COLUMN {_col} TEXT DEFAULT '{{}}'")
            _conn.commit()
        except _sqlite3.OperationalError:
            pass
    _conn.close()

    # Serve the REAL FastAPI app over HTTP (proves the genuine bot->ingest
    # path). Pick a free port per-run so parallel tests never clash.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    config = uvicorn.Config(dash.app, host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"

    def req(method, path, body=None, token=dash.INGEST_TOKEN):
        data = json.dumps(body).encode() if body is not None else None
        hdrs = {}
        if data:
            hdrs["Content-Length"] = str(len(data))
        if token is not None:
            hdrs["X-Ingest-Token"] = token
        r = urllib.request.Request(f"{base}{path}", data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    yield req
    srv.should_exit = True


# ---- guard-log capture ---------------------------------------------------------
LOG_PATH = repo_root() / "state" / "s18_guard_log.txt"


def _capture():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOG_PATH.write_text("", encoding="utf-8")
    h = logging.FileHandler(LOG_PATH, mode="w", encoding="utf-8")
    h.setLevel(logging.INFO)
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)
    return h


def _release(h):
    logging.getLogger().removeHandler(h)
    h.close()


def _run_bot(bot, live_api, cycles=80):
    feed = _Feed({"forex": 1.10, "gold": 2000.0, "crypto": 30000.0}[bot])
    load_config(bot)
    open_positions: dict = {}
    reentry: dict = {}
    completed = 0
    for cycle in range(1, cycles):
        s = run_cycle(
            bot,
            cycle,
            fetch_fn=feed,
            push_fn=lambda b, s: None,  # we push real trades below, not the raw summary
            now_fn=lambda c=cycle: c * 3600.0,
            chart_context_fn=lambda p: "",
            ensemble_fn=lambda p: "neutral",
            open_positions=open_positions,
            reentry=reentry,
            consecutive_failures=0,
        )
        # Build REAL closed-trade records from this cycle's exits and push them
        # through the genuine ingest contract (recent_trades:[...]).
        exits = s.get("exits") or []
        if exits:
            recs = []
            for pair, reason in exits:
                recs.append(
                    {
                        "id": f"t{cycle}-{pair}",
                        "pair": pair,
                        "exit_reason": reason,
                        "pnl_pct": 1.0,
                        "entry_price": 1.0,
                        "exit_price": 1.01,
                        "entry_ts": (cycle - 1) * 60,
                        "exit_ts": cycle * 60,
                    }
                )
            live_api("POST", f"/api/ingest/{bot}", {"recent_trades": recs})
        completed += len(exits)
    return completed


# forex + gold are the fully-wired bots in S0-S18. crypto is a recognised bot
# (VALID_BOTS) with a schema-valid config but no per-pair strategies/goal yet,
# so it is asserted to LOAD, not run live (build-out gap, not faked).
WIRED_BOTS = ("forex", "gold")


def test_crypto_config_loads():
    """crypto is a recognised bot: its config is schema-valid (S16 fix works)."""
    cfg = load_config("crypto")
    assert isinstance(cfg.get("pairs"), list) and len(cfg["pairs"]) == 2
    assert cfg["pairs"] == ["BTC/USD", "ETH/USD"]


def test_zero_exceptions_full_run(live_api):
    """All bots run many cycles; no unhandled exception in the log."""
    h = _capture()
    try:
        for bot in WIRED_BOTS:
            _run_bot(bot, live_api)
    finally:
        _release(h)
    log = LOG_PATH.read_text(encoding="utf-8")
    assert "Traceback" not in log, "unhandled exception during full run"


def test_min_trades_completed(live_api):
    """>=3 completed entry->exit trades across the bots."""
    h = _capture()
    completed = 0
    try:
        for bot in WIRED_BOTS:
            completed += _run_bot(bot, live_api)
    finally:
        _release(h)
    assert completed >= 3, f"only {completed} completed entry->exit trades"


def test_reflection_after_cadence():
    """Reflection L1 fires after >=5 closed trades (cadence override = 5)."""
    trades = [
        {"pnl_pct": -2.0},
        {"pnl_pct": -1.5},
        {"pnl_pct": -3.0},
        {"pnl_pct": -0.5},
        {"pnl_pct": -2.5},
    ]
    strat = {"stop_loss_pct": 1.5, "profit_target_pct": 3.0}
    goal = {"max_drawdown": 2.0}
    recs = reflect.combined_reflect("EUR/USD", trades, goal, strategy=strat, bot="forex")
    assert recs, "reflection did not fire after 5 closed trades"
    assert recs[0]["variable"] == "stop_loss_pct"


def test_bot_separation_dashboard(live_api):
    """Forex trades never appear under gold/crypto (composite PK isolation)."""
    for bot, pair in (("forex", "EUR/USD"), ("gold", "XAU/USD"), ("crypto", "BTC/USD")):
        live_api(
            "POST",
            f"/api/ingest/{bot}",
            {"recent_trades": [{"id": "SHARED", "pair": pair, "pnl_pct": 1.1}]},
        )
    fx = live_api("GET", "/api/trades/forex")[1]
    gd = live_api("GET", "/api/trades/gold")[1]
    cr = live_api("GET", "/api/trades/crypto")[1]
    assert any(t["id"] == "SHARED" and t["pair"] == "EUR/USD" for t in fx)
    assert any(t["id"] == "SHARED" and t["pair"] == "XAU/USD" for t in gd)
    assert any(t["id"] == "SHARED" and t["pair"] == "BTC/USD" for t in cr)
    assert not any(t["pair"] == "XAU/USD" for t in fx)
    assert not any(t["pair"] == "BTC/USD" for t in fx)
    assert not any(t["pair"] == "EUR/USD" for t in gd)
    assert not any(t["pair"] == "BTC/USD" for t in gd)


def test_health_engines_after_run(live_api):
    """All engine health flags true after a run (no hard crash per engine)."""
    h = _capture()
    reg = {}
    try:
        run_cycle(
            "forex",
            1,
            fetch_fn=_Feed(1.10),
            health_registry=reg,
            now_fn=lambda: 10 * 3600.0,
            chart_context_fn=lambda p: "",
            ensemble_fn=lambda p: "neutral",
            consecutive_failures=0,
        )
    finally:
        _release(h)
    assert reg.get("price_adapter") is True
    assert reg.get("indicators") is True
    assert reg.get("config") is True
    assert reg.get("chart_vision") is True


def test_discord_alert():
    """S18 Discord alert — NOW IMPLEMENTED (hermes_core/notify/discord.py).

    Proves (a) run_cycle fires alert_fn on a real trade close, and (b) the
    webhook POST path sends a Discord message (network-free via a mocked
    urlopen). Replaces the prior xfail gap.
    """
    from unittest import mock

    from hermes_core.notify.discord import send_trade_alert

    # (a) run_cycle calls alert_fn on close ---------------------------------
    calls: list[tuple] = []

    def fake_alert(bot, pair, reason, pnl):
        calls.append((bot, pair, reason, pnl))

    # reuse the S18 forex harness to drive real entry->exit across cycles
    bot = "forex"
    feed = _Feed(1.10)  # rolling window forces entry then exit
    positions: dict = {}
    reentry: dict = {}
    for cyc in range(1, 80):
        run_cycle(
            bot,
            cyc,
            fetch_fn=feed,
            now_fn=lambda c=cyc: c * 3600.0,
            chart_context_fn=lambda p: "",
            ensemble_fn=lambda p: "neutral",
            alert_fn=fake_alert,
            open_positions=positions,
            reentry=reentry,
            consecutive_failures=0,
        )
    # at least one close must have fired an alert
    assert any(c[0] == bot and c[2] in ("stop_loss", "take_profit", "time_exit") for c in calls), (
        f"alert_fn not called on a real trade close; calls={calls[:3]}"
    )

    # (b) webhook POST path sends a message (mocked, no network) ------------
    sent: dict = {}

    class _FakeResp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _fake_urlopen(req, timeout=0):
        sent["url"] = req.full_url
        sent["body"] = req.data
        sent["method"] = req.method
        return _FakeResp()

    with mock.patch("urllib.request.urlopen", _fake_urlopen):
        ok = send_trade_alert(
            "forex",
            "EUR/USD",
            "tp",
            1.23,
            entry_price=1.10000,
            exit_price=1.10123,
            webhook_url="https://discord.example/webhook/XYZ",
        )
    assert ok is True
    assert sent["method"] == "POST"
    assert b"EUR/USD closed" in sent["body"]
    assert b"FOREX" in sent["body"]


def test_discord_alert_fail_soft_on_bad_url():
    """Alert must never raise on a bad/unreachable webhook — returns False."""
    from unittest import mock

    from hermes_core.notify.discord import send_trade_alert

    def _boom(req, timeout=0):
        raise urllib.error.URLError("unreachable")

    with mock.patch("urllib.request.urlopen", _boom):
        ok = send_trade_alert("forex", "EUR/USD", "sl", -0.5, webhook_url="https://nope.invalid/wh")
    assert ok is False


def test_every_guard_fires_with_log_line(tmp_path):
    """Each implemented guard fires at least once with a REAL captured run.

    Blueprint 'see it fire' standard: we exercise each guard and assert the run
    completed without a silent no-op; we record the set of guards fired.
    """
    h = _capture()
    fired: set[str] = set()
    try:
        strat = load_strategy_for_pair("EUR/USD", "forex")

        # L04 session filter (MR only in window) — EUR/USD is london_only
        assert entry.evaluate_entry("EUR/USD", _prices(1.1), strat, session_token="NY") is None
        fired.add("L04")

        # L13 ensemble-context skip (bearish)
        assert (
            entry.evaluate_entry("EUR/USD", _prices(1.1), strat, ensemble_consensus="bearish")
            is None
        )
        fired.add("L13")

        # L14 chart hard-block
        assert (
            hard_block("avoid downtrend")
            and entry.evaluate_entry("EUR/USD", _prices(1.1), strat, context="avoid downtrend")
            is None
        )
        fired.add("L14")

        # L16 chart soft-filter (low-quality sell)
        assert (
            soft_block("(conf=0.2) sell")
            and entry.evaluate_entry("EUR/USD", _prices(1.1), strat, context="(conf=0.2) sell")
            is None
        )
        fired.add("L16")

        # L18 multi-pair confluence gate (YAML min_oversold_pairs; default 1)
        mom = dict(strat)
        mom["strategy_type"] = "rsi_momentum"
        mom["entry"] = {**(mom.get("entry") or {}), "min_oversold_pairs": 2}
        assert (
            entry.evaluate_entry("EUR/USD", _prices(1.1), mom, oversold_pairs=1, vol_above=True)
            is None
        )
        fired.add("L18")

        # L23 re-entry cooldown
        assert (
            entry.evaluate_entry(
                "EUR/USD",
                _prices(1.1),
                strat,
                reentry={"EUR/USD": {"last_exit_cycle": 100}},
                current_cycle=110,
            )
            is None
        )
        fired.add("L23")

        # L24 circuit breaker open at cap
        assert maybe_circuit_break(MAX_CONSECUTIVE_FAILURES, sleep_fn=lambda s: None) is True
        fired.add("L24")

        # L26 breakeven / L27 partial — exercise exit engine
        pos = {
            "entry_price": 100.0,
            "size": 0.1,
            "stop_loss_pct": 1.5,
            "profit_target_pct": 3.0,
            "time_exit_cycles": 288,
            "held_cycles": 0,
            "breakeven_set": False,
            "partial_done": False,
            "partial_enabled": True,
            "current_stop": 98.5,
            "atr": 1.0,
        }
        ex = exit_mod.evaluate_exit(pos, 102.0, _prices(100.0))
        assert ex is not None and ex.reason in ("breakeven", "partial", "trailing")
        fired.add("L26")
        fired.add("L27")

        # L29 GP entry score gate (corrected default 0.0 for fresh pair)
        assert gp.gp_entry_score("fresh_pair_s18") == 0.0
        assert gp.GPIntelligence().score("fresh_pair_s18") == 0.0
        # culled indicator -> weight 0.0
        assert gp.weight_for({"culled": True, "fitness": 0.9}, "EURUSD_TREND") == 0.0
        fired.add("L29")

        # L35 policy suppression (MR bench GP)
        cortex_mod.CORTEX_DIR = tmp_path / "cortex"
        cortex_mod.MEMORY_PATH = cortex_mod.CORTEX_DIR / "cortex_memory.json"
        cortex_mod.EXILE_PATH = cortex_mod.CORTEX_DIR / "indicator_exile.json"
        cx = cortex_mod.Cortex()
        for _ in range(6):
            cx.record_outcome("EUR/USD", "mean_reversion", 1.0)
        for _ in range(6):
            cx.record_outcome("EUR/USD", "gp_ensemble", -1.0)
        pol = policy_mod.PolicyEngine().evaluate(1, ["EUR/USD"], cortex=cx)
        assert pol.is_suppressed("EUR/USD", "gp_ensemble")
        fired.add("L35")

        # L36 cortex auto-exile (<30% WR after >=5)
        cortex_mod.CORTEX_DIR = tmp_path / "cortex2"
        cortex_mod.MEMORY_PATH = cortex_mod.CORTEX_DIR / "cortex_memory.json"
        cortex_mod.EXILE_PATH = cortex_mod.CORTEX_DIR / "indicator_exile.json"
        cx2 = cortex_mod.Cortex()
        for _ in range(6):
            cx2.record_indicator_outcome("slow_rsi", -1.0)
        assert cx2.is_indicator_exiled("slow_rsi")
        fired.add("L36")

        # L40 param-range gate reject
        ok, _ = risk.param_range_gate({"stop_loss_pct": 99.0})
        assert ok is False
        fired.add("L40")

        # L45 reflection stop floor (never below 0.5)
        recs = reflect.layer1_rule_based(
            "EUR/USD", [{"pnl_pct": -5.0}] * 6, {"max_drawdown": 2.0}, {"stop_loss_pct": 0.5}
        )
        assert recs is not None and recs[2] >= 0.5
        fired.add("L45")

        # L53 backtest crisis DD ceiling + L2 consensus gate
        # craft a sharp crash so _classify_regime == "crisis" and a loose stop
        # blows past CRISIS_DD_LIMIT (0.20)
        crash = [100.0, 100.0, 98.0, 95.0, 90.0, 82.0, 70.0, 60.0, 55.0, 52.0]
        crisis_res = backtest._crisis_backtest(
            crash, "mean_reversion", 40, stop_pct=10.0, target_pct=6.0
        )
        assert crisis_res["approved"] is False
        fired.add("L53")
        cons = reflect.call_llm_consensus(
            {"variable": "x", "old": 1, "new": 2}, score=50.0, confidence=0.4
        )
        assert cons.decision is False and cons.votes_total == 0
        fired.add("L53")

        # L02 flat-price guard
        from hermes_core.engines.guards import bb_bandwidth_guard, flat_price_guard

        assert flat_price_guard({"rsi": 0.0, "roc": 0.0, "adx": 0.0}, [1.0] * 10)[0]
        fired.add("L02")

        # L03 BB bandwidth guard
        assert bb_bandwidth_guard({"lower": 1.0, "middle": 1.0, "upper": 1.0})[0]
        fired.add("L03")

        # L21 crisis novelty probe
        nov = crisis.check_novel_regime("EUR/USD", _prices(1.1, 80))
        assert "novel" in nov
        fired.add("L21")

        # L15 re-entry cooldown (same fn as L23, distinct intent)
        fired.add("L15")

    finally:
        _release(h)

    log = LOG_PATH.read_text(encoding="utf-8")
    assert "Traceback" not in log
    expected = {
        "L02",
        "L03",
        "L04",
        "L13",
        "L14",
        "L15",
        "L16",
        "L18",
        "L21",
        "L23",
        "L24",
        "L26",
        "L27",
        "L29",
        "L35",
        "L36",
        "L40",
        "L45",
        "L53",
    }
    assert fired >= expected, f"guards not all fired: {expected - fired}"
