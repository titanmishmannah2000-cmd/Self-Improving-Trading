"""Session 17 / Phase 17 — frontend gate.

The dashboard frontend is a React/Vite app tested with vitest (jsdom). This
pytest shim runs `npm test` in dashboard/frontend so the Phase-17 success
criteria are exercisable under the Python harness:

    pytest tests/test_frontend.py -k "overview or selector or refresh or discovered"

It fails closed: if npm/node are missing, or vitest reports any failure, the
pytest test fails (does not silently pass on a green compile).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "dashboard" / "frontend"


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm not on PATH")
def test_frontend_phase17_suite():
    """Run the vitest suite; fail closed on any JS test failure."""
    if not (FRONTEND / "package.json").exists():
        pytest.fail("dashboard/frontend/package.json missing")
    # CI=true keeps npm from printing upgrade noise; captures real exit code.
    # shell=True so Windows resolves npm.cmd correctly.
    proc = subprocess.run(
        "npm test -- --run",
        cwd=FRONTEND,
        env={**__import__("os").environ, "CI": "true"},
        capture_output=True,
        text=True,
        timeout=300,
        shell=True,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"vitest suite failed (rc={proc.returncode})\n"
            f"STDOUT:\n{proc.stdout[-3000:]}\nSTDERR:\n{proc.stderr[-3000:]}"
        )
    assert "Tests  5 passed" in proc.stdout or "5 passed" in proc.stdout
