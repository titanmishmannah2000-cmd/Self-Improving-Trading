#!/usr/bin/env python3
"""Apply Profitability Path Phase 0 freeze to local .env (no secret echo)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"

# Phase 0 target values (non-secret policy flags only).
SET: dict[str, str] = {
    "BOOK_RISK": "1",
    "SOFT_WEIGHTS": "0",
    "KELLY_SIZING": "0",
    "REGIME_SIZING": "0",
    "ENTRY_RANKING": "0",
    "EXIT_INTEL": "0",
    "PROBE_SIZING": "0",
    "SKIP_SHADOW_REFLECT": "0",
    "SKIP_SHADOW_PROMOTE": "0",
    "CRISIS_RECOMMEND": "0",
    "GP_PROMOTE": "0",
    "REFLECT_AUTO_DEPLOY": "0",
    "REFLECT_DEPLOY_STAGE": "prove",
    "MICRO_LIVE": "0",
    "MICRO_LIVE_SIZE_MULT": "0.25",
    "REGIME_DECAY": "0",
    "SCORECARD_COST_PCT": "0.05",
    "GP_PROMOTE_COST_PCT": "0.05",
}


def upsert(text: str, key: str, value: str) -> str:
    pat = re.compile(rf"^(?:#\s*)?{re.escape(key)}\s*=.*$", re.M)
    line = f"{key}={value}"
    if pat.search(text):
        return pat.sub(line, text, count=1)
    # Append under a freeze marker block.
    block = f"\n# Profitability Path Phase 0 (auto-applied)\n{line}\n"
    if "# Profitability Path Phase 0 (auto-applied)" in text:
        # Marker exists but key missing — append after marker line.
        return text.rstrip() + "\n" + line + "\n"
    return text.rstrip() + block


def main() -> int:
    if not ENV.exists():
        print("FAIL: .env missing", file=sys.stderr)
        return 1
    raw = ENV.read_text(encoding="utf-8")
    out = raw
    changed: list[str] = []
    for key, val in SET.items():
        before = out
        out = upsert(out, key, val)
        if out != before:
            changed.append(key)
    if out != raw:
        ENV.write_text(out, encoding="utf-8", newline="\n")
    print(f"phase0_freeze: updated {len(changed)} keys: {', '.join(changed) or '(none)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
