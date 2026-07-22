"""Pytest configuration and shared fixtures."""

from __future__ import annotations

import asyncio
import inspect

import pytest


@pytest.fixture(autouse=True)
def _reset_discord_alert_budget():
    """D9 budget is module-global; reset before each test to avoid cross-test bleed."""
    from hermes_core.notify.discord import reset_alert_budget

    reset_alert_budget()
    yield
    reset_alert_budget()


@pytest.fixture(autouse=True)
def _reset_http_price_stale_state():
    """[GUARD L01] The REST adapter tracks last candle_ts in a process-global
    dict. Reset it before each test so cross-file ordering (e.g. test_wiring
    fetching EUR/USD@1000.0 before test_http_price) can't trip the stale guard."""
    from hermes_core.adapters import http_price

    http_price._last_candle_ts.clear()
    yield
    http_price._last_candle_ts.clear()


@pytest.fixture
def repo_root() -> str:
    from pathlib import Path

    return str(Path(__file__).resolve().parent.parent)


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function):
    """Run ``async def`` tests via ``asyncio.run`` when pytest-asyncio is absent.

    ``pyproject.toml`` declares pytest-asyncio, but a bare host Python may not
    have it installed — without this hook every aggregate async test fails with
    "async def functions are not natively supported".
    """
    testfunction = pyfuncitem.obj
    if not inspect.iscoroutinefunction(testfunction):
        return None
    try:
        import pytest_asyncio  # noqa: F401
    except ImportError:
        kwargs = {
            name: pyfuncitem.funcargs[name]
            for name in pyfuncitem._fixtureinfo.argnames
            if name in pyfuncitem.funcargs
        }
        asyncio.run(testfunction(**kwargs))
        return True
    return None
