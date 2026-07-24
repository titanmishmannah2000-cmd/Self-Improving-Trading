"""Universal container entrypoint (Railway single-image, multi-service).

All Railway services deploy the SAME image and run this file as the default
CMD. The service's role is chosen by the HERMES_BOT_NAME env var (set per
service in Railway), so no per-service start command is needed:

    HERMES_BOT_NAME=forex|gold|crypto  -> run that trading bot
    HERMES_BOT_NAME=dashboard          -> run the dashboard web server

This avoids relying on the non-standard railway.json services{} block (Railway
ignores it) while keeping one Dockerfile / one image for everything. [GUARD L62]
"""

from __future__ import annotations

import asyncio
import os
import sys

VALID_BOTS = {"forex", "gold", "crypto"}


def main() -> None:
    role = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HERMES_BOT_NAME", "")).strip()
    if role == "dashboard":
        from dashboard.backend.main import run

        run()
    elif role in VALID_BOTS:
        from bots._runner import run_bot

        asyncio.run(run_bot(role))
    else:
        raise SystemExit(
            f"entrypoint: HERMES_BOT_NAME must be one of {sorted(VALID_BOTS)} or "
            f"'dashboard' (got {role!r})"
        )


if __name__ == "__main__":
    main()
