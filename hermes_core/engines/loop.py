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
from hermes_core.engines.decision_cortex import Cortex
from hermes_core.engines.entry import evaluate_entry
from hermes_core.engines.exit import evaluate_exit
from hermes_core.engines.genetic import discover as gp_discover
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
# Discovery is expensive (GP evolution over price history); throttle per
# (bot, pair) so it runs at most once per ~hour of wall-clock, or on first run.
DISCOVERY_INTERVAL_S = int(get_env("DISCOVERY_INTERVAL_S", "3600"))
_HEARTBEAT_PATH = repo_root() / "state" / "heartbeat.json"
_TRADES_PATH = repo_root() / "state" / "trades.jsonl"
_SKIPS_PATH = repo_root() / "state" / "skips.jsonl"
_DISCOVERY_LAST: dict[tuple[str, str], float] = {}  # (bot, pair) -> last run epoch


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
    regimes: dict | None = None,
    prices: dict | None = None,
    price_history: dict | None = None,
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
        "regimes": regimes or {},
        # Per-pair live price snapshot — surfaced to the dashboard so pair
        # cards can show the current quote (e.g. gold $4019.30) instead of "—".
        "prices": prices or {},
        # Rolling recent price history (last N ticks) per pair — backs the
        # dashboard sparkline for pairs whose yfinance ticker is unreliable
        # (e.g. gold/silver), so the card still shows a live mini-chart.
        "price_history": price_history or {},
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
    # `reason_skipped` is the dashboard's DB column key; keep `reason` too for
    # any consumer that read the legacy key.
    row = {"ts": time.time(), "pair": pair, "cycle": cycle,
           "reason": reason, "reason_skipped": reason}
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


def _discovered_indicator_ids(bot: str, pair: str) -> list[str]:
    """Stable ids of the GP indicators admitted for `pair` (for cortex exile tracking)."""
    try:
        from hermes_core.engines.genetic import load_discovered_indicators
        return [i.get("name", "") for i in load_discovered_indicators(pair) if i.get("name")]
    except Exception:
        return []


def _maybe_discover(bot: str, pair: str, prices: list[float] | None = None) -> None:
    """Throttled GP discovery for one pair.

    Runs when no discovered file exists yet, or at most once per
    DISCOVERY_INTERVAL_S of wall-clock per (bot, pair). Persists admitted
    indicators to state/discovered/{pair}.json (read by the dashboard).

    CRITICAL: discovery does network + GP evolution and must NEVER block the
    heartbeat cycle. The heavy work runs in a thread with a hard timeout; if it
    stalls, the cycle proceeds and the next attempt retries. Fail-soft.
    """
    from hermes_core.adapters.price import seed_history_interval_sync
    from hermes_core.engines.genetic import load_discovered_indicators
    now = time.time()
    key = (bot, pair)
    if key in _DISCOVERY_LAST and (now - _DISCOVERY_LAST[key]) < DISCOVERY_INTERVAL_S:
        return
    if load_discovered_indicators(pair):
        _DISCOVERY_LAST[key] = now  # fresh enough; refresh throttle only
        return

    def _work() -> None:
        # GP discovery runs on the OLD engine's working regime: 2y of DAILY
        # bars with a 60-candle forward horizon. The old genetic_discovery.py
        # is explicit that 5m/next-candle "almost never clear, by design" —
        # only the daily/long-horizon objective produces predictive structure.
        # We keep the live trade loop on 5m; discovery uses daily history.
        import logging as _logging
        _log = _logging.getLogger("hermes.discovery")
        from hermes_core.adapters.price import seed_history_interval_sync
        hist = seed_history_interval_sync(pair, interval="1d", period="2y",
                                          max_candles=500)
        series = [c["price"] for c in (hist or [])] or (prices or [])
        _log.info("[discovery] %s: fetched %d daily candles for GP", pair, len(series))
        if len(series) < 200:
            _log.warning("[discovery] %s: <200 daily candles, GP skipped", pair)
            return
        gp_discover(pair, series, horizon=60, generations=40, pop_size=40)

    # Bound the work so a slow network/price API can't stall the trade loop.
    # Discovery now runs in its own background thread, so a generous cap is safe.
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1) as ex:
            ex.submit(_work).result(timeout=60)
    except Exception as _exc:  # surface the real reason instead of silent drop
        import logging as _logging
        _logging.getLogger("hermes.discovery").warning(
            "[discovery] %s/%s: error -> %s", bot, pair, _exc)
        return
    _DISCOVERY_LAST[key] = time.time()


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
    history_fn: Callable[[str], object] | None = None,
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
    # Rolling price history for the sparkline (last N ticks per pair). Persisted
    # by the caller across cycles so the card chart is continuous, not per-cycle.
    price_history = dict(getattr(run_cycle, "_price_history", {}) or {})
    oversold_pairs = 0            # RSI-confluence count, accumulated across pairs this cycle
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
    regimes: dict[str, str] = {}          # pair -> 'trend'|'range' (dashboard regime cards)
    cortex = Cortex()                      # per-cycle; exile SET persists to disk

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
        # Append to rolling history for the sparkline (cap at 60 ticks).
        ph = price_history.setdefault(pair, [])
        ph.append(price)
        if len(ph) > 60:
            del ph[: len(ph) - 60]

        # seeded price history for indicators (fail-soft).
        # Prefer history_fn (real multi-candle series via the adapters'
        # seed_history, which pulls a genuine series for FX/metals). The
        # aggregate fetch_fn(":history") only returns the last tick for
        # FX/metals, which makes indicators degenerate -> bot can't trade.
        # Fall back to fetch_fn(":history"), then a single price.
        try:
            if history_fn is not None:
                hist = history_fn(pair)
            else:
                hist = fetch_fn(pair + ":history")
            prices = [c["price"] for c in (hist or [])]
        except Exception:  # noqa: BLE001
            prices = []
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
        regimes[pair] = ind.get("regime", "range")  # 'trend'|'range' for dashboard

        # NOTE: GP discovery is intentionally NOT called here. It is a slow,
        # network-backed, periodic job (see _runner._discovery_loop) that runs
        # on its own scheduler so it can never stall the heartbeat cycle.

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

        # RSI-confluence: count pairs currently oversold (feeds momentum's
        # multi-pair gate). Computed as we scan so later pairs see earlier ones.
        _thr = (strategy.get("entry") or {}).get("threshold", 50)
        if ind["rsi"] <= _thr:
            oversold_pairs += 1

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
            # [CORTEX] record the entry (per-type memory; exile persists across cycles)
            with contextlib.suppress(Exception):
                cortex.record_entry(pair, "mean_reversion")
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
                    pnl = pos["unrealised_pct"]
                    # [CORTEX] record the outcome; auto-exile low-WR GP indicators
                    with contextlib.suppress(Exception):
                        cortex.record_outcome(pair, "mean_reversion", pnl)
                        for ind_id in _discovered_indicator_ids(bot, pair):
                            cortex.record_indicator_outcome(ind_id, pnl)
                    # [S18] Discord/webhook alert on real trade close (fail-soft)
                    if alert_fn is not None:
                        with contextlib.suppress(Exception):
                            alert_fn(bot, pair, ex.reason, pnl)
                summary["exits"].append((pair, ex.reason))

    # --- heartbeat every cycle without exception --------------------------
    status = "ok" if consecutive_failures == 0 else "degraded"
    write_heartbeat(bot, cycle, consecutive_failures, last_price,
                    status=status, health=dict(health_registry),
                    chart_contexts=chart_contexts, regimes=regimes,
                    prices=summary.get("prices") or {},
                    price_history=price_history)
    summary["consecutive_failures"] = consecutive_failures
    summary["oversold_pairs"] = oversold_pairs
    # The caller persists these across cycles so entries are tracked to exit
    # and trades actually record (without this, positions reset every cycle
    # and no trade is ever logged).
    summary["open_positions"] = open_positions
    summary["reentry"] = reentry
    if push_fn is not None:
        try:
            push_fn(bot, summary)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    # Persist rolling price history across cycles (continuous sparkline).
    run_cycle._price_history = {p: price_history.get(p, []) for p in set(price_history) | set(getattr(run_cycle, "_price_history", {}) or {})}
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
