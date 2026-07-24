"""Ensure no runtime loader points at polluted archive files (#28)."""

from __future__ import annotations

from pathlib import Path

import hermes_core

ROOT = Path(hermes_core.__file__).resolve().parent


def test_no_polluted_archive_loaders_in_hermes_core():
    offenders = []
    for path in ROOT.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "polluted_" in text and "archive" in text:
            # Allow comments mentioning archives in cleaners only outside engines.
            if "clean_" in path.name:
                continue
            offenders.append(str(path.relative_to(ROOT.parent)))
    assert offenders == []
