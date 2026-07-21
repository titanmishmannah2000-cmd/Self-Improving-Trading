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
import json
import sys
import threading
import time
from pathlib import Path

import httpx

from datetime import datetime, timezone


def _now_iso() -> str:
    """UTC ISO timestamp for open-trade pushes (no external dep)."""
    return datetime.now(timezone.utc).isoformat()


from hermes_core.adapters import make_aggregator_fetch, make_default_fetch, seed_history
from hermes_core.config.loader import load_config, load_strategy_for_pair
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


def _push_state(bot: str, cfg: dict, cycle: int, summary: dict | None = None) -> None:
    """POST the bot's full decision-state to /api/ingest/{bot} (fail-soft) [Gap 1].

    This is what actually populates the dashboard's pair cards (regime /
    strategy / blocked-by-conditions) and the overview. The loop only pushes
    prices on its own; the rich state below was never sent before -> empty
    dashboard tabs. We build it from config + the state files the loop writes
    under HERMES_STATE_ROOT/{bot}/state (now on the /data volume).
    """
    if not get_env("DASHBOARD_API_URL", ""):
        return
    client = _get_client()
    if client is None:
        return
    # Real runtime state lives where the loop writes it (HERMES_STATE_ROOT
    # volume, per-bot) — heartbeat/trades/skips below.
    sdir = Path(get_env("HERMES_STATE_ROOT", str(Path(__file__).resolve().parents[2]))) / bot / "state"

    def _read_jsonl(name: str, limit: int = 400):
        p = sdir / name
        if not p.exists():
            return []
        out = []
        try:
            for line in p.read_text(encoding="utf-8").splitlines()[-limit:]:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        except Exception:
            pass
        return out

    try:
        heartbeat = json.loads((sdir / "heartbeat.json").read_text(encoding="utf-8")) if (sdir / "heartbeat.json").exists() else {}
    except Exception:
        heartbeat = {}
    # Discovered + cortex are written by the genetic/cortex engines under
    # repo_root()/state/{discovered,cortex}/. Use the SAME repo_root() the
    # engines use (from hermes_core.config) so the read path matches the write
    # path even under a non-editable install (where __file__ parents[2] !=
    # the package root). /api/discovered expects a flat {pair:[inds]} map
    # (it iterates discovered_json.items() directly), so keep it flat here.
    # NOTE: because indicators are SHARED across pairs (SHARED_INDICATOR_GROUPS),
    # a pair's own file may not exist — its indicators live in the group's
    # anchor pair file. Use the shared-inclusive loader so every configured
    # pair is represented (matches what the entry engine actually trades on).
    from hermes_core.config import repo_root
    from hermes_core.engines.genetic import load_discovered_indicators
    discovered_pairs: dict = {}
    for p in (cfg.get("pairs") or []):
        try:
            inds = load_discovered_indicators(p, include_shared=True)
            if inds:
                # Tag bot/pair for the Discovered tab; do not mutate cached dicts.
                tagged = []
                for ind in inds:
                    d = dict(ind) if isinstance(ind, dict) else {"name": str(ind)}
                    d["_bot"] = bot
                    d["_pair"] = p
                    tagged.append(d)
                discovered_pairs[p] = tagged
        except Exception:
            continue
    discovered = discovered_pairs
    cortex: dict = {}
    # Cortex memory persists per-bot under HERMES_STATE_ROOT/{bot}/state/cortex/.
    # Always bind to THIS bot — Cortex() alone defaults to HERMES_BOT_NAME which
    # can be wrong if a helper process reads another bot's state.
    try:
        from hermes_core.engines.decision_cortex import Cortex
        from hermes_core.engines.policy_engine import PolicyEngine
        cx = Cortex(bot=bot)
        cortex = cx.summary()
        try:
            pol = PolicyEngine().evaluate(
                max(cycle, 0), list(cfg.get("pairs") or []), cortex=cx
            )
            cortex["policy"] = {
                **pol.to_dict(),
                "version": 1,
                "gates": {
                    "suppress_gp": "Bench GP when MR WR ≥ 40% and GP WR < 30%",
                    "suppress_mr": "Bench MR when GP WR ≥ 50%",
                    "priority_discovery": "≥2 exiled indicators → prioritize GP rediscovery",
                    "rollback": "Flag rollback when MR WR < 30% after ≥10 trades",
                },
            }
        except Exception:
            pass
    except Exception:
        cortex = {}
    # recent trades / skips from the jsonl the loop appends
    # Build a real per-pair strategy dict (the dashboard's overview calls
    # .keys() on strategy_json, so it MUST be a mapping, not a list).
    strategies = {}
    for p in (cfg.get("pairs") or []):
        try:
            strategies[p] = load_strategy_for_pair(p, bot)
        except Exception:
            continue
    # Live open positions (persisted across cycles in run_bot) -> dashboard.
    # Use the REAL id/entry_ts from the position — never invent fresh ones each
    # cycle (that broke held-time display and made the dashboard's staleness
    # filter meaningless). entry_type must travel intact for the GP Brain badge.
    open_positions = (summary or {}).get("open_positions") or {}
    recent_open_trades = [
        {
            "id": pos.get("id") or f"{bot}:{pair}:{int(time.time())}",
            "bot": bot,
            "pair": pair,
            "entry_type": pos.get("entry_type", "mean_reversion"),
            "entry_price": pos.get("entry_price"),
            "size": pos.get("size"),
            "entry_ts": pos.get("entry_ts") or _now_iso(),
            "stop_loss_pct": pos.get("stop_loss_pct"),
            "profit_target_pct": pos.get("profit_target_pct"),
            "held_cycles": pos.get("held_cycles", 0),
            "unrealised_pct": pos.get("unrealised_pct"),
            "gp_indicators": pos.get("gp_indicators") or [],
        }
        for pair, pos in open_positions.items()
    ]
    payload = {
        "strategies": strategies,
        "goal": cfg.get("goal"),
        "heartbeat": heartbeat,
        "recent_trades": _read_jsonl("trades.jsonl"),
        "recent_skips": _read_jsonl("skips.jsonl"),
        "discovered": discovered,
        "cortex": cortex,
        "flatlined_pairs": {},
        "recent_open_trades": recent_open_trades,
        "meta": {"oversold_pairs": (summary or {}).get("oversold_pairs", 0)},
    }
    with contextlib.suppress(Exception):
        client.post(
            f"{get_env('DASHBOARD_API_URL', '').rstrip('/')}/api/ingest/{bot}",
            json=payload,
            headers={"X-Ingest-Token": get_env("INGEST_TOKEN", "")},
        )


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


def _discovery_loop(bot: str, pairs: list[str], stop: threading.Event,
                    cortex=None, cfg: dict | None = None) -> None:
    """Background periodic GP discovery (decoupled from the heartbeat cycle).

    Runs _maybe_discover for each pair on its own interval so a slow price API
    or heavy GP evolution can NEVER stall the trade loop. Fully fail-soft but
    logs results so discovery activity is observable in the bot's stdout.

    `cortex` is an optional persistent Cortex instance (B10) used to feed real
    paper-PnL results back into discovered-indicator fitness. When omitted,
    discovery still runs but live feedback is skipped.

    Because the main trade loop can stall (slow fetches) and therefore stop
    pushing its decision-state, we push the discovered indicators HERE after
    each discovery pass — so newly-found indicators reach the dashboard even
    if the trade loop is wedged. [X1]
    """
    from hermes_core.engines.genetic import load_discovered_indicators
    interval = max(int(get_env("DISCOVERY_INTERVAL_S", "3600")), 60)
    # Run an immediate first pass shortly after startup, then every `interval`.
    while not stop.is_set():
        for pair in pairs:
            if stop.is_set():
                return
            try:
                from hermes_core.engines.loop import _maybe_discover
                _maybe_discover(bot, pair, cortex=cortex)
                n = len(load_discovered_indicators(pair))
                print(f"[hermes][discovery] {bot}/{pair}: discovered={n}",
                      flush=True)
            except Exception as exc:  # noqa: BLE001 — never let discovery kill the bot
                print(f"[hermes][discovery] {bot}/{pair}: ERROR {exc!r}",
                      file=sys.stderr, flush=True)
                continue
        # Push discovered state now (decoupled from the trade loop). [X1]
        try:
            if cfg is not None:
                _push_state(bot, cfg, cycle=-1, summary=None)
        except Exception:
            pass
        stop.wait(interval)


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
    _stop = threading.Event()
    # Positions + re-entry cooldowns MUST persist across cycles, or an entry is
    # never carried to its exit and NO trade is ever recorded (the bot "never
    # trades"). These live for the process lifetime; open_positions is also
    # pushed to the dashboard each cycle so live positions are visible.
    open_positions: dict = {}
    reentry: dict = {}
    # oversold_pairs from the previous cycle feeds momentum's multi-pair gate.
    oversold_pairs = 0
    # No volume source in the current aggregate feed -> momentum's vol_above
    # gate stays False until a volume feed is wired (honest, not faked).
    vol_above = False
    # Background GP discovery — fully decoupled from the heartbeat cycle.
    # A persistent Cortex instance lets B10 feed REAL paper-PnL results back
    # into discovered-indicator fitness (it reads the on-disk cortex memory).
    _disc_cortex = None
    try:
        from hermes_core.engines.decision_cortex import Cortex
        _disc_cortex = Cortex()
    except Exception:
        _disc_cortex = None
    _disc = threading.Thread(target=_discovery_loop,
                             args=(bot, pairs, _stop, _disc_cortex, cfg),
                             daemon=True)
    _disc.start()
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
                        run_cycle, bot, cycle, fetch_fn=fetch_fn,
                        open_positions=open_positions, reentry=reentry,
                        oversold_pairs=oversold_pairs, vol_above=vol_above,
                        history_fn=getattr(aggregator, "seed_history_fn", seed_history),
                    )
                except Exception:  # noqa: BLE001 — one pair must not kill the bot
                    print(f"[hermes] {pair} cycle {cycle} errored",
                          file=sys.stderr, flush=True)
                    continue
                # Carry the confluence count forward to the next cycle.
                if isinstance(summary, dict):
                    oversold_pairs = summary.get("oversold_pairs", oversold_pairs)
                # Push the per-cycle price snapshot to the dashboard (real-time
                # for FX/metals; crypto already streamed via on_tick between
                # cycles). Off-thread so a slow dashboard can't stall the loop.
                prices = summary.get("prices") if isinstance(summary, dict) else None
                if isinstance(prices, dict) and prices:
                    _push_prices_threaded(bot, prices)
                # Push the bot's full decision-state (strategies/goal/heartbeat/
                # trades/skips/open positions) so the dashboard's pair cards +
                # overview populate. Fail-soft; a dead dashboard must never
                # stall the bot. [Gap 1]
                _push_state(bot, cfg, cycle, summary)
            await asyncio.sleep(cycle_seconds)
    finally:
        _stop.set()
        if aggregator is not None:
            with contextlib.suppress(Exception):
                await aggregator.aclose()


def main() -> None:
    asyncio.run(run_bot("forex"))


if __name__ == "__main__":
    main()
