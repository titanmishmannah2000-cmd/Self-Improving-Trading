"""Shared bot runner (S19) — async loop driver for local/Railway launch.

Honors ONE env contract (hermes_core/env.get_env) so local `.env` and Railway
deploy read the same keys. The price backend is selected via PRICE_BACKEND
(default yfinance; "aggregate" opts into the Hermes multi-source feed).

Async hosting: the trade loop (run_cycle) stays SYNCHRONOUS and unchanged —
only this wrapper is async so it can host the live websocket price stream
(PriceStream.connect) for real-time crypto ticks, forward those ticks to the
dashboard the instant they arrive, and push the per-cycle price snapshot.
All side effects are fail-soft; a dead dashboard or socket never stops the bot.

Env:
  PRICE_BACKEND        yfinance | aggregate
  HERMES_BOT_NAME      forex | gold | crypto (override via argv for local runs)
  HERMES_CYCLE_SECONDS cycle cadence (default 60)
  DASHBOARD_API_URL    where the dashboard listens (empty -> no price push)
  INGEST_TOKEN         dashboard ingest auth (must match dashboard's INGEST_TOKEN)
  PRICE_WS_URL/_API_KEY  optional real-time crypto WS (else REST poll fallback)

Launch:  uv run python -m bots.forex.main
         uv run python -m bots.gold.main
         uv run python -m bots.crypto.main
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import threading
import time

import httpx

from hermes_core.adapters import make_aggregator_fetch, make_default_fetch
from hermes_core.config.loader import load_config
from hermes_core.engines.loop import run_cycle
from hermes_core.env import get_env, load_env

# One reusable HTTP client per bot process. httpx pools keep-alive connections
# (no per-tick socket churn) and is thread-safe for .post() from the forwarder
# and cycle-push threads. A new client per tick was causing a SYN_SENT pileup
# that filled the server backlog and intermittently refused / requests.
_PUSH_CLIENT: httpx.Client | None = None
_PUSH_CLIENT_LOCK = threading.Lock()


def _get_client() -> httpx.Client | None:
    global _PUSH_CLIENT
    url = get_env("DASHBOARD_API_URL", "").rstrip("/")
    token = get_env("INGEST_TOKEN", "")
    if not url or not token:
        return None
    if _PUSH_CLIENT is None:
        with _PUSH_CLIENT_LOCK:
            if _PUSH_CLIENT is None:
                _PUSH_CLIENT = httpx.Client(timeout=5.0)
    return _PUSH_CLIENT


def _push_prices(bot: str, prices: dict[str, float]) -> None:
    """POST the current price snapshot to the dashboard (fail-soft) [L64]."""
    if not prices:
        return
    client = _get_client()
    if client is None:
        return
    with contextlib.suppress(Exception):  # dashboard down must not stall the bot
        client.post(
            f"{get_env('DASHBOARD_API_URL', '').rstrip('/')}/api/price/{bot}",
            json={"prices": prices},
            headers={"X-Ingest-Token": get_env("INGEST_TOKEN", "")},
        )


# Throttle the websocket tick forwarder: a live crypto feed delivers many ticks
# per second, and pushing every one would replay the connection storm. Cap to at
# most one push per PAIR every 2 s (last-value wins). [GUARD L61]
_TICK_THROTTLE: dict[str, float] = {}
_TICK_THROTTLE_LOCK = threading.Lock()
_TICK_MIN_INTERVAL = 2.0


def _make_fetcher(bot: str, pairs: list[str]):
    """Build a synchronous fetch_fn. If aggregate backend, wire the live
    websocket tick forwarder so crypto ticks push to the dashboard instantly."""
    backend = get_env("PRICE_BACKEND", "yfinance")

    def forward_tick(pair: str, price: float) -> None:
        # Forward a single fresh crypto tick the moment the WS delivers it, but
        # throttled so a tick storm can't flood the dashboard. The persistent
        # pooled client is reused; no new socket per tick. [GUARD L61]
        now = time.monotonic()
        key = f"{bot}:{pair}"
        with _TICK_THROTTLE_LOCK:
            last = _TICK_THROTTLE.get(key, 0.0)
            if now - last < _TICK_MIN_INTERVAL:
                return
            _TICK_THROTTLE[key] = now
        _push_prices(bot, {pair: price})

    if backend == "aggregate":
        agg = make_aggregator_fetch(pairs, on_tick=forward_tick)  # type: ignore[arg-type]
        return agg, agg  # agg(pair) is the fetch_fn; it also has .connect()/.aclose()
    return make_default_fetch(backend=backend, pairs=pairs), None


def _push_prices_threaded(bot: str, prices: dict[str, float]) -> None:
    """Push price snapshot off the event loop so a slow dashboard can't stall it."""
    threading.Thread(target=_push_prices, args=(bot, prices), daemon=True).start()


async def run_bot(bot_name: str) -> None:
    load_env()  # apply .env (fail-soft) before anything reads keys
    # Bot-name resolution precedence: CLI override (argv[1]) > explicit call arg
    # (e.g. bots.crypto.main calls run_bot("crypto")) > HERMES_BOT_NAME in .env.
    # This prevents a stray HERMES_BOT_NAME=forex in .env from silently turning
    # `python -m bots.crypto.main` into a forex run.
    cli = sys.argv[1] if len(sys.argv) > 1 else None
    bot = cli or bot_name or get_env("HERMES_BOT_NAME", "forex")
    cfg = load_config(bot)
    pairs = cfg.get("pairs") or []
    cycle_seconds = int(get_env("HERMES_CYCLE_SECONDS", "60"))
    print(f"[hermes] bot={bot} pairs={pairs} backend={get_env('PRICE_BACKEND','yfinance')}",
          flush=True)

    # Build the price fetcher; for the aggregate backend this also sets up the
    # live crypto websocket with an on_tick forwarder to the dashboard.
    fetch_fn, aggregator = _make_fetcher(bot, pairs)

    # Open the live websocket stream (fail-soft; crypto falls back to REST poll
    # until/if the socket connects). [GUARD L61]
    if aggregator is not None:
        with contextlib.suppress(Exception):
            await aggregator.connect()

    cycle = 0
    try:
        while True:
            cycle += 1
            for pair in pairs:
                # Run the SYNCHRONOUS poll loop in a worker thread. This matters
                # because PriceAggregator.fetch_fn calls asyncio.run() internally
                # (per-call event loop) — which cannot be nested inside the
                # run_bot event loop. to_thread gives each cycle its own thread
                # + fresh loop, so the aggregate backend works under async. [L61]
                try:
                    summary = await asyncio.to_thread(
                        run_cycle, bot, cycle, fetch_fn=fetch_fn
                    )
                except Exception:  # noqa: BLE001 — one pair must not kill the bot
                    print(f"[hermes] {pair} cycle {cycle} errored",
                          file=sys.stderr, flush=True)
                    continue
                # Push the per-cycle price snapshot to the dashboard (real-time
                # for FX/metals; crypto already streamed via on_tick between
                # cycles). Off-thread so a slow dashboard can't stall the loop.
                prices = summary.get("prices") if isinstance(summary, dict) else None
                if isinstance(prices, dict) and prices:
                    _push_prices_threaded(bot, prices)
            await asyncio.sleep(cycle_seconds)
    finally:
        if aggregator is not None:
            with contextlib.suppress(Exception):
                await aggregator.aclose()


def main() -> None:
    asyncio.run(run_bot("forex"))


if __name__ == "__main__":
    main()
