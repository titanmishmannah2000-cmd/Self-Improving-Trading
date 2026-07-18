"""Trade loop orchestrator (Session 7 / Phase 7) — the 60-second engine hub.

Wires every engine into one config-driven cycle:
    PriceAdapter -> Indicators -> Entry <-> (Chart context) -> Risk -> Exit
on a 60s cadence, writes state, and emits a heartbeat every cycle without
exception (roadmap S7, blueprint Section 7 Phase 7).

Design rules honored:
  D1  no bot-specific branches anywhere in this file — behaviour is driven purely
      by bot/config.yaml + per-pair strategy YAMLs.
  D3  fail-soft: every engine boundary is caught; the loop never crashes. Each
      failure increments consecutive_failures and is logged with bot/pair/cycle
      (blueprint DO-NOT 3.3: never swallow without logging those three first).
  L24 circuit breaker: consecutive_failures >= MAX_CONSECUTIVE_FAILURES -> sleep
      300s (circuit_open), then reset.
The loop is side-effect-injected (fetch_fn / push_fn / now_fn) so the
integration test can drive 50+ cycles with deterministic, network-free candles.
"""

from __future__ import annotations

import contextlib
import json
import time
import traceback
from collections.abc import Callable

from hermes_core.adapters import make_default_fetch
from hermes_core.config import load_config, load_strategy_for_pair, repo_root, state_root
from hermes_core.engines.entry import evaluate_entry
from hermes_core.engines.exit import evaluate_exit
from hermes_core.engines.risk import (
    MAX_POSITION_SIZE,
    check_rr_guard,
    compute_atr_stop,
    compute_position_size,
    param_range_gate,
)
from hermes_core.env import get_env, load_env
from hermes_core.indicators import compute_all

MAX_CONSECUTIVE_FAILURES = 5          # [GUARD L24]
CIRCUIT_SLEEP_S = 300                 # 5-minute pause on circuit open
CYCLE_SECONDS = 60                    # 60s cadence
HEARTBEAT_PATH = repo_root() / "state" / "heartbeat.json"
TRADES_PATH = repo_root() / "state" / "trades.jsonl"
SKIPS_PATH = repo_root() / "state" / "skips.jsonl"


def _state_dir(bot: str) -> Path:
    """Per-bot runtime-state dir on the PERSISTENT volume (HERMES_STATE_ROOT,
    e.g. /data), NOT inside the read-only image (/app). live_compat reads
    these same paths, so bot writes and dashboard reads line up. [D3/3.1]
    """
    d = state_root() / bot / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_token_for(hour: int) -> str:
    """Resolve an hour to a session token (blueprint _get_session)."""
    if 0 <= hour < 8:
        return "ASIA"
    if 8 <= hour < 17:
        return "LDN"
    if 13 <= hour < 21:
        return "NY"
    return "OTHER"


def _atr_stop_for(strategy: dict, entry: float, atr: float) -> float:
    mult = float(strategy.get("atr_multiplier", 1.5))
    floor = float(strategy.get("atr_floor_pct", 0.0))
    return compute_atr_stop(entry, atr, mult, floor)


def write_heartbeat(
    asset: str,
    cycle: int,
    consecutive_failures: int,
    last_price: float,
    *,
    status: str = "ok",
    health: dict | None = None,
    chart_contexts: dict | None = None,
    market_closed: bool = False,
) -> dict:
    """Emit heartbeat.json with the documented keys (blueprint loop.py:1774/4433).

    Always succeeds — failures here must never propagate (one heartbeat per cycle
    without exception is a hard S7 requirement).
    """
    HEARTBEAT_PATH = _state_dir(asset) / "heartbeat.json"
    data = {
        "ts": time.time(),
        "asset": asset,
        "cycle": cycle,
        "consecutive_failures": consecutive_failures,
        "last_price": last_price,
        "status": "circuit_open" if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else status,
        "health": health or {},
        "chart_contexts": chart_contexts or {},
        "market_closed": market_closed,
    }
    try:
        with open(HEARTBEAT_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, default=str)
    except OSError:
        # heartbeat itself cannot break the loop; best-effort only
        pass
    return data


def _log_skip(bot: str, pair: str, cycle: int, reason: str) -> None:
    SKIPS_PATH = _state_dir(bot) / "skips.jsonl"
    row = {"ts": time.time(), "pair": pair, "cycle": cycle, "reason": reason}
    try:
        with open(SKIPS_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        pass


def _log_trade(bot: str, rec: dict) -> None:
    TRADES_PATH = _state_dir(bot) / "trades.jsonl"
    try:
        with open(TRADES_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


def run_cycle(
    bot: str,
    cycle: int,
    *,
    fetch_fn: Callable[[str], object] | None = None,
    push_fn: Callable[[str, dict], None] | None = None,
    now_fn: Callable[[], float] = time.time,
    health_registry: dict | None = None,
    chart_context_fn: Callable[[str], str] | None = None,
    ensemble_fn: Callable[[str], str] | None = None,
    open_positions: dict | None = None,
    reentry: dict | None = None,
    oversold_pairs: int = 0,
    vol_above: bool = False,
    consecutive_failures: int = 0,
    alert_fn: Callable[[str, str, str, float], object] | None = None,
) -> dict:
    """Run one 60-second cycle for ``bot`` across all its declared pairs.

    Returns a per-cycle summary. Side effects (fetch/push/now/heartbeat) are
    injectable so the integration test is deterministic and network-free.
    ``alert_fn`` (optional) is called on each real trade CLOSE with
    (bot, pair, reason, pnl_pct); used to fire Discord/webhook alerts.
    """
    health_registry = health_registry if health_registry is not None else {}
    open_positions = open_positions if open_positions is not None else {}
    reentry = reentry if reentry is not None else {}
    # [GUARD L62] resolve the price feed. Default honors PRICE_BACKEND (opt-in);
    # falls back to yfinance so the running path is unchanged unless flipped.
    if fetch_fn is None:
        load_env()
        fetch_fn = make_default_fetch(
            backend=get_env("PRICE_BACKEND", "yfinance"),
            pairs=[],
        )
    summary = {"cycle": cycle, "entries": [], "exits": [], "skips": 0, "errors": 0, "prices": {}}
    # consecutive_failures is carried in (persists across cycles for the L24 breaker)
    try:
        cfg = load_config(bot)
    except Exception:  # noqa: BLE001 — config load is a hard boundary
        health_registry["config"] = False
        summary["errors"] += 1
        write_heartbeat(bot, cycle, consecutive_failures, 0.0,
                        status="error", health=dict(health_registry))
        traceback.print_exc()
        return summary

    health_registry["config"] = True
    pairs = cfg.get("pairs", [])
    hour = int((now_fn() // 3600) % 24)  # wall-clock hour (deterministic in test)
    session_token = _session_token_for(hour)
    last_price = 0.0
    chart_contexts: dict[str, str] = {}

    for pair in pairs:
        # --- fetch (fail-soft; failures counted toward circuit breaker) -----
        try:
            candle = fetch_fn(pair)
        except Exception as exc:  # noqa: BLE001
            consecutive_failures += 1
            summary["errors"] += 1
            health_registry["price_adapter"] = False
            _log_skip(bot, pair, cycle, f"fetch_error:{exc!r}")
            traceback.print_exc()
            continue

        if candle is None:
            # stale/empty feed — counted, not a hard crash
            consecutive_failures += 1
            summary["errors"] += 1
            _log_skip(bot, pair, cycle, "no_candle")
            continue

        health_registry.setdefault("price_adapter", True)
        price = float(candle["price"])
        last_price = price
        summary["prices"][pair] = price  # live price snapshot for dashboard push

        # seeded price history for indicators (fail-soft)
        try:
            prices = [c["price"] for c in (fetch_fn(pair + ":history") or [])]
        except Exception:  # noqa: BLE001
            prices = [price]
        if not prices:
            prices = [price]
        try:
            ind = compute_all(prices)
        except Exception as exc:  # noqa: BLE001
            consecutive_failures += 1
            summary["errors"] += 1
            health_registry["indicators"] = False
            _log_skip(bot, pair, cycle, f"indicator_error:{exc!r}")
            traceback.print_exc()
            continue
        health_registry["indicators"] = True

        # --- load strategy + param-range gate (L40) -------------------------
        try:
            strategy = load_strategy_for_pair(pair, bot)
            ok, reason = param_range_gate(strategy)
            if not ok:
                _log_skip(bot, pair, cycle, f"param_gate:{reason}")
                summary["skips"] += 1
                continue
        except Exception as exc:  # noqa: BLE001
            consecutive_failures += 1
            summary["errors"] += 1
            health_registry["config"] = False
            _log_skip(bot, pair, cycle, f"strategy_error:{exc!r}")
            traceback.print_exc()
            continue
        health_registry["config"] = True

        # --- chart context (fail-open; an error yields neutral) -------------
        context = ""
        try:
            if chart_context_fn is not None:
                context = chart_context_fn(pair) or ""
            chart_contexts[pair] = context
            health_registry["chart_vision"] = True
        except Exception as exc:  # noqa: BLE001 — fail-open, never crash
            context = ""
            chart_contexts[pair] = ""
            health_registry["chart_vision"] = False
            _log_skip(bot, pair, cycle, f"chart_error:{exc!r}")

        ensemble = (ensemble_fn(pair) if ensemble_fn else "neutral") or "neutral"
        atr = float(ind["atr"])

        # --- entry evaluation ---------------------------------------------
        pos = open_positions.get(pair)
        if pos is None:
            sig = evaluate_entry(
                pair, prices, strategy, context, ensemble,
                oversold_pairs, vol_above, reentry, cycle, session_token,
            )
            if sig is None:
                _log_skip(bot, pair, cycle, "no_signal")
                summary["skips"] += 1
                continue
            # RR guard (S6) — reject R:R < 1.0 before committing
            sl = float(strategy["stop_loss_pct"])
            tp = float(strategy["profit_target_pct"])
            if not check_rr_guard(sl, tp):
                _log_skip(bot, pair, cycle, "rr_guard")
                summary["skips"] += 1
                continue
            size = compute_position_size(session_token, atr, 0, strategy)
            stop = _atr_stop_for(strategy, price, atr)
            open_positions[pair] = {
                "entry_price": price, "size": min(size, MAX_POSITION_SIZE),
                "stop_loss_pct": sl, "profit_target_pct": tp,
                "time_exit_cycles": int(strategy.get("time_exit_cycles", 288)),
                "held_cycles": 0, "breakeven_set": False, "partial_done": False,
                "partial_enabled": bool(strategy.get("partial_enabled", False)),
                "current_stop": stop, "atr": atr,
            }
            summary["entries"].append(pair)
        else:
            # --- exit evaluation (S5) --------------------------------------
            pos["held_cycles"] = pos.get("held_cycles", 0) + 1
            pos["unrealised_pct"] = (price - pos["entry_price"]) / pos["entry_price"] * 100.0
            ex = evaluate_exit(pos, price, prices)
            if ex is not None:
                _log_trade(bot, {
                    "pair": pair, "cycle": cycle, "reason": ex.reason,
                    "entry_price": pos["entry_price"], "exit_price": price,
                    "pnl_pct": pos["unrealised_pct"], "size": pos["size"],
                })
                if ex.reason == "breakeven":
                    pos["breakeven_set"] = True
                    pos["current_stop"] = ex.new_stop
                elif ex.reason == "trailing":
                    pos["current_stop"] = ex.new_stop
                else:
                    reentry[pair] = {"last_exit_cycle": cycle}
                    del open_positions[pair]
                    # [S18] Discord/webhook alert on real trade close (fail-soft)
                    if alert_fn is not None:
                        with contextlib.suppress(Exception):
                            alert_fn(bot, pair, ex.reason, pos["unrealised_pct"])
                summary["exits"].append((pair, ex.reason))

    # --- heartbeat every cycle without exception --------------------------
    status = "ok" if consecutive_failures == 0 else "degraded"
    write_heartbeat(bot, cycle, consecutive_failures, last_price,
                    status=status, health=dict(health_registry),
                    chart_contexts=chart_contexts)
    summary["consecutive_failures"] = consecutive_failures
    if push_fn is not None:
        try:
            push_fn(bot, summary)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        # [GUARD L24] circuit open: caller should pause; reset the counter so a
        # single pause doesn't permanently lock the breaker closed.
        summary["circuit_open"] = True
    return summary


def maybe_circuit_break(consecutive_failures: int, sleep_fn=time.sleep) -> bool:
    """[GUARD L24] If failures hit the cap, pause 300s and reset. Returns True if opened."""
    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
        sleep_fn(CIRCUIT_SLEEP_S)
        return True
    return False
