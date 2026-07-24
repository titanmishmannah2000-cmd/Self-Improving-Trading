"""One-shot cleaner: archive polluted hypotheses.jsonl lines, keep real ones.

Pollution signatures (from audit item 12):
  * L1 fixture with ``max_dd 2.00%`` (live goal is 10.0)
  * Dashboard seed stub: mode=shadow + reasoning, no status
  * rsi_period / improve WR stub
"""

from __future__ import annotations

import json
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def is_polluted(rec: dict) -> bool:
    reason = str(rec.get("reason") or "")
    if "max_dd 2.00%" in reason or "max_dd 2.0%" in reason:
        return True
    if rec.get("mode") == "shadow" and "reasoning" in rec and "status" not in rec:
        return True
    return bool(rec.get("variable") == "rsi_period" and rec.get("reasoning") == "improve WR")


def clean_file(path: Path, stamp: str) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    kept: list[str] = []
    dumped: list[str] = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            dumped.append(ln)
            continue
        (dumped if is_polluted(rec) else kept).append(ln)
    if dumped:
        arch_dir = path.parent / "archive"
        arch_dir.mkdir(parents=True, exist_ok=True)
        arch = arch_dir / f"hypotheses_polluted_{stamp}.jsonl"
        arch.write_text("\n".join(dumped) + "\n", encoding="utf-8")
    path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    return len(kept), len(dumped)


def main() -> None:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    targets = [
        ROOT / "state" / "hypotheses.jsonl",  # legacy stray root (tests sometimes wrote here)
        ROOT / "forex" / "state" / "hypotheses.jsonl",
        ROOT / "gold" / "state" / "hypotheses.jsonl",
        ROOT / "crypto" / "state" / "hypotheses.jsonl",
        ROOT / "bots" / "forex" / "state" / "hypotheses.jsonl",
        ROOT / "bots" / "gold" / "state" / "hypotheses.jsonl",
        ROOT / "bots" / "crypto" / "state" / "hypotheses.jsonl",
    ]
    for path in targets:
        if not path.exists():
            print(f"skip missing {path.relative_to(ROOT)}")
            continue
        kept, archived = clean_file(path, stamp)
        print(f"{path.relative_to(ROOT)}: kept={kept} archived={archived}")


if __name__ == "__main__":
    main()
