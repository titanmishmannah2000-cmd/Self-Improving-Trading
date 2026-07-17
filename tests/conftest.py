"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture
def repo_root() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parent.parent)
