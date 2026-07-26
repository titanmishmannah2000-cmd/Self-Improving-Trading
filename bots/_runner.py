"""Shared bot runner (S19) — async loop driver for local/Railway launch.

Honors ONE env contract (hermes_core/env.get_env) so local `.env` and Railway
deploy read the same keys. The price backend is selected via PRICE_BACKEND
(default aggregate; "yfinance" falls back to Yahoo scrape).

Async hosting: the trade loop (run_cycle) stays SYNCHRONOUS and unchanged —
only this wrapper is async so it can host the live websocket price stream
(PriceStream.connect) for real-time crypto ticks, forward those ticks to the
dashboard the instant they arrive, and push the per-cycle price snapshot.
All side effects are fail-soft; a dead dashboard or socket never stops the bot.

Env:
  PRICE_BACKEND        aggregate | yfinance | http
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
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def _now_iso() -> str:
    """UTC ISO timestamp for open-trade pushes (no external dep)."""
    return datetime.now(UTC).isoformat()


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
    sdir = (
        Path(get_env("HERMES_STATE_ROOT", str(Path(__file__).resolve().parents[2]))) / bot / "state"
    )

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
        heartbeat = (
            json.loads((sdir / "heartbeat.json").read_text(encoding="utf-8"))
            if (sdir / "heartbeat.json").exists()
            else {}
        )
    except Exception:
        heartbeat = {}
    # Discovered + cortex live under HERMES_STATE_ROOT/{bot}/state/
    # (discovered/, cortex/). Use bot_state_dir so ingest matches writers.
    # NOTE: because indicators are SHARED across pairs (SHARED_INDICATOR_GROUPS),
    # a pair's own file may not exist — its indicators live in the group's
    # anchor pair file. Use the shared-inclusive loader so every configured
    # pair is represented (matches what the entry engine actually trades on).
    from hermes_core.engines.genetic import (
        load_discovered_indicators,
        load_discovery_pulses,
        niche_map_from_indicators,
    )

    discovered_pairs: dict = {}
    for p in cfg.get("pairs") or []:
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
    # Phase B: attach discovery-run pulse + niche map (special keys stripped
    # by /api/discovered before treating entries as indicator lists).
    try:
        pulses = load_discovery_pulses(list(cfg.get("pairs") or []))
        if pulses:
            tagged = {}
            for pk, pv in pulses.items():
                row = dict(pv) if isinstance(pv, dict) else {"raw": pv}
                row["_bot"] = bot
                row.setdefault("pair", pk)
                tagged[pk] = row
            discovered_pairs["__gp_pulse__"] = tagged
        niche_maps = {}
        for p, inds in list(discovered_pairs.items()):
            if p.startswith("__") or not isinstance(inds, list):
                continue
            niche_maps[p] = niche_map_from_indicators(inds)
        if niche_maps:
            discovered_pairs["__gp_niche_map__"] = niche_maps
    except Exception:
        pass
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
        # Phase 0.2: attach reflection health so the dashboard can answer
        # "is reflection firing / proving / deploying?" from the same source of
        # truth the live cadence uses (trades.jsonl + latch + hypotheses).
        with contextlib.suppress(Exception):
            from hermes_core.engines.reflect import reflection_health

            cortex["reflection_health"] = reflection_health(
                bot, list(cfg.get("pairs") or []), goal=cfg.get("goal")
            )
        try:
            pol = PolicyEngine().evaluate(max(cycle, 0), list(cfg.get("pairs") or []), cortex=cx)
            cortex["policy"] = {
                **pol.to_dict(),
                "version": 1,
                "gates": {
                    "suppress_gp": "Bench GP when MR WR ≥ 40% and GP WR < 30%",
                    "suppress_mr": "Bench MR when GP WR ≥ 50%",
                    "priority_discovery": (
                        "≥2 exiled indicators OR reflection underperforming+"
                        "quarantined axes → accelerate GP invent (signal path only)"
                    ),
                    "rollback": "Flag rollback when MR WR < 30% after ≥10 trades",
                    "soft_weights": (
                        "HIF Phase-2: when SOFT_WEIGHTS=1, L35 benches shrink size "
                        "instead of blocking (explore floor keeps thin experts alive)"
                    ),
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
    for p in cfg.get("pairs") or []:
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
            # HIF Phase-1 probe sizing (dashboard Live / Detail indicators)
            "size_mode": pos.get("size_mode", "full"),
            "evidence_n": pos.get("evidence_n"),
            "evidence_state": pos.get("evidence_state", "disabled"),
            "base_size": pos.get("base_size"),
            "probe_fraction": pos.get("probe_fraction"),
            # HIF Phase-2 soft expert weights
            "expert_weight": pos.get("expert_weight"),
            "expert_mode": pos.get("expert_mode"),
            "suppressed_soft": pos.get("suppressed_soft"),
            "expert_reasons": pos.get("expert_reasons") or [],
            # HIF Phase-3 regime sizing
            "regime_mult": pos.get("regime_mult"),
            "regime_label": pos.get("regime_label"),
            "regime_mode": pos.get("regime_mode"),
            "fast_regime": pos.get("fast_regime"),
            "entry_regime": pos.get("entry_regime"),
            # HIF Phase-5 Kelly sizing
            "kelly_mult": pos.get("kelly_mult"),
            "kelly_mode": pos.get("kelly_mode"),
            "kelly_f": pos.get("kelly_f"),
            "p_bayes": pos.get("p_bayes"),
            "ci_low": pos.get("ci_low"),
            "ci_high": pos.get("ci_high"),
            "kelly_reasons": pos.get("kelly_reasons") or [],
            # HIF Layer B entry ranking
            "ranking_mode": pos.get("ranking_mode"),
            "rank_score": pos.get("rank_score"),
            "rank_reason": pos.get("rank_reason"),
            "rank_candidates": pos.get("rank_candidates") or [],
            # HIF book risk
            "book_mode": pos.get("book_mode"),
            "book_mult": pos.get("book_mult"),
            "book_tilt": pos.get("book_tilt"),
            "book_used": pos.get("book_used"),
            "book_cap": pos.get("book_cap"),
            "book_remaining": pos.get("book_remaining"),
            "book_reasons": pos.get("book_reasons") or [],
            # HIF exit intelligence
            "exit_intel_mode": pos.get("exit_intel_mode"),
            "honor_current_stop": pos.get("honor_current_stop"),
            "be_trigger_frac": pos.get("be_trigger_frac"),
            "trailing_atr_mult": pos.get("trailing_atr_mult"),
            "exit_intel_n": pos.get("exit_intel_n"),
            "exit_intel_reasons": pos.get("exit_intel_reasons") or [],
            "partial_enabled": pos.get("partial_enabled"),
            "avg_giveback_frac": pos.get("avg_giveback_frac"),
            # MFE/MAE peak tracking
            "peak_mfe_pct": pos.get("peak_mfe_pct"),
            "trough_mae_pct": pos.get("trough_mae_pct"),
            "mfe_tracking": pos.get("mfe_tracking"),
        }
        for pair, pos in open_positions.items()
    ]
    # recent trades / skips / hypotheses / flatline from the jsonl the engines append
    flatline_events = _read_jsonl("flatline_log.jsonl", limit=200)
    gp_promote_gate: dict = {}
    with contextlib.suppress(Exception):
        from hermes_core.engines.gp_promote_gate import snapshot_for_dashboard

        gp_promote_gate = snapshot_for_dashboard(bot, list(cfg.get("pairs") or []))
    payload = {
        "strategies": strategies,
        "goal": cfg.get("goal"),
        "heartbeat": heartbeat,
        "recent_trades": _read_jsonl("trades.jsonl"),
        "recent_skips": _read_jsonl("skips.jsonl"),
        "recent_hypotheses": _read_jsonl("hypotheses.jsonl"),
        "discovered": discovered,
        "cortex": cortex,
        # List of flatline events (crisis L21). Stored in flatlined_json so the
        # dashboard Flatline tab works across Railway volumes (not filesystem).
        "flatlined_pairs": flatline_events,
        # GP Brain promote-gate bans + exclude/include recommendations (advisory).
        "gp_promote_gate": gp_promote_gate,
        "recent_open_trades": recent_open_trades,
        "meta": {
            "oversold_pairs": (summary or {}).get("oversold_pairs", 0),
            "skip_shadow": (summary or {}).get("skip_shadow") or {},
        },
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
    backend = get_env("PRICE_BACKEND", "aggregate")

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


def _discovery_loop(
    bot: str, pairs: list[str], stop: threading.Event, cortex=None, cfg: dict | None = None
) -> None:
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
    last_pass_started = 0.0
    while not stop.is_set():
        last_pass_started = time.time()
        for pair in pairs:
            if stop.is_set():
                return
            try:
                from hermes_core.engines.loop import _DISCOVERY_IN_FLIGHT, _maybe_discover

                _maybe_discover(bot, pair, cortex=cortex)
                n = len(load_discovered_indicators(pair))
                in_flight = any(k[0] == bot and k[1] == pair for k in _DISCOVERY_IN_FLIGHT)
                print(
                    f"[hermes][discovery] {bot}/{pair}: discovered={n} in_flight={in_flight}",
                    flush=True,
                )
            except Exception as exc:  # noqa: BLE001 — never let discovery kill the bot
                print(
                    f"[hermes][discovery] {bot}/{pair}: ERROR {exc!r}", file=sys.stderr, flush=True
                )
                continue
        # Watchdog breadcrumb: if this line stops appearing, invent hung the thread.
        age = time.time() - last_pass_started
        print(
            f"[hermes][discovery] {bot}: pass_complete pairs={len(pairs)} elapsed_s={age:.1f}",
            flush=True,
        )
        # Push discovered state now (decoupled from the trade loop). [X1]
        try:
            if cfg is not None:
                _push_state(bot, cfg, cycle=-1, summary=None)
        except Exception:
            pass
        stop.wait(interval)


async def run_bot(bot_name: str) -> None:
    import signal
    import traceback as _tb

    from hermes_core.engines.loop import maybe_circuit_break
    from hermes_core.engines.soak_controls import (
        append_trade,
        load_open_book,
        orphan_force_close_records,
        save_open_book,
    )
    from hermes_core.notify.discord import send_trade_alert

    load_env()  # apply .env (fail-soft) before anything reads keys
    # Bot-name resolution precedence: CLI override (argv[1]) > explicit call arg
    # (e.g. bots.crypto.main calls run_bot("crypto")) > HERMES_BOT_NAME in .env.
    # This prevents a stray HERMES_BOT_NAME=forex in .env from silently turning
    # `python -m bots.crypto.main` into a forex run.
    cli = sys.argv[1] if len(sys.argv) > 1 else None
    bot = cli or bot_name or get_env("HERMES_BOT_NAME", "forex")
    # Keep process env aligned with the resolved bot so PolicyEngine /
    # apply_live_feedback / current_bot() cannot write another bot's state.
    os.environ["HERMES_BOT_NAME"] = bot
    # Seed volume strategies from image defaults (never overwrites existing).
    with contextlib.suppress(Exception):
        from hermes_core.config import ensure_bot_strategies_seeded

        ensure_bot_strategies_seeded(bot)
    cfg = load_config(bot)
    pairs = cfg.get("pairs") or []
    cycle_seconds = int(get_env("HERMES_CYCLE_SECONDS", "60"))
    print(
        f"[hermes] bot={bot} pairs={pairs} backend={get_env('PRICE_BACKEND', 'aggregate')}",
        flush=True,
    )

    # Build the price fetcher; for the aggregate backend this also sets up the
    # live crypto websocket with an on_tick forwarder to the dashboard.
    fetch_fn, aggregator = _make_fetcher(bot, pairs)

    # Open the live websocket stream (fail-soft; crypto falls back to REST poll
    # until/if the socket connects). [GUARD L61]
    if aggregator is not None:
        with contextlib.suppress(Exception):
            await aggregator.connect()

    _stop = threading.Event()
    # Restore open book from disk (survives Railway restart).
    book = load_open_book(bot)
    open_positions: dict = dict(book.get("open_positions") or {})
    reentry: dict = dict(book.get("reentry") or {})
    consecutive_failures = int(book.get("consecutive_failures") or 0)
    cycle = int(book.get("cycle") or 0)
    if open_positions:
        print(
            f"[hermes] restored {len(open_positions)} open position(s) from disk",
            flush=True,
        )
    # oversold_pairs from the previous cycle feeds momentum's multi-pair gate.
    oversold_pairs = 0
    # No real volume feed yet — pass False so evaluate_entry uses the ATR%
    # proxy against YAML vol_* thresholds (unlocks gold/AUD momentum).
    vol_above = False

    def _alert_fn(b: str, pair: str, reason: str, pnl: float) -> None:
        with contextlib.suppress(Exception):
            send_trade_alert(b, pair, reason, float(pnl))

    def _persist_book() -> None:
        with contextlib.suppress(Exception):
            save_open_book(
                bot,
                open_positions=open_positions,
                reentry=reentry,
                cycle=cycle,
                consecutive_failures=consecutive_failures,
            )

    def _on_signal(signum, _frame) -> None:
        print(f"[hermes] signal {signum} — graceful stop", flush=True)
        _stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(Exception):
            signal.signal(sig, _on_signal)

    # Background GP discovery — fully decoupled from the heartbeat cycle.
    _disc_cortex = None
    try:
        from hermes_core.engines.decision_cortex import Cortex

        _disc_cortex = Cortex(bot=bot)
    except Exception:
        _disc_cortex = None
    print(f"[hermes] starting discovery loop for {bot} pairs={pairs}", flush=True)
    _disc = threading.Thread(
        target=_discovery_loop, args=(bot, pairs, _stop, _disc_cortex, cfg), daemon=True
    )
    _disc.start()
    try:
        while not _stop.is_set():
            cycle += 1
            # run_cycle already iterates ALL configured pairs — call it once per
            # cadence. The old per-pair loop re-ran the full cycle N times and
            # inflated cooldowns / oversold confluence counts.
            try:
                summary = await asyncio.to_thread(
                    run_cycle,
                    bot,
                    cycle,
                    fetch_fn=fetch_fn,
                    open_positions=open_positions,
                    reentry=reentry,
                    oversold_pairs=oversold_pairs,
                    vol_above=vol_above,
                    history_fn=getattr(aggregator, "seed_history_fn", seed_history),
                    consecutive_failures=consecutive_failures,
                    alert_fn=_alert_fn,
                )
            except Exception:  # noqa: BLE001 — one bad cycle must not kill the bot
                print(f"[hermes] {bot} cycle {cycle} errored", file=sys.stderr, flush=True)
                _tb.print_exc()
                summary = None
            if isinstance(summary, dict):
                oversold_pairs = summary.get("oversold_pairs", oversold_pairs)
                consecutive_failures = int(
                    summary.get("consecutive_failures", consecutive_failures) or 0
                )
                # L24 circuit breaker — pause 300s then reset (wired in production).
                if summary.get("circuit_open") or consecutive_failures >= 5:
                    print(
                        f"[hermes][L24] {bot}: circuit open cf={consecutive_failures}",
                        flush=True,
                    )
                    await asyncio.to_thread(maybe_circuit_break, consecutive_failures)
                    consecutive_failures = 0
                prices = summary.get("prices")
                if isinstance(prices, dict) and prices:
                    _push_prices_threaded(bot, prices)
                # Push the bot's full decision-state (strategies/goal/heartbeat/
                # trades/skips/open positions) so the dashboard's pair cards +
                # overview populate. Fail-soft; a dead dashboard must never
                # stall the bot. [Gap 1]
                _push_state(bot, cfg, cycle, summary)
            _persist_book()
            # Interruptible sleep so SIGTERM can stop promptly.
            if _stop.wait(cycle_seconds):
                break
    finally:
        _stop.set()
        _persist_book()
        # If we still have opens at shutdown and HALT_FLATTEN=1, force-flat orphans.
        if get_env("HALT_FLATTEN", "0") == "1" and open_positions:
            for rec in orphan_force_close_records(bot, open_positions):
                append_trade(bot, rec)
            open_positions.clear()
            _persist_book()
        if aggregator is not None:
            with contextlib.suppress(Exception):
                await aggregator.aclose()


def main() -> None:
    asyncio.run(run_bot("forex"))


if __name__ == "__main__":
    main()
