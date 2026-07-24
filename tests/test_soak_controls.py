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
    assert halted and reason == "halt:file"

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
