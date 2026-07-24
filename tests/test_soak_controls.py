"""Soak-readiness controls: halt, price sanity, state bootstrap, audit."""

from __future__ import annotations

from hermes_core.engines import self_audit
from hermes_core.engines.loop import run_cycle
from hermes_core.engines.soak_controls import (
    clear_halt,
    ensure_state_files,
    entries_halted,
    price_sanity_book,
    write_halt,
)


class FakeFeed:
    def __init__(self):
        self.calls = 0

    def __call__(self, pair):
        self.calls += 1
        # Pair-specific non-stub FX so sanity does not trip.
        base = {
            "EUR/USD": 1.0850,
            "GBP/USD": 1.2750,
            "AUD/USD": 0.6620,
            "GBP/JPY": 191.20,
            "XAU/USD": 4010.0,
            "XAG/USD": 58.5,
            "BTC/USD": 65000.0,
            "ETH/USD": 3200.0,
        }.get(pair, 1.0850)
        price = base + 0.0001 * (self.calls % 5)
        return {
            "price": price,
            "high": price + 0.0002,
            "low": price - 0.0002,
            "candle_ts": 1_700_000_000 + self.calls,
            "ts": self.calls,
        }


def test_price_sanity_rejects_stub_ladder():
    ok, reason = price_sanity_book(
        {"EUR/USD": 1.1, "GBP/USD": 1.11, "AUD/USD": 1.12},
        {
            "EUR/USD": [1.1, 1.11, 1.12, 1.13] * 3,
            "GBP/USD": [1.1, 1.11, 1.12, 1.13] * 3,
            "AUD/USD": [1.1, 1.11, 1.12, 1.13] * 3,
        },
    )
    assert not ok
    assert "stub" in reason or "ladder" in reason or "scale" in reason


def test_price_sanity_accepts_real_fx():
    ok, reason = price_sanity_book(
        {"EUR/USD": 1.085, "GBP/USD": 1.275, "AUD/USD": 0.66},
    )
    assert ok, reason


def test_ensure_state_files_creates_trades(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    d = ensure_state_files("gold")
    assert (d / "trades.jsonl").exists()
    assert (d / "skips.jsonl").exists()


def test_halt_file_blocks_new_entries(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("HALT_ENTRIES", raising=False)
    ensure_state_files("forex")
    write_halt("forex", "test")
    halted, reason = entries_halted("forex")
    assert halted and ("test" in reason or reason == "halt:file")

    open_positions = {}
    feed = FakeFeed()
    # Seed a long history so indicators work.
    hist = [{"price": 1.08 + i * 0.0001} for i in range(80)]
    summary = run_cycle(
        "forex",
        1,
        fetch_fn=feed,
        history_fn=lambda pair: hist,
        now_fn=lambda: 12 * 3600,
        open_positions=open_positions,
    )
    assert summary.get("halted") is True
    # No new entries while halted (exits path unused — empty book).
    assert summary.get("entries") == []
    clear_halt("forex")


def test_halt_env_blocks(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("HALT_ENTRIES", "1")
    ensure_state_files("forex")
    halted, reason = entries_halted("forex")
    assert halted and reason == "halt:env"


def test_self_audit_flags_missing_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    ensure_state_files("crypto")
    report = self_audit.run("crypto")
    names = {c["name"]: c for c in report.checks}
    assert names["trades_file"]["passed"] is True
    assert names["heartbeat_fresh"]["passed"] is False
    assert report.go_nogo is False


def test_self_audit_archive_orphan(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    # Create goldbot orphan under repo — audit looks at repo_root path.
    from hermes_core.config import repo_root

    orphan = repo_root() / "goldbot" / "state"
    orphan.mkdir(parents=True, exist_ok=True)
    gate = orphan / "gp_promote_gate.json"
    gate.write_text("{}", encoding="utf-8")
    try:
        ensure_state_files("forex")
        report = self_audit.run("forex")
        names = {c["name"]: c for c in report.checks}
        assert names["archive_isolated"]["passed"] is False
    finally:
        import shutil

        shutil.rmtree(repo_root() / "goldbot", ignore_errors=True)


def test_idle_skip_slo_detects_paused(tmp_path):
    import json
    import time

    from hermes_core.engines.soak_controls import idle_skip_slo

    p = tmp_path / "skips.jsonl"
    now = time.time()
    lines = [json.dumps({"ts": now - 60, "reason": "no_signal:rsi"}) for _ in range(25)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = idle_skip_slo(p, hours=6.0)
    assert out["effectively_paused"] is True


def test_halt_recovers_when_idle_clears(tmp_path, monkeypatch):
    import json
    import time

    from hermes_core.engines.soak_controls import maybe_recover_halt, write_halt

    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("HALT_ENTRIES", raising=False)
    ensure_state_files("forex")
    write_halt("forex", "idle_slo:test", alert=False)
    # Fresh non-idle skips.
    skips = tmp_path / "forex" / "state" / "skips.jsonl"
    now = time.time()
    lines = [
        json.dumps({"ts": now - 10, "reason": "no_signal:rsi"}),
        json.dumps({"ts": now - 5, "reason": "rr_guard"}),
    ]
    # Need mixed reasons so not effectively paused — write 25 mixed.
    lines = [
        json.dumps({"ts": now - i, "reason": "rr_guard" if i % 2 else "no_signal:rsi"})
        for i in range(25)
    ]
    skips.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = maybe_recover_halt("forex")
    assert out["recovered"] is True
    assert not (tmp_path / "forex" / "state" / "halt").exists()


def test_book_drawdown_halt(tmp_path, monkeypatch):
    import json

    from hermes_core.engines.soak_controls import book_drawdown_status

    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    ensure_state_files("forex")
    trades = tmp_path / "forex" / "state" / "trades.jsonl"
    rows = [{"pnl_pct": -3.0} for _ in range(6)]
    trades.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    st = book_drawdown_status("forex", {"max_drawdown": 10.0, "failure_below": -10.0})
    assert st["breached"] is True
    assert "equity" in st["reason"] or "dd" in st["reason"]


def test_open_book_persists(tmp_path, monkeypatch):
    from hermes_core.engines.soak_controls import load_open_book, save_open_book

    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    ensure_state_files("gold")
    save_open_book(
        "gold",
        open_positions={"XAU/USD": {"entry_price": 2000.0, "size": 0.1}},
        reentry={},
        cycle=42,
    )
    book = load_open_book("gold")
    assert "XAU/USD" in book["open_positions"]
    assert book["cycle"] == 42


def test_flatline_blocks_new_entries(tmp_path, monkeypatch):
    """L21 novel-regime pause skips entries while exits remain available."""
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("HALT_ENTRIES", raising=False)
    ensure_state_files("forex")
    clear_halt("forex")

    # Force flatline pause sticky state without needing a real novel signature.
    run_cycle._flatline_pause = {"EUR/USD": 3}
    open_positions = {}
    feed = FakeFeed()
    hist = [{"price": 1.08 + i * 0.0001} for i in range(80)]
    summary = run_cycle(
        "forex",
        1,
        fetch_fn=feed,
        history_fn=lambda pair: hist,
        now_fn=lambda: 12 * 3600,
        open_positions=open_positions,
    )
    assert summary.get("entries") == []
    # Pause counter should have decremented and persisted.
    assert int(getattr(run_cycle, "_flatline_pause", {}).get("EUR/USD", 0)) == 2
    run_cycle._flatline_pause = {}


def test_halt_skip_reason_not_no_signal(tmp_path, monkeypatch):
    """Halted bots must log halt skips, not no_signal (idle SLO hygiene)."""
    import json

    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("HALT_ENTRIES", raising=False)
    ensure_state_files("forex")
    write_halt("forex", "operator_test", alert=False)
    feed = FakeFeed()
    hist = [{"price": 1.08 + i * 0.0001} for i in range(80)]
    run_cycle(
        "forex",
        1,
        fetch_fn=feed,
        history_fn=lambda pair: hist,
        now_fn=lambda: 12 * 3600,
        open_positions={},
    )
    skips = (tmp_path / "forex" / "state" / "skips.jsonl").read_text(encoding="utf-8")
    reasons = [json.loads(line)["reason"] for line in skips.splitlines() if line.strip()]
    assert reasons, "expected skip rows while halted"
    assert all("no_signal" not in r for r in reasons)
    assert any("operator_test" in r or r.startswith("halt") for r in reasons)
    clear_halt("forex")


def test_exit_before_guard_on_fetch_error(tmp_path, monkeypatch):
    """Open positions still manage/exit when the quote fetch fails."""
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("HALT_ENTRIES", raising=False)
    ensure_state_files("forex")
    clear_halt("forex")

    open_positions = {
        "EUR/USD": {
            "side": "long",
            "entry_price": 1.08,
            "size": 0.1,
            "stop": 1.07,
            "target": 1.10,
            "entry_cycle": 1,
            "bars_held": 0,
            "signal": "test",
            "entry_type": "mean_reversion",
        }
    }

    def boom(_pair):
        raise RuntimeError("feed down")

    hist = [{"price": 1.08 + i * 0.0001} for i in range(80)]
    run_cycle._mark_fails = {"EUR/USD": 10}
    summary = run_cycle(
        "forex",
        5,
        fetch_fn=boom,
        history_fn=lambda pair: hist,
        now_fn=lambda: 12 * 3600,
        open_positions=open_positions,
    )
    run_cycle._mark_fails = {}
    # Position either closed or still tracked — never silently dropped mid-cycle
    # without going through manage path. Fetch errors must not skip manage.
    assert summary.get("errors", 0) >= 1
    # With enough mark fails, data_halt_exit should close.
    assert "EUR/USD" not in open_positions or summary.get("exits")


def test_circuit_open_flag_in_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    ensure_state_files("forex")
    clear_halt("forex")

    def boom(_pair):
        raise RuntimeError("down")

    summary = run_cycle(
        "forex",
        1,
        fetch_fn=boom,
        history_fn=lambda pair: [],
        now_fn=lambda: 12 * 3600,
        open_positions={},
        consecutive_failures=4,  # +1 per pair will trip L24
    )
    assert summary.get("circuit_open") is True or int(summary.get("consecutive_failures") or 0) >= 5


def test_discovered_corrupt_quarantined(tmp_path, monkeypatch):
    from hermes_core.engines import genetic as gen

    monkeypatch.setattr(gen, "DISCOVERED_DIR", tmp_path)
    bad = tmp_path / "EUR_USD.json"
    bad.write_text("{not-json", encoding="utf-8")
    assert gen._read_indicators_file(bad) == []
    assert not bad.exists()
    assert any(tmp_path.glob("EUR_USD.json.corrupt-*"))


def test_soak_clock_refuses_red_audit(tmp_path, monkeypatch):
    from tools import start_soak_clock as clock

    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    ensure_state_files("forex")
    # No heartbeat → go_nogo false.
    rc = clock.main(["forex"])
    assert rc == 1
    assert not (tmp_path / "forex" / "state" / "soak_started.json").exists()


def test_regime_updates_while_in_trade(tmp_path, monkeypatch):
    """Open positions skip the entry path; regime must still land in heartbeat."""
    import json

    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.delenv("HALT_ENTRIES", raising=False)
    ensure_state_files("gold")
    clear_halt("gold")

    open_positions = {
        "XAU/USD": {
            "side": "long",
            "entry_price": 4000.0,
            "size": 0.1,
            "stop": 3950.0,
            "target": 4100.0,
            "entry_cycle": 1,
            "bars_held": 0,
            "signal": "test",
            "entry_type": "rsi_momentum",
            "entry_regime": "trend",
        }
    }
    # Only XAU open; feed both metals so gold config pairs work.
    prices = {"XAU/USD": 4050.0, "XAG/USD": 58.0}

    def feed(pair):
        p = prices[pair]
        return {"price": p, "high": p + 1, "low": p - 1, "candle_ts": 1_700_000_000, "ts": 1}

    hist = [{"price": 4000.0 + i * 0.5} for i in range(80)]
    run_cycle(
        "gold",
        10,
        fetch_fn=feed,
        history_fn=lambda pair: hist,
        now_fn=lambda: 12 * 3600,
        open_positions=open_positions,
    )
    hb = json.loads((tmp_path / "gold" / "state" / "heartbeat.json").read_text(encoding="utf-8"))
    assert hb.get("regimes", {}).get("XAU/USD") in {"trend", "range"}
    # Sticky in-memory copy for next cycle
    assert getattr(run_cycle, "_regimes", {}).get("XAU/USD") in {"trend", "range"}
