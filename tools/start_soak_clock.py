"""Write soak_started.json markers and rebuild cortex on a bot volume.

Runs inside Railway containers (tools/ may be missing on older images).
Usage on volume:
  HERMES_BOT_NAME=forex HERMES_STATE_ROOT=/data python - <<'PY'
  ... paste or: uv run python -c \"from tools.start_soak_clock import main; main(['forex'])\"
  PY
"""

from __future__ import annotations

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
    bots = argv if argv is not None else sys.argv[1:]
    if not bots:
        bots = ["forex", "gold", "crypto"]
    for bot in bots:
        info = rebuild_bot(bot)
        marker = write_soak_started(bot)
        print(json.dumps({"rebuild": info, "soak_started": str(marker)}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
