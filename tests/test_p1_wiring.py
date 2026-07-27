"""P1 wiring: crisis close-path + periodic self_audit (fail-soft)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import hermes_core.engines.crisis_learning as cl
from hermes_core.engines import loop as loop_mod


@pytest.fixture
def _tmp_crisis(tmp_path, monkeypatch):
    db = tmp_path / "crisis_embeddings.json"
    log = tmp_path / "flatline_log.jsonl"
    monkeypatch.setattr(cl, "DB_PATH", db)
    monkeypatch.setattr(cl, "FLATLINE_LOG", log)
    yield


def _known_series(n=80, start=1.10, drift=-0.0005, vol=0.002, seed=5):
    import random

    rng = random.Random(seed)
    out = [start]
    for _ in range(1, n):
        out.append(out[-1] * (1 + drift + rng.uniform(-vol, vol)))
    return out


def test_process_exit_saves_lived_crisis_on_adverse(_tmp_crisis, monkeypatch):
    """Real close with adverse PnL must call save_adverse_lived_crisis."""
    saved = {}

    def _fake_save(pair, pnl, prices, *, exit_reason=None, **_kw):
        saved["pair"] = pair
        saved["pnl"] = pnl
        saved["exit_reason"] = exit_reason
        saved["n"] = len(prices or [])
        return "lived_EUR/USD_test"

    monkeypatch.setattr(loop_mod, "save_adverse_lived_crisis", _fake_save)
    monkeypatch.setattr(loop_mod, "_log_trade", lambda *_a, **_k: True)
    monkeypatch.setattr(loop_mod, "_maybe_reflect_after_close", lambda *_a, **_k: None)

    pos = {
        "id": "t1",
        "entry_price": 1.10,
        "size": 0.05,
        "unrealised_pct": -2.0,
        "entry_type": "mean_reversion",
        "held_cycles": 3,
        "entry_ts": "2026-01-01T00:00:00+00:00",
    }
    open_positions = {"EUR/USD": pos}
    reentry: dict = {}
    summary: dict = {"exits": []}
    cortex = MagicMock()
    ex = SimpleNamespace(reason="stop_loss", new_stop=None, partial_close_fraction=None)

    loop_mod._process_exit(
        "forex",
        "EUR/USD",
        10,
        pos,
        1.08,
        ex,
        cortex=cortex,
        reentry=reentry,
        open_positions=open_positions,
        summary=summary,
        alert_fn=None,
        prices=_known_series(),
    )

    assert saved["pair"] == "EUR/USD"
    assert saved["pnl"] == -2.0
    assert saved["exit_reason"] == "stop_loss"
    assert summary["lived_crises"][0]["crisis_id"] == "lived_EUR/USD_test"
    assert "EUR/USD" not in open_positions


def test_process_exit_adverse_save_failure_is_soft(_tmp_crisis, monkeypatch):
    monkeypatch.setattr(
        loop_mod,
        "save_adverse_lived_crisis",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(loop_mod, "_log_trade", lambda *_a, **_k: True)
    monkeypatch.setattr(loop_mod, "_maybe_reflect_after_close", lambda *_a, **_k: None)

    pos = {
        "id": "t2",
        "entry_price": 1.10,
        "size": 0.05,
        "unrealised_pct": -3.0,
        "entry_type": "mean_reversion",
        "held_cycles": 1,
    }
    open_positions = {"EUR/USD": pos}
    summary: dict = {"exits": []}
    ex = SimpleNamespace(reason="stop_loss", new_stop=None, partial_close_fraction=None)

    # Must not raise
    loop_mod._process_exit(
        "forex",
        "EUR/USD",
        11,
        pos,
        1.07,
        ex,
        cortex=MagicMock(),
        reentry={},
        open_positions=open_positions,
        summary=summary,
        alert_fn=None,
        prices=_known_series(),
    )
    assert "EUR/USD" not in open_positions


def test_maybe_self_audit_writes_report(tmp_path, monkeypatch):
    from bots import _runner as runner

    monkeypatch.setenv("SELF_AUDIT_EVERY_CYCLES", "5")
    # Force env reader to see the new value
    from hermes_core.env import get_env as _ge

    monkeypatch.setattr(
        runner,
        "get_env",
        lambda k, d="": "5" if k == "SELF_AUDIT_EVERY_CYCLES" else _ge(k, d),
    )

    state = tmp_path / "forex" / "state"
    state.mkdir(parents=True)

    class _R:
        ok = True
        go_nogo = True
        checks = [{"name": "heartbeat", "ok": True}]

        def to_dict(self):
            return {"bot": "forex", "ok": True, "go_nogo": True, "checks": self.checks}

    monkeypatch.setattr(
        "hermes_core.engines.self_audit.run",
        lambda _bot: _R(),
    )
    monkeypatch.setattr(
        "hermes_core.state.paths.bot_state_dir",
        lambda _bot: state,
    )

    runner._maybe_self_audit("forex", 5)
    out = state / "self_audit.json"
    assert out.exists()
    import json

    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert data["cycle"] == 5

    # Off-cadence must not rewrite
    out.write_text("{}", encoding="utf-8")
    runner._maybe_self_audit("forex", 6)
    assert out.read_text(encoding="utf-8") == "{}"


def test_maybe_self_audit_disabled(monkeypatch):
    from bots import _runner as runner

    called = {"n": 0}

    def _boom(_bot):
        called["n"] += 1
        raise AssertionError("should not run")

    monkeypatch.setattr(
        runner,
        "get_env",
        lambda k, d="": "0" if k == "SELF_AUDIT_EVERY_CYCLES" else d,
    )
    monkeypatch.setattr("hermes_core.engines.self_audit.run", _boom)
    runner._maybe_self_audit("forex", 60)
    assert called["n"] == 0
