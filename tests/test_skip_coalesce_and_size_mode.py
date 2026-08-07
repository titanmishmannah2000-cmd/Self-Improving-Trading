"""Skip coalesce + skip-feed dedupe + size_mode stamp for sentient/chart probes."""

from __future__ import annotations

from hermes_core.engines import loop as loop_mod
from hermes_core.engines.size_stamp import normalize_open_size_fields, resolve_size_stamp
from bots._runner import dedupe_skips


def test_log_skip_coalesces_identical_reasons(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    monkeypatch.setattr(loop_mod, "_state_dir", lambda bot: tmp_path / bot / "state")
    (tmp_path / "btc" / "state").mkdir(parents=True)
    loop_mod.run_cycle._skip_latch = {}

    loop_mod._log_skip("btc", "BTC/USDT", 100, "no_signal:sentient:alt_quota")
    loop_mod._log_skip("btc", "BTC/USDT", 101, "no_signal:sentient:alt_quota")
    loop_mod._log_skip("btc", "BTC/USDT", 102, "no_signal:sentient:alt_quota")
    path = tmp_path / "btc" / "state" / "skips.jsonl"
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1

    # After 60 cycles, allow another write
    loop_mod._log_skip("btc", "BTC/USDT", 170, "no_signal:sentient:alt_quota")
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2

    # Reason change always writes
    loop_mod._log_skip("btc", "BTC/USDT", 171, "no_signal:donchian:no_breakout")
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 3


def test_dedupe_skips_keeps_latest_per_reason():
    rows = [
        {"pair": "BTC/USDT", "reason": "no_signal:sentient:alt_quota", "cycle": 1},
        {"pair": "BTC/USDT", "reason": "no_signal:sentient:alt_quota", "cycle": 2},
        {"pair": "BTC/USDT", "reason": "no_signal:donchian:no_breakout", "cycle": 3},
        {"pair": "BTC/USDT", "reason": "no_signal:sentient:alt_quota", "cycle": 4},
    ]
    out = dedupe_skips(rows)
    assert len(out) == 2
    by_reason = {r["reason"]: r for r in out}
    assert by_reason["no_signal:sentient:alt_quota"]["cycle"] == 4
    assert "no_signal:donchian:no_breakout" in by_reason


def test_resolve_size_stamp_sentient_probe():
    s = resolve_size_stamp(
        size_mode="full",
        entry_decision="probe",
        chart_size_mult=1.0,
        size=0.075,
        base_size=0.15,
    )
    assert s["size_mode"] == "probe"
    assert s["size_reason"] == "sentient_probe"
    assert s["probe_fraction"] == 0.5


def test_resolve_size_stamp_chart_soft():
    s = resolve_size_stamp(
        size_mode="full",
        entry_decision="take",
        chart_size_mult=0.5,
        size=0.05,
        base_size=0.1,
    )
    assert s["size_mode"] == "probe"
    assert s["size_reason"] == "chart_soft"
    assert s["probe_fraction"] == 0.5


def test_normalize_legacy_open_full_with_probe_decision():
    out = normalize_open_size_fields(
        {
            "size_mode": "full",
            "entry_decision": "probe",
            "size": 0.075,
            "base_size": 0.15,
            "chart_size_mult": 0.5,
        }
    )
    assert out["size_mode"] == "probe"
    assert out["size_reason"] == "sentient_probe"
    assert out["probe_fraction"] == 0.5
