"""Weekly GP discovery cron — config-driven (Session 13).

Replaces the S0 scaffold. Each bot service sets HERMES_BOT_NAME; this job
fetches daily history and runs GP discovery for every configured pair.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    from hermes_core.adapters.price import seed_history_interval_sync
    from hermes_core.config import load_config
    from hermes_core.engines.genetic import discover

    bot = os.getenv("HERMES_BOT_NAME", "forex")
    cfg = load_config(bot)
    pairs = cfg.get("pairs") or []
    if not pairs:
        print(f"[cron] gp_cron: no pairs for bot={bot}", flush=True)
        return

    for pair in pairs:
        try:
            candles = seed_history_interval_sync(pair, interval="1d", period="2y")
            prices = [
                float(c["price"] if isinstance(c, dict) else c)
                for c in candles
            ]
            if len(prices) < 200:
                print(f"[cron] gp_cron: {pair} skipped (<200 daily bars)", flush=True)
                continue
            inds = discover(pair, prices, horizon=60, generations=40, pop_size=40)
            print(f"[cron] gp_cron: {pair} -> {len(inds)} indicators", flush=True)
        except Exception as exc:  # noqa: BLE001 — cron must not crash the job
            print(f"[cron] gp_cron: {pair} error -> {exc}", flush=True, file=sys.stderr)

    print(f"[cron] gp_cron complete for bot={bot}", flush=True)


if __name__ == "__main__":
    main()
