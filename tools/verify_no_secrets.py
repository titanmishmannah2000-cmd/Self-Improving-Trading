#!/usr/bin/env python3
"""G-secret: scan for token-shaped credential literals in source files."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ["hermes_core", "bots", "dashboard", "cron", "tests", "tools"]

# Build artifacts / third-party trees must never be scanned (only our source).
SKIP_DIRS = {"node_modules", "dist", "__pycache__", ".git"}
SKIP_FILES = {".env", ".env.example"}

# 40+ char hex/base64-like strings that look like API keys or tokens
TOKEN_PATTERN = re.compile(r'["\']([a-fA-F0-9]{40,}|[A-Za-z0-9+/]{40,}={0,2})["\']')


def main() -> int:
    violations: list[str] = []
    for scan_dir in SCAN_DIRS:
        root = REPO_ROOT / scan_dir
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.name in SKIP_FILES:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue
            if path.suffix not in {".py", ".yaml", ".yml", ".json", ".toml", ".md"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for match in TOKEN_PATTERN.finditer(text):
                line_no = text[: match.start()].count("\n") + 1
                violations.append(f"{path}:{line_no}: possible secret literal")

    if violations:
        print("G-secret violations:")
        for v in violations:
            print(v)
        return 1

    print("G-secret: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
