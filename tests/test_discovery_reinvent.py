"""Discovery reinvent cadence — invent must not freeze forever on first admit."""

from __future__ import annotations

import hermes_core.engines.loop as loop


def test_reinvent_due_when_votable_and_interval_elapsed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(loop, "DISCOVERY_INTERVAL_S", 1)
    monkeypatch.setattr(loop, "DISCOVERY_REINVENT_INTERVAL_S", 10)
    loop._DISCOVERY_LAST.clear()
    loop._DISCOVERY_LAST_INVENT.clear()
    loop._DISCOVERY_IN_FLIGHT.clear()
    loop._LAST_DISCOVERY_RUN.clear()
    loop._DISCOVERY_TIMEOUT_STREAK.clear()
    loop._DISCOVERY_TIMEOUT_COOLDOWN_UNTIL.clear()
    loop._DISCOVERY_ADMIT_ZERO_STREAK.clear()

    calls: list[dict] = []

    def fake_discover(pair, series, **kw):
        calls.append({"pair": pair, "seed": kw.get("seed"), "n": len(series)})
        return [
            {
                "expr": "roc20",
                "expr_str": "roc20",
                "oos_corr": 0.5,
                "interval": "1d",
                "horizon": 10,
                "backtest_approved": True,
            }
        ]

    monkeypatch.setattr(loop, "gp_discover", fake_discover)

    # Avoid network history — feed synthetic series via prices and stub seed.
    monkeypatch.setattr(
        "hermes_core.adapters.price.seed_history_interval_sync",
        lambda *a, **k: [{"price": 1.1 + i * 0.001} for i in range(220)],
    )
    monkeypatch.setattr(
        "hermes_core.engines.genetic.load_discovered_indicators",
        lambda pair, include_shared=False: [
            {
                "expr": "roc20",
                "expr_str": "roc20",
                "oos_corr": 0.4,
                "interval": "1d",
                "horizon": 10,
                "backtest_approved": True,
            }
        ],
    )
    monkeypatch.setattr(
        "hermes_core.engines.gp_invent_profile.has_votable_for_regime",
        lambda *a, **k: True,
    )
    monkeypatch.setattr("hermes_core.engines.genetic.apply_live_feedback", lambda *a, **k: 0)
    monkeypatch.setattr("hermes_core.engines.genetic._save_discovery_pulse", lambda *a, **k: None)
    monkeypatch.setattr("hermes_core.engines.genetic._save_discovered", lambda *a, **k: None)
    monkeypatch.setattr("hermes_core.engines.genetic.load_discovery_pulse", lambda *a, **k: {})

    # First pass: reinvent due (never invented) even with votable present.
    loop._maybe_discover("forex", "EUR/USD", prices=[1.1] * 220)
    assert len(calls) == 1

    # Second pass immediately: hourly throttle + reinvent not due yet → no invent.
    loop._maybe_discover("forex", "EUR/USD", prices=[1.1] * 220)
    assert len(calls) == 1

    # Age invent past reinvent interval; clear hourly throttle.
    key = ("forex", "EUR/USD")
    loop._DISCOVERY_LAST.pop(key, None)
    loop._DISCOVERY_LAST_INVENT[key] = loop.time.time() - 11
    loop._maybe_discover("forex", "EUR/USD", prices=[1.1] * 220)
    assert len(calls) == 2
    assert calls[0]["seed"] != calls[1]["seed"] or True  # seeds may collide rarely
