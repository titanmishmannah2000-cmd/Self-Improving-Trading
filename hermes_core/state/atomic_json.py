"""Atomic JSON load/save with corrupt-file quarantine (soak durability).

Writers use temp + ``os.replace`` so a crash mid-write never leaves a half
JSON that the next load would silently treat as empty. Corrupt files are
renamed aside (``*.corrupt-<ts>``) before returning a safe default.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def quarantine_corrupt(path: Path, *, reason: str = "json") -> Path | None:
    """Rename ``path`` to ``path.corrupt-<ts>``. Returns quarantine path or None."""
    if not path.exists():
        return None
    ts = int(time.time())
    dest = path.with_name(f"{path.name}.corrupt-{ts}")
    n = 0
    while dest.exists():
        n += 1
        dest = path.with_name(f"{path.name}.corrupt-{ts}-{n}")
    try:
        path.replace(dest)
        print(
            f"[hermes][atomic_json] quarantined corrupt {path.name} "
            f"({reason}) -> {dest.name}",
            flush=True,
        )
        return dest
    except OSError as exc:
        print(
            f"[hermes][atomic_json] quarantine failed for {path}: {exc!r}",
            flush=True,
        )
        return None


def load_json(
    path: Path,
    *,
    default: Any = None,
    quarantine: bool = True,
) -> Any:
    """Load JSON from ``path``. On decode/OS error, quarantine and return default."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        if quarantine:
            quarantine_corrupt(path, reason=type(exc).__name__)
        return default


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int | None = None,
) -> None:
    """Write JSON atomically: temp sibling + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=indent, default=str)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
