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
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    """UTC ISO-8601 timestamp (matches the dashboard's entry_ts/exit_ts format)."""
    return datetime.now(timezone.utc).isoformat()

from hermes_core.adapters import make_default_fetch
from hermes_core.config import load_config, load_strategy_for_pair, state_root
from hermes_core.engines.decision_cortex import Cortex
from hermes_core.engines.entry import evaluate_entry, _entry_rsi_threshold
from hermes_core.engines.exit import evaluate_exit
from hermes_core.engines.genetic import discover as gp_discover
from hermes_core.engines.policy_engine import PolicyEngine, soft_weights_enabled
from hermes_core.engines.expert_weights import apply_expert_weight, expert_weight
from hermes_core.engines.regime_sizing import apply_regime_sizing, regime_sizing_enabled
from hermes_core.engines.kelly_sizing import apply_kelly_sizing, kelly_sizing_enabled
from hermes_core.engines.mom_range_guard import (
    apply_mom_range_guard,
    gp_agree_bullish,
    mom_range_guard_enabled,
)
from hermes_core.engines.risk import (
    MAX_POSITION_SIZE,
    apply_probe_sizing,
    check_rr_guard,
    compute_atr_stop,
    compute_position_size,
    param_range_gate,
    size_regime_from_market,
)
from hermes_core.env import get_env, load_env
from hermes_core.engines.guards import bb_bandwidth_guard, flat_price_guard
from hermes_core.indicators import compute_all

MAX_CONSECUTIVE_FAILURES = 5          # [GUARD L24]
CIRCUIT_SLEEP_S = 300                 # 5-minute pause on circuit open
CYCLE_SECONDS = 60                    # 60s cadence
# Discovery is expensive (GP evolution over price history); throttle per
# (bot, pair) so it runs at most once per ~hour of wall-clock, or on first run.
DISCOVERY_INTERVAL_S = int(get_env("DISCOVERY_INTERVAL_S", "3600"))
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



def _precount_oversold(bot: str, pairs: list, fetch_fn, history_fn) -> int:
    """Count how many bot pairs are RSI-oversold this cycle (both-metal confluence).

    Runs before the pair loop so XAU sees XAG (and vice versa). Fail-soft → 0.
    """
    rows = 0
    for pair in pairs or []:
        try:
            strategy = load_strategy_for_pair(pair, bot)
            if history_fn is not None:
                hist = history_fn(pair)
            else:
                hist = fetch_fn(pair + ":history")
            prices = [c["price"] for c in (hist or [])]
            if len(prices) < 5:
                continue
            ind = compute_all(prices)
            if float(ind.get("rsi", 50)) <= float(_entry_rsi_threshold(strategy)):
                rows += 1
        except Exception:  # noqa: BLE001 — confluence must never break the cycle
            continue
    return rows


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


def _process_exit(bot, pair, cycle, pos, price, ex, *, cortex, reentry,
                  open_positions, summary, alert_fn,
                  prices=None, chart_context="", goal=None) -> None:
    """Apply the result of `evaluate_exit` to an OPEN position.

    Stop-adjustments (breakeven / trailing) only move the stop — the
    position stays OPEN and is NOT logged as a trade close. Only a genuine
    close (sl/tp/time/explicit) writes a closed-trade record, and that record
    uses the exact keys the dashboard backend reads (id, exit_reason,
    entry_ts, exit_ts) so it is counted as a real close downstream.
    """
    summary["exits"].append((pair, ex.reason))
    if ex.reason in ("breakeven", "trailing"):
        # Stop-adjustment only — position stays OPEN, not a trade close.
        if ex.reason == "breakeven":
            pos["breakeven_set"] = True
        pos["current_stop"] = ex.new_stop
        return
    # HIF EXIT_INTEL: true half-partial — close fraction, keep remainder at BE.
    if (
        ex.reason == "partial_close"
        and pos.get("honor_current_stop")
        and ex.partial_close_fraction
        and not pos.get("partial_done")
    ):
        try:
            frac = max(0.05, min(0.95, float(ex.partial_close_fraction)))
        except (TypeError, ValueError):
            frac = 0.5
        full_size = float(pos.get("size") or 0.0)
        closed_size = full_size * frac
        remain = full_size - closed_size
        entry_type = pos.get("entry_type", "mean_reversion")
        pnl = pos["unrealised_pct"]
        _exc = {}
        with contextlib.suppress(Exception):
            from hermes_core.engines.excursion import excursion_from_position
            _exc = excursion_from_position(pos, pnl)
        _log_trade(bot, {
            "id": (pos.get("id") or f"{bot}:{pair}:{int(time.time())}") + ":partial",
            "bot": bot, "pair": pair, "cycle": cycle,
            "reason": ex.reason, "exit_reason": ex.reason,
            "entry_type": entry_type,
            "strategy_version": pos.get("strategy_version") or entry_type,
            "entry_price": pos["entry_price"], "exit_price": price,
            "entry_ts": pos.get("entry_ts"), "exit_ts": _now_iso(),
            "pnl_pct": pnl, "size": closed_size,
            "hold_cycles": pos.get("held_cycles", 0),
            "partial": True,
            **{k: _exc[k] for k in ("mfe_pct", "mae_pct", "giveback_pct", "giveback_frac", "mfe_capture")
               if k in _exc},
        })
        with contextlib.suppress(Exception):
            cortex.record_outcome(
                pair, entry_type, pnl,
                mfe_pct=_exc.get("mfe_pct"),
                mae_pct=_exc.get("mae_pct"),
                giveback_pct=_exc.get("giveback_pct"),
                giveback_frac=_exc.get("giveback_frac"),
                mfe_capture=_exc.get("mfe_capture"),
            )
        pos["size"] = remain
        pos["partial_done"] = True
        pos["breakeven_set"] = True
        if ex.new_stop is not None:
            pos["current_stop"] = ex.new_stop
        return
    # --- REAL close: log the trade with the keys the dashboard reads.
    entry_type = pos.get("entry_type", "mean_reversion")
    pnl = pos["unrealised_pct"]
    _exc = {}
    with contextlib.suppress(Exception):
        from hermes_core.engines.excursion import excursion_from_position
        _exc = excursion_from_position(pos, pnl)
    _log_trade(bot, {
        "id": pos.get("id") or f"{bot}:{pair}:{int(time.time())}",
        "bot": bot, "pair": pair, "cycle": cycle,
        "reason": ex.reason, "exit_reason": ex.reason,
        "entry_type": entry_type,
        # Prefer the stamped strategy version; fall back to entry style.
        "strategy_version": pos.get("strategy_version") or entry_type,
        "entry_price": pos["entry_price"], "exit_price": price,
        "entry_ts": pos.get("entry_ts"), "exit_ts": _now_iso(),
        "pnl_pct": pnl, "size": pos["size"],
        "hold_cycles": pos.get("held_cycles", 0),
        **{k: _exc[k] for k in ("mfe_pct", "mae_pct", "giveback_pct", "giveback_frac", "mfe_capture")
           if k in _exc},
    })
    reentry[pair] = {"last_exit_cycle": cycle}
    del open_positions[pair]
    # [CORTEX] record the outcome under the REAL entry_type;
    # auto-exile low-WR GP indicators. B9: credit ONLY the indicators that
    # actually fired on THIS trade (carried on pos["gp_indicators"]), not every
    # discovered indicator for the pair (the old code credited all equally).
    # GP trades open as "shadow" until promoted, but a shadow GP paper-trade is
    # still real GP evidence we must learn from — so credit its indicators and
    # record the outcome under "gp_ensemble" whenever the trade was GP-driven
    # (gp_indicators non-empty), not only when already promoted to live.
    with contextlib.suppress(Exception):
        is_gp = bool(pos.get("gp_indicators")) or entry_type in (
            "gp_ensemble", "shadow",
        )
        _record_type = "gp_ensemble" if is_gp else entry_type
        cortex.record_outcome(
            pair, _record_type, pnl,
            mfe_pct=_exc.get("mfe_pct"),
            mae_pct=_exc.get("mae_pct"),
            giveback_pct=_exc.get("giveback_pct"),
            giveback_frac=_exc.get("giveback_frac"),
            mfe_capture=_exc.get("mfe_capture"),
        )
        if is_gp:
            _credited = pos.get("gp_indicators") or []
        else:
            _credited = []
        for ind_id in _credited:
            cortex.record_indicator_outcome(ind_id, pnl, entry_type="gp_ensemble")
        # GPIntelligence consecutive-loss lockout (feeds should_suppress).
        if is_gp:
            from hermes_core.engines import gp_intelligence as gpi
            if pnl > 0:
                gpi.record_win(pair)
            else:
                gpi.record_loss(pair)
            # Feed paper GP closes into the promote gate (ban/unban evidence).
            with contextlib.suppress(Exception):
                from hermes_core.engines import gp_promote_gate as gpg
                gpg.record_pnl(bot, pair, float(pnl))
    # [S18] Discord/webhook alert on real trade close (fail-soft)
    if alert_fn is not None:
        with contextlib.suppress(Exception):
            alert_fn(bot, pair, ex.reason, pnl)
    # Reflection latch: every N closed trades → L1 → (L2) → backtest → deploy.
    # Fail-soft: never let reflection break the close/heartbeat path.
    with contextlib.suppress(Exception):
        _maybe_reflect_after_close(
            bot, pair, prices=prices, chart_context=chart_context or "",
            goal=goal,
        )


def _maybe_reflect_after_close(
    bot: str,
    pair: str,
    *,
    prices: list[float] | None = None,
    chart_context: str = "",
    goal: dict | None = None,
) -> dict | None:
    """Invoke the reflection pipeline when the every-N latch fires.

    Runs in a daemon thread so a slow price fetch / backtest cannot stall the
    60s heartbeat. Fail-soft: exceptions are logged, never raised to the loop.
    """
    import logging as _logging
    import threading

    from hermes_core.engines.reflect import maybe_reflect_pair

    auto = get_env("REFLECT_AUTO_DEPLOY", "1") != "0"
    log = _logging.getLogger("hermes.reflect")

    def _work() -> None:
        try:
            result = maybe_reflect_pair(
                bot, pair, goal=goal, chart_context=chart_context,
                prices=prices, auto_deploy=auto,
            )
            if result is not None:
                log.info(
                    "[reflect] %s/%s: %s closed=%s deployed=%s",
                    bot, pair, result.get("status"), result.get("closed"),
                    result.get("deployed"),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("[reflect] %s/%s: error -> %s", bot, pair, exc)

    threading.Thread(
        target=_work, name=f"reflect-{bot}-{pair}", daemon=True,
    ).start()
    return None


def _discovered_indicator_ids(bot: str, pair: str) -> list[str]:
    """Stable ids of the GP indicators admitted for `pair` (for cortex exile tracking)."""
    try:
        from hermes_core.engines.genetic import load_discovered_indicators
        return [i.get("name", "") for i in load_discovered_indicators(pair) if i.get("name")]
    except Exception:
        return []


# Logs the GP-ensemble "would-be" signal for a pair every cycle. SHADOW ONLY:
# it writes a structured record to state/{bot}/gp_shadow.jsonl and NEVER opens
# an order. This is the out-of-sample track record we require before any live
# promotion of the GP brain (faithful to "shadow/log-only first").
_GP_SHADOW_LAST: dict[tuple, float] = {}
_GP_CONSENSUS_CACHE: dict[str, str] = {}  # pair -> last known consensus (L13)
GP_SHADOW_LOG_INTERVAL_S = 300  # at most one shadow record per 5 min per pair


def _gp_vote(pair: str, prices: list[float], strategy: dict, *,
             cortex=None, promote: bool = False, use_daily: bool = True):
    """Evaluate GP ensemble once; apply L36 exile filter. Fail-soft -> None.

    ``use_daily=True`` (promote path) evaluates on the discovery regime.
    ``use_daily=False`` (shadow/L13) uses the live series the cycle already has
    — no extra network fetch, matches the original shadow logger.
    """
    try:
        from hermes_core.engines.entry import gp_ensemble_signal, gp_daily_prices
        exiled: set[str] = set()
        if cortex is not None:
            with contextlib.suppress(Exception):
                exiled = set(cortex.get_exiled_indicators() or [])
        daily = gp_daily_prices(pair) if use_daily else None
        return gp_ensemble_signal(
            pair, prices, strategy,
            daily_prices=daily,
            promote=promote,
            exiled_ids=exiled,
        )
    except Exception:  # noqa: BLE001
        return None


def _log_gp_shadow(bot: str, pair: str, prices: list[float], strategy: dict,
                   *, cortex=None, sig=None) -> str:
    """Evaluate/log the GP shadow entry; return consensus label for L13.

    Fail-soft: any exception is swallowed (logging must never break the cycle).
    Returns a consensus string suitable for evaluate_entry's ensemble_consensus
    (``neutral`` when no GP vote).
    """
    consensus = "neutral"
    try:
        if len(prices) < 50:
            _GP_CONSENSUS_CACHE[pair] = consensus
            return consensus
        if sig is None:
            # Shadow observation on the live series (no daily network fetch).
            sig = _gp_vote(
                pair, prices, strategy, cortex=cortex,
                promote=False, use_daily=False,
            )
        if sig is not None:
            consensus = sig.meta.get("consensus") or "neutral"
        _GP_CONSENSUS_CACHE[pair] = consensus

        # Forward-settle shadow expectancy into the promote gate (banned pairs
        # still accumulate evidence while invent/shadow keep running).
        with contextlib.suppress(Exception):
            from hermes_core.engines import gp_promote_gate as gpg
            _dir = None
            if sig is not None:
                _gs = float(sig.meta.get("gp_strength") or 0.0)
                if _gs > 0:
                    _dir = 1
                elif _gs < 0:
                    _dir = -1
            gpg.observe_shadow(bot, pair, float(prices[-1]), direction=_dir)

        now = time.time()
        key = (bot, pair)
        if key in _GP_SHADOW_LAST and (now - _GP_SHADOW_LAST[key]) < GP_SHADOW_LOG_INTERVAL_S:
            return consensus
        _GP_SHADOW_LAST[key] = now
        rec = {
            "ts": time.time(),
            "pair": pair,
            "signal": None if sig is None else sig.type,
            "consensus": (sig.meta.get("consensus") if sig else None),
            "gp_strength": (sig.meta.get("gp_strength") if sig else None),
            "num_active": (sig.meta.get("num_active") if sig else 0),
            "shadow": True,
        }
        path = _state_dir(bot) / "gp_shadow.jsonl"
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except Exception:  # noqa: BLE001 — observation must never break the cycle
        pass
    return _GP_CONSENSUS_CACHE.get(pair, "neutral")


def _maybe_discover(bot: str, pair: str, prices: list[float] | None = None,
                    *, cortex=None) -> None:
    """Throttled GP discovery + live feedback for one pair (B10 closes the loop).

    On each throttled pass it:
      1. (re)discovers indicators if none are stored yet;
      2. ALWAYS applies live paper-PnL feedback (B10) so persisted indicators
         are re-ranked toward realized results — this is what makes the GP
         brain self-evolve rather than sit on its historical-correlation fitness.

    Runs at most once per DISCOVERY_INTERVAL_S of wall-clock per (bot, pair).
    Persists admitted + re-ranked indicators to state/discovered/{pair}.json
    (read by the dashboard + entry engine).

    CRITICAL: discovery does network + GP evolution and must NEVER block the
    heartbeat cycle. The heavy work runs in a thread with a hard timeout; if it
    stalls, the cycle proceeds and the next attempt retries. Fail-soft.
    """
    from hermes_core.adapters.price import seed_history_interval_sync
    from hermes_core.engines.genetic import (
        _discovered_path,
        apply_live_feedback,
        indicator_expr,
        is_backtest_approved,
        load_discovered_indicators,
    )
    now = time.time()
    key = (bot, pair)
    if key in _DISCOVERY_LAST and (now - _DISCOVERY_LAST[key]) < DISCOVERY_INTERVAL_S:
        return

    # Skip invent only when THIS pair already has S10-approved GP formulas.
    own = load_discovered_indicators(pair, include_shared=False)
    discovered = any(indicator_expr(i) and is_backtest_approved(i) for i in own)

    def _work() -> None:
        import logging as _logging
        _log = _logging.getLogger("hermes.discovery")
        # B10 live feedback: re-rank persisted indicators toward realized PnL.
        # Runs on every throttled pass (even when re-discovery isn't needed)
        # so the ensemble keeps learning from closed paper trades.
        updated = apply_live_feedback(pair, cortex)
        if updated:
            _log.info("[discovery] %s: live feedback updated %d indicators",
                      pair, updated)
        # (Re)discover only when THIS pair has no votable own formulas yet.
        if discovered:
            return
        # GP discovery runs on the OLD engine's working regime: 2y of DAILY
        # bars with a 60-candle forward horizon. The old genetic_discovery.py
        # is explicit that 5m/next-candle "almost never clear, by design" —
        # only the daily/long-horizon objective produces predictive structure.
        # We keep the live trade loop on 5m; discovery uses daily history.
        hist = seed_history_interval_sync(pair, interval="1d", period="2y",
                                          max_candles=500)
        series = [c["price"] for c in (hist or [])] or (prices or [])
        _log.info("[discovery] %s: fetched %d daily candles for GP", pair, len(series))
        if len(series) < 200:
            _log.warning("[discovery] %s: <200 daily candles, GP skipped", pair)
            return
        inds = gp_discover(pair, series, horizon=60, generations=40, pop_size=40,
                           n_islands=2)
        _log.info("[discovery] %s: admitted=%d -> %s",
                  pair, len(inds), _discovered_path(pair))

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
    # [GUARD L62] resolve the price feed AFTER config so aggregate gets real pairs
    # (crypto WS subscribe list). Default is multi-source aggregator for live quotes.
    if fetch_fn is None:
        load_env()
        fetch_fn = make_default_fetch(
            backend=get_env("PRICE_BACKEND", "aggregate"),
            pairs=list(pairs),
        )
    oversold_total = 0
    with contextlib.suppress(Exception):
        oversold_total = _precount_oversold(bot, list(pairs), fetch_fn, history_fn)
    hour = int((now_fn() // 3600) % 24)  # wall-clock hour (deterministic in test)
    session_token = _session_token_for(hour)
    last_price = 0.0
    chart_contexts: dict[str, str] = {}
    # Sticky regimes: start from last cycle so a transient no_candle (common for
    # single-source XAG) doesn't blank the dashboard Regime field.
    regimes: dict[str, str] = {
        p: r for p, r in (getattr(run_cycle, "_regimes", {}) or {}).items()
        if p in pairs
    }
    cortex = Cortex(bot)                   # per-cycle; exile SET persists to disk
    # [GUARD L35] evaluate policy once per cycle from cortex WRs, then apply
    # suppressions before opening new positions.
    try:
        policy = PolicyEngine().evaluate(cycle, pairs, cortex=cortex)
    except Exception:  # noqa: BLE001 — fail-open: never block trading on policy I/O
        policy = None

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

        # [GUARD L02] flat-price / stale-data gate
        is_flat, flat_reason = flat_price_guard(ind, prices)
        if is_flat:
            _log_skip(bot, pair, cycle, flat_reason)
            summary["skips"] += 1
            continue

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

        # [GUARD L03] BB bandwidth — MR only (no edge on flat bands)
        if strategy.get("strategy_type") == "mean_reversion":
            bb_skip, bb_reason = bb_bandwidth_guard(ind["bb"])
            if bb_skip:
                _log_skip(bot, pair, cycle, bb_reason)
                summary["skips"] += 1
                continue

        # ── GP vote once (live series): shadow log + L13 ensemble consensus ──
        # [GUARD L13] MR longs are blocked when GP consensus is bearish.
        # Previously ensemble_fn defaulted to "neutral" so L13 never engaged.
        # Use live prices here (no daily network fetch) so the heartbeat cycle
        # stays fast; promote path below re-votes on daily when GP_PROMOTE=1.
        gp_shadow_sig = _gp_vote(
            pair, prices, strategy, cortex=cortex,
            promote=False, use_daily=False,
        )
        gp_consensus = _log_gp_shadow(
            bot, pair, prices, strategy, cortex=cortex, sig=gp_shadow_sig,
        )

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

        # Injected ensemble_fn wins (tests); else live GP consensus for L13.
        ensemble = (
            (ensemble_fn(pair) if ensemble_fn else None)
            or gp_consensus
            or _GP_CONSENSUS_CACHE.get(pair)
            or "neutral"
        )
        atr = float(ind["atr"])

        # RSI-confluence: count pairs currently oversold (feeds momentum's
        # multi-pair gate). Computed as we scan so later pairs see earlier ones.
        _thr = _entry_rsi_threshold(strategy)
        if ind["rsi"] <= _thr:
            oversold_pairs += 1

        # --- entry evaluation ---------------------------------------------
        pos = open_positions.get(pair)
        if pos is None:
            from hermes_core.engines.entry_ranking import (
                entry_ranking_enabled,
                rank_candidates,
                score_candidate,
            )
            from hermes_core.engines.kelly_sizing import bayesian_p

            _rank_on = False
            try:
                _rank_on = entry_ranking_enabled()
            except Exception:  # noqa: BLE001
                _rank_on = False

            # Prefer pre-scanned multi-pair count so XAU sees XAG the same cycle.
            _os_count = max(int(oversold_pairs), int(oversold_total))
            trad_sig = evaluate_entry(
                pair, prices, strategy, context, ensemble,
                _os_count, vol_above, reentry, cycle, session_token,
            )
            gp_sig = None
            # GP promote gate: expectancy-driven per-pair ban/unban (seeds from
            # GP_EXCLUDE_PAIRS). Invent/shadow still run when banned.
            _want_gp = False
            if get_env("GP_PROMOTE") == "1":
                try:
                    from hermes_core.engines import gp_promote_gate as gpg
                    _want_gp = gpg.is_promote_allowed(bot, pair)
                except Exception:  # noqa: BLE001 — fail open to env list only
                    _excl = {
                        p.strip().upper()
                        for p in get_env("GP_EXCLUDE_PAIRS", "GBP/JPY,BTC/USD").split(",")
                        if p.strip()
                    }
                    _want_gp = pair.upper() not in _excl
            # Legacy: GP only if traditional quiet. Ranking: also score GP when
            # traditional fires so the better edge can win.
            if _want_gp and (trad_sig is None or _rank_on):
                try:
                    from hermes_core.engines import gp_intelligence as gpi
                    _sup, _reason = gpi.should_suppress(pair)
                    if _sup and trad_sig is None and not _rank_on:
                        _log_skip(bot, pair, cycle, f"gp_intel_suppress:{_reason}")
                        summary["skips"] += 1
                        continue
                    if not _sup:
                        gp_sig = _gp_vote(
                            pair, prices, strategy, cortex=cortex,
                            promote=True, use_daily=True,
                        )
                except Exception:  # noqa: BLE001 — GP must never break the cycle
                    gp_sig = None

            sig = None
            _rank_meta: dict = {
                "ranking_mode": "disabled",
                "rank_score": None,
                "rank_reason": None,
                "rank_candidates": [],
            }
            if _rank_on:
                cands: list[dict] = []
                for _s in (trad_sig, gp_sig):
                    if _s is None:
                        continue
                    _et = (
                        _s.meta.get("entry_type")
                        or getattr(_s, "type", None)
                        or "mean_reversion"
                    )
                    _wr = None
                    _pb = None
                    _ew = 1.0
                    try:
                        _st = cortex.edge_stats(pair, _et)
                        _wr = (
                            (_st["wins"] / _st["n"])
                            if _st and _st.get("n")
                            else cortex.entry_type_wr(_et, pair=pair)
                        )
                        if _st and _st.get("n"):
                            _pb = bayesian_p(
                                int(_st.get("wins") or 0),
                                int(_st.get("losses") or 0),
                            )
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        _soft_tmp = soft_weights_enabled()
                        _sup_tmp = bool(
                            policy is not None and policy.is_suppressed(pair, _et)
                        )
                        _ew = float(expert_weight(
                            enabled=_soft_tmp,
                            suppressed=_sup_tmp,
                            evidence_n=cortex.evidence_n(pair, _et),
                            wr=_wr,
                        ).get("weight") or 1.0)
                    except Exception:  # noqa: BLE001
                        _ew = 1.0
                    _sc = score_candidate(
                        entry_type=_et,
                        quality=getattr(_s, "quality", None),
                        wr=_wr,
                        p_bayes=_pb,
                        expert_weight=_ew,
                        gp_strength=_s.meta.get("gp_strength"),
                    )
                    cands.append({
                        "sig": _s,
                        "entry_type": _et,
                        "score": _sc["score"],
                        "components": _sc["components"],
                    })
                _picked = rank_candidates(cands)
                _win = _picked.get("winner")
                if _win is not None:
                    sig = _win["sig"]
                    _rank_meta = {
                        "ranking_mode": "soft",
                        "rank_score": _win.get("score"),
                        "rank_reason": _picked.get("reason"),
                        "rank_candidates": _picked.get("ranked") or [],
                    }
            else:
                sig = trad_sig if trad_sig is not None else gp_sig

            if sig is None:
                _log_skip(bot, pair, cycle, "no_signal")
                summary["skips"] += 1
                continue
            # [GUARD L35] policy may bench GP or MR when the other type is clearly better.
            # Prefer meta.entry_type; fall back to Signal.type so momentum is never
            # mis-labelled as mean_reversion (which poisoned cortex/policy WRs).
            _etype = sig.meta.get("entry_type") or getattr(sig, "type", None) or "mean_reversion"
            _suppressed = bool(
                policy is not None and policy.is_suppressed(pair, _etype)
            )
            # HIF Phase-2: soft weights turn L35 benches into size shrinks.
            # Flag OFF → hard skip (legacy). Flag ON → never skip for policy.
            _soft = False
            try:
                _soft = soft_weights_enabled()
            except Exception:  # noqa: BLE001
                _soft = False
            if _suppressed and not _soft:
                _log_skip(bot, pair, cycle, f"policy_suppress:{_etype}")
                summary["skips"] += 1
                continue
            # RR guard (S6) — reject R:R < 1.0 before committing
            sl = float(strategy["stop_loss_pct"])
            tp = float(strategy["profit_target_pct"])
            if not check_rr_guard(sl, tp):
                _log_skip(bot, pair, cycle, "rr_guard")
                summary["skips"] += 1
                continue
            # Base size from MARKET regime (trend/range + fast direction), NOT
            # session token — LDN/NY were incorrectly hitting NEUTRAL ×0.6 always.
            _size_regime = size_regime_from_market(
                ind.get("regime") or regimes.get(pair),
                ind.get("fast_regime"),
            )
            _open_bullish = sum(1 for p in open_positions if p != pair)
            size = compute_position_size(
                _size_regime, atr, _open_bullish, strategy,
            )
            # HIF Phase-1 probe sizing: shrink only when PROBE_SIZING=1 and
            # cortex evidence for (pair, entry_type) is thin. Never skips.
            # Fail-open to full size if cortex cannot be read.
            _probe_enabled = get_env("PROBE_SIZING", "0") == "1"
            _evidence_n: int | None = None
            if _probe_enabled or _soft:
                try:
                    _evidence_n = int(cortex.evidence_n(pair, _etype))
                except Exception:  # noqa: BLE001 — fail-open → full
                    _evidence_n = None
            _probe = apply_probe_sizing(
                size, enabled=_probe_enabled, evidence_n=_evidence_n,
            )
            size = float(_probe["size"])
            # HIF: momentum range/confluence guard (Jul 23 gold — chop lesson).
            _mg = {
                "mom_guard_mode": "disabled",
                "mom_guard_action": "disabled",
                "mom_guard_confirmed": False,
                "mom_guard_reasons": [],
                "oversold_count": int(oversold_total),
                "gp_agree": False,
            }
            try:
                _mg_on = mom_range_guard_enabled(bot=bot)
                _gp_str = None
                if gp_shadow_sig is not None:
                    with contextlib.suppress(Exception):
                        _gp_str = gp_shadow_sig.meta.get("gp_strength")
                if gp_sig is not None and _gp_str is None:
                    with contextlib.suppress(Exception):
                        _gp_str = gp_sig.meta.get("gp_strength")
                _gp_ok = gp_agree_bullish(ensemble, gp_strength=_gp_str)
                _mg = apply_mom_range_guard(
                    size,
                    enabled=_mg_on,
                    entry_type=_etype,
                    regime=ind.get("regime") or regimes.get(pair),
                    oversold_count=max(int(oversold_pairs), int(oversold_total)),
                    gp_agree=_gp_ok,
                )
                if _mg.get("mom_guard_action") == "bench":
                    _log_skip(
                        bot, pair, cycle,
                        "mom_range_bench:" + ",".join(_mg.get("mom_guard_reasons") or []),
                    )
                    summary["skips"] += 1
                    continue
                size = float(_mg["size"])
            except Exception:  # noqa: BLE001 — fail-open
                pass
            # HIF Phase-2 soft expert weight (after probe, before cap).
            _wr = None
            try:
                _wr = cortex.entry_type_wr(_etype, pair=pair)
            except Exception:  # noqa: BLE001
                _wr = None
            _winfo = expert_weight(
                enabled=_soft,
                suppressed=_suppressed,
                evidence_n=_evidence_n,
                wr=_wr,
            )
            _weighted = apply_expert_weight(size, _winfo)
            size = float(_weighted["size"])
            # HIF Phase-3 soft regime size mult (never skips).
            _reg_on = False
            try:
                _reg_on = regime_sizing_enabled()
            except Exception:  # noqa: BLE001
                _reg_on = False
            _regime = apply_regime_sizing(
                size,
                enabled=_reg_on,
                regime=ind.get("regime") or regimes.get(pair),
                fast_regime=ind.get("fast_regime"),
                adx=ind.get("adx"),
            )
            size = float(_regime["size"])
            # HIF Phase-5 Bayesian fractional Kelly (never skips).
            _kelly_on = False
            try:
                _kelly_on = kelly_sizing_enabled()
            except Exception:  # noqa: BLE001
                _kelly_on = False
            _edge = {"wins": 0, "losses": 0, "avg_win": None, "avg_loss": None}
            if _kelly_on:
                try:
                    _edge = cortex.edge_stats(pair, _etype) or _edge
                except Exception:  # noqa: BLE001
                    pass
            _rr_b = None
            try:
                if sl > 0:
                    _rr_b = float(tp) / float(sl)
            except (TypeError, ValueError, ZeroDivisionError):
                _rr_b = None
            _kelly = apply_kelly_sizing(
                size,
                enabled=_kelly_on,
                wins=int(_edge.get("wins") or 0),
                losses=int(_edge.get("losses") or 0),
                avg_win=_edge.get("avg_win"),
                avg_loss=_edge.get("avg_loss"),
                rr_b=_rr_b,
            )
            size = float(_kelly["size"])
            # HIF book-level risk (after Kelly, before hard cap).
            from hermes_core.engines.book_risk import apply_book_risk, book_risk_enabled
            _book_on = False
            try:
                _book_on = book_risk_enabled()
            except Exception:  # noqa: BLE001
                _book_on = False
            _book = apply_book_risk(
                size,
                enabled=_book_on,
                open_positions=open_positions,
                pair=pair,
                entry_type=_etype,
                cortex=cortex,
            )
            size = float(_book["size"])
            # HIF exit intelligence — stamp knobs only (no size / fill change).
            from hermes_core.engines.exit_intel import apply_exit_intel, exit_intel_enabled
            _xi_on = False
            try:
                _xi_on = exit_intel_enabled()
            except Exception:  # noqa: BLE001
                _xi_on = False
            _xi = apply_exit_intel(
                enabled=_xi_on,
                pair=pair,
                entry_type=_etype,
                strategy=strategy,
                cortex=cortex,
            )
            # Trail + honor ATR/BE stops so protectors can fire before time_exit
            # (EXIT_INTEL may override trail; YAML / default 1.5 otherwise).
            from hermes_core.engines.exit import (
                DEFAULT_MFE_GIVEBACK_FRAC,
                DEFAULT_MFE_GIVEBACK_MIN_PCT,
                DEFAULT_TIME_EXIT_CYCLES,
            )
            _trail = _xi.get("trailing_atr_mult")
            if _trail is None:
                try:
                    _trail = float(strategy.get("trailing_atr_mult", 1.5))
                except (TypeError, ValueError):
                    _trail = 1.5
            _honor = bool(_xi.get("honor_current_stop")) or _trail is not None
            try:
                _gb_min = float(strategy.get(
                    "mfe_giveback_min_pct", DEFAULT_MFE_GIVEBACK_MIN_PCT,
                ))
            except (TypeError, ValueError):
                _gb_min = DEFAULT_MFE_GIVEBACK_MIN_PCT
            try:
                _gb_frac = float(strategy.get(
                    "mfe_giveback_frac", DEFAULT_MFE_GIVEBACK_FRAC,
                ))
            except (TypeError, ValueError):
                _gb_frac = DEFAULT_MFE_GIVEBACK_FRAC
            _gb_on = strategy.get("mfe_giveback_enabled", True) is not False
            stop = _atr_stop_for(strategy, price, atr)
            open_positions[pair] = {
                "id": f"{bot}:{pair}:{int(time.time())}",
                "entry_ts": _now_iso(),
                "entry_price": price, "size": min(size, MAX_POSITION_SIZE),
                "stop_loss_pct": sl, "profit_target_pct": tp,
                "time_exit_cycles": int(strategy.get(
                    "time_exit_cycles", DEFAULT_TIME_EXIT_CYCLES,
                )),
                "held_cycles": 0, "breakeven_set": False, "partial_done": False,
                "partial_enabled": bool(_xi.get("partial_enabled")),
                "current_stop": stop, "atr": atr,
                "entry_type": _etype,
                "strategy_version": str(strategy.get("version", "00")),
                # B9: firing GP indicator IDs so that on close ONLY these are
                # credited (per-vote credit, not the whole ensemble blob).
                "gp_indicators": sig.meta.get("gp_indicators", []),
                # HIF Phase-1 dashboard fields
                "size_mode": _probe["size_mode"],
                "size_regime": _size_regime,
                "evidence_n": _probe.get("evidence_n") if _probe_enabled else _evidence_n,
                "evidence_state": _probe["evidence_state"],
                "base_size": _probe.get("base_size"),
                "probe_fraction": _probe.get("probe_fraction"),
                # HIF Phase-2 dashboard fields
                "expert_weight": _weighted.get("expert_weight"),
                "expert_mode": _weighted.get("expert_mode"),
                "suppressed_soft": _weighted.get("suppressed_soft"),
                "expert_reasons": _weighted.get("expert_reasons") or [],
                # HIF Phase-3 dashboard fields
                "regime_mult": _regime.get("regime_mult"),
                "regime_label": _regime.get("regime_label"),
                "regime_mode": _regime.get("regime_mode"),
                "fast_regime": _regime.get("fast_regime"),
                "entry_regime": _regime.get("regime") or regimes.get(pair),
                # HIF Phase-5 dashboard fields
                "kelly_mult": _kelly.get("kelly_mult"),
                "kelly_mode": _kelly.get("kelly_mode"),
                "kelly_f": _kelly.get("kelly_f"),
                "p_bayes": _kelly.get("p_bayes"),
                "ci_low": _kelly.get("ci_low"),
                "ci_high": _kelly.get("ci_high"),
                "kelly_reasons": _kelly.get("reasons") or [],
                # HIF Layer B entry ranking
                "ranking_mode": _rank_meta.get("ranking_mode"),
                "rank_score": _rank_meta.get("rank_score"),
                "rank_reason": _rank_meta.get("rank_reason"),
                "rank_candidates": _rank_meta.get("rank_candidates") or [],
                # HIF book risk
                "book_mode": _book.get("book_mode"),
                "book_mult": _book.get("book_mult"),
                "book_tilt": _book.get("book_tilt"),
                "book_used": _book.get("book_used"),
                "book_cap": _book.get("book_cap"),
                "book_remaining": _book.get("book_remaining"),
                "book_reasons": _book.get("book_reasons") or [],
                # HIF exit intelligence + baseline trail (trail before time_exit)
                "exit_intel_mode": _xi.get("exit_intel_mode"),
                "honor_current_stop": _honor,
                "be_trigger_frac": _xi.get("be_trigger_frac"),
                "trailing_atr_mult": _trail,
                "exit_intel_n": _xi.get("exit_intel_n"),
                "exit_intel_reasons": _xi.get("exit_intel_reasons") or [],
                "avg_giveback_frac": _xi.get("avg_giveback_frac"),
                # MFE giveback hard exit (locks winners before time_exit)
                "mfe_giveback_enabled": _gb_on,
                "mfe_giveback_min_pct": _gb_min,
                "mfe_giveback_frac": _gb_frac,
                # HIF momentum range / confluence guard
                "mom_guard_mode": _mg.get("mom_guard_mode"),
                "mom_guard_action": _mg.get("mom_guard_action"),
                "mom_guard_confirmed": _mg.get("mom_guard_confirmed"),
                "mom_guard_reasons": _mg.get("mom_guard_reasons") or [],
                "mom_oversold_count": _mg.get("oversold_count"),
                "mom_gp_agree": _mg.get("gp_agree"),
                # MFE/MAE peak tracking (always updated — needed for giveback exit;
                # MFE_TRACKING still gates cortex / trade-log fields)
                "peak_mfe_pct": 0.0,
                "trough_mae_pct": 0.0,
                "mfe_tracking": False,
            }
            # [CORTEX] record the entry (per-type memory; exile persists across cycles)
            with contextlib.suppress(Exception):
                cortex.record_entry(pair, _etype)
            summary["entries"].append(pair)
        else:
            # --- exit evaluation (S5) --------------------------------------
            pos["held_cycles"] = pos.get("held_cycles", 0) + 1
            pos["unrealised_pct"] = (price - pos["entry_price"]) / pos["entry_price"] * 100.0
            # Soft-migrate positions opened before giveback/trail defaults so
            # live opens immediately stop donating MFE to time_exit.
            with contextlib.suppress(Exception):
                from hermes_core.engines.exit import (
                    DEFAULT_MFE_GIVEBACK_FRAC,
                    DEFAULT_MFE_GIVEBACK_MIN_PCT,
                )
                if "mfe_giveback_min_pct" not in pos:
                    pos["mfe_giveback_min_pct"] = DEFAULT_MFE_GIVEBACK_MIN_PCT
                if "mfe_giveback_frac" not in pos:
                    pos["mfe_giveback_frac"] = DEFAULT_MFE_GIVEBACK_FRAC
                if "mfe_giveback_enabled" not in pos:
                    pos["mfe_giveback_enabled"] = True
                if pos.get("trailing_atr_mult") is None:
                    pos["trailing_atr_mult"] = 1.5
                if not pos.get("honor_current_stop"):
                    pos["honor_current_stop"] = True
                # Tighten legacy 288-cycle clocks in-flight (≤150).
                te = pos.get("time_exit_cycles")
                if te is not None and int(te) > 150:
                    pos["time_exit_cycles"] = 150
            # Always update peak MFE/MAE so mfe_giveback can fire; mfe_tracking
            # flag is informational for dashboard (closes always log excursions).
            with contextlib.suppress(Exception):
                from hermes_core.engines.excursion import (
                    mfe_tracking_enabled,
                    update_position_excursions,
                )
                update_position_excursions(pos, pos["unrealised_pct"])
                if mfe_tracking_enabled():
                    pos["mfe_tracking"] = True
            ex = evaluate_exit(pos, price, prices)
            if ex is not None:
                _process_exit(
                    bot, pair, cycle, pos, price, ex,
                    cortex=cortex, reentry=reentry,
                    open_positions=open_positions, summary=summary,
                    alert_fn=alert_fn,
                    prices=prices,
                    chart_context=chart_contexts.get(pair, context),
                    goal=cfg.get("goal"),
                )

    # HIF Phase-4: skip + GP-shadow observational learning (shadow notes only).
    try:
        from hermes_core.engines.skip_shadow_learn import (
            maybe_promote_skip_shadow,
            maybe_skip_shadow_learn,
        )
        _strats: dict = {}
        for p in pairs:
            with contextlib.suppress(Exception):
                _strats[p] = load_strategy_for_pair(p, bot)
        summary["skip_shadow"] = maybe_skip_shadow_learn(
            bot, list(pairs), strategies=_strats,
        )
        # HIF: gated promote of deployable skip_shadow_proposed (never blind).
        summary["skip_shadow_promote"] = maybe_promote_skip_shadow(
            bot, strategies=_strats,
        )
    except Exception:  # noqa: BLE001
        summary["skip_shadow"] = {"enabled": False}
        summary["skip_shadow_promote"] = {"enabled": False}

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
    run_cycle._regimes = dict(regimes)
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
