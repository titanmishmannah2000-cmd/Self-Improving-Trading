"""Helpers to stamp / refresh layered-hold fields on open positions."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_core.env import get_env


def sentient_hold_enabled() -> bool:
    return get_env("SENTIENT_HOLD", "0") == "1"


def limit_removal_enabled() -> bool:
    return get_env("LIMIT_REMOVAL", "0") == "1" or sentient_hold_enabled()


def continuous_vision_enabled() -> bool:
    return get_env("CONTINUOUS_VISION", "0") == "1"


def sentient_entry_enabled() -> bool:
    return get_env("SENTIENT_ENTRY", "0") == "1"


def strategy_hold_knobs(strategy: dict, *, entry_type: str | None = None) -> dict:
    """Extract L0–L1 knobs from strategy YAML for open stamp.

    Failed-breakout exits only apply to Donchian breakouts — pullback / MR
    sleeves get ``failed_breakout_bars=0`` so they are not knife-cut on the
    first red exit bar.
    """
    s = strategy or {}
    et = (entry_type or "").strip().lower()
    donchian = et in {"", "donchian_breakout", "donchian", "breakout"}
    # Empty entry_type: keep strategy default (legacy opens). Explicit alts: off.
    if et and not donchian:
        fb_bars = 0
    else:
        fb_bars = int(s.get("failed_breakout_bars") or 2)
    return {
        "early_reeval_cycles": int(s.get("early_reeval_cycles") or 120),
        "time_exit_max_cycles": int(s.get("time_exit_max_cycles") or 720),
        "min_bank_net_pct": float(s.get("min_bank_net_pct") or 0.10),
        "peak_epsilon_pct": float(s.get("peak_epsilon_pct") or 0.05),
        "mfe_stall_bars": int(s.get("mfe_stall_bars") or 1),
        "clock_lock_frac": float(s.get("clock_lock_frac") or 0.5),
        "path_slack": float(s.get("path_slack") or 1.25),
        "soft_partial_tp_frac": float(s.get("soft_partial_tp_frac") or 0.4),
        "failed_breakout_bars": fb_bars,
        "failed_breakout_min_mae_pct": float(
            s.get("failed_breakout_min_mae_pct") or 0.40
        ),
        "cycles_per_exit_bar": int(s.get("cycles_per_exit_bar") or 240),
        "soft_partial_done": False,
        "exit_bars_since_peak": 0,
        "exit_bars_held": 0,
        "cycles_since_peak_mfe": 0,
        "mfe_bar_peaks": [],
        "mfe_path": [],
        "exit_tf_source": "live",
        "use_exit_experts": sentient_hold_enabled(),
    }


def tf_cache_path(bot: str, pair: str, interval: str) -> Path:
    from hermes_core.state.paths import bot_state_dir

    safe = pair.replace("/", "_")
    d = bot_state_dir(bot) / "tf_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe}_{interval}.json"


def load_tf_cache(bot: str, pair: str, interval: str) -> list[float] | None:
    p = tf_cache_path(bot, pair, interval)
    try:
        if not p.exists():
            return None
        data = json.loads(p.read_text(encoding="utf-8"))
        xs = data.get("prices")
        if isinstance(xs, list) and len(xs) >= 30:
            return [float(x) for x in xs]
    except Exception:  # noqa: BLE001
        return None
    return None


def save_tf_cache(bot: str, pair: str, interval: str, prices: list[float]) -> None:
    try:
        p = tf_cache_path(bot, pair, interval)
        p.write_text(
            json.dumps({"prices": [float(x) for x in prices[-800:]]}),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def synthetic_tf_from_prices(prices: list[float], bucket: int = 240) -> list[float]:
    """Resample 1m-ish closes into synthetic TF closes."""
    if not prices or bucket <= 1:
        return list(prices or [])
    out: list[float] = []
    for i in range(bucket - 1, len(prices), bucket):
        out.append(float(prices[i]))
    if len(prices) % bucket and prices:
        out.append(float(prices[-1]))
    return out if len(out) >= 5 else list(prices)


def resolve_exit_tf_prices(
    bot: str,
    pair: str,
    pos: dict,
    live_prices: list[float] | None,
) -> tuple[float | None, list[float] | None, str]:
    """Return (exit_mark, exit_prices, source)."""
    exit_tf = str(pos.get("exit_tf") or pos.get("signal_interval") or "").strip().lower()
    if exit_tf not in {"4h", "1h", "1d", "2h", "6h", "12h"} or not pair:
        return None, live_prices, "none"
    try:
        from hermes_core.engines.entry import gp_invent_prices

        tf_px = gp_invent_prices(
            pair,
            interval=exit_tf,
            period=str(pos.get("signal_period") or "120d"),
            max_candles=int(pos.get("signal_max_candles") or 800),
        )
        if tf_px and len(tf_px) >= 30:
            save_tf_cache(bot, pair, exit_tf, tf_px)
            return float(tf_px[-1]), tf_px, "live"
    except Exception:  # noqa: BLE001
        pass
    cached = load_tf_cache(bot, pair, exit_tf)
    if cached:
        return float(cached[-1]), cached, "live"
    # synthetic
    bucket = {"1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440}.get(
        exit_tf, 240
    )
    syn = synthetic_tf_from_prices(list(live_prices or []), bucket=bucket)
    if syn and len(syn) >= 5:
        return float(syn[-1]), syn, "synthetic"
    return None, live_prices, "none"


def enrich_open_cycle(bot: str, pair: str, pos: dict, prices: list[float] | None) -> None:
    """Refresh structure / world / D1 / chart patience / policy on an open."""
    try:
        from hermes_core.engines.structure import analyze_structure, structure_patience_mult

        st = analyze_structure(
            list(prices or []),
            donchian_period=int(pos.get("donchian_period") or 20),
            entry_price=pos.get("entry_price"),
        )
        pos["structure"] = st
        pos["structure_patience_mult"] = structure_patience_mult(st)
        if st.get("failed_auction"):
            pos["structure_event"] = "failed_auction"
    except Exception:  # noqa: BLE001
        pass

    try:
        from hermes_core.engines import btc_regime as br

        if str(pair).upper().startswith("BTC/"):
            brd = br.classify_btc_regime(pair)
            pos["live_d1"] = brd.get("label")
            pos["d1"] = brd.get("label")
    except Exception:  # noqa: BLE001
        pass

    if limit_removal_enabled() or sentient_hold_enabled():
        try:
            from hermes_core.adapters.derivatives_context import (
                fetch_derivatives_context,
                world_patience_mult,
            )

            world = fetch_derivatives_context(pair, bot=bot)
            pos["world"] = world
            pos["world_degraded"] = bool(world.get("world_degraded"))
            pos["world_freshness"] = world.get("world_freshness")
            pos["world_source"] = world.get("world_source")
            wp = world_patience_mult(world, side=str(pos.get("side") or "long"))
            pos["playbook_patience_mult"] = float(pos.get("playbook_patience_mult") or 1.0) * wp
        except Exception:  # noqa: BLE001
            pos["world_degraded"] = True

        try:
            from hermes_core.adapters.event_context import (
                event_patience_mult,
                fetch_event_context,
            )

            ev = fetch_event_context(bot=bot)
            pos["event_risk"] = ev.get("event_risk")
            pos["narrative_tilt"] = ev.get("narrative_tilt")
            pos["event_hard_pause"] = ev.get("hard_pause")
            pos["playbook_patience_mult"] = float(
                pos.get("playbook_patience_mult") or 1.0
            ) * event_patience_mult(ev)
        except Exception:  # noqa: BLE001
            pass

    # hold policy prediction
    try:
        from hermes_core.engines import hold_policy as hp
        from hermes_core.state.paths import bot_state_dir

        pol = hp.load_hold_policy(bot_state_dir(bot) / "hold_policy.json")
        peak = float(pos.get("peak_mfe_pct") or 0.0)
        tp = max(float(pos.get("profit_target_pct") or 1.5), 1e-6)
        unreal = float(pos.get("unrealised_pct") or 0.0)
        bars = int(pos.get("exit_bars_since_peak") or 0)
        feats = {
            "progress": max(0.0, min(1.0, peak / tp)),
            "fresh": 1.0 / (1.0 + bars),
            "capture": (unreal / peak) if peak > 1e-9 else 0.0,
            "funding": (pos.get("world") or {}).get("funding"),
            "oi_z": (pos.get("world") or {}).get("oi_z"),
            "dist_res": (pos.get("structure") or {}).get("dist_to_resistance_pct"),
        }
        pos["hold_policy_p_hold"] = hp.predict_p_hold(feats, pol)
        pos["p_hit_tp"] = hp.predict_p_hit_tp(feats, pol)
        pos["hold_policy_n"] = int(pol.get("n") or 0)
        if pol.get("weights"):
            pos["hold_score_weights"] = pol["weights"]
    except Exception:  # noqa: BLE001
        pass

    # playbook patience
    try:
        from hermes_core.engines.playbooks import playbook_patience

        pb = playbook_patience(
            pair=pair,
            entry_type=str(pos.get("entry_type") or ""),
            d1=str(pos.get("live_d1") or pos.get("d1") or ""),
            bot=bot,
        )
        if pb is not None:
            pos["playbook_patience_mult"] = float(pos.get("playbook_patience_mult") or 1.0) * pb
    except Exception:  # noqa: BLE001
        pass
