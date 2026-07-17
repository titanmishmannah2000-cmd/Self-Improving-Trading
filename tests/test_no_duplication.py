"""G-rep: enforce D1 — no bot-specific branching inside hermes_core/."""

from __future__ import annotations

import re
from pathlib import Path

HERMES_CORE = Path(__file__).resolve().parent.parent / "hermes_core"

FORBIDDEN_PATTERNS = [
    re.compile(r"\bif\s+bot\s*=="),
    re.compile(r"\bif\s+bot\s+in\b"),
    re.compile(r"\belif\s+bot\s*=="),
    re.compile(r"\belif\s+bot\s+in\b"),
    re.compile(r"\bif\s+pair\s+in\s*\("),
]


def test_no_bot_branching_in_hermes_core() -> None:
    violations: list[str] = []
    for path in sorted(HERMES_CORE.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN_PATTERNS:
            for match in pattern.finditer(text):
                line_no = text[: match.start()].count("\n") + 1
                violations.append(f"{path}:{line_no}: {match.group()}")

    assert not violations, "D1 violation — bot branching found in hermes_core/:\n" + "\n".join(
        violations
    )
