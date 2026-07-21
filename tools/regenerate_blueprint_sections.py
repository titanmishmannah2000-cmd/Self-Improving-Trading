#!/usr/bin/env python3
"""G-blueprint: regenerate Section 3 guard list and Appendix C schemas from code.

S16+: walks hermes_core/ for [GUARD L##] tags to build the guard list, and
renders the dashboard SQLite schema (Appendix H) verbatim from the live DDL in
dashboard/backend/main.py. Implements D10 (docs generated from code, not drift).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "hermes_core"
BACKEND = ROOT / "dashboard" / "backend" / "main.py"


def collect_guards() -> dict[int, list[str]]:
    """Map guard number -> list of source locations bearing [GUARD L##]."""
    guards: dict[int, list[str]] = {}
    if not CORE.exists():
        return guards
    for path in sorted(CORE.rglob("*.py")):
        for ln, text in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in re.finditer(r"\[GUARD L(\d{1,2})\]", text):
                num = int(m.group(1))
                guards.setdefault(num, []).append(f"{path.relative_to(ROOT)}:{ln}")
    return guards


def render_guard_section() -> str:
    guards = collect_guards()
    lines = ["# Section 3 — Guard list (auto-generated from [GUARD L##] tags)", ""]
    if not guards:
        lines.append("_no guard tags found_")
        return "\n".join(lines)
    for num in sorted(guards):
        locs = ", ".join(guards[num])
        lines.append(f"- **L{num:02d}** — referenced at {locs}")
    return "\n".join(lines)


def render_schema_section() -> str:
    """Emit the dashboard SQLite DDL (Appendix H) verbatim from live code."""
    if not BACKEND.exists():
        return "# Appendix H — schema: dashboard/backend/main.py not found"
    src = BACKEND.read_text(encoding="utf-8")
    m = re.search(r"DDL = \"(.*?)\"\"\"", src, re.S)
    if not m:
        return "# Appendix H — DDL constant not found in dashboard/backend/main.py"
    ddl = m.group(1).strip()
    head = "# Appendix H — Dashboard SQLite DDL (generated from dashboard/backend/main.py)"
    return head + "\n\n```sql\n" + ddl + "\n```"


def main() -> int:
    out = []
    out.append(render_guard_section())
    out.append("")
    out.append(render_schema_section())
    text = "\n".join(out)
    target = ROOT / "docs" / "generated_sections.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    previous = target.read_text(encoding="utf-8") if target.exists() else ""
    target.write_text(text, encoding="utf-8")
    print(text)
    print(f"\n[S16] wrote {target.relative_to(ROOT)}", file=sys.stderr)
    if previous and previous.strip() != text.strip():
        print("NOTE: generated_sections.md changed — review diff", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
