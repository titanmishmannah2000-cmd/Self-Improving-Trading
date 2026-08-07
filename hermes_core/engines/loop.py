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
from datetime import UTC, datetime
from pathlib import Path


def _now_iso() -> str:
    """UTC ISO-8601 timestamp (matches the dashboard's entry_ts/exit_ts format)."""
    return datetime.now(UTC).isoformat()


from hermes_core.adapters import make_default_fetch
from hermes_core.config import load_config, load_strategy_for_pair, state_root
from hermes_core.engines.chart_vision import (
    apply_chart_soft_to_signal,
    chart_size_mult,
    hard_block,
)
from hermes_core.engines.size_stamp import resolve_size_stamp
from hermes_core.engines.crisis_learning import (
    check_novel_regime,
    recommend_from_prices,
    save_adverse_lived_crisis,
    soft_widen_stop,
)
from hermes_core.engines.decision_cortex import Cortex
from hermes_core.engines.entry import (
    _entry_rsi_threshold,
    evaluate_entry_detailed,
)
from hermes_core.engines.exit import evaluate_exit
from hermes_core.engines.expert_weights import apply_expert_weight, expert_weight
from hermes_core.engines.genetic import discover as gp_discover
from hermes_core.engines.guards import bb_bandwidth_guard, flat_price_guard
from hermes_core.engines.kelly_sizing import apply_kelly_sizing, kelly_sizing_enabled
from hermes_core.engines.market_hours import is_bot_market_closed, live_book_is_flat
from hermes_core.engines.mom_range_guard import (
    apply_mom_range_guard,
    gp_agree_bullish,
    mom_range_guard_enabled,
)
from hermes_core.engines.policy_engine import PolicyEngine, soft_weights_enabled
from hermes_core.engines.regime_sizing import apply_regime_sizing, regime_sizing_enabled
from hermes_core.engines.risk import (
    MAX_POSITION_SIZE,
    apply_probe_sizing,
    check_rr_guard,
    compute_atr_stop,
    compute_position_size,
    param_range_gate,
    size_regime_from_market,
)
from hermes_core.engines.soak_controls import (
    DATA_HALT_EXIT_AFTER,
    append_bb_sample,
    append_trade,
    book_drawdown_status,
    ensure_state_files,
    entries_halted,
    feed_error_rate,
    idle_skip_slo,
    maybe_recover_halt,
    price_sanity_book,
    price_sanity_pair,
    rotate_jsonl_if_large,
    save_open_book,
    write_halt,
)
from hermes_core.env import get_env, load_env
from hermes_core.indicators import compute_all
from hermes_core.state.atomic_json import atomic_write_json

MAX_CONSECUTIVE_FAILURES = 5  # [GUARD L24]
CIRCUIT_SLEEP_S = 300  # 5-minute pause on circuit open
CYCLE_SECONDS = 60  # 60s cadence
# Discovery is expensive (GP evolution over price history); throttle per
# (bot, pair) so it runs at most once per ~hour of wall-clock, or on first run.
DISCOVERY_INTERVAL_S = int(get_env("DISCOVERY_INTERVAL_S", "3600"))
# When votable formulas already exist, still re-invent on this longer cadence
# (default 6h). Without this, invent freezes forever on the first admit and
# the Discovered UI never shows new exprs.
DISCOVERY_REINVENT_INTERVAL_S = int(get_env("DISCOVERY_REINVENT_INTERVAL_S", str(6 * 3600)))
# After this many consecutive invent timeouts, shrink the search budget.
# First timeout already means the full budget is too heavy for this host —
# shrink immediately so the next pass can finish and land new formulas.
DISCOVERY_TIMEOUT_SHRINK_AFTER = int(get_env("DISCOVERY_TIMEOUT_SHRINK_AFTER", "1"))
# After this many consecutive invent timeouts, skip invent for a cooldown window.
DISCOVERY_TIMEOUT_SKIP_AFTER = int(get_env("DISCOVERY_TIMEOUT_SKIP_AFTER", "4"))
DISCOVERY_TIMEOUT_COOLDOWN_S = int(get_env("DISCOVERY_TIMEOUT_COOLDOWN_S", "3600"))
# Soft Discord alert when admit_zero streaks this high (0 disables).
DISCOVERY_ADMIT_ZERO_ALERT_AFTER = int(get_env("DISCOVERY_ADMIT_ZERO_ALERT_AFTER", "5"))
# After hard-timeout abandon, wait this long for the zombie invent thread to
# finish before starting the next pair (prevents stacking GP workers → more timeouts).
# Keep short: write_token already fences late disk writes.
DISCOVERY_ABANDON_DRAIN_S = int(get_env("DISCOVERY_ABANDON_DRAIN_S", "90"))
_DISCOVERY_LAST: dict[tuple[str, str], float] = {}  # (bot, pair) -> last pass epoch
_DISCOVERY_LAST_INVENT: dict[tuple[str, str], float] = {}  # (bot, pair) -> last full invent
# Per-bot wall-clock of last discovery pass (any outcome) — surfaces on heartbeat
# as last_discovery_run_ts for dashboard stale-day metrics.
_LAST_DISCOVERY_RUN: dict[str, float] = {}
# In-flight invents: timeout abandons the waiter but the worker may still run;
# keep the key until the worker exits so we don't spawn another invent on top.
_DISCOVERY_IN_FLIGHT: set[tuple[str, str]] = set()
_DISCOVERY_TIMEOUT_STREAK: dict[tuple[str, str], int] = {}
_DISCOVERY_TIMEOUT_COOLDOWN_UNTIL: dict[tuple[str, str], float] = {}
_DISCOVERY_ADMIT_ZERO_STREAK: dict[tuple[str, str], int] = {}


def _drain_bot_invents(bot: str, *, max_wait_s: float | None = None) -> None:
    """Block until abandoned invent workers for ``bot`` exit (or max_wait).

    Hard-timeout abandons the waiter but leaves the GP thread running. Starting
    the next pair's invent on top of that zombie saturates CPU and causes the
    whole discovery pass to time out — Discovered then looks "stuck".
    """
    budget = float(max_wait_s if max_wait_s is not None else DISCOVERY_ABANDON_DRAIN_S)
    deadline = time.time() + max(0.0, budget)
    while time.time() < deadline:
        inflight = [k for k in list(_DISCOVERY_IN_FLIGHT) if k[0] == bot]
        if not inflight:
            return
        print(
            f"[hermes][discovery] {bot}: draining abandoned invent {inflight}",
            flush=True,
        )
        time.sleep(2.0)
    # Force-clear so one permanently wedged worker cannot freeze discovery forever.
    leftover = [k for k in list(_DISCOVERY_IN_FLIGHT) if k[0] == bot]
    for k in leftover:
        _DISCOVERY_IN_FLIGHT.discard(k)
        print(
            f"[hermes][discovery] {bot}: force-cleared stale in_flight {k}",
            flush=True,
        )


def _state_dir(bot: str) -> Path:
    """Per-bot runtime-state dir on the PERSISTENT volume (HERMES_STATE_ROOT,
    e.g. /data), NOT inside the read-only image (/app). live_compat reads
    these same paths, so bot writes and dashboard reads line up. [D3/3.1]
    """
    d = state_root() / bot / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_token_for(hour: int) -> str:
    """Resolve an hour to a session token (blueprint _get_session).

    LDN/NY overlap (13–16 UTC) returns ``LDN_NY`` so ny_only strategies can fire.
    """
    h = int(hour) % 24
    if 0 <= h < 8:
        return "ASIA"
    if 13 <= h < 17:
        return "LDN_NY"  # London/NY overlap
    if 8 <= h < 17:
        return "LDN"
    if 17 <= h < 21:
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
            hist = history_fn(pair) if history_fn is not None else fetch_fn(pair + ":history")
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
    """ATR stop price, never tighter than ``stop_loss_pct`` (long: lower stop).

    ``honor_current_stop`` used to arm a sub-``stop_loss_pct`` ATR floor (~0.3%)
    that exited as ``stop_loss`` before the YAML SL mattered. Clamp so %-SL is
    the minimum initial risk distance.
    """
    mult = float(strategy.get("atr_multiplier", 1.5))
    floor = float(strategy.get("atr_floor_pct", 0.0))
    use_floor = strategy.get("use_atr_floor", True) is not False
    stop = compute_atr_stop(entry, atr, mult, floor, use_atr_floor=use_floor)
    try:
        sl_pct = float(strategy.get("stop_loss_pct") or 0.0)
    except (TypeError, ValueError):
        sl_pct = 0.0
    if sl_pct > 0 and entry > 0:
        sl_stop = float(entry) * (1.0 - sl_pct / 100.0)
        # Wider long stop = lower price → take the farther (min) stop.
        stop = min(float(stop), sl_stop)
    return stop


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
    hif_flags: dict | None = None,
    regime_split: dict | None = None,
    btc_d1_regimes: dict | None = None,
    sentient: dict | None = None,
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
    if btc_d1_regimes:
        data["btc_d1_regimes"] = btc_d1_regimes
    if hif_flags is not None:
        data["hif_flags"] = hif_flags
    if regime_split is not None:
        data["regime_split"] = regime_split
    if sentient is not None:
        data["sentient"] = sentient
    with contextlib.suppress(Exception):
        from hermes_core.env import llm_keys_present

        data["llm_keys"] = llm_keys_present()
    disc_ts = _LAST_DISCOVERY_RUN.get(asset)
    if disc_ts:
        data["last_discovery_run_ts"] = datetime.fromtimestamp(disc_ts, UTC).isoformat()
    try:
        atomic_write_json(HEARTBEAT_PATH, data)
    except OSError:
        # heartbeat itself cannot break the loop; best-effort only
        pass
    return data


def _log_skip(bot: str, pair: str, cycle: int, reason: str) -> None:
    """Append a skip row, coalescing identical reasons so the feed is not spam.

    Same (pair, reason) within 60 cycles updates an in-memory latch only —
    we rewrite at most every 60 cycles so dashboards still see a fresh ts.
    """
    latch = getattr(run_cycle, "_skip_latch", None)
    if not isinstance(latch, dict):
        latch = {}
        run_cycle._skip_latch = latch
    key = f"{bot}:{pair}"
    prev = latch.get(key) if isinstance(latch.get(key), dict) else None
    if (
        prev
        and str(prev.get("reason") or "") == str(reason)
        and int(cycle) - int(prev.get("cycle") or 0) < 60
    ):
        return
    latch[key] = {"reason": str(reason), "cycle": int(cycle)}

    SKIPS_PATH = _state_dir(bot) / "skips.jsonl"
    # `reason_skipped` is the dashboard's DB column key; keep `reason` too for
    # any consumer that read the legacy key.
    row = {
        "ts": time.time(),
        "pair": pair,
        "cycle": cycle,
        "reason": reason,
        "reason_skipped": reason,
    }
    try:
        with open(SKIPS_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
    except OSError:
        pass


def _capture_chart_context(
    pair: str,
    *,
    chart_context_fn: Callable | None,
    chart_contexts: dict[str, str],
    health_registry: dict,
    bot: str,
    cycle: int,
) -> str:
    """Fetch chart vision for ``pair`` and stamp heartbeat health.

    Called as soon as a pair has a live quote/regime so early entry guards
    (flat-price, flatline pause, BB, etc.) cannot leave ``chart_contexts`` empty
    for priced pairs (e.g. BTC missing while ETH is present).
    """
    _UNUSABLE_EXACT = (
        "",
        "chart data unavailable.",
        "chart generation failed.",
    )
    context = ""
    try:
        if chart_context_fn is None:
            chart_contexts[pair] = ""
            health_registry.setdefault("chart_vision", False)
            return ""
        context = chart_context_fn(pair) or ""
        chart_contexts[pair] = context
        low = str(context).strip().lower()
        usable = bool(low) and low not in _UNUSABLE_EXACT and not low.startswith(
            "chart: unavailable"
        )
        if usable:
            health_registry["chart_vision"] = True
        else:
            health_registry.setdefault("chart_vision", False)
    except Exception as exc:  # noqa: BLE001 — fail-open, never crash
        context = ""
        chart_contexts[pair] = ""
        health_registry["chart_vision"] = False
        _log_skip(bot, pair, cycle, f"chart_error:{exc!r}")
    return context


def _log_trade(bot: str, rec: dict) -> bool:
    """Append a closed-trade row. Returns False if disk write failed."""
    return append_trade(bot, rec)


def _process_exit(
    bot,
    pair,
    cycle,
    pos,
    price,
    ex,
    *,
    cortex,
    reentry,
    open_positions,
    summary,
    alert_fn,
    prices=None,
    chart_context="",
    goal=None,
) -> None:
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
        _log_trade(
            bot,
            {
                "id": (pos.get("id") or f"{bot}:{pair}:{int(time.time())}") + ":partial",
                "bot": bot,
                "pair": pair,
                "cycle": cycle,
                "reason": ex.reason,
                "exit_reason": ex.reason,
                "entry_type": entry_type,
                "strategy_version": pos.get("strategy_version") or entry_type,
                "entry_price": pos["entry_price"],
                "exit_price": price,
                "entry_ts": pos.get("entry_ts"),
                "exit_ts": _now_iso(),
                "pnl_pct": pnl,
                "size": closed_size,
                "hold_cycles": pos.get("held_cycles", 0),
                "partial": True,
                **{
                    k: _exc[k]
                    for k in ("mfe_pct", "mae_pct", "giveback_pct", "giveback_frac", "mfe_capture")
                    if k in _exc
                },
            },
        )
        with contextlib.suppress(Exception):
            is_gp = bool(pos.get("gp_indicators")) or entry_type in (
                "gp_ensemble", "shadow",
            )
            _record_type = "gp_ensemble" if is_gp else entry_type
            cortex.record_outcome(
                pair,
                _record_type,
                pnl,
                mfe_pct=_exc.get("mfe_pct"),
                mae_pct=_exc.get("mae_pct"),
                giveback_pct=_exc.get("giveback_pct"),
                giveback_frac=_exc.get("giveback_frac"),
                mfe_capture=_exc.get("mfe_capture"),
                partial=True,
            )
        pos["size"] = remain
        pos["partial_done"] = True
        pos["soft_partial_done"] = True
        pos["breakeven_set"] = True
        if ex.new_stop is not None:
            pos["current_stop"] = ex.new_stop
            pos["stop_source"] = "clock_protect"
        return
    # --- REAL close: log the trade BEFORE deleting the open (durability).
    entry_type = pos.get("entry_type", "mean_reversion")
    _side = str(pos.get("side") or "long")
    _entry_mid = float(pos.get("entry_mid") or pos["entry_price"])
    _exit_mid = float(price)
    _exit_fill = _exit_mid
    _entry_fill = float(pos["entry_price"])
    with contextlib.suppress(Exception):
        from hermes_core.engines.cost_model import apply_exit_fill

        _exit_fill = apply_exit_fill(
            _exit_mid, _side, float(pos.get("exit_haircut_pct") or 0.0)
        )
    if _side.lower() in ("short", "sell"):
        gross = (_entry_mid - _exit_mid) / _entry_mid * 100.0 if _entry_mid else 0.0
        net = (_entry_fill - _exit_fill) / _entry_fill * 100.0 if _entry_fill else 0.0
    else:
        gross = (_exit_mid - _entry_mid) / _entry_mid * 100.0 if _entry_mid else 0.0
        net = (_exit_fill - _entry_fill) / _entry_fill * 100.0 if _entry_fill else 0.0
    fees_pct = float(pos.get("fees_pct_rt") or 0.0)
    # Prefer explicit fee field; if missing, infer from gross−net.
    if fees_pct <= 0 and abs(gross - net) > 1e-12:
        fees_pct = abs(gross - net)
    pnl = net  # authoritative closed PnL is cost-aware
    pos["unrealised_pct"] = pnl
    _exc = {}
    with contextlib.suppress(Exception):
        from hermes_core.engines.excursion import excursion_from_position

        _exc = excursion_from_position(pos, pnl)
    trade_rec = {
        "id": pos.get("id") or f"{bot}:{pair}:{int(time.time())}",
        "bot": bot,
        "pair": pair,
        "cycle": cycle,
        "reason": ex.reason,
        "exit_reason": ex.reason,
        "entry_type": entry_type,
        "strategy_version": pos.get("strategy_version"),
        "entry_price": _entry_fill,
        "entry_mid": _entry_mid,
        "exit_price": _exit_fill,
        "exit_mid": _exit_mid,
        "entry_ts": pos.get("entry_ts"),
        "exit_ts": _now_iso(),
        "gross_pnl_pct": round(gross, 6),
        "fees_pct": round(fees_pct, 6),
        "slippage_pct": round(
            float((pos.get("cost_model") or {}).get("slippage_pct_one_way") or 0.0) * 2.0,
            6,
        ),
        "net_pnl_pct": round(net, 6),
        "pnl_pct": round(net, 6),
        "size": pos["size"],
        "hold_cycles": pos.get("held_cycles", 0),
        "cost_model": pos.get("cost_model"),
        # Phase 5.1: stamp entry regime so reflection can build same-regime batches.
        "entry_regime": pos.get("entry_regime") or pos.get("regime_label") or pos.get("regime"),
        "size_mode": pos.get("size_mode"),
        "size_reason": pos.get("size_reason"),
        "probe_fraction": pos.get("probe_fraction"),
        "entry_decision": pos.get("entry_decision"),
        "entry_conviction": pos.get("entry_conviction"),
        "entry_sleeve": pos.get("entry_sleeve"),
        "chart_size_mult": pos.get("chart_size_mult"),
        "chart_soft_reasons": list(pos.get("chart_soft_reasons") or []),
        "base_size": pos.get("base_size"),
        "decision_source": "stamped" if pos.get("entry_decision") is not None else "unknown",
        "stop_loss_pct": pos.get("stop_loss_pct"),
        "profit_target_pct": pos.get("profit_target_pct"),
        **{
            k: _exc[k]
            for k in ("mfe_pct", "mae_pct", "giveback_pct", "giveback_frac", "mfe_capture")
            if k in _exc
        },
    }
    _mfe_path = list(pos.get("mfe_path") or [])
    if len(_mfe_path) >= 3:
        trade_rec["mfe_path"] = _mfe_path
    with contextlib.suppress(Exception):
        from hermes_core.engines.trade_truth import append_entry_taken

        append_entry_taken(
            bot,
            {
                "id": trade_rec["id"],
                "pair": pair,
                "event": "close",
                "entry_type": entry_type,
                "entry_decision": pos.get("entry_decision"),
                "size_mode": pos.get("size_mode"),
                "size": pos.get("size"),
                "base_size": pos.get("base_size"),
                "entry_mid": _entry_mid,
                "exit_reason": ex.reason,
                "pnl_pct": trade_rec.get("pnl_pct"),
            },
        )
    with contextlib.suppress(Exception):
        from hermes_core.engines.outcome_class import stamp_exit_class

        trade_rec["exit_class"] = stamp_exit_class(ex.reason)
        trade_rec["soft_bank"] = trade_rec["exit_class"] == "soft_capture"
        if pos.get("exit_votes"):
            trade_rec["exit_votes"] = pos.get("exit_votes")
    with contextlib.suppress(Exception):
        from hermes_core.engines.playbooks import update_playbook_on_close
        from hermes_core.engines.sentient_entry import _playbook_stats

        _d1 = str(pos.get("live_d1") or pos.get("d1") or pos.get("entry_regime") or "")
        update_playbook_on_close(
            bot=bot,
            pair=pair,
            entry_type=str(entry_type),
            d1=_d1,
            pnl=float(pnl),
            mfe=_exc.get("mfe_pct"),
            capture=_exc.get("mfe_capture"),
            hold_cycles=pos.get("held_cycles"),
        )
        _pb = _playbook_stats(bot, pair, str(entry_type), _d1)
        _pb_wr = float(_pb.get("wr") or 0.5) if _pb else 0.5
        _world = 1.0
        with contextlib.suppress(Exception):
            w = pos.get("world") or {}
            if isinstance(w, dict) and w.get("mult") is not None:
                _world = float(w.get("mult"))
        from hermes_core.engines.sentient_entry import credit_entry_on_close

        credit_entry_on_close(
            bot,
            entry_type=str(entry_type),
            conviction=pos.get("entry_conviction"),
            pnl=float(pnl),
            playbook_wr=_pb_wr,
            world_mult=_world,
        )
    with contextlib.suppress(Exception):
        from hermes_core.engines.sentient_entry import release_alt_quota_on_green

        release_alt_quota_on_green(
            bot, entry_type=str(entry_type), pnl=float(pnl)
        )
    if str(ex.reason) == "failed_breakout":
        with contextlib.suppress(Exception):
            from hermes_core.engines.sentient_entry import note_failed_breakout_cooldown
            from hermes_core.config.loader import load_strategy_for_pair

            _strat = {}
            with contextlib.suppress(Exception):
                _strat = load_strategy_for_pair(pair, bot) or {}
            note_failed_breakout_cooldown(
                bot,
                pair,
                entry_type=str(entry_type),
                current_cycle=int(cycle),
                strategy=_strat,
            )
    with contextlib.suppress(Exception):
        from hermes_core.engines import counterfactual_exits as cfe
        from hermes_core.engines import hold_policy as hp
        from hermes_core.state.paths import bot_state_dir

        path_pts = list(pos.get("mfe_path") or [])
        if len(path_pts) >= 3:
            labels = cfe.label_hold_vs_bank(
                path_pts, cost_pct=float(pos.get("exit_haircut_pct") or 0.0)
            )
            feats = []
            tpv = max(float(pos.get("profit_target_pct") or 1.5), 1e-6)
            for i, p in enumerate(path_pts):
                peak = float(p.get("peak") or 0.0)
                u = float(p.get("unreal") or 0.0)
                bars_since = max(0, len(path_pts) - 1 - i)
                # Prefer stamped counter when present on the point / position.
                if p.get("bars_since_peak") is not None:
                    try:
                        bars_since = int(p.get("bars_since_peak"))
                    except (TypeError, ValueError):
                        pass
                elif pos.get("exit_bars_since_peak") is not None and i == len(path_pts) - 1:
                    try:
                        bars_since = int(pos.get("exit_bars_since_peak"))
                    except (TypeError, ValueError):
                        pass
                fresh = 1.0 / (1.0 + float(bars_since))
                feats.append(
                    {
                        "progress": max(0.0, min(1.0, peak / tpv)),
                        "fresh": fresh,
                        "capture": (u / peak) if peak > 1e-9 else 0.0,
                    }
                )
            pol_path = bot_state_dir(bot) / "hold_policy.json"
            pol = hp.fit_from_labels(hp.load_hold_policy(pol_path), labels, feats)
            hp.save_hold_policy(pol_path, pol)
            # CF observation → hypotheses when best policy beats realized net.
            with contextlib.suppress(Exception):
                from hermes_core.engines.counterfactual_exits import counterfactual_evs
                from hermes_core.engines.reflect import _log_hypothesis

                cost = float(pos.get("fees_pct_rt") or pos.get("exit_haircut_pct") or 0.22)
                ev = counterfactual_evs(
                    path_pts,
                    tp=float(pos.get("profit_target_pct") or 1.5),
                    cost_pct=cost,
                    min_bank_net=float(pos.get("min_bank_net_pct") or 0.10),
                )
                best = float(ev.get("best") or 0.0)
                if best > float(pnl) + max(0.05, cost * 0.25):
                    pol_name = str(ev.get("best_policy") or "")
                    axis = "min_bank_net_pct"
                    if "giveback" in pol_name:
                        axis = "mfe_giveback_frac"
                    elif "tp" in pol_name:
                        axis = "profit_target_pct"
                    elif "trail" in pol_name:
                        axis = "trailing_stop_pct"
                    _log_hypothesis(
                        {
                            "pair": pair,
                            "bot": bot,
                            "status": "cf_observation",
                            "variable": axis,
                            "gap_pct": round(best - float(pnl), 4),
                            "best_policy": pol_name,
                            "realized_pnl": round(float(pnl), 4),
                            "cf_best": round(best, 4),
                            "ts": time.time(),
                        }
                    )
    with contextlib.suppress(Exception):
        if pos.get("use_exit_experts") and pos.get("exit_votes"):
            from hermes_core.engines.exit_experts import credit_experts, load_weights, save_weights
            from hermes_core.state.paths import bot_state_dir

            wpath = bot_state_dir(bot) / "exit_expert_weights.json"
            w = load_weights(wpath)
            best = "hold"
            if ex.reason in (
                "profit_bank",
                "profit_target",
                "mfe_giveback",
                "failed_breakout",
            ):
                best = "bank"
            elif ex.reason == "trailing":
                best = "trail"
            elif ex.reason == "partial_close":
                best = "partial"
            w = credit_experts(w, list(pos.get("exit_votes") or []), best)
            save_weights(wpath, w)
    if not _log_trade(bot, trade_rec):
        # Disk write failed — keep the open so we retry next cycle.
        print(
            f"[hermes][trade-log] {bot}/{pair}: close log FAILED — keeping open",
            flush=True,
        )
        return
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
            "gp_ensemble",
            "shadow",
        )
        _record_type = "gp_ensemble" if is_gp else entry_type
        cortex.record_outcome(
            pair,
            _record_type,
            pnl,
            mfe_pct=_exc.get("mfe_pct"),
            mae_pct=_exc.get("mae_pct"),
            giveback_pct=_exc.get("giveback_pct"),
            giveback_frac=_exc.get("giveback_frac"),
            mfe_capture=_exc.get("mfe_capture"),
            exit_class=trade_rec.get("exit_class"),
            exit_reason=ex.reason,
        )
        _credited = pos.get("gp_indicators") or [] if is_gp else []
        for ind_id in _credited:
            cortex.record_indicator_outcome(ind_id, pnl, entry_type="gp_ensemble")
        # GPIntelligence consecutive-loss lockout + rolling score WR.
        # Flat PnL is neutral — does not increment loss_seq or pair score.
        if is_gp:
            from hermes_core.engines import gp_intelligence as gpi

            gpi.record_outcome(pair, float(pnl))
            # Feed paper GP closes into the promote gate (ban/unban evidence).
            with contextlib.suppress(Exception):
                from hermes_core.engines import gp_promote_gate as gpg

                gpg.record_pnl(bot, pair, float(pnl))
            # Regime cull on discovered registry (Problem 4).
            with contextlib.suppress(Exception):
                from hermes_core.engines.genetic import (
                    _save_discovered,
                    load_discovered_indicators,
                )

                regime = str(
                    pos.get("entry_regime")
                    or pos.get("regime_label")
                    or pos.get("regime")
                    or "range"
                )
                registry = load_discovered_indicators(pair, include_shared=False) or []
                if registry and _credited:
                    for ind_id in _credited:
                        gpi.update_indicator(registry, ind_id, float(pnl), regime)
                    _save_discovered(pair, registry)
        # Phase 5 — refresh regime-decay votes from cortex WR + recent DD proxy.
        with contextlib.suppress(Exception):
            from hermes_core.engines.regime_decay import regime_decay_enabled, update_pair_decay

            if regime_decay_enabled():
                n = int(cortex.evidence_n(pair, _record_type) or 0)
                wr = cortex.entry_type_wr(_record_type, pair=pair)
                wins = int(round(float(wr or 0.0) * n)) if n else 0
                losses = max(0, n - wins)
                live_dd = abs(min(0.0, float(pnl)))
                bt_mdd = float((goal or {}).get("max_drawdown") or 10.0)
                update_pair_decay(
                    bot,
                    pair,
                    wins=wins,
                    losses=losses,
                    live_dd=live_dd,
                    backtest_mdd=bt_mdd,
                )
    # [S18] Discord/webhook alert on real trade close (fail-soft)
    if alert_fn is not None:
        with contextlib.suppress(Exception):
            alert_fn(bot, pair, ex.reason, pnl)
    # Crisis learning: append-only lived fingerprint on adverse closes.
    # Fail-soft — never break the close/heartbeat path.
    with contextlib.suppress(Exception):
        _cid = save_adverse_lived_crisis(
            pair,
            float(pnl),
            list(prices) if prices else None,
            exit_reason=ex.reason,
        )
        if _cid:
            summary.setdefault("lived_crises", []).append(
                {"pair": pair, "crisis_id": _cid, "pnl_pct": pnl}
            )
    # Reflection latch: every N closed trades → L1 → (L2) → backtest → deploy.
    # Fail-soft: never let reflection break the close/heartbeat path.
    with contextlib.suppress(Exception):
        _maybe_reflect_after_close(
            bot,
            pair,
            prices=prices,
            chart_context=chart_context or "",
            goal=goal,
        )


def _try_manage_open(
    bot: str,
    pair: str,
    cycle: int,
    *,
    pos: dict,
    price: float | None,
    fetch_fn,
    history_fn,
    cortex,
    reentry: dict,
    open_positions: dict,
    summary: dict,
    alert_fn,
    chart_contexts: dict,
    goal: dict | None,
    mark_fails: dict,
    price_history: dict,
    health_registry: dict,
    consecutive_failures: int,
    regimes: dict | None = None,
    market_closed: bool = False,
) -> tuple[bool, float, int]:
    """Manage an open position even when entry guards would skip.

    Returns ``(handled, last_price, consecutive_failures)``. When handled is
    True the caller must ``continue`` (entry path skipped).
    """
    mark = price
    quote_unchanged = False
    if mark is None:
        mark = pos.get("last_mark") or pos.get("entry_price")
        # Weekend: L01 ages out recycled quotes — keep last mark, do not
        # escalate toward data_halt_exit (hold through the gap).
        if not market_closed:
            mark_fails[pair] = int(mark_fails.get(pair) or 0) + 1
    else:
        try:
            mark_f0 = float(mark)
        except (TypeError, ValueError):
            mark_f0 = None
        prev_mark = pos.get("last_mark")
        try:
            prev_f = float(prev_mark) if prev_mark is not None else None
        except (TypeError, ValueError):
            prev_f = None
        # Recycled identical quotes (weekend FX / flat GoldAPI silver) are not
        # a fresh mark — do not reset mark_fails or append duplicate ticks.
        quote_unchanged = (
            mark_f0 is not None
            and prev_f is not None
            and prev_f > 0
            and abs(mark_f0 - prev_f) / prev_f < 1e-9
        )
        if mark_f0 is None:
            mark = pos.get("last_mark") or pos.get("entry_price")
            mark_fails[pair] = int(mark_fails.get(pair) or 0) + 1
        elif quote_unchanged:
            # Identical recycled quote (delayed FX / flat metals) is not a mark
            # loss — do not escalate toward data_halt_exit. True misses still
            # count via price is None / invalid above.
            mark = prev_f
            summary["prices"][pair] = float(prev_f)
        else:
            mark_fails[pair] = 0
            pos["last_mark"] = float(mark_f0)
            last = float(mark_f0)
            summary["prices"][pair] = last
            ph = price_history.setdefault(pair, [])
            ph.append(last)
            if len(ph) > 60:
                del ph[: len(ph) - 60]
            health_registry.setdefault("price_adapter", True)
            mark = mark_f0

    if mark is None:
        return True, 0.0, consecutive_failures

    mark_f = float(mark)
    prices: list[float] = []
    try:
        hist = history_fn(pair) if history_fn is not None else None
        if hist is None and price is not None:
            hist = fetch_fn(pair + ":history")
        prices = [c["price"] for c in (hist or [])]
    except Exception:  # noqa: BLE001
        prices = []
    if not prices:
        prices = [mark_f]

    # Keep dashboard Regime live while a pair is in a trade. The entry path
    # that normally writes regimes[pair] is skipped after EXIT-BEFORE-GUARD.
    if regimes is not None:
        with contextlib.suppress(Exception):
            from hermes_core.indicators import compute_all

            ind = compute_all(prices)
            regimes[pair] = ind.get("regime") or pos.get("entry_regime") or "range"
            health_registry["indicators"] = True
        if pair not in regimes:
            regimes[pair] = pos.get("entry_regime") or pos.get("regime_label") or "range"

    # BTC Focus: keep D1 overlay fresh during open trades (entry path is skipped).
    if str(pair).upper().startswith("BTC/"):
        with contextlib.suppress(Exception):
            from hermes_core.engines import btc_regime as br

            _br = br.classify_btc_regime(pair)
            if not hasattr(run_cycle, "_btc_d1_regimes"):
                run_cycle._btc_d1_regimes = {}
            run_cycle._btc_d1_regimes[pair] = {
                "live": (regimes or {}).get(pair) or pos.get("entry_regime"),
                "d1": _br.get("label"),
                "d1_reason": _br.get("reason"),
                "d1_adx": _br.get("adx"),
            }

    # Weekend recycled quotes must not burn time_exit held_cycles.
    hold_tick = not (market_closed and quote_unchanged)
    if hold_tick:
        pos["held_cycles"] = pos.get("held_cycles", 0) + 1
    pos["unrealised_pct"] = (mark_f - pos["entry_price"]) / pos["entry_price"] * 100.0
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
        # Do NOT clamp time_exit_cycles to 150 — that killed 4H Donchian holds.
        te = pos.get("time_exit_cycles")
        if te is not None:
            try:
                pos["time_exit_cycles"] = max(60, min(int(te), 2880))
            except (TypeError, ValueError):
                pass

    # Exit TF mark + bar id (for stall counters) before excursion update.
    exit_mark = mark_f
    exit_prices = prices
    exit_bar_id = None
    with contextlib.suppress(Exception):
        from hermes_core.engines.layered_hold import resolve_exit_tf_prices

        em, ep, src = resolve_exit_tf_prices(bot, pair, pos, prices)
        pos["exit_tf_source"] = src
        if em is not None and ep:
            exit_mark = em
            exit_prices = ep
            exit_bar_id = f"{src}:{len(ep)}:{round(float(ep[-1]), 4)}"

    with contextlib.suppress(Exception):
        from hermes_core.engines.excursion import (
            append_mfe_path_point,
            mfe_tracking_enabled,
            update_position_excursions,
        )

        update_position_excursions(
            pos,
            pos["unrealised_pct"],
            tick=hold_tick,
            exit_bar_id=exit_bar_id,
            peak_epsilon_pct=pos.get("peak_epsilon_pct"),
        )
        if mfe_tracking_enabled():
            pos["mfe_tracking"] = True
        if hold_tick and exit_bar_id and pos.get("exit_bar_id") == str(exit_bar_id):
            append_mfe_path_point(
                pos,
                {
                    "world": (pos.get("world") or {}).get("funding"),
                    "d1": pos.get("live_d1"),
                },
            )

    with contextlib.suppress(Exception):
        from hermes_core.engines.layered_hold import enrich_open_cycle

        enrich_open_cycle(bot, pair, pos, prices)

    # L2: refresh chart patience on new exit-TF bar / structure event
    with contextlib.suppress(Exception):
        from hermes_core.engines.chart_vision import (
            chart_soft_reasons,
            chart_quality_mult,
        )
        from hermes_core.engines.layered_hold import continuous_vision_enabled

        prev_bar = pos.get("_chart_bar_id")
        struct_ev = pos.get("structure_event")
        refresh = False
        if exit_bar_id and exit_bar_id != prev_bar:
            refresh = True
            pos["_chart_bar_id"] = exit_bar_id
        if struct_ev:
            refresh = True
            pos.pop("structure_event", None)
        if continuous_vision_enabled() and hold_tick and int(pos.get("held_cycles") or 0) % 15 == 0:
            refresh = True
        ctx = chart_contexts.get(pair, "") if chart_contexts else ""
        if refresh and ctx:
            soft = chart_soft_reasons(ctx, strategy_type=str(pos.get("entry_type") or ""))
            pos["live_chart_patience"] = 0.7 if soft else 1.0
            if "avoid" in str(ctx).lower():
                pos["live_chart_patience"] = min(float(pos["live_chart_patience"]), 0.7)
            pos["chart_quality_mult"] = chart_quality_mult(
                ctx, strategy_type=str(pos.get("entry_type") or "")
            )
        # L7b cheap 15m structure digest already in enrich_open_cycle

    from hermes_core.engines.exit import Exit

    if (not market_closed) and int(mark_fails.get(pair) or 0) >= DATA_HALT_EXIT_AFTER:
        force = Exit(reason="data_halt_exit", price=mark_f)
        _process_exit(
            bot,
            pair,
            cycle,
            pos,
            mark_f,
            force,
            cortex=cortex,
            reentry=reentry,
            open_positions=open_positions,
            summary=summary,
            alert_fn=alert_fn,
            prices=prices,
            chart_context=chart_contexts.get(pair, ""),
            goal=goal,
        )
        mark_fails.pop(pair, None)
        return True, mark_f, consecutive_failures

    # Tag clock protect stops for wick guard
    if pos.get("stop_source") is None and pos.get("current_stop") is not None:
        pos["stop_source"] = "initial"

    ex = evaluate_exit(pos, exit_mark, exit_prices)
    if ex is not None and ex.reason == "trailing" and ex.new_stop is not None:
        pos["stop_source"] = "clock_protect"
    if ex is not None:
        # Fill / PnL still use the live mark path inside _process_exit.
        _process_exit(
            bot,
            pair,
            cycle,
            pos,
            mark_f,
            ex,
            cortex=cortex,
            reentry=reentry,
            open_positions=open_positions,
            summary=summary,
            alert_fn=alert_fn,
            prices=prices,
            chart_context=chart_contexts.get(pair, ""),
            goal=goal,
        )
    return True, mark_f, consecutive_failures


_REFLECT_SEM: object | None = None  # threading.Semaphore; lazy-init in reflect


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

    auto = get_env("REFLECT_AUTO_DEPLOY", "0") != "0"
    log = _logging.getLogger("hermes.reflect")

    # Cap concurrent reflect workers so a burst of closes cannot spawn unbounded
    # threads during a 30-day soak (each worker may fetch/backtest).
    global _REFLECT_SEM
    if _REFLECT_SEM is None:
        try:
            n = max(1, int(get_env("REFLECT_MAX_THREADS", "2")))
        except ValueError:
            n = 2
        _REFLECT_SEM = threading.Semaphore(n)

    def _work() -> None:
        if not _REFLECT_SEM.acquire(blocking=False):
            log.info("[reflect] %s/%s: skipped (thread cap) — no latch, will retry", bot, pair)
            return
        try:
            result = maybe_reflect_pair(
                bot,
                pair,
                goal=goal,
                chart_context=chart_context,
                prices=prices,
                auto_deploy=auto,
            )
            if result is not None:
                log.info(
                    "[reflect] %s/%s: %s closed=%s deployed=%s",
                    bot,
                    pair,
                    result.get("status"),
                    result.get("closed"),
                    result.get("deployed"),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("[reflect] %s/%s: error -> %s", bot, pair, exc)
        finally:
            _REFLECT_SEM.release()

    threading.Thread(
        target=_work,
        name=f"reflect-{bot}-{pair}",
        daemon=True,
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


def _gp_vote(
    pair: str,
    prices: list[float],
    strategy: dict,
    *,
    cortex=None,
    promote: bool = False,
    use_invent_tf: bool = True,
    bot: str | None = None,
    use_daily: bool | None = None,
):
    """Evaluate GP ensemble once; apply L36 exile filter. Fail-soft -> None.

    ``use_invent_tf=True`` (default) evaluates on the bot invent candle TF so
    invent TF == live GP eval TF. ``use_invent_tf=False`` uses the live series
    only (legacy fast path; mismatched formulas will not vote).

    ``use_daily`` is retained as a deprecated alias for ``use_invent_tf``.
    """
    try:
        from hermes_core.engines.entry import gp_ensemble_signal, gp_invent_prices
        from hermes_core.engines.gp_invent_profile import invent_profile

        if use_daily is not None:
            use_invent_tf = bool(use_daily)
        prof = invent_profile(bot, pair=pair)
        exiled: set[str] = set()
        if cortex is not None:
            with contextlib.suppress(Exception):
                exiled = set(cortex.get_exiled_indicators() or [])
        invent_px = None
        if use_invent_tf:
            invent_px = gp_invent_prices(
                pair,
                interval=prof["interval"],
                period=prof["period"],
                max_candles=prof["max_candles"],
            )
        return gp_ensemble_signal(
            pair,
            prices,
            strategy,
            daily_prices=invent_px,
            promote=promote,
            exiled_ids=exiled,
            invent_interval=prof["interval"],
            invent_horizon=prof["horizon"],
        )
    except Exception:  # noqa: BLE001
        return None


def _log_gp_shadow(
    bot: str, pair: str, prices: list[float], strategy: dict, *, cortex=None, sig=None
) -> str:
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
            # Shadow observation on invent TF (same regime as formulas).
            sig = _gp_vote(
                pair,
                prices,
                strategy,
                cortex=cortex,
                bot=bot,
                promote=False,
                use_invent_tf=True,
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


def _maybe_discover(bot: str, pair: str, prices: list[float] | None = None, *, cortex=None) -> None:
    """Throttled GP discovery + live feedback for one pair (B10 closes the loop).

    On each throttled pass it:
      1. Applies live paper-PnL feedback (B10) so persisted indicators re-rank;
      2. Runs a full invent when this pair has no votable formulas for the
         active invent regime, OR when ``DISCOVERY_REINVENT_INTERVAL_S`` has
         elapsed since the last full invent (so the brain keeps searching for
         NEW formulas instead of freezing on yesterday's admits).

    Runs at most once per DISCOVERY_INTERVAL_S of wall-clock per (bot, pair).
    Persists admitted indicators to state/discovered/{pair}.json.

    CRITICAL: discovery does network + GP evolution and must NEVER block the
    heartbeat cycle. The heavy work runs in a thread with a hard timeout; if it
    stalls, the cycle proceeds and the next attempt retries. Fail-soft.
    """
    from datetime import datetime

    from hermes_core.adapters.price import seed_history_interval_sync
    from hermes_core.engines.genetic import (
        ENGINE_VERSION,
        _discovered_path,
        _save_discovery_pulse,
        apply_live_feedback,
        begin_invent_write_token,
        load_discovered_indicators,
    )
    from hermes_core.engines.gp_invent_profile import (
        has_votable_for_regime,
        invent_profile,
    )

    now = time.time()
    key = (bot, pair)
    if key in _DISCOVERY_LAST and (now - _DISCOVERY_LAST[key]) < DISCOVERY_INTERVAL_S:
        return

    # Phase 3 invent freeze (bots/btc invent.enabled=false).
    try:
        from hermes_core.config import load_config

        _inv = (load_config(bot).get("invent") or {}) if bot else {}
        if isinstance(_inv, dict) and "enabled" in _inv:
            _raw = _inv.get("enabled")
            _on = (
                _raw
                if isinstance(_raw, bool)
                else str(_raw).strip().lower() in {"1", "true", "yes", "on"}
            )
            if not _on:
                return
    except Exception:  # noqa: BLE001 — fail open to legacy invent
        pass

    prof = invent_profile(bot, pair=pair)
    # Skip invent only when THIS pair already has S10-approved formulas on the
    # *current* invent regime (interval + horizon). Old daily/h60 crypto junk
    # must not block a fresh 1h invent. Re-invent still runs on the longer
    # DISCOVERY_REINVENT_INTERVAL_S cadence so new exprs can appear.
    own = load_discovered_indicators(pair, include_shared=False)
    have_votable = has_votable_for_regime(
        own,
        interval=prof["interval"],
        horizon=prof["horizon"],
    )
    reinvent_s = max(int(DISCOVERY_REINVENT_INTERVAL_S), int(DISCOVERY_INTERVAL_S))
    last_invent = float(_DISCOVERY_LAST_INVENT.get(key) or 0.0)
    reinvent_due = (now - last_invent) >= reinvent_s
    should_invent = (not have_votable) or reinvent_due

    # Adaptive invent budget after chronic timeouts (shrink search / extend cap).
    timeout_streak = int(_DISCOVERY_TIMEOUT_STREAK.get(key) or 0)
    gens = int(prof["generations"])
    pop = int(prof["pop_size"])
    islands = int(prof["n_islands"])
    timeout_s = int(prof["timeout_s"])
    if timeout_streak >= max(1, DISCOVERY_TIMEOUT_SHRINK_AFTER):
        shrink = 2 ** min(timeout_streak - DISCOVERY_TIMEOUT_SHRINK_AFTER + 1, 2)
        gens = max(8, gens // shrink)
        pop = max(12, pop // shrink)
        islands = 1
        timeout_s = min(timeout_s + 120 * min(timeout_streak, 3), int(timeout_s * 1.75) + 60)

    def _status_pulse(**extra) -> None:
        """Always leave a dashboard-visible invent pulse (even on skip/timeout).

        Control-plane pulses omit write_token so they always land; invent-worker
        pulses pass write_token so abandoned threads cannot clobber newer passes.
        """
        try:
            pulse = {
                "pair": pair,
                "_bot": bot,
                "engine_version": ENGINE_VERSION,
                "ts": datetime.now(UTC).isoformat(),
                "interval": prof["interval"],
                "horizon": prof["horizon"],
                "generations": extra.get("generations", gens),
                "pop_size": extra.get("pop_size", pop),
                "n_islands": extra.get("n_islands", islands),
                "candidates_unique": extra.get("candidates_unique"),
                "candidates_gated": extra.get("candidates_gated"),
                "admitted": int(extra.get("admitted") or 0),
                "best_oos": extra.get("best_oos"),
                "status": extra.get("status") or "ok",
                "reason": extra.get("reason"),
                "reject_counts": extra.get("reject_counts"),
                "near_misses": extra.get("near_misses"),
                "seed": extra.get("seed"),
                "timeout_streak": timeout_streak,
                "admit_zero_streak": int(_DISCOVERY_ADMIT_ZERO_STREAK.get(key) or 0),
                "map_elites": extra.get("map_elites")
                or {
                    "filled": 0,
                    "total_cells": 27,
                    "coverage": 0.0,
                },
                "lexicase_cases": extra.get("lexicase_cases"),
                "write_token": extra.get("write_token"),
            }
            _save_discovery_pulse(
                pair,
                pulse,
                write_token=extra.get("write_token_fence"),
            )
        except Exception:  # noqa: BLE001 — pulse must never break invent
            pass

    def _mark_discovery_run() -> None:
        _LAST_DISCOVERY_RUN[bot] = time.time()

    def _note_timeout() -> None:
        streak = int(_DISCOVERY_TIMEOUT_STREAK.get(key) or 0) + 1
        _DISCOVERY_TIMEOUT_STREAK[key] = streak
        if streak >= max(1, DISCOVERY_TIMEOUT_SKIP_AFTER):
            _DISCOVERY_TIMEOUT_COOLDOWN_UNTIL[key] = time.time() + max(
                60, int(DISCOVERY_TIMEOUT_COOLDOWN_S)
            )

    def _note_invent_finished(admitted_n: int, *, write_token: int) -> None:
        from hermes_core.engines.genetic import invent_write_token_current

        # Late finish after hard-abandon (token bumped on timeout) — ignore.
        if int(write_token) != int(invent_write_token_current(pair)):
            return
        _DISCOVERY_TIMEOUT_STREAK[key] = 0
        _DISCOVERY_TIMEOUT_COOLDOWN_UNTIL.pop(key, None)
        if admitted_n > 0:
            _DISCOVERY_ADMIT_ZERO_STREAK[key] = 0
            return
        streak = int(_DISCOVERY_ADMIT_ZERO_STREAK.get(key) or 0) + 1
        _DISCOVERY_ADMIT_ZERO_STREAK[key] = streak
        alert_after = int(DISCOVERY_ADMIT_ZERO_ALERT_AFTER)
        if alert_after > 0 and streak >= alert_after and streak % alert_after == 0:
            with contextlib.suppress(Exception):
                from hermes_core.notify import send_text_alert

                send_text_alert(
                    f"[discovery] {bot}/{pair}: admit_zero streak={streak} "
                    f"(S10 strict; check near_misses pulse / classical fills)"
                )

    def _work(write_token: int) -> None:
        import logging as _logging

        _log = _logging.getLogger("hermes.discovery")
        # B10 live feedback: re-rank persisted indicators toward realized PnL.
        # Runs on every throttled pass (even when re-discovery isn't needed)
        # so the ensemble keeps learning from closed paper trades.
        updated = apply_live_feedback(pair, cortex)
        if updated:
            _log.info("[discovery] %s: live feedback updated %d indicators", pair, updated)
        if not should_invent:
            age_h = (now - last_invent) / 3600.0 if last_invent else -1.0
            _status_pulse(
                status="skipped_have_formulas",
                reason=(
                    f"votable formulas on invent regime; "
                    f"next reinvent in {max(0.0, reinvent_s - (now - last_invent)) / 3600.0:.1f}h "
                    f"(age={age_h:.1f}h)"
                ),
                admitted=len(own) if isinstance(own, list) else 0,
            )
            return
        cool_until = float(_DISCOVERY_TIMEOUT_COOLDOWN_UNTIL.get(key) or 0.0)
        if cool_until > time.time():
            _status_pulse(
                status="chronic_timeout_backoff",
                reason=(
                    f"skip invent until cooldown "
                    f"({max(0.0, cool_until - time.time()) / 60.0:.1f}m left); "
                    f"timeout_streak={timeout_streak}"
                ),
            )
            return
        # Time/pair-varying seed — fixed seed=7 recycled the same doomed candidates.
        invent_seed = (int(now) ^ (hash((bot, pair, int(now) // 3600)) & 0xFFFFFFFF)) & 0xFFFFFFFF
        print(
            f"[hermes][discovery] {bot}/{pair}: invent start "
            f"{prof['interval']}/h={prof['horizon']} "
            f"gens={gens} pop={pop} islands={islands} "
            f"timeout={timeout_s}s seed={invent_seed} "
            f"reinvent={have_votable} timeout_streak={timeout_streak} "
            f"write_token={write_token}",
            flush=True,
        )
        hist = seed_history_interval_sync(
            pair,
            interval=prof["interval"],
            period=prof["period"],
            max_candles=prof["max_candles"],
        )
        series = [c["price"] for c in (hist or [])] or (prices or [])
        _log.info(
            "[discovery] %s: fetched %d %s candles (h=%s) for GP",
            pair,
            len(series),
            prof["interval"],
            prof["horizon"],
        )
        print(
            f"[hermes][discovery] {bot}/{pair}: fetched {len(series)} {prof['interval']} candles",
            flush=True,
        )
        if len(series) < int(prof["min_bars"]):
            _log.warning(
                "[discovery] %s: <%s %s candles, GP skipped",
                pair,
                prof["min_bars"],
                prof["interval"],
            )
            _status_pulse(
                status="skipped_short_history",
                reason=f"<{prof['min_bars']} {prof['interval']} bars",
                seed=invent_seed,
                write_token=write_token,
                write_token_fence=write_token,
            )
            return
        inds = gp_discover(
            pair,
            series,
            horizon=int(prof["horizon"]),
            generations=gens,
            pop_size=pop,
            n_islands=islands,
            interval=str(prof["interval"]),
            seed=int(invent_seed),
            write_token=write_token,
            # Finish admit before invent hard-timeout abandons the worker.
            deadline=time.time() + max(45, int(timeout_s) - 15),
        )
        _DISCOVERY_LAST_INVENT[key] = time.time()
        _note_invent_finished(len(inds), write_token=write_token)
        # Reinvent merge: don't wipe prior votable formulas when a new run admits.
        if inds and have_votable:
            try:
                from hermes_core.engines.genetic import (
                    _save_discovered,
                    indicator_expr,
                )

                prior = [i for i in (own or []) if indicator_expr(i)]
                by_key: dict[str, dict] = {}
                for row in prior + list(inds):
                    k = str(row.get("expr") or row.get("expr_str") or "").strip()
                    if not k:
                        continue
                    prev = by_key.get(k)
                    if prev is None or float(row.get("oos_corr") or 0) >= float(
                        prev.get("oos_corr") or 0
                    ):
                        by_key[k] = row
                merged = sorted(
                    by_key.values(),
                    key=lambda x: float(x.get("oos_corr") or 0),
                    reverse=True,
                )[:15]
                _save_discovered(pair, merged, write_token=write_token)
                inds = merged
            except Exception:  # noqa: BLE001 — keep discover()'s write on merge failure
                pass
        _log.info("[discovery] %s: admitted=%d -> %s", pair, len(inds), _discovered_path(pair))
        # Phase 4.5: if reflection handed this pair to GP and invent admitted,
        # clear the handoff and schedule a risk-param retune. GP never bumps
        # strategy YAML versions (Phase 4.2 / 4.4 — signal path only).
        with contextlib.suppress(Exception):
            from hermes_core.engines.experiment_control import on_gp_admit

            on_gp_admit(bot, pair, admitted=len(inds or []))
        # discover() already writes a full pulse; tag bot/seed for the dashboard.
        try:
            from hermes_core.engines.genetic import load_discovery_pulse

            existing = load_discovery_pulse(pair) or {}
            existing["_bot"] = bot
            existing["pair"] = pair
            existing["status"] = "ok" if inds else "admit_zero"
            existing["admitted"] = len(inds)
            existing["seed"] = invent_seed
            existing["write_token"] = write_token
            existing["timeout_streak"] = 0
            existing["admit_zero_streak"] = int(_DISCOVERY_ADMIT_ZERO_STREAK.get(key) or 0)
            existing["generations"] = gens
            existing["pop_size"] = pop
            existing["n_islands"] = islands
            if have_votable and not inds:
                existing["reason"] = "reinvent_ran_admit_zero"
            _save_discovery_pulse(pair, existing, write_token=write_token)
        except Exception:  # noqa: BLE001
            _status_pulse(
                status="ok" if inds else "admit_zero",
                admitted=len(inds),
                seed=invent_seed,
                write_token=write_token,
                write_token_fence=write_token,
            )

    # Bound the work so a slow network/price API can't stall the discovery
    # daemon. CRITICAL: do NOT use `with ThreadPoolExecutor` — on timeout its
    # __exit__ waits for the hung worker and freezes discovery forever. Abandon
    # the waiter, leave the worker daemonized, and skip re-entry via in-flight.
    if key in _DISCOVERY_IN_FLIGHT:
        _status_pulse(
            status="in_flight",
            reason="previous invent still running after timeout; not spawning another",
        )
        _mark_discovery_run()
        _DISCOVERY_LAST[key] = time.time()
        return

    # Fence disk writes for this invent attempt. Beginning a new token invalidates
    # any abandoned prior worker for this pair.
    write_token = begin_invent_write_token(pair) if should_invent else 0

    try:
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as FuturesTimeout

        print(
            f"[hermes][discovery] {bot}/{pair}: pass begin "
            f"(have_votable={have_votable} should_invent={should_invent} "
            f"timeout_streak={timeout_streak})",
            flush=True,
        )
        _DISCOVERY_IN_FLIGHT.add(key)

        def _work_guarded() -> None:
            try:
                _work(write_token)
            finally:
                _DISCOVERY_IN_FLIGHT.discard(key)

        ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"invent-{bot}")
        # Use (possibly stretched) timeout_s when inventing; short cap otherwise.
        wait_s = timeout_s if should_invent else min(30, timeout_s)
        try:
            fut = ex.submit(_work_guarded)
            fut.result(timeout=int(wait_s))
        except FuturesTimeout:
            msg = (
                f"[discovery] {bot}/{pair}: error -> TimeoutError after "
                f"{wait_s}s (worker abandoned, not joined)"
            )
            print(msg, flush=True)
            if should_invent:
                _note_timeout()
                # Hard-abandon: fence late writes. Keep ``key`` in
                # ``_DISCOVERY_IN_FLIGHT`` until ``_work_guarded`` finishes so the
                # discovery loop can drain zombies before starting the next pair.
                begin_invent_write_token(pair)
            _status_pulse(
                status="timeout",
                reason=f"invent exceeded {wait_s}s (streak={_DISCOVERY_TIMEOUT_STREAK.get(key, 0)})",
                generations=gens,
                pop_size=pop,
                n_islands=islands,
            )
            _mark_discovery_run()
            # Throttle retries so we don't spam while the abandoned worker runs.
            _DISCOVERY_LAST[key] = time.time()
            return
        finally:
            # wait=False: never block the discovery loop on a hung invent worker.
            ex.shutdown(wait=False, cancel_futures=True)
    except Exception as _exc:  # surface the real reason instead of silent drop
        import logging as _logging

        _DISCOVERY_IN_FLIGHT.discard(key)
        msg = f"[discovery] {bot}/{pair}: error -> {type(_exc).__name__}: {_exc}"
        _logging.getLogger("hermes.discovery").warning(msg)
        print(msg, flush=True)
        _status_pulse(status="error", reason=str(_exc)[:200])
        _mark_discovery_run()
        return
    _DISCOVERY_LAST[key] = time.time()
    _mark_discovery_run()
    # Confirm invent regime in bot stdout (even when admitted=0).
    print(
        f"[hermes][discovery] {bot}/{pair}: invent={prof['interval']}/h={prof['horizon']} "
        f"timeout={timeout_s}s gens={gens} pop={pop} done",
        flush=True,
    )


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
    oversold_pairs = 0  # RSI-confluence count, accumulated across pairs this cycle
    # consecutive_failures is carried in (persists across cycles for the L24 breaker)
    try:
        _now_ts = float(now_fn())
    except Exception:  # noqa: BLE001
        _now_ts = time.time()
    market_closed = bool(is_bot_market_closed(bot, _now_ts))
    summary["market_closed"] = market_closed
    with contextlib.suppress(Exception):  # bootstrap must never block the cycle
        ensure_state_files(bot)
    try:
        cfg = load_config(bot)
    except Exception:  # noqa: BLE001 — config load is a hard boundary
        health_registry["config"] = False
        summary["errors"] += 1
        write_heartbeat(
            bot, cycle, consecutive_failures, 0.0, status="error", health=dict(health_registry)
        )
        traceback.print_exc()
        return summary

    health_registry["config"] = True
    pairs = cfg.get("pairs", [])
    _halted, _halt_reason = entries_halted(bot)
    summary["halted"] = _halted
    with contextlib.suppress(Exception):
        if cycle % 60 == 1:
            rotate_jsonl_if_large(bot)
    # Feed SLO: auto-halt when recent skips are dominated by feed/chart errors.
    _feed: dict = {"ok": True}
    _idle: dict = {"effectively_paused": False}
    try:
        _feed = feed_error_rate(_state_dir(bot) / "skips.jsonl")
        summary["feed_slo"] = _feed
        if not _feed.get("ok") and not _halted:
            write_halt(bot, f"feed_slo:rate={_feed.get('rate')}")
            _halted, _halt_reason = True, f"feed_slo:rate={_feed.get('rate')}"
            summary["halted"] = True
            print(
                f"[hermes][feed-slo] {bot}: auto-halt rate={_feed.get('rate')}",
                flush=True,
            )
    except Exception:  # noqa: BLE001
        pass
    # Pause detector (#25): all recent skips idle/feed for hours → effectively paused.
    try:
        _idle_hours = {"crypto": 4.0, "btc": 4.0, "gold": 8.0, "forex": 6.0}.get(bot, 6.0)
        _idle = idle_skip_slo(_state_dir(bot) / "skips.jsonl", hours=_idle_hours)
        summary["idle_slo"] = _idle
        if _idle.get("effectively_paused") and not _halted:
            write_halt(bot, f"idle_slo:{_idle.get('detail')}")
            _halted, _halt_reason = True, f"idle_slo:{_idle.get('detail')}"
            summary["halted"] = True
            print(
                f"[hermes][idle-slo] {bot}: effectively paused — {_idle.get('detail')}",
                flush=True,
            )
    except Exception:  # noqa: BLE001
        pass
    # DD / failure_below book halt (config goal).
    try:
        _dd = book_drawdown_status(bot, cfg.get("goal") or {})
        summary["book_dd"] = _dd
        if not _dd.get("ok") and not _halted:
            write_halt(bot, _dd.get("reason") or "dd_halt")
            _halted, _halt_reason = True, str(_dd.get("reason") or "dd_halt")
            summary["halted"] = True
            print(f"[hermes][dd-halt] {bot}: {_dd.get('reason')}", flush=True)
    except Exception:  # noqa: BLE001
        pass
    # Auto-clear recoverable idle/feed halts when SLOs recover.
    try:
        _rec = maybe_recover_halt(bot, feed=_feed, idle=_idle, price_ok=True)
        summary["halt_recovery"] = _rec
        if _rec.get("recovered"):
            _halted, _halt_reason = entries_halted(bot)
            summary["halted"] = _halted
    except Exception:  # noqa: BLE001
        pass
    # Sticky L21 flatline pauses (pair -> remaining cycles).
    flatline_pause: dict[str, int] = dict(getattr(run_cycle, "_flatline_pause", {}) or {})
    mark_fails: dict[str, int] = dict(getattr(run_cycle, "_mark_fails", {}) or {})
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
    regimes: dict[str, str] = {}
    for p, r in (getattr(run_cycle, "_regimes", {}) or {}).items():
        if p not in pairs:
            continue
        # Legacy BTC overlay briefly stored a dict here — coerce back to str.
        if isinstance(r, dict):
            regimes[p] = str(r.get("live") or r.get("d1") or "range")
        elif r:
            regimes[p] = str(r)
    run_cycle._btc_d1_regimes = {}
    cortex = Cortex(bot)  # per-cycle; exile SET persists to disk
    # [GUARD L35] evaluate policy once per cycle from cortex WRs, then apply
    # suppressions before opening new positions.
    try:
        policy = PolicyEngine().evaluate(cycle, pairs, cortex=cortex)
    except Exception:  # noqa: BLE001 — fail-open: never block trading on policy I/O
        policy = None
    # Policy rollback = fleet MR WR breach. PolicyEngine already hard-suppresses
    # mean_reversion on every pair; do NOT halt the whole bot (GP/momentum must
    # keep trading). Alert once per process when rollback first latches.
    if policy is not None and getattr(policy, "rollback", False):
        summary["policy_rollback"] = True
        if not getattr(run_cycle, "_policy_rollback_alerted", False):
            run_cycle._policy_rollback_alerted = True
            with contextlib.suppress(Exception):
                from hermes_core.notify import send_text_alert

                send_text_alert(
                    f"[policy] {bot}: MR WR breach — mean_reversion benched "
                    f"(GP/momentum still live)",
                    bot=bot,
                    pair="*",
                    guard="policy_rollback",
                )
    # Priority discovery: nudge reinvent sooner when many indicators are exiled
    # OR when reflection has handed a pair to GP (Phase 4.1 underperforming +
    # quarantined axes). Per-pair handoff is more precise than the fleet bool.
    _prio_pairs: set[str] = set()
    if policy is not None and getattr(policy, "priority_discovery", False):
        _prio_pairs |= set(pairs)
    with contextlib.suppress(Exception):
        from hermes_core.engines.experiment_control import gp_handoff_pairs

        _prio_pairs |= set(gp_handoff_pairs(bot))
        if policy is not None:
            _prio_pairs |= set(getattr(policy, "priority_discovery_pairs", None) or [])
    if _prio_pairs:
        with contextlib.suppress(Exception):
            now_prio = time.time()
            for _p in _prio_pairs:
                key = (bot, _p)
                last = float(_DISCOVERY_LAST_INVENT.get(key) or 0.0)
                if (not last) or (now_prio - last) > 3600:
                    _DISCOVERY_LAST_INVENT[key] = now_prio - DISCOVERY_REINVENT_INTERVAL_S

    for pair in pairs:
        pos = open_positions.get(pair)
        # --- fetch (fail-soft; failures counted toward circuit breaker) -----
        candle = None
        fetch_failed = False
        try:
            candle = fetch_fn(pair)
        except Exception as exc:  # noqa: BLE001
            fetch_failed = True
            if not market_closed:
                consecutive_failures += 1
                summary["errors"] += 1
            health_registry["price_adapter"] = False
            _log_skip(
                bot,
                pair,
                cycle,
                "market_closed" if market_closed else f"fetch_error:{exc!r}",
            )
            if not market_closed:
                traceback.print_exc()
            candle = None

        if candle is None and pos is None:
            if not fetch_failed:
                if market_closed:
                    _log_skip(bot, pair, cycle, "market_closed")
                    summary["skips"] += 1
                else:
                    consecutive_failures += 1
                    summary["errors"] += 1
                    _log_skip(bot, pair, cycle, "no_candle")
            continue

        price: float | None = None
        if candle is not None:
            try:
                price = float(candle["price"])
            except (TypeError, ValueError, KeyError):
                price = None
            if price is None and pos is None:
                if market_closed:
                    _log_skip(bot, pair, cycle, "market_closed")
                    summary["skips"] += 1
                else:
                    consecutive_failures += 1
                    summary["errors"] += 1
                    _log_skip(bot, pair, cycle, "no_candle")
                continue

        # EXIT-BEFORE-GUARD: always manage opens (even on fetch failure).
        if pos is not None:
            # Still refresh chart for the dashboard even while managing an open.
            if price is not None:
                _capture_chart_context(
                    pair,
                    chart_context_fn=chart_context_fn,
                    chart_contexts=chart_contexts,
                    health_registry=health_registry,
                    bot=bot,
                    cycle=cycle,
                )
            _handled, _lp, consecutive_failures = _try_manage_open(
                bot,
                pair,
                cycle,
                pos=pos,
                price=price,
                fetch_fn=fetch_fn,
                history_fn=history_fn,
                cortex=cortex,
                reentry=reentry,
                open_positions=open_positions,
                summary=summary,
                alert_fn=alert_fn,
                chart_contexts=chart_contexts,
                goal=cfg.get("goal"),
                mark_fails=mark_fails,
                price_history=price_history,
                health_registry=health_registry,
                consecutive_failures=consecutive_failures,
                regimes=regimes,
                market_closed=bool(summary.get("market_closed")),
            )
            if _lp:
                last_price = _lp
            continue

        if price is None:
            continue

        health_registry.setdefault("price_adapter", True)
        consecutive_failures = 0  # good quote resets L24 streak
        last_price = float(price)
        summary["prices"][pair] = float(price)  # live price snapshot for dashboard push
        # Append to rolling history for the sparkline (cap at 60 ticks).
        ph = price_history.setdefault(pair, [])
        ph.append(float(price))
        if len(ph) > 60:
            del ph[: len(ph) - 60]

        # seeded price history for indicators (fail-soft).
        # Prefer history_fn (real multi-candle series via the adapters'
        # seed_history, which pulls a genuine series for FX/metals). The
        # aggregate fetch_fn(":history") only returns the last tick for
        # FX/metals, which makes indicators degenerate -> bot can't trade.
        # Fall back to fetch_fn(":history"), then a single price.
        try:
            hist = history_fn(pair) if history_fn is not None else fetch_fn(pair + ":history")
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
        # BTC Focus: keep regimes[pair] a STRING (dashboard renders it).
        # D1 details live under heartbeat.btc_d1_regimes separately.
        if str(pair).upper().startswith("BTC/"):
            with contextlib.suppress(Exception):
                from hermes_core.engines import btc_regime as br

                _br = br.classify_btc_regime(pair)
                if not hasattr(run_cycle, "_btc_d1_regimes"):
                    run_cycle._btc_d1_regimes = {}
                run_cycle._btc_d1_regimes[pair] = {
                    "live": regimes[pair],
                    "d1": _br.get("label"),
                    "d1_reason": _br.get("reason"),
                    "d1_adx": _br.get("adx"),
                }
                if cycle % 60 == 0:
                    br.append_regime_log(bot, _br)

        # Chart vision ASAP after regime so flatline/BB/param early-continues
        # still populate heartbeat chart_contexts (BTC was missing for this).
        context = _capture_chart_context(
            pair,
            chart_context_fn=chart_context_fn,
            chart_contexts=chart_contexts,
            health_registry=health_registry,
            bot=bot,
            cycle=cycle,
        )

        # [GUARD L02] flat-price / stale-data gate
        is_flat, flat_reason = flat_price_guard(ind, prices)
        if is_flat:
            _log_skip(bot, pair, cycle, flat_reason)
            summary["skips"] += 1
            continue

        # [GUARD L21] novel-regime flatline: pause NEW entries for N cycles (#26).
        # BTC Focus: FX crisis-DB novelty permanently false-positives on BTC
        # (distance≈0.5 vs FX crises). D1 btc_regime is the BTC gate — log only.
        try:
            _is_btc_pair = str(pair).upper().startswith("BTC/")
            remaining = int(flatline_pause.get(pair) or 0)
            if remaining <= 0 and len(prices) >= 60 and not _is_btc_pair:
                _nov = check_novel_regime(pair, prices)
                if _nov.get("flatlined"):
                    remaining = int(_nov.get("pause_cycles") or 0)
                    flatline_pause[pair] = remaining
                    summary.setdefault("flatlined", {})[pair] = _nov
            elif _is_btc_pair:
                # Drop any sticky pause left from pre-fix deploys.
                flatline_pause.pop(pair, None)
                remaining = 0
            if remaining > 0:
                flatline_pause[pair] = remaining - 1
                if flatline_pause[pair] <= 0:
                    flatline_pause.pop(pair, None)
                # Only block new entries; open positions continue to exit logic.
                if open_positions.get(pair) is None:
                    _log_skip(bot, pair, cycle, "flatline:NOVEL_REGIME")
                    summary["skips"] += 1
                    continue
        except Exception:  # noqa: BLE001 — L21 must never crash the cycle
            pass

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
            # Persist bw samples for soak measurement (tune BB_BW_MIN from evidence).
            with contextlib.suppress(Exception):
                bb = ind.get("bb") or {}
                mid = float(bb.get("middle") or 0.0)
                if mid > 0:
                    bw = (float(bb.get("upper") or mid) - float(bb.get("lower") or mid)) / mid
                    append_bb_sample(bot, pair, bw)
            if bb_skip:
                _log_skip(bot, pair, cycle, bb_reason)
                summary["skips"] += 1
                continue

        # ── GP vote once (invent TF): shadow log + L13 ensemble consensus ──
        # [GUARD L13] MR longs are blocked when GP consensus is bearish.
        # Invent TF == live GP eval TF (cached); mismatched formulas cannot vote.
        gp_shadow_sig = _gp_vote(
            pair,
            prices,
            strategy,
            cortex=cortex,
            bot=bot,
            promote=False,
            use_invent_tf=True,
        )
        gp_consensus = _log_gp_shadow(
            bot,
            pair,
            prices,
            strategy,
            cortex=cortex,
            sig=gp_shadow_sig,
        )

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
        # Phase 3.5: honour reflection safe mode. ``paused`` blocks new entries;
        # ``size_down`` shrinks size once we've computed it below. Fail-open.
        _safe_mode = None
        with contextlib.suppress(Exception):
            from hermes_core.engines.experiment_control import pair_safe_mode

            _safe_mode = pair_safe_mode(bot, pair)
        if pos is None and _safe_mode and _safe_mode.get("mode") == "paused":
            _log_skip(bot, pair, cycle, "safe_mode_paused")
            summary["skips"] += 1
            continue
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
            trad_sig, _trad_skip = evaluate_entry_detailed(
                prices,
                strategy,
                pair=pair,
                context=context,
                ensemble_consensus=ensemble,
                oversold_pairs=_os_count,
                vol_above=vol_above,
                reentry=reentry,
                current_cycle=cycle,
                session_token=session_token,
                regime=regimes.get(pair) or ind.get("regime"),
                bot=bot,
            )
            # Layered sentient entries (replaces blunt L7d idle sleeve).
            _sentient_meta: dict = {}
            with contextlib.suppress(Exception):
                from hermes_core.engines.sentient_entry import (
                    is_btc_entry_bot,
                    run_sentient_entry,
                )

                if is_btc_entry_bot(bot, pair):
                    _se = run_sentient_entry(
                        bot=bot,
                        pair=pair,
                        prices=prices,
                        strategy=strategy,
                        context=context,
                        trad_sig=trad_sig,
                        trad_skip=_trad_skip or "",
                        current_cycle=cycle,
                        ensemble_consensus=ensemble,
                        vol_above=vol_above,
                        reentry=reentry,
                        session_token=session_token,
                        regime=regimes.get(pair) or ind.get("regime"),
                    )
                    _sentient_meta = _se
                    if _se.get("event_pause"):
                        _log_skip(bot, pair, cycle, "event:hard_pause")
                        summary["skips"] += 1
                        continue
                    if _se.get("signal") is not None:
                        trad_sig, _trad_skip = _se["signal"], ""
                    elif not _se.get("observe_only"):
                        trad_sig, _trad_skip = None, str(
                            _se.get("skip") or _trad_skip or "sentient:no_winner"
                        )
                    summary["sentient_entry"] = {
                        "decision": _se.get("decision"),
                        "conviction": _se.get("conviction"),
                        "candidates": _se.get("candidates") or [],
                        "blocked_by_regime": _se.get("blocked_by_regime"),
                    }
                    run_cycle._sentient_last = dict(summary["sentient_entry"])
            gp_sig = None
            # GP promote gate: expectancy-driven per-pair ban/unban (seeds from
            # GP_EXCLUDE_PAIRS). Invent/shadow still run when banned.
            _want_gp = False
            from hermes_core.engines.hif_flags import gp_promote_enabled

            if gp_promote_enabled():
                try:
                    from hermes_core.engines import gp_promote_gate as gpg

                    _want_gp = gpg.is_promote_allowed(bot, pair)
                except Exception:  # noqa: BLE001 — fail open to env list only
                    _excl = {
                        p.strip().upper()
                        for p in get_env("GP_EXCLUDE_PAIRS", "GBP/JPY").split(",")
                        if p.strip()
                    }
                    if bot in {"crypto", "btc"}:
                        _excl = {p for p in _excl if not p.startswith("BTC/")}
                    _want_gp = pair.upper() not in _excl
            # Legacy: GP only if traditional quiet. Ranking: also score GP when
            # traditional fires so the better edge can win.
            if _want_gp and (trad_sig is None or _rank_on):
                # BTC Focus: D1 regime also gates GP entries (shadow + promote).
                _btc_block = False
                if str(pair).upper().startswith("BTC/"):
                    with contextlib.suppress(Exception):
                        from hermes_core.engines import btc_regime as br

                        _brg = br.classify_btc_regime(pair)
                        if br.hard_blocks_entry(
                            str(_brg.get("label") or ""),
                            strategy_type=str(
                                (strategy or {}).get("strategy_type") or ""
                            ),
                        ):
                            _btc_block = True
                            if trad_sig is None:
                                _log_skip(
                                    bot,
                                    pair,
                                    cycle,
                                    f"btc_regime_gp:{_brg.get('label')}:{_brg.get('reason')}",
                                )
                                summary["skips"] += 1
                if _btc_block and trad_sig is None and not _rank_on:
                    continue
                if not _btc_block:
                    try:
                        from hermes_core.engines import gp_intelligence as gpi

                        _sup, _reason = gpi.should_suppress(pair)
                        if _sup and trad_sig is None and not _rank_on:
                            _log_skip(bot, pair, cycle, f"gp_intel_suppress:{_reason}")
                            summary["skips"] += 1
                            continue
                        if not _sup:
                            gp_sig = _gp_vote(
                                pair,
                                prices,
                                strategy,
                                cortex=cortex,
                                bot=bot,
                                promote=True,
                                use_invent_tf=True,
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
                    _et = _s.meta.get("entry_type") or getattr(_s, "type", None) or "mean_reversion"
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
                        _sup_tmp = bool(policy is not None and policy.is_suppressed(pair, _et))
                        _ew = float(
                            expert_weight(
                                enabled=_soft_tmp,
                                suppressed=_sup_tmp,
                                evidence_n=cortex.evidence_n(pair, _et),
                                wr=_wr,
                            ).get("weight")
                            or 1.0
                        )
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
                    cands.append(
                        {
                            "sig": _s,
                            "entry_type": _et,
                            "score": _sc["score"],
                            "components": _sc["components"],
                        }
                    )
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

            # L14 capital veto: sleeve-aware when REGIME_SPLIT is on (MR vs trend).
            # GP promote still uses legacy avoid veto (unproven sleeve).
            if sig is not None:
                _etype_pre = (
                    sig.meta.get("entry_type") or getattr(sig, "type", None) or "mean_reversion"
                )
                _block = False
                try:
                    from hermes_core.engines.regime_split import (
                        chart_blocks_sleeve,
                        classify_market,
                        regime_split_enabled,
                    )

                    if regime_split_enabled(bot=bot, strategy=strategy) and _etype_pre != "gp_ensemble":
                        _mkt = classify_market(
                            adx=(ind or {}).get("adx"),
                            regime=regimes.get(pair) or (ind or {}).get("regime"),
                            context=context,
                        )
                        _block = chart_blocks_sleeve(
                            context, sleeve=str(_etype_pre), market=_mkt
                        )
                    else:
                        from hermes_core.engines.chart_vision import chart_hard_blocks_strategy

                        _block = chart_hard_blocks_strategy(
                            context, strategy_type=str(_etype_pre)
                        )
                except Exception:  # noqa: BLE001
                    from hermes_core.engines.chart_vision import chart_hard_blocks_strategy

                    _block = chart_hard_blocks_strategy(
                        context,
                        strategy_type=str(
                            (sig.meta.get("entry_type") if sig else None)
                            or (strategy or {}).get("strategy_type")
                            or ""
                        ),
                    )
                if _block:
                    _log_skip(bot, pair, cycle, "no_signal:chart:hard_block")
                    summary["skips"] += 1
                    continue
            # GP signals never pass through evaluate_entry — stamp soft chart tilt.
            if sig is not None and "chart_quality_mult" not in (sig.meta or {}):
                apply_chart_soft_to_signal(sig, context)

            # Halt blocks NEW entries only (exits still managed above).
            # Check before no_signal logging so idle SLO is not polluted by halt.
            if _halted:
                _log_skip(bot, pair, cycle, _halt_reason or "halt")
                summary["skips"] += 1
                continue
            if summary.get("market_closed"):
                _log_skip(bot, pair, cycle, "market_closed")
                summary["skips"] += 1
                continue
            if sig is None:
                _skip = _trad_skip or "no_signal"
                # Ops clarity: chart capital veto + GP promote silent/absent.
                if (
                    _skip == "chart:hard_block"
                    and _want_gp
                    and gp_sig is None
                ):
                    _skip = "chart:hard_block+gp:silent"
                if _skip == "no_signal":
                    _skip = "no_signal"
                elif not _skip.startswith(
                    (
                        "session",
                        "rsi",
                        "vol",
                        "quality",
                        "chart",
                        "cooldown",
                        "ensemble",
                        "other",
                        "regime",
                        "trend",
                        "cost",
                    )
                ):
                    _skip = f"no_signal:{_skip}"
                else:
                    # Keep structured sub-reason; prefix for dashboard filters.
                    _skip = f"no_signal:{_skip}"
                _log_skip(bot, pair, cycle, _skip)
                summary["skips"] += 1
                # Gold diagnostics: snapshot why metals stay idle.
                if bot == "gold":
                    with contextlib.suppress(Exception):
                        print(
                            f"[hermes][gold-diag] {pair} cycle={cycle} "
                            f"skip={_skip} rsi={ind.get('rsi')} "
                            f"adx={ind.get('adx')} session={session_token} "
                            f"bb_bw="
                            f"{((ind.get('bb') or {}).get('upper', 0) - (ind.get('bb') or {}).get('lower', 0)) / max((ind.get('bb') or {}).get('middle') or 1, 1e-9):.6f}",
                            flush=True,
                        )
                continue
            # Per-quote sanity before opening.
            _ok_px, _px_reason = price_sanity_pair(pair, price)
            if not _ok_px:
                _log_skip(bot, pair, cycle, _px_reason)
                summary["skips"] += 1
                write_halt(bot, _px_reason)
                _halted, _halt_reason = True, _px_reason
                continue
            # [GUARD L35] policy may bench GP or MR when the other type is clearly better.
            # Prefer meta.entry_type; fall back to Signal.type so momentum is never
            # mis-labelled as mean_reversion (which poisoned cortex/policy WRs).
            _etype = sig.meta.get("entry_type") or getattr(sig, "type", None) or "mean_reversion"
            _suppressed = bool(policy is not None and policy.is_suppressed(pair, _etype))
            # HIF Phase-2: soft weights turn L35 benches into size shrinks.
            # Flag OFF → hard skip (legacy). Flag ON → never skip for policy —
            # EXCEPT fleet MR rollback, which must hard-bench mean_reversion.
            _soft = False
            try:
                _soft = soft_weights_enabled()
            except Exception:  # noqa: BLE001
                _soft = False
            _rollback_hard = bool(
                policy is not None
                and getattr(policy, "rollback", False)
                and _etype == "mean_reversion"
            )
            if _suppressed and (not _soft or _rollback_hard):
                _log_skip(bot, pair, cycle, f"policy_suppress:{_etype}")
                summary["skips"] += 1
                continue
            # Phase 5 — regime decay 2-of-3 suppress (new entries only).
            with contextlib.suppress(Exception):
                from hermes_core.engines.regime_decay import (
                    is_pair_suppressed,
                    regime_decay_enabled,
                )

                if regime_decay_enabled() and is_pair_suppressed(bot, pair):
                    _log_skip(bot, pair, cycle, "regime_decay_suppress")
                    summary["skips"] += 1
                    continue
            # RR guard (S6) — classic target/stop >= 1.0.
            # BTC swing stack banks via trail / soft-partial / time-bank, so fixed
            # TP may be <= SL by design; do not hard-block those.
            sl = float(strategy["stop_loss_pct"])
            tp = float(strategy["profit_target_pct"])
            # Sentient sleeve risk: stamp on position knobs only (never mutate strategy).
            with contextlib.suppress(Exception):
                from hermes_core.engines.sentient_entry import sleeve_risk_overlays

                _ov = sleeve_risk_overlays(strategy, str(_etype))
                if _ov.get("stop_loss_pct") is not None:
                    sl = float(_ov["stop_loss_pct"])
                if _ov.get("profit_target_pct") is not None:
                    tp = float(_ov["profit_target_pct"])
            _rr_relax = False
            with contextlib.suppress(Exception):
                _rr_relax = (
                    str(bot or "").lower() in {"btc", "crypto"}
                    or float(strategy.get("trailing_stop_pct") or 0) > 0
                    or bool(strategy.get("partial_enabled"))
                    or str(_etype) in {"pullback", "donchian_breakout"}
                )
            # Soft crisis recommend: widen stop only when non-novel + RR still ok.
            # Target not applied (would shrink R:R). Stamp rec on the position.
            # Default OFF (soak-dormant); set CRISIS_RECOMMEND=1 to enable.
            _crisis_rec: dict = {}
            with contextlib.suppress(Exception):
                from hermes_core.engines.hif_flags import crisis_recommend_enabled

                if crisis_recommend_enabled():
                    _crisis_rec = recommend_from_prices(prices)
                    if not _crisis_rec.get("novel"):
                        _sl_w = soft_widen_stop(sl, _crisis_rec)
                        if _sl_w != sl and check_rr_guard(_sl_w, tp):
                            sl = _sl_w
            if not _rr_relax and not check_rr_guard(sl, tp):
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
                _size_regime,
                atr,
                _open_bullish,
                strategy,
            )
            # HIF Phase-1 probe sizing: shrink only when PROBE_SIZING=1 and
            # cortex evidence for (pair, entry_type) is thin. Never skips.
            # Fail-open to full size if cortex cannot be read.
            from hermes_core.engines.hif_flags import probe_sizing_enabled

            _probe_enabled = probe_sizing_enabled()
            _evidence_n: int | None = None
            if _probe_enabled or _soft:
                try:
                    _evidence_n = int(cortex.evidence_n(pair, _etype))
                except Exception:  # noqa: BLE001 — fail-open → full
                    _evidence_n = None
            _probe = apply_probe_sizing(
                size,
                enabled=_probe_enabled,
                evidence_n=_evidence_n,
            )
            size = float(_probe["size"])
            # Phase 5 micro-live: scale all new sizes when MICRO_LIVE=1.
            with contextlib.suppress(Exception):
                if get_env("MICRO_LIVE", "0") == "1":
                    try:
                        _ml = float(get_env("MICRO_LIVE_SIZE_MULT", "0.25"))
                    except ValueError:
                        _ml = 0.25
                    size = round(max(0.0, size * max(0.0, min(1.0, _ml))), 6)
            # Chart soft size tilt (downtrend / wait-for-pullback) — never a veto.
            _chart_size_mult = 1.0
            _chart_soft_reasons: list = []
            with contextlib.suppress(Exception):
                _chart_size_mult = float(chart_size_mult(context))
                _chart_soft_reasons = list((sig.meta or {}).get("chart_soft_reasons") or [])
                if _chart_size_mult < 1.0:
                    size = round(max(0.0, size * _chart_size_mult), 6)
            # Phase 3.5: reflection size-down safe mode (all axes exhausted).
            if _safe_mode and _safe_mode.get("mode") == "size_down":
                try:
                    _sf = float(get_env("REFLECT_SAFE_SIZE_FACTOR", "0.5"))
                except ValueError:
                    _sf = 0.5
                size = round(size * max(0.0, min(1.0, _sf)), 6)
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
                    chart_context=context,
                    chart_soft_reasons=list(
                        (sig.meta or {}).get("chart_soft_reasons") or _chart_soft_reasons or []
                    ),
                )
                if _mg.get("mom_guard_action") == "bench":
                    _log_skip(
                        bot,
                        pair,
                        cycle,
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
                with contextlib.suppress(Exception):
                    _edge = cortex.edge_stats(pair, _etype) or _edge
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
            if size <= 0:
                _dec = str((sig.meta or {}).get("entry_decision") or "")
                _min_probe = 0.0
                with contextlib.suppress(Exception):
                    _min_probe = float(strategy.get("min_probe_size") or 0.0)
                if _dec == "probe" and _min_probe > 0:
                    size = _min_probe
                elif _dec == "probe":
                    _log_skip(bot, pair, cycle, "probe_size_zero")
                    summary["skips"] += 1
                    continue
                else:
                    _log_skip(bot, pair, cycle, "size_zero")
                    summary["skips"] += 1
                    continue
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
                _gb_min = float(
                    strategy.get(
                        "mfe_giveback_min_pct",
                        DEFAULT_MFE_GIVEBACK_MIN_PCT,
                    )
                )
            except (TypeError, ValueError):
                _gb_min = DEFAULT_MFE_GIVEBACK_MIN_PCT
            try:
                _gb_frac = float(
                    strategy.get(
                        "mfe_giveback_frac",
                        DEFAULT_MFE_GIVEBACK_FRAC,
                    )
                )
            except (TypeError, ValueError):
                _gb_frac = DEFAULT_MFE_GIVEBACK_FRAC
            _gb_on = strategy.get("mfe_giveback_enabled", True) is not False
            stop = _atr_stop_for(strategy, price, atr)
            _hold_knobs: dict = {}
            with contextlib.suppress(Exception):
                from hermes_core.engines.layered_hold import strategy_hold_knobs

                _hold_knobs = strategy_hold_knobs(strategy, entry_type=_etype)
            _side = str(getattr(sig, "side", None) or strategy.get("entry", {}).get("direction") or "long")
            _entry_mid = float(price)
            _cost = None
            _entry_fill = _entry_mid
            with contextlib.suppress(Exception):
                from hermes_core.engines.cost_model import estimate, apply_entry_fill

                _atr_pct = None
                if atr and _entry_mid > 0:
                    _atr_pct = float(atr) / _entry_mid * 100.0
                _cost = estimate(pair, atr_pct=_atr_pct)
                _entry_fill = apply_entry_fill(_entry_mid, _side, _cost.entry_haircut_pct)
            # Accurate size_mode for dashboard (sentient probe / chart soft / HIF probe).
            _size_stamp = resolve_size_stamp(
                size_mode=_probe.get("size_mode"),
                entry_decision=(sig.meta or {}).get("entry_decision"),
                chart_size_mult=_chart_size_mult,
                size=size,
                base_size=_probe.get("base_size") or size,
                probe_fraction=_probe.get("probe_fraction"),
            )
            _size_mode = _size_stamp["size_mode"]
            _size_reason = _size_stamp["size_reason"]
            _probe_frac = _size_stamp["probe_fraction"]
            open_positions[pair] = {
                "id": f"{bot}:{pair}:{int(time.time())}",
                "entry_ts": _now_iso(),
                "entry_price": _entry_fill,
                "entry_mid": _entry_mid,
                "side": _side,
                "cost_model": _cost.as_dict() if _cost is not None else None,
                "entry_haircut_pct": (_cost.entry_haircut_pct if _cost else 0.0),
                "exit_haircut_pct": (_cost.exit_haircut_pct if _cost else 0.0),
                "fees_pct_rt": (_cost.round_trip_pct if _cost else 0.0),
                "size": min(size, MAX_POSITION_SIZE),
                "stop_loss_pct": sl,
                "profit_target_pct": tp,
                "time_exit_cycles": int(
                    strategy.get(
                        "time_exit_cycles",
                        DEFAULT_TIME_EXIT_CYCLES,
                    )
                ),
                "held_cycles": 0,
                "breakeven_set": False,
                "partial_done": False,
                "partial_enabled": bool(
                    strategy.get("partial_enabled")
                    if strategy.get("partial_enabled") is not None
                    else _xi.get("partial_enabled")
                ),
                "current_stop": stop,
                "atr": atr,
                "atr_floor_pct": float(strategy.get("atr_floor_pct") or 0.0),
                "exit_tf": str(
                    strategy.get("exit_tf")
                    or (strategy.get("entry") or {}).get("interval")
                    or ""
                ),
                "signal_interval": str(
                    (strategy.get("entry") or {}).get("interval")
                    or strategy.get("signal_interval")
                    or ""
                ),
                "signal_period": str(
                    (strategy.get("entry") or {}).get("period") or "120d"
                ),
                "signal_max_candles": int(
                    (strategy.get("entry") or {}).get("max_candles") or 800
                ),
                "entry_type": _etype,
                "strategy_version": str(strategy.get("version", "00")),
                "entry_conviction": (sig.meta or {}).get("conviction"),
                "entry_decision": (sig.meta or {}).get("entry_decision"),
                "entry_sleeve": (sig.meta or {}).get("entry_sleeve")
                or (
                    _etype
                    if _etype in {"pullback", "mean_reversion"}
                    else None
                ),
                "world": (sig.meta or {}).get("world"),
                # B9: firing GP indicator IDs so that on close ONLY these are
                # credited (per-vote credit, not the whole ensemble blob).
                "gp_indicators": sig.meta.get("gp_indicators", []),
                # HIF Phase-1 + sentient/chart size mode (accurate for dashboard)
                "size_mode": _size_mode,
                "size_reason": _size_reason,
                "size_regime": _size_regime,
                "evidence_n": _probe.get("evidence_n") if _probe_enabled else _evidence_n,
                "evidence_state": _probe["evidence_state"],
                "base_size": _probe.get("base_size"),
                "probe_fraction": _probe_frac,
                # Chart soft tilt (L14 avoid = hard veto earlier; this is size only)
                "chart_size_mult": _chart_size_mult,
                "chart_quality_mult": (sig.meta or {}).get("chart_quality_mult"),
                "chart_soft_reasons": _chart_soft_reasons
                or list((sig.meta or {}).get("chart_soft_reasons") or []),
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
                "entry_regime": _regime.get("regime")
                or (
                    regimes.get(pair)
                    if not isinstance(regimes.get(pair), dict)
                    else (regimes.get(pair) or {}).get("live")
                ),
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
                "be_trigger_frac": (
                    strategy.get("be_trigger_frac")
                    if strategy.get("be_trigger_frac") is not None
                    else _xi.get("be_trigger_frac")
                ),
                "trailing_atr_mult": _trail,
                "trailing_stop_pct": float(strategy.get("trailing_stop_pct", 0.0) or 0.0),
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
                # Layered sentient hold knobs (L0–L1)
                **_hold_knobs,
                # Crisis recommend (soft advisory; stop may already be widened)
                "crisis_name": _crisis_rec.get("crisis_name"),
                "crisis_novel": _crisis_rec.get("novel"),
                "crisis_distance": _crisis_rec.get("distance"),
                "crisis_recommended_stop_pct": _crisis_rec.get("recommended_stop_pct"),
                "crisis_recommended_target_pct": _crisis_rec.get("recommended_target_pct"),
            }
            with contextlib.suppress(Exception):
                from hermes_core.engines.trade_truth import append_entry_taken

                _op = open_positions[pair]
                append_entry_taken(
                    bot,
                    {
                        "id": _op.get("id"),
                        "pair": pair,
                        "event": "open",
                        "reason": "taken_open",
                        "entry_type": _op.get("entry_type"),
                        "entry_decision": _op.get("entry_decision"),
                        "size_mode": _op.get("size_mode"),
                        "size": _op.get("size"),
                        "base_size": _op.get("base_size"),
                        "entry_mid": _op.get("entry_mid"),
                        "mark": _op.get("entry_mid"),
                    },
                )
            # [CORTEX] record the entry (per-type memory; exile persists across cycles)
            with contextlib.suppress(Exception):
                cortex.record_entry(pair, _etype)
            # Burn alt daily quota only after a real open (not on rejected signals).
            if str(_etype) in {"pullback", "mean_reversion"}:
                with contextlib.suppress(Exception):
                    from hermes_core.engines.sentient_entry import (
                        load_entry_runtime,
                        save_entry_runtime,
                    )

                    _rt = load_entry_runtime(bot)
                    _rt["alt_entries_today"] = int(_rt.get("alt_entries_today") or 0) + 1
                    save_entry_runtime(bot, _rt)
            summary["entries"].append(pair)
        # Open positions are managed at the top of the pair loop (exit-before-guard).

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
            bot,
            list(pairs),
            strategies=_strats,
        )
        # HIF: gated promote of deployable skip_shadow_proposed (never blind).
        summary["skip_shadow_promote"] = maybe_promote_skip_shadow(
            bot,
            strategies=_strats,
        )
    except Exception:  # noqa: BLE001
        summary["skip_shadow"] = {"enabled": False}
        summary["skip_shadow_promote"] = {"enabled": False}

    # --- heartbeat every cycle without exception --------------------------
    # Weekend / holiday: expected empty FX candles must not leave L24 open.
    if market_closed:
        consecutive_failures = 0
    status = "ok" if consecutive_failures == 0 else "degraded"
    # Holiday / feed-freeze backup: calendar open but every live tick buffer flat.
    if not market_closed and live_book_is_flat(
        price_history,
        min_pairs=max(1, len(pairs) // 2),
    ):
        market_closed = True
        summary["market_closed"] = True
        summary["market_closed_reason"] = "flat_book"
    _book_ok, _book_reason = price_sanity_book(
        summary.get("prices") or {},
        price_history,
    )
    if not _book_ok:
        status = "degraded"
        summary["price_sanity"] = _book_reason
        write_halt(bot, _book_reason)
        summary["halted"] = True
    _hif = None
    with contextlib.suppress(Exception):
        from hermes_core.engines.hif_flags import snapshot as hif_snapshot

        _hif = hif_snapshot()
        summary["hif_flags"] = _hif
    _rs = None
    with contextlib.suppress(Exception):
        from hermes_core.engines.regime_split import regime_split_enabled

        _on = bool(regime_split_enabled(bot=bot))
        _rs = {
            "enabled": _on,
            "bot": bot,
            "scope": "all_bots_default" if _on else "off",
        }
        summary["regime_split"] = _rs
    write_heartbeat(
        bot,
        cycle,
        consecutive_failures,
        last_price,
        status=status,
        health=dict(health_registry),
        chart_contexts=chart_contexts,
        market_closed=market_closed,
        regimes=regimes,
        prices=summary.get("prices") or {},
        price_history=price_history,
        hif_flags=_hif,
        regime_split=_rs,
        btc_d1_regimes=getattr(run_cycle, "_btc_d1_regimes", None),
        sentient={
            "SENTIENT_ENTRY": get_env("SENTIENT_ENTRY", "0") == "1",
            "SENTIENT_HOLD": get_env("SENTIENT_HOLD", "0") == "1",
            "CONTINUOUS_VISION": get_env("CONTINUOUS_VISION", "0") == "1",
            "last": getattr(run_cycle, "_sentient_last", None),
        },
    )
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
    run_cycle._price_history = {
        p: price_history.get(p, [])
        for p in set(price_history) | set(getattr(run_cycle, "_price_history", {}) or {})
    }
    run_cycle._regimes = dict(regimes)
    run_cycle._flatline_pause = {
        p: n for p, n in flatline_pause.items() if int(n) > 0 and p in pairs
    }
    run_cycle._mark_fails = {
        p: n for p, n in mark_fails.items() if int(n) > 0 and p in pairs
    }
    # Persist open book each cycle so restarts can resume exits.
    with contextlib.suppress(Exception):
        save_open_book(
            bot,
            open_positions=open_positions,
            reentry=reentry,
            mark_fails=mark_fails,
            cycle=cycle,
            consecutive_failures=consecutive_failures,
        )
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
