"""Soak / discovery monitor cron — Discord alerts + weekly digest.

Usage:
  HERMES_BOT_NAME=forex python -m cron.soak_monitor
  python -m cron.soak_monitor          # audits forex+gold+crypto
  SOAK_MONITOR_FORCE_WEEKLY=1 python -m cron.soak_monitor
"""

from __future__ import annotations

import os

from hermes_core.engines.soak_monitor import main, run_all, run_once


def cli() -> None:
    bot = os.getenv("HERMES_BOT_NAME")
    force = os.getenv("SOAK_MONITOR_FORCE_WEEKLY", "").strip() in {"1", "true", "True"}
    if bot:
        run_once(bot, force_weekly=force)
    else:
        run_all(force_weekly=force)


if __name__ == "__main__":
    main()
