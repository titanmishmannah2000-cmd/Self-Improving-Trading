#!/usr/bin/env python3
"""Phase 1 helper: run reflection-style backtest at 1× and 2× cost stress.

  uv run python -m tools.phase1_backtest --pair EUR/USD --param stop_loss_pct --old 1.5 --new 1.2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from hermes_core.engines.backtest import backtest_with_history  # noqa: E402
from hermes_core.env import get_env  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase 1 cost-stress backtest")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--bot", default=None)
    ap.add_argument("--param", required=True)
    ap.add_argument("--old", type=float, required=True)
    ap.add_argument("--new", type=float, required=True)
    ap.add_argument("--cost", type=float, default=None)
    args = ap.parse_args(argv)

    bot = args.bot
    if bot is None:
        from hermes_core.state.paths import bot_for_pair

        bot = bot_for_pair(args.pair)

    base_cost = args.cost
    if base_cost is None:
        try:
            base_cost = float(get_env("SCORECARD_COST_PCT", "0.05") or 0.05)
        except ValueError:
            base_cost = 0.05

    base = backtest_with_history(
        args.pair,
        args.param,
        args.old,
        args.new,
        bot=bot,
        strict=True,
        cost_pct=base_cost,
        cost_stress_mult=1.0,
    )
    stress = backtest_with_history(
        args.pair,
        args.param,
        args.old,
        args.new,
        bot=bot,
        strict=True,
        cost_pct=base_cost,
        cost_stress_mult=2.0,
    )
    out = {
        "pair": args.pair,
        "bot": bot,
        "param": args.param,
        "cost_1x": {"approved": base.get("approved"), "reason": base.get("reason"), "pnl": base.get("new_pnl")},
        "cost_2x": {
            "approved": stress.get("approved"),
            "reason": stress.get("reason"),
            "pnl": stress.get("new_pnl"),
        },
        "phase1_cost_ok": bool(stress.get("approved")),
    }
    print(json.dumps(out, indent=2, default=str))
    return 0 if out["phase1_cost_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
