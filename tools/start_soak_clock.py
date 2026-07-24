"""Write soak_started.json markers and rebuild cortex on a bot volume.

Runs inside Railway containers (tools/ may be missing on older images).
Usage on volume:
  HERMES_BOT_NAME=forex HERMES_STATE_ROOT=/data python - <<'PY'
  ... paste or: uv run python -c \"from tools.start_soak_clock import main; main(['forex'])\"
  PY

Refuses to stamp the clock unless self_audit go_nogo is True (or --force).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _state_dir(bot: str) -> Path:
    """Prefer hermes_core path helpers (respects HERMES_STATE_ROOT / local repo)."""
    try:
        from hermes_core.state.paths import bot_state_dir

        return bot_state_dir(bot)
    except Exception:  # noqa: BLE001 — container without package path tweaks
        import os

        root = Path(
            os.environ.get("HERMES_STATE_ROOT")
            or os.environ.get("HERMES_STATE")
            or "/data"
        )
        return root / bot / "state"


def audit_go_nogo(bot: str) -> tuple[bool, dict]:
    """Return (go_nogo, audit_payload) for one bot."""
    try:
        from hermes_core.engines.self_audit import run

        report = run(bot)
        payload = {
            "go_nogo": bool(getattr(report, "go_nogo", False)),
            "failed_checks": [
                c.get("name")
                for c in (getattr(report, "checks", None) or [])
                if isinstance(c, dict) and not c.get("passed")
            ],
        }
        return bool(payload["go_nogo"]), payload
    except Exception as exc:  # noqa: BLE001
        return False, {"error": repr(exc), "go_nogo": False}


def write_soak_started(bot: str, *, note: str = "cortex_30d_ops") -> Path:
    path = _state_dir(bot) / "soak_started.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "bot": bot,
        "started_at": datetime.now(UTC).isoformat(),
        "started_ts": time.time(),
        "note": note,
        "mode": "paper",
        "days": 30,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def rebuild_bot(bot: str) -> dict:
    """Prefer tools.rebuild_cortex; fall back to state_hygiene rebuild_learning."""
    try:
        from tools.rebuild_cortex import rebuild_bot as _rb

        return _rb(bot, dry_run=False)
    except Exception as exc:  # noqa: BLE001
        from tools.state_hygiene import rebuild_learning

        actions = rebuild_learning(bot)
        return {"bot": bot, "fallback": "state_hygiene", "actions": actions, "error": repr(exc)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stamp 30-day soak clock after go/no-go")
    parser.add_argument("bots", nargs="*", default=["forex", "gold", "crypto"])
    parser.add_argument(
        "--force",
        action="store_true",
        help="Stamp even when self_audit go_nogo is False (ops escape hatch)",
    )
    parser.add_argument(
        "--skip-rebuild",
        action="store_true",
        help="Only stamp the clock (no cortex rebuild)",
    )
    args = parser.parse_args(argv)

    rc = 0
    for bot in args.bots:
        ok, audit = audit_go_nogo(bot)
        if not ok and not args.force:
            print(
                json.dumps(
                    {
                        "bot": bot,
                        "refused": True,
                        "reason": "go_nogo_false",
                        "failed_checks": audit.get("failed_checks") or audit.get("checks"),
                        "hint": "Fix self_audit failures, then re-run; or pass --force",
                    },
                    default=str,
                ),
                flush=True,
            )
            rc = 1
            continue
        info = None if args.skip_rebuild else rebuild_bot(bot)
        note = "cortex_30d_ops" if ok else "forced_despite_go_nogo"
        marker = write_soak_started(bot, note=note)
        print(
            json.dumps(
                {
                    "rebuild": info,
                    "soak_started": str(marker),
                    "go_nogo": ok,
                    "forced": bool(args.force and not ok),
                },
                default=str,
            ),
            flush=True,
        )
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
