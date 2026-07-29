"""One-shot / startup hygiene: keep focus forex pairs on 24h sessions.

Live volume strategies are never overwritten by image seeds (reflection must
stick). That left EUR/USD stuck on ``london_only`` after we moved the seed to
``24h``. This helper patches only ``entry.session_filter`` when it is still a
legacy London lock — no other knobs are touched.
"""

from __future__ import annotations

from typing import Any

import yaml

from hermes_core.config.loader import strategy_yaml_path
from hermes_core.env import get_env

_FOCUS = ("EUR/USD", "GBP/USD")
_LEGACY_LOCKS = frozenset({"london_only", "ny_only", "asian_only"})


def align_forex_focus_sessions(*, bot: str = "forex", force: bool = False) -> list[str]:
    """Set focus-pair ``session_filter`` to ``24h`` when locked to one session.

    Returns human-readable action strings. Fail-soft (never raises).
    """
    if bot != "forex":
        return []
    raw = (get_env("FOREX_SESSION_24H", "1") or "1").strip()
    if not force and raw in ("0", "false", "no", "off"):
        return []
    actions: list[str] = []
    for pair in _FOCUS:
        try:
            path = strategy_yaml_path(pair, bot)
            if not path.exists():
                continue
            data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                continue
            entry = data.get("entry") if isinstance(data.get("entry"), dict) else {}
            cur = str(entry.get("session_filter") or data.get("session_filter") or "").strip()
            if cur == "24h":
                continue
            if cur and cur not in _LEGACY_LOCKS and not force:
                continue
            entry = dict(entry)
            entry["session_filter"] = "24h"
            data["entry"] = entry
            data["session_filter"] = "24h"
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
            tmp.replace(path)
            actions.append(f"{pair}: session_filter {cur or '(missing)'} -> 24h")
        except Exception as exc:  # noqa: BLE001
            actions.append(f"{pair}: skip ({exc!r})")
    return actions
