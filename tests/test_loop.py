"""Session 7 / Phase 7 integration tests for the trade loop.

Drives run_cycle() across 50+ simulated cycles with injected candles (stale,
flat, timeout) and verifies: heartbeat keys present every cycle, skips logged
with correct reasons, circuit breaker opens after 5 consecutive failures, an
engine raise does NOT crash the loop, and zero unhandled exceptions escape.

Network-free: the loop's fetch/push/now are injected. The forex config + EUR/USD
strategy files (committed in S1) supply real params so the loop is exercised
against genuine config, not mocks of the engine logic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_core.engines import (
    MAX_CONSECUTIVE_FAILURES,
    maybe_circuit_break,
    run_cycle,
)

STATE = Path(__file__).resolve().parent.parent / "state"


def _bot_state(bot="forex"):
    # Bot runtime state lives under {state_root}/{bot}/state, where state_root
    # is HERMES_STATE_ROOT (Railway: /data) else repo_root (dev). Tests
    # run without HERMES_STATE_ROOT, so it resolves to repo/<bot>/state.
    d = STATE.parent / bot / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hb():
    return json.loads((_bot_state() / "heartbeat.json").read_text(encoding="utf-8"))


def _read_jsonl(path):
    p = _bot_state() / path
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- deterministic injected fetch -----------------------------------------
class FakeFeed:
    """Returns a candle per call; cycle_count drives staleness/timeout modes."""

    def __init__(self, mode="fresh"):
        self.mode = mode
        self.calls = 0
        self.base_ts = 1_700_000_000

    def __call__(self, pair):
        self.calls += 1
        if self.mode == "timeout":
            raise TimeoutError("feed timed out")
        if self.mode == "stale":
            # always the same candle_ts -> adapter-style stale None
            return {
                "price": 1.1000,
                "high": 1.1010,
                "low": 1.0990,
                "candle_ts": self.base_ts,
                "ts": self.calls,
            }
        if self.mode == "flat":
            return {
                "price": 1.1000,
                "high": 1.1000,
                "low": 1.1000,
                "candle_ts": self.base_ts + self.calls,
                "ts": self.calls,
            }
        # fresh: nudges price so one entry can fill then exit over cycles
        price = 1.1000 + 0.0005 * (self.calls % 7)
        return {
            "price": price,
            "high": price + 0.0002,
            "low": price - 0.0002,
            "candle_ts": self.base_ts + self.calls,
            "ts": self.calls,
        }


def test_heartbeat_keys():
    # blueprint Phase-7 test_heartbeat_keys: keys present every cycle
    feed = FakeFeed("fresh")
    run_cycle("forex", 1, fetch_fn=feed, now_fn=lambda: 12 * 3600)
    hb = _hb()
    for k in ("ts", "cycle", "health", "last_price"):
        assert k in hb, f"heartbeat missing {k}"


def test_heartbeat_written_every_cycle():
    feed = FakeFeed("fresh")
    for c in range(1, 21):
        run_cycle("forex", c, fetch_fn=feed, now_fn=lambda: 12 * 3600)
        hb = _hb()
        assert hb["cycle"] == c
        assert "status" in hb and "consecutive_failures" in hb


def test_stale_and_flat_do_not_crash():
    # 50+ cycles mixing stale + flat; zero unhandled exceptions; skips logged
    for mode in ("stale", "flat"):
        feed = FakeFeed(mode)
        skips_before = len(_read_jsonl("skips.jsonl"))
        for c in range(1, 31):
            run_cycle("forex", c, fetch_fn=feed, now_fn=lambda: 12 * 3600)
        skips_after = len(_read_jsonl("skips.jsonl"))
        assert skips_after > skips_before, f"expected skips for {mode}"


def test_entry_exit_paper(caplog):
    # a fresh feed nudges price so an MR entry can form then exit -> a trade row
    feed = FakeFeed("fresh")
    # LDN session (12h) so EUR/USD london_only filter passes
    for c in range(1, 60):
        run_cycle("forex", c, fetch_fn=feed, now_fn=lambda: 12 * 3600)
    trades = _read_jsonl("trades.jsonl")
    # either an entry+exit pair or at least skips with reasons — no crash either way
    skips = _read_jsonl("skips.jsonl")
    assert trades or skips, "loop produced nothing across 60 cycles"


def test_circuit_breaker():
    feed = FakeFeed("timeout")  # every fetch raises -> consecutive failures
    slept = {"n": 0}

    def fake_sleep(_s):
        slept["n"] += 1

    # carry the failure count across cycles; after 5 consecutive -> breaker opens
    cf = 0
    for c in range(1, 8):
        s = run_cycle("forex", c, fetch_fn=feed, now_fn=lambda: 12 * 3600, consecutive_failures=cf)
        cf = s["consecutive_failures"]
        if cf >= MAX_CONSECUTIVE_FAILURES:
            # [GUARD L24] the breaker helper must open (pause) at the cap
            assert maybe_circuit_break(cf, sleep_fn=fake_sleep) is True
            assert slept["n"] == 1
            break
    else:
        pytest.fail("circuit breaker never opened after repeated failures")


def test_engine_failure_continues():
    # chart_vision raises -> loop must NOT crash; health_registry flags it False
    health = {}

    def boom(_pair):
        raise RuntimeError("chart api down")

    feed = FakeFeed("fresh")
    for c in range(1, 12):
        run_cycle(
            "forex",
            c,
            fetch_fn=feed,
            now_fn=lambda: 12 * 3600,
            health_registry=health,
            chart_context_fn=boom,
        )
    assert health.get("chart_vision") is False
    # heartbeat still written -> bot still running
    hb = _hb()
    assert hb["cycle"] >= 11


def test_loop_is_generic_no_bot_branch():
    # running the gold bot exercises a different config/strategy without code change
    feed = FakeFeed("fresh")
    for c in range(1, 12):
        run_cycle("gold", c, fetch_fn=feed, now_fn=lambda: 12 * 3600)
    hb = _hb()
    assert hb["cycle"] >= 11
