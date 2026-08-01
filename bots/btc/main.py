"""BTC bot entrypoint — shared engine runner (BTC/USDT focus)."""

from __future__ import annotations

import asyncio

from bots._runner import run_bot


def main() -> None:
    asyncio.run(run_bot("btc"))


if __name__ == "__main__":
    main()
