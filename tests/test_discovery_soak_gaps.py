"""Discovery soak-gap hardening: timeout abandon, pulses, lockout/exile decay, KB rotate."""

from __future__ import annotations

import json
import time

import hermes_core.engines.backtest as bt
import hermes_core.engines.decision_cortex as dc
import hermes_core.engines.gp_intelligence as gp
import hermes_core.engines.loop as loop
from hermes_core.engines import genetic as gen


def test_invent_timeout_does_not_join_hung_worker(monkeypatch, tmp_path):
    """Timeout must abandon the waiter — never hang joining a stuck invent."""
    import threading

    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(loop, "DISCOVERY_INTERVAL_S", 1)
    monkeypatch.setattr(loop, "DISCOVERY_REINVENT_INTERVAL_S", 1)
    loop._DISCOVERY_LAST.clear()
    loop._DISCOVERY_LAST_INVENT.clear()
    loop._DISCOVERY_IN_FLIGHT.clear()
    loop._LAST_DISCOVERY_RUN.clear()
    loop._DISCOVERY_TIMEOUT_STREAK.clear()
    loop._DISCOVERY_TIMEOUT_COOLDOWN_UNTIL.clear()
    loop._DISCOVERY_ADMIT_ZERO_STREAK.clear()
    gen._INVENT_WRITE_TOKEN.clear()

    pulses: list[dict] = []
    release = threading.Event()
    pair = "AUD/USD"
    key = ("forex", pair)

    def fake_profile(bot, pair=None):
        return {
            "interval": "1d",
            "horizon": 10,
            "generations": 1,
            "pop_size": 2,
            "n_islands": 1,
            "timeout_s": 1,
            "period": "1y",
            "max_candles": 300,
            "min_bars": 50,
        }

    def hung_discover(*a, **k):
        release.wait(timeout=120)
        return []

    monkeypatch.setattr("hermes_core.engines.gp_invent_profile.invent_profile", fake_profile)
    monkeypatch.setattr(
        "hermes_core.engines.gp_invent_profile.has_votable_for_regime",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "hermes_core.engines.genetic.load_discovered_indicators",
        lambda *a, **k: [],
    )
    monkeypatch.setattr("hermes_core.engines.genetic.apply_live_feedback", lambda *a, **k: 0)
    monkeypatch.setattr(
        "hermes_core.adapters.price.seed_history_interval_sync",
        lambda *a, **k: [{"price": 1.0 + i * 0.001} for i in range(220)],
    )
    monkeypatch.setattr(loop, "gp_discover", hung_discover)
    monkeypatch.setattr(
        "hermes_core.engines.genetic._save_discovery_pulse",
        lambda p, pulse, write_token=None: pulses.append(dict(pulse)),
    )

    try:
        t0 = time.time()
        loop._maybe_discover("forex", pair, prices=[1.1] * 220)
        elapsed = time.time() - t0
        assert elapsed < 8.0, f"timeout path hung for {elapsed:.1f}s"
        assert any(p.get("status") == "timeout" for p in pulses)
        assert "forex" in loop._LAST_DISCOVERY_RUN
        assert loop._DISCOVERY_TIMEOUT_STREAK.get(key, 0) >= 1
        # Hard-abandon keeps in_flight until the worker exits so the discovery
        # loop can drain zombies before starting another invent.
        assert key in loop._DISCOVERY_IN_FLIGHT
        # Abandoned worker token was invalidated — a new begin token is current.
        assert gen.invent_write_token_current(pair) >= 1
    finally:
        release.set()
        for _ in range(100):
            if key not in loop._DISCOVERY_IN_FLIGHT:
                break
            time.sleep(0.02)
        loop._DISCOVERY_IN_FLIGHT.discard(key)


def test_discover_early_exit_writes_pulse(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_BOT_NAME", "forex")
    pulses: list[dict] = []
    monkeypatch.setattr(
        gen,
        "_save_discovery_pulse",
        lambda pair, pulse, write_token=None: pulses.append(dict(pulse)),
    )
    out = gen.discover("EUR/USD", prices=[1.0] * 20, generations=1, pop_size=2)
    assert out == []
    assert pulses and pulses[0].get("status") == "skipped_short_history"


def test_lockout_wall_clock_decay(monkeypatch, tmp_path):
    monkeypatch.setattr(gp, "GP_STATE", tmp_path / "gp.json")
    monkeypatch.setattr(gp, "LOCKOUT_DECAY_S", 1)
    pair = "EUR/USD"
    for _ in range(3):
        gp.record_loss(pair)
    assert gp.is_locked(pair) is True
    state = gp._load_state(pair)
    state["lockout_ts"][pair] = time.time() - 5
    gp._save_state(state, pair)
    assert gp.is_locked(pair) is False


def test_exile_wall_clock_reinstate(monkeypatch, tmp_path):
    cortex_dir = tmp_path / "cortex"
    cortex_dir.mkdir()
    monkeypatch.setattr(dc, "CORTEX_DIR", cortex_dir)
    monkeypatch.setattr(dc, "EXILE_PATH", cortex_dir / "indicator_exile.json")
    monkeypatch.setattr(dc, "MEMORY_PATH", cortex_dir / "cortex_memory.json")
    monkeypatch.setattr(dc, "EXILE_WALL_CLOCK_S", 1)
    c = dc.Cortex()
    ind = "aged_out"
    for _ in range(5):
        c.record_indicator_outcome(ind, -1.0)
    assert c.is_indicator_exiled(ind) is True
    exiles = dc._load_exiles()
    exiles[ind]["exiled_at"] = time.time() - 10
    dc._save_exiles(exiles)
    assert c.is_indicator_exiled(ind) is False


def test_hypotheses_kb_rotate(tmp_path, monkeypatch):
    path = tmp_path / "hypotheses_kb.jsonl"
    monkeypatch.setattr(bt, "KB_PATH", path)
    monkeypatch.setattr(bt, "KB_MAX_LINES", 5)
    monkeypatch.setattr(bt, "KB_KEEP_LINES", 3)
    lines = [
        json.dumps({"i": i, "pair": "EUR/USD", "param": "x", "old": "", "new": str(i)})
        for i in range(8)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    dropped = bt.rotate_hypotheses_kb(path)
    assert dropped == 5
    kept = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(kept) == 3
    assert json.loads(kept[-1])["i"] == 7


def test_heartbeat_includes_discovery_ts(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    loop._LAST_DISCOVERY_RUN["forex"] = time.time()
    data = loop.write_heartbeat("forex", cycle=1, consecutive_failures=0, last_price=1.1)
    assert data.get("last_discovery_run_ts")
    hb = json.loads((tmp_path / "forex" / "state" / "heartbeat.json").read_text(encoding="utf-8"))
    assert hb.get("last_discovery_run_ts")


def test_seed_fixtures_never_returned_as_discoveries(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_BOT_NAME", "forex")
    path = gen._discovered_path("EUR/USD")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            [
                {"expr": "ta.rsi(close,14)", "source": "seed", "name": "ta.rsi(close,14)"},
                {
                    "expr": "roc(close,10)",
                    "expr_str": "roc(close,10)",
                    "source": "genetic",
                    "oos_corr": 0.4,
                    "backtest_approved": True,
                    "interval": "1d",
                    "horizon": 10,
                },
            ]
        ),
        encoding="utf-8",
    )
    # Force votable path: if seed scrub leaves genetic row, seed must be gone.
    # roc(close,10) may or may not parse as votable depending on grammar —
    # at minimum seed fixture must not appear.
    rows = gen.load_discovered_indicators("EUR/USD", include_shared=False)
    assert all(not gen._is_dashboard_seed_fixture(r) for r in rows)


def test_stale_write_token_cannot_clobber_pulse_or_discovered(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_BOT_NAME", "forex")
    gen._INVENT_WRITE_TOKEN.clear()
    pair = "NZD/USD"
    t1 = gen.begin_invent_write_token(pair)
    gen._save_discovery_pulse(pair, {"status": "ok", "admitted": 1, "v": 1}, write_token=t1)
    gen._save_discovered(
        pair,
        [{"expr": "roc20", "expr_str": "roc20", "oos_corr": 0.5, "backtest_approved": True}],
        write_token=t1,
    )
    t2 = gen.begin_invent_write_token(pair)
    assert t2 != t1
    # Abandoned worker with t1 must not overwrite the newer book/pulse.
    assert gen._save_discovery_pulse(pair, {"status": "stale", "v": 99}, write_token=t1) is None
    assert gen._save_discovered(pair, [{"expr": "bad"}], write_token=t1) is None
    pulse = gen.load_discovery_pulse(pair)
    assert pulse and pulse.get("v") == 1
    rows = gen.load_discovered_indicators(pair, include_shared=False)
    assert rows and rows[0].get("expr") == "roc20"
    # Current token still writes.
    gen._save_discovery_pulse(pair, {"status": "ok", "v": 2}, write_token=t2)
    assert (gen.load_discovery_pulse(pair) or {}).get("v") == 2


def test_chronic_timeout_shrinks_then_cools_down(monkeypatch, tmp_path):
    import threading

    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(loop, "DISCOVERY_INTERVAL_S", 1)
    monkeypatch.setattr(loop, "DISCOVERY_REINVENT_INTERVAL_S", 1)
    monkeypatch.setattr(loop, "DISCOVERY_TIMEOUT_SHRINK_AFTER", 2)
    monkeypatch.setattr(loop, "DISCOVERY_TIMEOUT_SKIP_AFTER", 3)
    monkeypatch.setattr(loop, "DISCOVERY_TIMEOUT_COOLDOWN_S", 3600)
    loop._DISCOVERY_LAST.clear()
    loop._DISCOVERY_LAST_INVENT.clear()
    loop._DISCOVERY_IN_FLIGHT.clear()
    loop._LAST_DISCOVERY_RUN.clear()
    loop._DISCOVERY_TIMEOUT_STREAK.clear()
    loop._DISCOVERY_TIMEOUT_COOLDOWN_UNTIL.clear()
    loop._DISCOVERY_ADMIT_ZERO_STREAK.clear()
    gen._INVENT_WRITE_TOKEN.clear()

    pair = "CAD/USD"
    key = ("forex", pair)
    releases: list[threading.Event] = []
    seen_gens: list[int] = []
    pulses: list[dict] = []

    def fake_profile(bot, pair=None):
        return {
            "interval": "1d",
            "horizon": 10,
            "generations": 40,
            "pop_size": 40,
            "n_islands": 2,
            "timeout_s": 1,
            "period": "1y",
            "max_candles": 300,
            "min_bars": 50,
        }

    def hung_discover(pair_arg, series, **kw):
        seen_gens.append(int(kw.get("generations") or 0))
        ev = threading.Event()
        releases.append(ev)
        ev.wait(timeout=120)
        return []

    monkeypatch.setattr("hermes_core.engines.gp_invent_profile.invent_profile", fake_profile)
    monkeypatch.setattr(
        "hermes_core.engines.gp_invent_profile.has_votable_for_regime",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "hermes_core.engines.genetic.load_discovered_indicators",
        lambda *a, **k: [],
    )
    monkeypatch.setattr("hermes_core.engines.genetic.apply_live_feedback", lambda *a, **k: 0)
    monkeypatch.setattr(
        "hermes_core.adapters.price.seed_history_interval_sync",
        lambda *a, **k: [{"price": 1.0 + i * 0.001} for i in range(220)],
    )
    monkeypatch.setattr(loop, "gp_discover", hung_discover)
    monkeypatch.setattr(
        "hermes_core.engines.genetic._save_discovery_pulse",
        lambda p, pulse, write_token=None: pulses.append(dict(pulse)),
    )

    try:
        # Three hard-abandoned timeouts → streak hits skip + cooldown.
        for _ in range(3):
            loop._DISCOVERY_LAST.pop(key, None)
            loop._maybe_discover("forex", pair, prices=[1.1] * 220)
            assert any(p.get("status") == "timeout" for p in pulses)
            pulses.clear()
            # Release abandoned worker so in_flight clears before the next attempt
            # (production discovery loop drains zombies between pairs).
            if releases:
                releases[-1].set()
                for _j in range(50):
                    if key not in loop._DISCOVERY_IN_FLIGHT:
                        break
                    time.sleep(0.02)
                loop._DISCOVERY_IN_FLIGHT.discard(key)

        assert loop._DISCOVERY_TIMEOUT_STREAK.get(key, 0) >= 3
        assert key in loop._DISCOVERY_TIMEOUT_COOLDOWN_UNTIL
        # After shrink threshold, gens must be reduced from 40.
        assert any(g < 40 for g in seen_gens)

        # Next pass while cooled down should not invent.
        n_before = len(seen_gens)
        loop._DISCOVERY_LAST.pop(key, None)
        loop._maybe_discover("forex", pair, prices=[1.1] * 220)
        assert len(seen_gens) == n_before
        assert any(p.get("status") == "chronic_timeout_backoff" for p in pulses)
    finally:
        for ev in releases:
            ev.set()
        time.sleep(0.05)
        loop._DISCOVERY_IN_FLIGHT.discard(key)


def test_admit_zero_streak_increments(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(loop, "DISCOVERY_INTERVAL_S", 1)
    monkeypatch.setattr(loop, "DISCOVERY_REINVENT_INTERVAL_S", 1)
    monkeypatch.setattr(loop, "DISCOVERY_ADMIT_ZERO_ALERT_AFTER", 0)  # no discord
    loop._DISCOVERY_LAST.clear()
    loop._DISCOVERY_LAST_INVENT.clear()
    loop._DISCOVERY_IN_FLIGHT.clear()
    loop._LAST_DISCOVERY_RUN.clear()
    loop._DISCOVERY_TIMEOUT_STREAK.clear()
    loop._DISCOVERY_TIMEOUT_COOLDOWN_UNTIL.clear()
    loop._DISCOVERY_ADMIT_ZERO_STREAK.clear()
    gen._INVENT_WRITE_TOKEN.clear()

    pair = "CHF/USD"
    key = ("forex", pair)

    def fake_profile(bot, pair=None):
        return {
            "interval": "1d",
            "horizon": 10,
            "generations": 2,
            "pop_size": 4,
            "n_islands": 1,
            "timeout_s": 30,
            "period": "1y",
            "max_candles": 300,
            "min_bars": 50,
        }

    monkeypatch.setattr("hermes_core.engines.gp_invent_profile.invent_profile", fake_profile)
    monkeypatch.setattr(
        "hermes_core.engines.gp_invent_profile.has_votable_for_regime",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "hermes_core.engines.genetic.load_discovered_indicators",
        lambda *a, **k: [],
    )
    monkeypatch.setattr("hermes_core.engines.genetic.apply_live_feedback", lambda *a, **k: 0)
    monkeypatch.setattr(
        "hermes_core.adapters.price.seed_history_interval_sync",
        lambda *a, **k: [{"price": 1.0 + i * 0.001} for i in range(220)],
    )
    monkeypatch.setattr(loop, "gp_discover", lambda *a, **k: [])
    monkeypatch.setattr(
        "hermes_core.engines.genetic._save_discovery_pulse",
        lambda *a, **k: None,
    )
    monkeypatch.setattr("hermes_core.engines.genetic.load_discovery_pulse", lambda *a, **k: {})

    loop._maybe_discover("forex", pair, prices=[1.1] * 220)
    assert loop._DISCOVERY_ADMIT_ZERO_STREAK.get(key) == 1
    loop._DISCOVERY_LAST.pop(key, None)
    loop._maybe_discover("forex", pair, prices=[1.1] * 220)
    assert loop._DISCOVERY_ADMIT_ZERO_STREAK.get(key) == 2
    # Admit success resets streak.
    loop._DISCOVERY_LAST.pop(key, None)
    monkeypatch.setattr(
        loop,
        "gp_discover",
        lambda *a, **k: [
            {
                "expr": "roc20",
                "expr_str": "roc20",
                "oos_corr": 0.5,
                "interval": "1d",
                "horizon": 10,
                "backtest_approved": True,
            }
        ],
    )
    monkeypatch.setattr("hermes_core.engines.genetic._save_discovered", lambda *a, **k: None)
    loop._maybe_discover("forex", pair, prices=[1.1] * 220)
    assert loop._DISCOVERY_ADMIT_ZERO_STREAK.get(key) == 0
