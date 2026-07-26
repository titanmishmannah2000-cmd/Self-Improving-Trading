"""Cheap per-pair closed-trade reads for reflection / experiment control (#8).

Full-file `trades.jsonl` scans on every close / health poll get expensive under
load. This module keeps a small process-local cache keyed by
``(path, mtime_ns, size, pair)`` so unchanged files are not re-parsed.

Fail-soft: any OS/stat error falls through to a fresh read. Tests that rewrite
the jsonl bump mtime/size and naturally invalidate.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

_LOCK = threading.Lock()
# path -> {"mtime": int, "size": int, "by_pair": {pair: [recs]}}
_CACHE: dict[str, dict] = {}


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))), int(st.st_size)
    except OSError:
        return None


def _load_all(path: Path) -> dict[str, list[dict]]:
    by_pair: dict[str, list[dict]] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return by_pair
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("orphan"):
            continue
        pair = rec.get("pair")
        if not pair:
            continue
        if not (rec.get("exit_reason") or rec.get("reason") or "pnl_pct" in rec):
            continue
        by_pair.setdefault(str(pair), []).append(rec)
    return by_pair


def closed_trades(bot: str, pair: str) -> list[dict]:
    """Closed trades for ``pair`` (same semantics as reflect._closed_trades_for_pair)."""
    from hermes_core.state.paths import bot_state_dir

    path = bot_state_dir(bot) / "trades.jsonl"
    key = str(path)
    info = _stat(path)
    if info is None:
        return []
    mtime, size = info

    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit.get("mtime") == mtime and hit.get("size") == size:
            return list(hit.get("by_pair", {}).get(pair, []))
        by_pair = _load_all(path)
        _CACHE[key] = {"mtime": mtime, "size": size, "by_pair": by_pair}
        return list(by_pair.get(pair, []))


def invalidate(bot: str | None = None) -> None:
    """Drop cache entries (tests / after bulk rewrites)."""
    with _LOCK:
        if bot is None:
            _CACHE.clear()
            return
        from hermes_core.state.paths import bot_state_dir

        key = str(bot_state_dir(bot) / "trades.jsonl")
        _CACHE.pop(key, None)
