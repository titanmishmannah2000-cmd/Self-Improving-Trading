"""Gate check: every env key the code reads is documented in .env.example.

Keeps the "single source of truth" contract honest — when you add a new key
anywhere via get_env(), this fails the gate until .env.example lists it.

Run:  uv run python tools/verify_env_example.py
"""

from __future__ import annotations

import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(REPO, ".env.example")


def main() -> int:
    if not os.path.exists(EXAMPLE):
        print("FAIL: .env.example missing")
        return 1

    with open(EXAMPLE, encoding="utf-8") as fh:
        example_text = fh.read()
    example_keys = set(re.findall(r"^([A-Z][A-Z0-9_]*)\s*=", example_text, re.M))

    # Collect keys read via get_env("NAME", ...) across hermes_core + bots + tools
    used: set[str] = set()
    self_path = os.path.abspath(__file__)
    for root in ("hermes_core", "bots", "tools"):
        rootdir = os.path.join(REPO, root)
        if not os.path.isdir(rootdir):
            continue
        for dirpath, _dirs, files in os.walk(rootdir):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fn)
                if os.path.abspath(fpath) == self_path:
                    continue  # don't scan our own source (contains the pattern literal)
                with open(fpath, encoding="utf-8", errors="ignore") as fh:
                    text = fh.read()
                # match get_env("NAME" or 'NAME' — real calls only
                for m in re.findall(r'get_env\(\s*["\']([A-Z][A-Z0-9_]*)["\']', text):
                    used.add(m)

    missing = sorted(used - example_keys)
    if missing:
        print("FAIL: keys read via get_env() but missing from .env.example:")
        for k in missing:
            print(f"  - {k}")
        return 1

    print(f"env-example: {len(example_keys)} documented, {len(used)} used, in sync")
    return 0


if __name__ == "__main__":
    sys.exit(main())
