"""Gold bot entrypoint — shared engine runner (wired S19)."""

from __future__ import annotations

import asyncio

from bots._runner import run_bot


def main() -> None:
    asyncio.run(run_bot("gold"))


if __name__ == "__main__":
    main()
