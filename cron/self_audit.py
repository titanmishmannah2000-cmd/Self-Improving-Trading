"""Ops self-audit cron — report-only go/no-go snapshot (Session 15+).

Writes ``{bot}/state/self_audit.json`` and prints a one-line summary.
Never mutates live trading state. Safe to schedule alongside heartbeat.

Exit codes:
  0  report.ok (critical checks passed)
  1  report not ok / unreadable state
"""

from __future__ import annotations

import json
import os
import sys
import time


def main() -> None:
    from hermes_core.engines.self_audit import run as self_audit_run
    from hermes_core.state.atomic_json import atomic_write_json
    from hermes_core.state.paths import bot_state_dir, current_bot

    bot = os.getenv("HERMES_BOT_NAME", current_bot())
    try:
        report = self_audit_run(bot)
    except Exception as exc:  # noqa: BLE001 — cron must not crash the host
        msg = f"[self_audit] {bot}: FAILED {exc!r}"
        print(msg, flush=True)
        sys.exit(1)

    payload = report.to_dict()
    payload["ts"] = time.time()
    payload["source"] = "cron"
    out = bot_state_dir(bot) / "self_audit.json"
    try:
        atomic_write_json(out, payload)
    except OSError as exc:
        print(f"[self_audit] {bot}: write failed {exc!r}", flush=True)
        sys.exit(1)

    print(
        f"[self_audit] {bot}: ok={report.ok} go_nogo={report.go_nogo} "
        f"checks={len(report.checks)} path={out}",
        flush=True,
    )
    # Compact JSON line for log scrapers.
    print(json.dumps({"bot": bot, "ok": report.ok, "go_nogo": report.go_nogo}), flush=True)
    sys.exit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
