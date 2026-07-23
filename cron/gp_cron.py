"""Weekly GP discovery cron — config-driven (Session 13).

Replaces the S0 scaffold. Each bot service sets HERMES_BOT_NAME; this job
fetches invent-regime history and runs GP discovery for every configured pair
using the per-bot invent profile (TF + horizon + size).
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    from hermes_core.adapters.price import seed_history_interval_sync
    from hermes_core.config import load_config
    from hermes_core.engines.genetic import discover
    from hermes_core.engines.gp_invent_profile import invent_profile

    bot = os.getenv("HERMES_BOT_NAME", "forex")
    cfg = load_config(bot)
    pairs = cfg.get("pairs") or []
    if not pairs:
        print(f"[cron] gp_cron: no pairs for bot={bot}", flush=True)
        return

    prof = invent_profile(bot)
    print(
        f"[cron] gp_cron bot={bot} invent={prof['interval']}/h={prof['horizon']} "
        f"gens={prof['generations']} pop={prof['pop_size']} islands={prof['n_islands']}",
        flush=True,
    )

    for pair in pairs:
        try:
            candles = seed_history_interval_sync(
                pair,
                interval=prof["interval"],
                period=prof["period"],
                max_candles=prof["max_candles"],
            )
            prices = [
                float(c["price"] if isinstance(c, dict) else c)
                for c in candles
            ]
            if len(prices) < int(prof["min_bars"]):
                print(
                    f"[cron] gp_cron: {pair} skipped "
                    f"(<{prof['min_bars']} {prof['interval']} bars)",
                    flush=True,
                )
                continue
            inds = discover(
                pair, prices,
                horizon=int(prof["horizon"]),
                generations=int(prof["generations"]),
                pop_size=int(prof["pop_size"]),
                n_islands=int(prof["n_islands"]),
                interval=str(prof["interval"]),
            )
            print(f"[cron] gp_cron: {pair} -> {len(inds)} indicators", flush=True)
        except Exception as exc:  # noqa: BLE001 — cron must not crash the job
            print(f"[cron] gp_cron: {pair} error -> {exc}", flush=True, file=sys.stderr)

    print(f"[cron] gp_cron complete for bot={bot}", flush=True)


if __name__ == "__main__":
    main()
