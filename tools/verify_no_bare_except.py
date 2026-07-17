#!/usr/bin/env python3
"""G-except: fail on bare except: or except Exception: pass without logging."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HERMES_CORE = REPO_ROOT / "hermes_core"

BARE_EXCEPT = re.compile(r"^\s*except\s*:\s*$")
SILENT_EXCEPT = re.compile(r"^\s*except\s+Exception\s*:\s*pass\s*$")


def main() -> int:
    violations: list[str] = []
    for path in sorted(HERMES_CORE.rglob("*.py")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if BARE_EXCEPT.match(line) or SILENT_EXCEPT.match(line):
                violations.append(f"{path}:{line_no}: {line.strip()}")

    if violations:
        print("G-except violations:")
        for v in violations:
            print(v)
        return 1

    print("G-except: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
