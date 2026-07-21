"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_discord_alert_budget():
    """D9 budget is module-global; reset before each test to avoid cross-test bleed."""
    from hermes_core.notify.discord import reset_alert_budget

    reset_alert_budget()
    yield
    reset_alert_budget()


@pytest.fixture
def repo_root() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parent.parent)
