"""Exit engine — layered sentient hold/bank (L0–L1 core, L5 arbiter hook).

Pure + deterministic given trade dict + price (+ optional history).
Exactly ONE exit reason per evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes_core.env import get_env
from hermes_core.indicators import compute_atr

CIRCUIT_MAX_CONSECUTIVE_FAILURES = 5  # [GUARD L24]


def _sentient_hold_enabled() -> bool:
    return get_env("SENTIENT_HOLD", "0") == "1"

DEFAULT_TIME_EXIT_CYCLES = 150
DEFAULT_MFE_GIVEBACK_MIN_PCT = 0.4
DEFAULT_MFE_GIVEBACK_FRAC = 0.5
DEFAULT_EARLY_REEVAL_CYCLES = 120
DEFAULT_TIME_EXIT_MAX_CYCLES = 720
DEFAULT_MIN_BANK_NET_PCT = 0.10
DEFAULT_PEAK_EPSILON_PCT = 0.05
DEFAULT_MFE_STALL_BARS = 1
DEFAULT_CLOCK_LOCK_FRAC = 0.5
DEFAULT_PATH_SLACK = 1.25
DEFAULT_SOFT_PARTIAL_TP_FRAC = 0.4
HOLD_SCORE_PRIORS = (0.45, 0.35, 0.20)  # progress, fresh, capture


@dataclass
class Exit:
    reason: str
    price: float
    new_stop: float | None = None
    partial_close_fraction: float | None = None


def _unrealised_pct(trade: dict, current_price: float) -> float:
    if trade.get("unrealised_pct") is not None:
        return float(trade["unrealised_pct"])
    entry = trade["entry_price"]
    if entry == 0:
        return 0.0
    return (current_price - entry) / entry * 100.0


def should_circuit_break(
    consecutive_failures: int,
    max_consecutive: int = CIRCUIT_MAX_CONSECUTIVE_FAILURES,
) -> bool:
    return consecutive_failures >= max_consecutive


def _f(trade: dict, key: str, default: float) -> float:
    try:
        if trade.get(key) is not None:
            return float(trade[key])
    except (TypeError, ValueError):
        pass
    return float(default)


def _i(trade: dict, key: str, default: int) -> int:
    try:
        if trade.get(key) is not None:
            return int(trade[key])
    except (TypeError, ValueError):
        pass
    return int(default)


def _exit_haircut(trade: dict) -> float:
    return max(0.0, _f(trade, "exit_haircut_pct", 0.0))


def net_unreal(trade: dict, unreal: float) -> float:
    """Gross unreal minus remaining exit haircut (entry already in fill)."""
    return float(unreal) - _exit_haircut(trade)


def min_net_floor(trade: dict) -> float:
    """Minimum net % to treat as bankable / soft-clock protected.

    Floor is the max of stamped ``min_bank_net_pct``, exit haircut, and (when
    ``min_bank_fee_mode`` is round_trip) full round-trip fees so we do not
    bank or hold through fee-killed noise.
    """
    floor = max(_exit_haircut(trade), _f(trade, "min_bank_net_pct", DEFAULT_MIN_BANK_NET_PCT))
    # Default exit_only preserves legacy floor (max of haircut + min_bank).
    # BTC stamps min_bank_fee_mode=round_trip so floor also clears RT fees.
    mode = str(trade.get("min_bank_fee_mode") or "exit_only").strip().lower()
    if mode in {"round_trip", "rt", "fees_rt"}:
        rt = _f(trade, "fees_pct_rt", 0.0)
        if rt <= 0:
            rt = _f(trade, "fees_pct", 0.0)
        if rt > 0:
            floor = max(floor, rt)
    elif mode in {"stressed_rt", "stressed"}:
        rt = _f(trade, "fees_pct_rt", 0.0)
        if rt <= 0:
            rt = _f(trade, "fees_pct", 0.0)
        if rt > 0:
            floor = max(floor, rt * 2.0)
    return floor


def _probe_soft_cut_hit(trade: dict, held: int) -> bool:
    """Dead-probe cut: low peak MFE after soft-cut arm cycles."""
    if str(trade.get("size_mode") or "").strip().lower() != "probe":
        return False
    if trade.get("probe_soft_cut_enabled") is not True:
        return False
    lo = _i(trade, "probe_soft_cut_min_cycles", 120)
    if held < lo:
        return False
    # Optional upper bound: still cut after hi if MFE never cleared (default on).
    thresh = _f(trade, "probe_soft_cut_mfe_pct", 0.40)
    try:
        peak = float(trade.get("peak_mfe_pct") or 0.0)
    except (TypeError, ValueError):
        peak = 0.0
    return peak < thresh


def _is_donchian_entry(trade: dict) -> bool:
    et = str(trade.get("entry_type") or trade.get("entry_sleeve") or "").strip().lower()
    if not et:
        # Legacy / unset: treat as Donchian-capable (strategy default sleeve).
        return True
    return et in {"donchian_breakout", "donchian", "breakout"}


def is_failed_breakout_cut(trade: dict, unreal: float) -> bool:
    """True when Donchian failed-breakout exit should fire.

    Guards:
    * only Donchian sleeves (pullback/MR never use FB)
    * enough exit bars held
    * still not net-green
    * adverse depth past ``failed_breakout_min_mae_pct`` (fee-floor knife guard)
    """
    if not _is_donchian_entry(trade):
        return False
    fb_bars = _i(trade, "failed_breakout_bars", 0)
    if fb_bars <= 0:
        return False
    if _i(trade, "exit_bars_held", 0) < fb_bars:
        return False
    if float(unreal) > 0:
        return False
    if net_unreal(trade, unreal) >= min_net_floor(trade):
        return False
    min_mae = _f(trade, "failed_breakout_min_mae_pct", 0.0)
    if min_mae > 0:
        try:
            trough = trade.get("trough_mae_pct")
            mae_depth = (
                abs(float(trough)) if trough is not None else abs(min(0.0, float(unreal)))
            )
        except (TypeError, ValueError):
            mae_depth = abs(min(0.0, float(unreal)))
        if mae_depth < min_mae:
            return False
    return True


def _mfe_giveback_hit(trade: dict, unreal: float) -> bool:
    if trade.get("mfe_giveback_enabled", True) is False:
        return False
    try:
        peak = float(trade.get("peak_mfe_pct") or 0.0)
        min_mfe = float(trade.get("mfe_giveback_min_pct", DEFAULT_MFE_GIVEBACK_MIN_PCT))
        thresh = float(trade.get("mfe_giveback_frac", DEFAULT_MFE_GIVEBACK_FRAC))
    except (TypeError, ValueError):
        return False
    if peak < min_mfe or peak <= 1e-9 or thresh <= 0:
        return False
    giveback = max(0.0, peak - float(unreal))
    return (giveback / peak) >= thresh


def hold_score_weights(trade: dict) -> tuple[float, float, float]:
    w = trade.get("hold_score_weights")
    if isinstance(w, (list, tuple)) and len(w) == 3:
        try:
            a, b, c = float(w[0]), float(w[1]), float(w[2])
            s = a + b + c
            if s > 1e-9:
                return a / s, b / s, c / s
        except (TypeError, ValueError):
            pass
    return HOLD_SCORE_PRIORS


def compute_hold_score(trade: dict, unreal: float) -> float:
    tp = max(_f(trade, "profit_target_pct", 1.5), 1e-6)
    peak = max(_f(trade, "peak_mfe_pct", 0.0), 0.0)
    progress = max(0.0, min(1.0, peak / tp))
    bars = max(0, _i(trade, "exit_bars_since_peak", 0))
    fresh = 1.0 / (1.0 + float(bars))
    capture = (float(unreal) / peak) if peak > 1e-9 else 0.0
    capture = max(0.0, min(1.0, capture))
    wp, wf, wc = hold_score_weights(trade)
    # Optional hold_policy posterior mix
    prior = wp * progress + wf * fresh + wc * capture
    try:
        post = trade.get("hold_policy_p_hold")
        if post is not None:
            n = _i(trade, "hold_policy_n", 0)
            if n >= 30:
                blend = 0.4 * prior + 0.6 * float(post)
                return max(0.0, min(1.0, blend))
    except (TypeError, ValueError):
        pass
    return max(0.0, min(1.0, prior))


def patience_mult(trade: dict) -> float:
    mult = 1.0
    label = str(
        trade.get("live_d1")
        or trade.get("d1")
        or trade.get("regime_label")
        or trade.get("entry_regime")
        or ""
    ).lower()
    if "trend_up" in label or label == "up":
        mult *= 1.5
    elif "trend_down" in label or label == "down":
        mult *= 0.5
    elif "range" in label or "chop" in label:
        mult *= 0.7
    try:
        chart_p = trade.get("live_chart_patience")
        if chart_p is not None:
            mult *= float(chart_p)
    except (TypeError, ValueError):
        pass
    if trade.get("chart_soft_reasons"):
        mult *= 0.8
    try:
        sp = trade.get("structure_patience_mult")
        if sp is not None:
            mult *= float(sp)
    except (TypeError, ValueError):
        pass
    try:
        pb = trade.get("playbook_patience_mult")
        if pb is not None:
            mult *= float(pb)
    except (TypeError, ValueError):
        pass
    try:
        world_deg = trade.get("world_degraded")
        if world_deg:
            mult *= 0.75
        event_risk = trade.get("event_risk")
        if event_risk is not None and float(event_risk) >= 0.7:
            mult *= 0.7
    except (TypeError, ValueError):
        pass
    return max(0.4, min(2.0, mult))


def effective_stall_bars(trade: dict) -> int:
    base = max(1, _i(trade, "mfe_stall_bars", DEFAULT_MFE_STALL_BARS))
    if (
        str(trade.get("size_mode") or "").strip().lower() == "probe"
        and trade.get("probe_ignore_patience_stall", True) is not False
    ):
        return base
    return max(1, int(round(base * patience_mult(trade))))


def dual_slope_path(trade: dict, unreal: float, tp: float | None) -> dict:
    """Fast/slow MFE slopes and path_ok / path_fail flags."""
    peak = _f(trade, "peak_mfe_pct", 0.0)
    hist = trade.get("mfe_bar_peaks") or []
    try:
        peaks = [float(x) for x in hist][-4:]
    except (TypeError, ValueError):
        peaks = []
    if peak and (not peaks or peaks[-1] != peak):
        peaks = (peaks + [peak])[-4:]
    fast = 0.0
    slow = 0.0
    if len(peaks) >= 2:
        fast = peaks[-1] - peaks[-2]
    if len(peaks) >= 2:
        span = min(3, len(peaks) - 1)
        slow = (peaks[-1] - peaks[-1 - span]) / float(span)
    held = _i(trade, "held_cycles", 0)
    hard = _i(trade, "time_exit_max_cycles", DEFAULT_TIME_EXIT_MAX_CYCLES)
    # ~240 cycles per 4h bar at 60s
    cyc_per_bar = max(1, _i(trade, "cycles_per_exit_bar", 240))
    bars_left = max(1.0, (hard - held) / float(cyc_per_bar))
    slack = _f(trade, "path_slack", DEFAULT_PATH_SLACK)
    need = max(0.0, float(tp or 0.0) - float(unreal))
    path_ok = slow > 0 and (need <= 1e-9 or (need / slow) <= bars_left * slack)
    path_fail = (fast <= 0 and slow <= 0) or (slow > 0 and need / max(slow, 1e-9) > bars_left * slack)
    # p_hit_tp forecast override when present (L6)
    try:
        p_hit = trade.get("p_hit_tp")
        if p_hit is not None:
            thr = _f(trade, "p_hit_tp_threshold", 0.35)
            if float(p_hit) < thr:
                path_fail = True
                path_ok = False
            elif float(p_hit) >= thr and slow >= 0:
                path_ok = True
    except (TypeError, ValueError):
        pass
    return {
        "fast_slope": fast,
        "slow_slope": slow,
        "path_ok": path_ok,
        "path_fail": path_fail,
        "bars_left": bars_left,
    }


def _protect_stop(trade: dict, entry: float, unreal: float) -> float:
    hair = _exit_haircut(trade)
    peak = max(_f(trade, "peak_mfe_pct", 0.0), 0.0)
    lock_frac = _f(trade, "clock_lock_frac", DEFAULT_CLOCK_LOCK_FRAC)
    fee_lock = entry * (1.0 + hair / 100.0)
    cap_lock = entry * (1.0 + (lock_frac * peak) / 100.0)
    stop = max(fee_lock, cap_lock)
    # atr floor distance under mark if we have atr_floor
    floor = _f(trade, "atr_floor_pct", 0.0)
    src = str(trade.get("exit_tf_source") or "live")
    if src == "synthetic":
        floor *= 1.25
    # never arm above a level that locks less than min net if peak tiny
    return stop


def _stop_locks_min_net(trade: dict, entry: float) -> bool:
    cur = trade.get("current_stop")
    if cur is None:
        return False
    try:
        # stop at/above fee lock implies protected
        fee_lock = entry * (1.0 + _exit_haircut(trade) / 100.0)
        return float(cur) + 1e-12 >= fee_lock
    except (TypeError, ValueError):
        return False


def _layered_hold_bank(
    trade: dict, current_price: float, entry: float, unreal: float, tp: float | None
) -> Exit | None:
    """L0–L1 clock / score / path bank-protect brain (after classic protectors)."""
    held = _i(trade, "held_cycles", 0)
    early = _i(trade, "early_reeval_cycles", DEFAULT_EARLY_REEVAL_CYCLES)
    te = trade.get("time_exit_cycles")
    te_i = _i(trade, "time_exit_cycles", DEFAULT_TIME_EXIT_CYCLES) if te is not None else None
    hard = _i(trade, "time_exit_max_cycles", DEFAULT_TIME_EXIT_MAX_CYCLES)

    # Failed breakout: Donchian-only, N bars red, MAE past fee floor
    net = net_unreal(trade, unreal)
    floor = min_net_floor(trade)
    if is_failed_breakout_cut(trade, unreal):
        return Exit("failed_breakout", current_price)

    # Dead probes: no meaningful MFE after soft-cut arm → cut before 8h clock.
    if _probe_soft_cut_hit(trade, held):
        return Exit("probe_soft_cut", current_price)

    armed = held >= early
    past_soft = te_i is not None and held >= te_i
    past_hard = held >= hard

    if not armed and not past_soft and not past_hard:
        return None

    if past_soft and net < floor:
        return Exit("time_exit", current_price)

    if net < floor:
        # early arm but not soft: don't cut slight losers early
        if past_hard:
            return Exit("time_exit", current_price)
        return None

    # net-green path
    score = compute_hold_score(trade, unreal)
    path = dual_slope_path(trade, unreal, tp)
    stall_need = effective_stall_bars(trade)
    stalled = _i(trade, "exit_bars_since_peak", 0) >= stall_need
    # legacy missing stall counter past soft → treat as stalled (bank zombies)
    if past_soft and trade.get("exit_bars_since_peak") is None:
        stalled = True

    # Protect first if needed
    if not _stop_locks_min_net(trade, entry):
        if str(trade.get("exit_tf_source") or "live") != "none":
            new_stop = _protect_stop(trade, entry, unreal)
            cur = trade.get("current_stop")
            if cur is None or new_stop > float(cur) + 1e-12:
                return Exit("trailing", current_price, new_stop=new_stop)

    # Bank when score low or path failed / stalled
    bank_bias = 0.35 / max(patience_mult(trade), 0.5)
    try:
        score_ceil = float(trade.get("bank_score_ceiling") or 0.55)
    except (TypeError, ValueError):
        score_ceil = 0.55
    score_ceil = max(0.25, min(0.7, score_ceil))
    if past_hard and stalled:
        return Exit("profit_bank", current_price)
    if past_hard and path["path_ok"] and not stalled:
        # re-tighten protect once
        new_stop = _protect_stop(trade, entry, unreal)
        cur = trade.get("current_stop")
        if cur is None or new_stop > float(cur) + 1e-12:
            return Exit("trailing", current_price, new_stop=new_stop)
        return Exit("profit_bank", current_price)

    if (stalled and path["path_fail"]) or score < bank_bias:
        return Exit("profit_bank", current_price)
    if stalled and score < score_ceil:
        return Exit("profit_bank", current_price)
    if path["path_fail"] and score < score_ceil:
        return Exit("profit_bank", current_price)

    # mid score → already protected; hold
    if score >= score_ceil and path["path_ok"]:
        return None
    if 0.35 <= score < score_ceil:
        return None
    return None


def evaluate_exit(
    trade: dict, current_price: float, prices: list[float] | None = None
) -> Exit | None:
    if not trade or "entry_price" not in trade:
        return None

    entry = trade["entry_price"]
    sl = trade.get("stop_loss_pct")
    tp = trade.get("profit_target_pct")
    unreal = _unrealised_pct(trade, current_price)
    partial_enabled = trade.get("partial_enabled", False)
    partial_done = trade.get("partial_done", False)
    soft_partial_done = trade.get("soft_partial_done", False)
    breakeven_set = trade.get("breakeven_set", False)

    # Skip honor_current_stop on non-TF fallback for clock_protect
    honor = trade.get("honor_current_stop")
    stop_src = str(trade.get("stop_source") or "")
    tf_src = str(trade.get("exit_tf_source") or "live")
    if honor and trade.get("current_stop") is not None:
        skip_wick = stop_src == "clock_protect" and tf_src not in ("live", "synthetic")
        if not skip_wick:
            try:
                if current_price <= float(trade["current_stop"]):
                    return Exit("stop_loss", current_price)
            except (TypeError, ValueError):
                pass

    if sl is not None and current_price <= entry * (1 - sl / 100.0):
        return Exit("stop_loss", current_price)

    # Soft partial ladder (L1) before full TP — always, even under SENTIENT_HOLD
    if (
        tp is not None
        and partial_enabled
        and not soft_partial_done
        and not partial_done
    ):
        soft_frac = _f(trade, "soft_partial_tp_frac", DEFAULT_SOFT_PARTIAL_TP_FRAC)
        soft_lvl = entry * (1.0 + (float(tp) * soft_frac) / 100.0)
        if current_price >= soft_lvl:
            return Exit(
                "partial_close",
                current_price,
                new_stop=entry * (1.0 + _exit_haircut(trade) / 100.0),
                partial_close_fraction=0.5,
            )

    if tp is not None and current_price >= entry * (1 + tp / 100.0):
        if partial_enabled and not partial_done:
            return Exit("partial_close", current_price, new_stop=entry, partial_close_fraction=0.5)
        return Exit("profit_target", current_price)

    if _mfe_giveback_hit(trade, unreal):
        return Exit("mfe_giveback", current_price)

    be_frac = 0.5
    try:
        if trade.get("be_trigger_frac") is not None:
            be_frac = max(0.15, min(0.9, float(trade["be_trigger_frac"])))
    except (TypeError, ValueError):
        be_frac = 0.5
    if tp is not None and not breakeven_set and unreal >= tp * be_frac:
        return Exit("breakeven", current_price, new_stop=entry)

    try:
        min_trail_unreal = float(
            trade.get("mfe_giveback_min_pct", DEFAULT_MFE_GIVEBACK_MIN_PCT)
        )
    except (TypeError, ValueError):
        min_trail_unreal = DEFAULT_MFE_GIVEBACK_MIN_PCT
    min_trail_unreal = max(min_trail_unreal, _exit_haircut(trade))

    mult = trade.get("trailing_atr_mult")
    if mult is not None and unreal >= min_trail_unreal and prices:
        atr = compute_atr(prices)
        if atr > 0:
            floor_pct = _f(trade, "atr_floor_pct", 0.0)
            if str(trade.get("exit_tf_source") or "") == "synthetic":
                floor_pct *= 1.25
            min_dist = max(float(atr) * float(mult), abs(entry) * (floor_pct / 100.0))
            if min_dist <= 0:
                min_dist = float(atr) * float(mult)
            trail_stop = current_price - min_dist
            cur = trade.get("current_stop")
            if cur is None or trail_stop > float(cur):
                return Exit("trailing", current_price, new_stop=trail_stop)

    trail_pct = _f(trade, "trailing_stop_pct", 0.0)
    if trail_pct > 0 and unreal >= min_trail_unreal:
        peak = _f(trade, "peak_mfe_pct", unreal)
        if peak > trail_pct:
            pct_trail_stop = entry * (1.0 + (peak - trail_pct) / 100.0)
            cur = trade.get("current_stop")
            if cur is None or pct_trail_stop > float(cur):
                return Exit("trailing", current_price, new_stop=pct_trail_stop)

    # L5 expert arbiter only for hold/bank after classic protectors (fail-open)
    if trade.get("use_exit_experts") or _sentient_hold_enabled():
        try:
            from hermes_core.engines.exit_experts import arbitrate_exit

            return arbitrate_exit(trade, current_price, prices)
        except Exception:  # noqa: BLE001
            pass

    return _layered_hold_bank(
        trade, current_price, entry, unreal, float(tp) if tp is not None else None
    )
