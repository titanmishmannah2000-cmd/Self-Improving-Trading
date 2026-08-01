#!/usr/bin/env python3
"""CLI: Phase 0 feed / heartbeat health for focus pairs.

  uv run python -m tools.health_check --bot gold
  uv run python -m tools.health_check --bot forex --max-age 900
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hermes_core.engines.feed_health import check_heartbeat_health  # noqa: E402
from hermes_core.engines.profitability_freeze import (  # noqa: E402
    assert_phase0_freeze,
    focus_pairs_for_bot,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hermes feed/heartbeat health")
    ap.add_argument("--bot", required=True, choices=("forex", "gold", "crypto", "btc"))
    ap.add_argument("--max-age", type=float, default=900.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--with-freeze", action="store_true")
    args = ap.parse_args(argv)

    focus = focus_pairs_for_bot(args.bot)
    report = check_heartbeat_health(args.bot, focus_pairs=focus, max_age_s=args.max_age)
    out: dict = {"health": report}
    if args.with_freeze:
        out["freeze"] = assert_phase0_freeze()

    if args.json:
        print(json.dumps(out, indent=2, default=str))
    else:
        print(f"bot={args.bot} ok={report['ok']} status={report.get('status')} age_s={report.get('age_s')}")
        for pair, info in (report.get("pairs") or {}).items():
            print(f"  {pair}: price={info.get('price')} sane={info.get('sane')} regime={info.get('regime')}")
        if report.get("violations"):
            print(f"  violations: {report['violations']}")
        if args.with_freeze:
            fr = out["freeze"]
            print(f"freeze_ok={fr['ok']} violations={fr['violations']}")

    ok = report["ok"] and (not args.with_freeze or out["freeze"]["ok"])
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
