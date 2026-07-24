"""Ensure no runtime loader points at polluted archive files (#28)."""

from __future__ import annotations

import json
from pathlib import Path

import hermes_core
from hermes_core.engines import self_audit
from hermes_core.engines.soak_controls import ensure_state_files

ROOT = Path(hermes_core.__file__).resolve().parent


def test_no_polluted_archive_loaders_in_hermes_core():
    offenders = []
    for path in ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "polluted_" in text and "archive" in text:
            # Allow comments / quarantine naming in cleaners + hygiene only.
            if path.name in {
                "clean_hypotheses.py",
                "clean_trades.py",
                "state_hygiene.py",
                "self_audit.py",
            }:
                continue
            if "clean_" in path.name:
                continue
            offenders.append(str(path.relative_to(ROOT.parent)))
    assert offenders == []


def test_self_audit_flags_live_pointer_to_polluted_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    ensure_state_files("forex")
    state = tmp_path / "forex" / "state"
    # Live policy embeds a path into a polluted archive (must fail isolation).
    (state / "policy.json").write_text(
        json.dumps({"reload_from": str(state / "archive" / "hypotheses_polluted_x.jsonl")}),
        encoding="utf-8",
    )
    report = self_audit.run("forex")
    names = {c["name"]: c for c in report.checks}
    assert names["archive_isolated"]["passed"] is False
    assert "live_pointer" in names["archive_isolated"]["detail"]


def test_archive_polluted_files_alone_do_not_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    ensure_state_files("gold")
    state = tmp_path / "gold" / "state"
    arch = state / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    (arch / "hypotheses_polluted_20260101.jsonl").write_text("{}\n", encoding="utf-8")
    # Fresh heartbeat so other go/no-go fields are not the focus.
    import time

    (state / "heartbeat.json").write_text(
        json.dumps({"ts": time.time(), "cycle": 3, "prices": {"XAU/USD": 2400.0}}),
        encoding="utf-8",
    )
    report = self_audit.run("gold")
    names = {c["name"]: c for c in report.checks}
    assert names["archive_isolated"]["passed"] is True
