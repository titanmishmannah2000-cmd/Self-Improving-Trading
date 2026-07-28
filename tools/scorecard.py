#!/usr/bin/env python3
"""CLI: Profitability Path scorecard.

  uv run python -m tools.scorecard --bot forex
  uv run python -m tools.scorecard --bot gold --min-n 20 --gate
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow ``python tools/scorecard.py`` from repo root.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hermes_core.engines.profitability_freeze import (  # noqa: E402
    assert_phase0_freeze,
    focus_pairs_for_bot,
)
from hermes_core.engines.scorecard import build_scorecard, phase1_gate  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Hermes profitability scorecard")
    ap.add_argument("--bot", required=True, choices=("forex", "gold", "crypto"))
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--cost", type=float, default=None, help="Round-trip cost %% haircut")
    ap.add_argument("--gate", action="store_true", help="Emit Phase 1 kill/continue")
    ap.add_argument("--freeze-check", action="store_true", help="Assert Phase 0 HIF freeze")
    ap.add_argument("--json", action="store_true", help="Raw JSON only")
    args = ap.parse_args(argv)

    out: dict = {}
    if args.freeze_check:
        out["freeze"] = assert_phase0_freeze()

    card = build_scorecard(args.bot, cost=args.cost, min_n=args.min_n)
    out["scorecard"] = card

    if args.gate:
        out["phase1_gate"] = phase1_gate(
            card,
            focus_pairs=focus_pairs_for_bot(args.bot),
            min_n=args.min_n,
        )

    if args.json:
        print(json.dumps(out, indent=2, default=str))
        return 0 if not args.freeze_check or out.get("freeze", {}).get("ok") else 1

    print(f"bot={args.bot} n_trades={card['n_trades']} cost_pct={card['cost_pct']}")
    fleet = card["fleet"]
    print(
        f"  fleet: n={fleet['n']} wr={fleet['wr']} "
        f"E[R]={fleet['expectancy']} PF={fleet['profit_factor']} "
        f"DD={fleet['max_dd']} -> {fleet['verdict']}"
    )
    for key, s in card["buckets"].items():
        print(
            f"  {key}: n={s['n']} wr={s['wr']} E[R]={s['expectancy']} "
            f"PF={s['profit_factor']} DD={s['max_dd']} -> {s['verdict']}"
            + ("" if s.get("sample_ok") else " (thin sample)")
        )
    if args.freeze_check:
        fr = out["freeze"]
        print(f"freeze_ok={fr['ok']} enabled={fr['enabled']} violations={fr['violations']}")
    if args.gate:
        for key, d in (out.get("phase1_gate") or {}).get("decisions", {}).items():
            print(f"gate {key}: {d['verdict']} ({d['reason']})")
    if args.freeze_check and not out["freeze"]["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
