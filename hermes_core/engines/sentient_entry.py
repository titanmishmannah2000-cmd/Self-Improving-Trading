"""Layered sentient entries (L0–L7, tight v1).

Conviction reuses exit-side patience mults. Alternate sleeves: pullback + MR
only (no structure sleeve). Opens gated by SENTIENT_ENTRY; soft-reinterpret
of wait_for_pullback is always-on in entry.py.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from hermes_core.env import get_env

HARD_SOFT = frozenset({"avoid", "downtrend"})
ACTIONABLE_SOFT = frozenset({"wait_for_pullback"})
_PRIORITY = {"donchian_breakout": 0, "pullback": 1, "mean_reversion": 2}
_SHADOW_MAX_LINES = 5000
_FROZEN_REFLECT_KEYS = frozenset(
    {
        "entry_conviction_take",
        "entry_conviction_probe",
        "pullback_max_dist_pct",
        "pullback_cooldown_cycles",
        "sleeve_promote_n",
        "max_alt_entries_per_day",
        "breakout_confirm_bars",
        "entry_policy_min_n",
        "pullback_stop_pct",
        "pullback_tp_pct",
        "mr_stop_pct",
        "mr_tp_pct",
        "min_probe_size",
        "shadow_horizon_bars",
    }
)


def sentient_entry_enabled() -> bool:
    return get_env("SENTIENT_ENTRY", "0") == "1"


def is_btc_entry_bot(bot: str | None, pair: str | None = None) -> bool:
    b = (bot or "").strip().lower()
    if b in {"btc", "crypto"}:
        return True
    return bool(pair and str(pair).upper().startswith("BTC/"))


def split_soft_reasons(reasons: list[str] | None) -> tuple[list[str], list[str]]:
    soft = list(reasons or [])
    hard = [r for r in soft if r in HARD_SOFT]
    actionable = [r for r in soft if r in ACTIONABLE_SOFT]
    return hard, actionable


def parse_support_level(context: str | None) -> float | None:
    """Parse 'support at X' from chart vision text. Fail-open → None."""
    c = context or ""
    m = re.search(r"support\s+at\s+([0-9]+(?:\.[0-9]+)?)", c, re.I)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def _state_paths(bot: str | None) -> tuple[Path, Path, Path]:
    from hermes_core.state.paths import bot_state_dir

    d = bot_state_dir(bot)
    return d / "entry_runtime.json", d / "entry_policy.json", d / "entry_shadow.jsonl"


# Bump when runtime semantics change so poisoned volume state self-heals once.
# 3: clear alt_entries_today after winners burned the v07 cap of 2.
_ENTRY_RUNTIME_SCHEMA = 3


def load_entry_runtime(bot: str | None) -> dict:
    path, _, _ = _state_paths(bot)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                if int(data.get("schema") or 0) < _ENTRY_RUNTIME_SCHEMA:
                    # Pre-fix alt quota could burn slots without opens; clear once.
                    data["alt_entries_today"] = 0
                    data["schema"] = _ENTRY_RUNTIME_SCHEMA
                    save_entry_runtime(bot, data)
                return data
    except Exception:  # noqa: BLE001
        pass
    return {
        "pairs": {},
        "day": "",
        "alt_entries_today": 0,
        "schema": _ENTRY_RUNTIME_SCHEMA,
    }


def save_entry_runtime(bot: str | None, data: dict) -> None:
    path, _, _ = _state_paths(bot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def bump_runtime_cycle(
    bot: str | None,
    pair: str,
    *,
    had_breakout_candidate: bool,
    current_cycle: int,
) -> dict:
    """Persist cycles_since_breakout / day caps across restarts."""
    rt = load_entry_runtime(bot)
    day = _today()
    if str(rt.get("day") or "") != day:
        rt["day"] = day
        rt["alt_entries_today"] = 0
    pairs = rt.setdefault("pairs", {})
    st = pairs.setdefault(pair, {})
    if had_breakout_candidate:
        st["cycles_since_breakout"] = 0
    else:
        st["cycles_since_breakout"] = int(st.get("cycles_since_breakout") or 0) + 1
    st["last_cycle"] = int(current_cycle)
    pairs[pair] = st
    rt["pairs"] = pairs
    save_entry_runtime(bot, rt)
    return st


def release_alt_quota_on_green(
    bot: str | None,
    *,
    entry_type: str,
    pnl: float,
) -> None:
    """Free one daily alt slot when a pullback/MR close is net-green.

    Quota exists to cap losing spam, not to lock the bot after winners while
    D1 chop still blocks Donchian.
    """
    if str(entry_type or "").strip().lower() not in {"pullback", "mean_reversion"}:
        return
    if float(pnl) <= 0:
        return
    rt = load_entry_runtime(bot)
    cur = int(rt.get("alt_entries_today") or 0)
    if cur <= 0:
        return
    rt["alt_entries_today"] = cur - 1
    save_entry_runtime(bot, rt)


def note_failed_breakout_cooldown(
    bot: str | None,
    pair: str,
    *,
    entry_type: str,
    current_cycle: int,
    strategy: dict | None = None,
) -> None:
    """Latch re-entry cooldown after a failed_breakout exit (stops 30m grind)."""
    cd = int((strategy or {}).get("failed_breakout_cooldown_cycles") or 60)
    if cd <= 0:
        return
    rt = load_entry_runtime(bot)
    pairs = rt.setdefault("pairs", {})
    pst = pairs.setdefault(pair, {})
    pst["fb_cooldown_until"] = int(current_cycle) + cd
    pst["fb_cooldown_sleeve"] = str(entry_type or "donchian_breakout")
    pairs[pair] = pst
    rt["pairs"] = pairs
    save_entry_runtime(bot, rt)


def failed_breakout_cooldown_active(
    bot: str | None,
    pair: str,
    *,
    current_cycle: int,
    entry_type: str | None = None,
) -> bool:
    rt = load_entry_runtime(bot)
    pst = (rt.get("pairs") or {}).get(pair) or {}
    until = int(pst.get("fb_cooldown_until") or 0)
    if until <= 0 or int(current_cycle) >= until:
        return False
    sleeve = str(pst.get("fb_cooldown_sleeve") or "donchian_breakout").strip().lower()
    et = str(entry_type or "donchian_breakout").strip().lower()
    donchian = {"donchian_breakout", "donchian", "breakout", ""}
    if sleeve in donchian:
        return et in donchian
    return et == sleeve


def load_entry_policy(bot: str | None) -> dict:
    _, path, _ = _state_paths(bot)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {"n": 0, "weights": [0.4, 0.35, 0.25], "bias": 0.0}


def save_entry_policy(bot: str | None, data: dict) -> None:
    _, path, _ = _state_paths(bot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def predict_policy_mult(features: dict, policy: dict, *, min_n: int = 20) -> float:
    """Haircut/boost only when policy has enough samples; else 1.0 (fail-open)."""
    n = int(policy.get("n") or 0)
    if n < min_n:
        return 1.0
    w = policy.get("weights") or [0.4, 0.35, 0.25]
    if len(w) != 3:
        w = [0.4, 0.35, 0.25]
    conv = float(features.get("conviction_raw") or 0.5)
    play = float(features.get("playbook_wr") or 0.5)
    world = float(features.get("world_mult") or 1.0)
    score = w[0] * conv + w[1] * play + w[2] * min(1.0, world)
    bias = float(policy.get("bias") or 0.0)
    # Map score to mult around 1.0
    return max(0.7, min(1.15, 0.85 + 0.3 * score + bias))


def update_entry_policy_on_label(
    bot: str | None,
    *,
    conviction: float,
    playbook_wr: float,
    world_mult: float,
    y_take: float,
) -> None:
    pol = load_entry_policy(bot)
    w = list(pol.get("weights") or [0.4, 0.35, 0.25])
    if len(w) != 3:
        w = [0.4, 0.35, 0.25]
    feats = [conviction, playbook_wr, min(1.0, world_mult)]
    target = feats if y_take >= 0.5 else [1.0 - f for f in feats]
    lr = 0.05
    out = []
    for wi, ti in zip(w, target):
        out.append(max(0.1, min(0.6, wi * (1 - lr) + ti * lr)))
    s = sum(out) or 1.0
    pol["weights"] = [x / s for x in out]
    pol["n"] = int(pol.get("n") or 0) + 1
    save_entry_policy(bot, pol)


def append_entry_shadow(bot: str | None, row: dict) -> None:
    _, _, path = _state_paths(bot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
        _rotate_shadow(path)
    except Exception:  # noqa: BLE001
        pass


def _rotate_shadow(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= _SHADOW_MAX_LINES:
            return
        path.write_text("\n".join(lines[-_SHADOW_MAX_LINES:]) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def resolve_pending_shadows(
    bot: str | None,
    pair: str,
    prices: list[float] | None,
    *,
    horizon_bars: int = 8,
    cost_rt_pct: float = 0.22,
) -> int:
    """Label pending shadows once enough forward bars exist. Returns n labeled."""
    _, _, path = _state_paths(bot)
    if not path.exists() or not prices or len(prices) < 2:
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return 0
    marked = 0
    out_lines: list[str] = []
    px = [float(x) for x in prices]
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:  # noqa: BLE001
            out_lines.append(line)
            continue
        if row.get("labeled") or str(row.get("pair") or "") != pair:
            out_lines.append(line)
            continue
        entry_i = int(row.get("price_index") or -1)
        if entry_i < 0 or entry_i >= len(px):
            out_lines.append(json.dumps(row, default=str))
            continue
        end = entry_i + max(1, horizon_bars)
        if end >= len(px):
            out_lines.append(json.dumps(row, default=str))
            continue
        entry_px = float(row.get("mark") or px[entry_i])
        window = px[entry_i : end + 1]
        if not window or entry_px <= 0:
            out_lines.append(json.dumps(row, default=str))
            continue
        mfe = (max(window) - entry_px) / entry_px * 100.0
        mae = (min(window) - entry_px) / entry_px * 100.0
        net = mfe - float(cost_rt_pct)
        row["labeled"] = True
        row["mfe_pct"] = round(mfe, 4)
        row["mae_pct"] = round(mae, 4)
        row["net_vs_cost"] = round(net, 4)
        row["y_take"] = 1.0 if net > 0 else 0.0
        marked += 1
        update_entry_policy_on_label(
            bot,
            conviction=float((row.get("features") or {}).get("conviction_raw") or 0.5),
            playbook_wr=float((row.get("features") or {}).get("playbook_wr") or 0.5),
            world_mult=float((row.get("features") or {}).get("world_mult") or 1.0),
            y_take=float(row["y_take"]),
        )
        out_lines.append(json.dumps(row, default=str))
    try:
        path.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return marked


def credit_entry_on_close(
    bot: str | None,
    *,
    entry_type: str,
    conviction: float | None,
    pnl: float,
    playbook_wr: float = 0.5,
    world_mult: float = 1.0,
) -> None:
    if conviction is None:
        return
    update_entry_policy_on_label(
        bot,
        conviction=float(conviction),
        playbook_wr=playbook_wr,
        world_mult=world_mult,
        y_take=1.0 if pnl > 0 else 0.0,
    )


def sleeve_risk_overlays(strategy: dict, entry_type: str) -> dict:
    """Return stop/tp to stamp on position (never mutate validated strategy)."""
    et = (entry_type or "").strip().lower()
    out: dict[str, float] = {}
    try:
        if et == "pullback":
            if strategy.get("pullback_stop_pct") is not None:
                out["stop_loss_pct"] = float(strategy["pullback_stop_pct"])
            if strategy.get("pullback_tp_pct") is not None:
                out["profit_target_pct"] = float(strategy["pullback_tp_pct"])
        elif et == "mean_reversion":
            if strategy.get("mr_stop_pct") is not None:
                out["stop_loss_pct"] = float(strategy["mr_stop_pct"])
            if strategy.get("mr_tp_pct") is not None:
                out["profit_target_pct"] = float(strategy["mr_tp_pct"])
    except (TypeError, ValueError):
        return {}
    return out


def frozen_reflect_keys() -> frozenset[str]:
    return _FROZEN_REFLECT_KEYS


def _playbook_stats(bot: str | None, pair: str, entry_type: str, d1: str) -> dict:
    try:
        from hermes_core.engines.playbooks import load_playbooks, setup_key

        books = load_playbooks(bot)
        return books.get(setup_key(pair, entry_type, d1)) or {}
    except Exception:  # noqa: BLE001
        return {}


def _sleeve_promoted(st: dict, promote_n: int) -> bool:
    return int(st.get("n") or 0) >= promote_n and float(st.get("wr") or 0) >= 0.55


def compute_entry_conviction(
    *,
    quality: float,
    world_mult: float = 1.0,
    structure_mult: float = 1.0,
    playbook_mult: float = 1.0,
    chart_alignment: float = 1.0,
    cost_edge: float = 1.0,
    idle_boost: float = 1.0,
) -> float:
    """Blend reused exit-side mults with signal quality (0..1)."""
    mult = (
        max(0.4, min(1.5, world_mult))
        * max(0.4, min(1.5, structure_mult))
        * max(0.4, min(1.5, playbook_mult))
        * max(0.5, min(1.2, chart_alignment))
        * max(0.5, min(1.2, cost_edge))
        * max(0.8, min(1.2, idle_boost))
    )
    # Center mult=1 → quality; mult>1 lifts toward 1
    base = max(0.0, min(1.0, float(quality)))
    return max(0.0, min(1.0, base * (0.65 + 0.35 * mult)))


def resistance_chase(
    price: float,
    *,
    support: float | None,
    resistance: float | None,
) -> bool:
    """True when price is hugging / above resistance (chase, not pullback).

    Absolute %-to-resistance is a poor guard on tight SR bands (~1%): mid-range
    can sit 0.3% under resistance and flip every few ticks. Prefer position in
    the support→resistance span (upper quartile = chase). Fall back to a tight
    absolute band only when support is missing.
    """
    if price <= 0 or resistance is None:
        return False
    try:
        res = float(resistance)
        if res <= 0:
            return False
        if price >= res:
            return True
        if support is not None and float(support) > 0 and res > float(support):
            span = res - float(support)
            if span <= 0:
                return False
            return (price - float(support)) / span >= 0.75
        return abs(price - res) / res * 100.0 <= 0.35
    except (TypeError, ValueError):
        return False


def near_support(
    price: float,
    *,
    support: float | None,
    donchian_mid: float | None,
    max_dist_pct: float,
    resistance: float | None = None,
) -> bool:
    """True when price is in a pullback zone (near support/mid), not chasing resistance."""
    if price <= 0 or max_dist_pct <= 0:
        return False
    if resistance_chase(price, support=support, resistance=resistance):
        return False
    targets = [t for t in (support, donchian_mid) if t is not None and t > 0]
    for t in targets:
        dist = abs(price - t) / t * 100.0
        if dist <= max_dist_pct:
            return True
    # Value zone: between support and mid (inclusive), even if mid gap > max_dist.
    try:
        if (
            support is not None
            and donchian_mid is not None
            and float(support) > 0
            and float(donchian_mid) > float(support)
            and float(support) * 0.998 <= price <= float(donchian_mid)
        ):
            return True
    except (TypeError, ValueError):
        pass
    return False


def parse_resistance_level(context: str | None) -> float | None:
    c = context or ""
    m = re.search(r"resistance\s+at\s+([0-9]+(?:\.[0-9]+)?)", c, re.I)
    if not m:
        return None
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return None


def build_context_bundle(
    *,
    bot: str | None,
    pair: str,
    prices: list[float],
    strategy: dict,
    context: str = "",
) -> dict:
    """Fetch world/structure/playbook/cost features once per cycle (fail-open)."""
    bundle: dict[str, Any] = {
        "world": {},
        "world_mult": 1.0,
        "structure": {},
        "structure_mult": 1.0,
        "chart_missing": not bool((context or "").strip()),
        "support": parse_support_level(context),
        "resistance": parse_resistance_level(context),
        "event_hard_pause": False,
        "cost_rt": 0.22,
        "cost_stressed": 0.44,
    }
    try:
        from hermes_core.adapters.derivatives_context import (
            fetch_derivatives_context,
            world_patience_mult,
        )

        world = fetch_derivatives_context(pair, bot=bot)
        bundle["world"] = world
        bundle["world_mult"] = world_patience_mult(world, side="long")
    except Exception:  # noqa: BLE001
        bundle["world_mult"] = 0.85
        bundle["world"] = {"world_degraded": True}

    try:
        from hermes_core.engines.structure import analyze_structure, structure_patience_mult

        period = int(
            (strategy.get("entry") or {}).get("donchian_period")
            or strategy.get("donchian_period")
            or 20
        )
        st = analyze_structure(list(prices or []), donchian_period=period)
        bundle["structure"] = st
        bundle["structure_mult"] = structure_patience_mult(st)
        if st.get("donchian_upper") is not None and st.get("donchian_lower") is not None:
            bundle["donchian_mid"] = (
                float(st["donchian_upper"]) + float(st["donchian_lower"])
            ) / 2.0
    except Exception:  # noqa: BLE001
        pass

    try:
        from hermes_core.adapters.event_context import fetch_event_context

        if sentient_entry_enabled() or get_env("BTC_EVENT_PAUSE", "0") == "1":
            ev = fetch_event_context(bot=bot)
            bundle["event_hard_pause"] = bool(ev.get("hard_pause"))
    except Exception:  # noqa: BLE001
        bundle["event_hard_pause"] = False

    try:
        from hermes_core.engines.cost_model import estimate

        last = float(prices[-1]) if prices else 0.0
        atr_pct = None
        cm = estimate(pair, atr_pct=atr_pct)
        bundle["cost_rt"] = float(cm.round_trip_pct)
        bundle["cost_stressed"] = float(cm.stressed_round_trip_pct)
    except Exception:  # noqa: BLE001
        pass

    return bundle


def _cost_edge(tp_pct: float, stressed_rt: float) -> float:
    """<1 when stressed costs eat the target."""
    if tp_pct <= 0:
        return 0.5
    ratio = (tp_pct - stressed_rt) / max(tp_pct, 1e-6)
    return max(0.5, min(1.2, 0.7 + 0.5 * ratio))


def try_pullback_candidate(
    *,
    prices: list[float],
    strategy: dict,
    pair: str,
    context: str,
    d1: str,
    bot: str | None,
    bundle: dict,
    runtime_pair: dict,
    soft_actionable: list[str],
) -> dict | None:
    if not sentient_entry_enabled():
        return None
    if not soft_actionable and bundle.get("chart_missing"):
        return None
    if "wait_for_pullback" not in soft_actionable and not soft_actionable:
        # Allow pullback when vision explicitly waited, or when near support in trend
        pass
    from hermes_core.engines import btc_regime as br

    # Pullback eligibility:
    # - trend_down: never
    # - trend_up: when chart asks wait_for_pullback
    # - chop: only when chart asks wait_for_pullback (probe) — otherwise zero
    #   trades while Donchian waits for breakout + ADX was also locking
    d1_lab = str(d1 or "").strip().lower()
    if d1_lab == br.TREND_DOWN:
        return None
    if d1_lab not in {br.TREND_UP, br.CHOP}:
        return None
    if bundle.get("chart_missing") and "wait_for_pullback" not in soft_actionable:
        return None
    if "wait_for_pullback" not in soft_actionable:
        return None

    cooldown_until = int(runtime_pair.get("pullback_cooldown_until") or 0)
    cur = int(runtime_pair.get("last_cycle") or 0)
    if cooldown_until and cur < cooldown_until:
        return None

    entry_cfg = strategy.get("entry") if isinstance(strategy.get("entry"), dict) else {}
    max_dist = float(
        strategy.get("pullback_max_dist_pct")
        or entry_cfg.get("pullback_max_dist_pct")
        or 2.0
    )
    price = float(prices[-1])
    if not near_support(
        price,
        support=bundle.get("support"),
        donchian_mid=bundle.get("donchian_mid"),
        max_dist_pct=max_dist,
        resistance=bundle.get("resistance"),
    ):
        return None

    pb = _playbook_stats(bot, pair, "pullback", d1)
    try:
        from hermes_core.engines.playbooks import playbook_patience

        pb_mult = playbook_patience(pair=pair, entry_type="pullback", d1=d1, bot=bot) or 1.0
    except Exception:  # noqa: BLE001
        pb_mult = 1.0

    tp = float(strategy.get("pullback_tp_pct") or strategy.get("profit_target_pct") or 1.0)
    quality = 0.52
    conv = compute_entry_conviction(
        quality=quality,
        world_mult=float(bundle.get("world_mult") or 1.0),
        structure_mult=float(bundle.get("structure_mult") or 1.0),
        playbook_mult=float(pb_mult),
        chart_alignment=1.1 if "wait_for_pullback" in soft_actionable else 1.0,
        cost_edge=_cost_edge(tp, float(bundle.get("cost_stressed") or 0.44)),
    )
    return {
        "entry_type": "pullback",
        "strategy_type": "donchian_breakout",  # schema-safe; identity via entry_type
        "quality": quality,
        "conviction": conv,
        "size_mult": 0.5,
        "features": {
            "conviction_raw": conv,
            "playbook_wr": float(pb.get("wr") or 0.5),
            "world_mult": float(bundle.get("world_mult") or 1.0),
            "support": bundle.get("support"),
        },
        "playbook": pb,
        "source": "sentient_pullback",
    }


def try_mr_candidate(
    *,
    prices: list[float],
    strategy: dict,
    pair: str,
    context: str,
    d1: str,
    bot: str | None,
    bundle: dict,
    runtime_pair: dict,
    ensemble_consensus: str = "neutral",
    vol_above: bool = False,
    reentry: dict | None = None,
    current_cycle: int = 0,
    session_token: str = "LDN",
    regime: str | None = None,
) -> dict | None:
    if not sentient_entry_enabled():
        return None
    from hermes_core.engines import btc_regime as br

    if br.hard_blocks_entry(d1, strategy_type="mean_reversion"):
        return None
    idle_need = int(strategy.get("idle_sleeve_cycles") or 60)
    cycles_quiet = int(runtime_pair.get("cycles_since_breakout") or 0)
    if cycles_quiet < idle_need:
        return None

    from hermes_core.engines.entry import evaluate_entry_detailed

    alt = dict(strategy)
    alt["strategy_type"] = "mean_reversion"
    alt["position_size_r"] = float(strategy.get("position_size_r") or 0.15) * 0.5
    # Avoid clean-chart / donchian path
    entry_cfg = dict(alt.get("entry") or {})
    entry_cfg["require_clean_chart"] = False
    alt["entry"] = entry_cfg
    sig, skip = evaluate_entry_detailed(
        prices,
        alt,
        pair=pair,
        context=context,
        ensemble_consensus=ensemble_consensus,
        vol_above=vol_above,
        reentry=reentry,
        current_cycle=current_cycle,
        session_token=session_token,
        regime=regime,
        bot=bot,
    )
    if sig is None:
        return None
    pb = _playbook_stats(bot, pair, "mean_reversion", d1)
    try:
        from hermes_core.engines.playbooks import playbook_patience

        pb_mult = (
            playbook_patience(pair=pair, entry_type="mean_reversion", d1=d1, bot=bot)
            or 1.0
        )
    except Exception:  # noqa: BLE001
        pb_mult = 1.0
    tp = float(strategy.get("mr_tp_pct") or strategy.get("profit_target_pct") or 1.5)
    idle_boost = 1.0 + min(0.15, cycles_quiet / max(idle_need * 4, 1))
    conv = compute_entry_conviction(
        quality=float(sig.quality),
        world_mult=float(bundle.get("world_mult") or 1.0),
        structure_mult=float(bundle.get("structure_mult") or 1.0),
        playbook_mult=float(pb_mult),
        chart_alignment=1.0,
        cost_edge=_cost_edge(tp, float(bundle.get("cost_stressed") or 0.44)),
        idle_boost=idle_boost,
    )
    return {
        "entry_type": "mean_reversion",
        "strategy_type": "mean_reversion",
        "quality": float(sig.quality),
        "conviction": conv,
        "size_mult": 0.5,
        "signal": sig,
        "features": {
            "conviction_raw": conv,
            "playbook_wr": float(pb.get("wr") or 0.5),
            "world_mult": float(bundle.get("world_mult") or 1.0),
            "cycles_since_breakout": cycles_quiet,
        },
        "playbook": pb,
        "source": "sentient_mr",
    }


def donchian_candidate_from_signal(
    sig,
    *,
    bot: str | None,
    pair: str,
    d1: str,
    bundle: dict,
    strategy: dict,
) -> dict:
    pb = _playbook_stats(bot, pair, "donchian_breakout", d1)
    try:
        from hermes_core.engines.playbooks import playbook_patience

        pb_mult = (
            playbook_patience(pair=pair, entry_type="donchian_breakout", d1=d1, bot=bot)
            or 1.0
        )
    except Exception:  # noqa: BLE001
        pb_mult = 1.0
    tp = float(strategy.get("profit_target_pct") or 1.5)
    conv = compute_entry_conviction(
        quality=float(sig.quality),
        world_mult=float(bundle.get("world_mult") or 1.0),
        structure_mult=float(bundle.get("structure_mult") or 1.0),
        playbook_mult=float(pb_mult),
        chart_alignment=1.0,
        cost_edge=_cost_edge(tp, float(bundle.get("cost_stressed") or 0.44)),
    )
    return {
        "entry_type": "donchian_breakout",
        "strategy_type": "donchian_breakout",
        "quality": float(sig.quality),
        "conviction": conv,
        "size_mult": 1.0,
        "signal": sig,
        "features": {
            "conviction_raw": conv,
            "playbook_wr": float(pb.get("wr") or 0.5),
            "world_mult": float(bundle.get("world_mult") or 1.0),
        },
        "playbook": pb,
        "source": "donchian",
    }


def meta_decision(
    cand: dict,
    strategy: dict,
    policy: dict,
) -> str:
    """Return take | skip | probe."""
    take_th = float(strategy.get("entry_conviction_take") or 0.55)
    probe_th = float(strategy.get("entry_conviction_probe") or 0.40)
    min_n = int(strategy.get("entry_policy_min_n") or 20)
    conv = float(cand.get("conviction") or 0.0)
    mult = predict_policy_mult(cand.get("features") or {}, policy, min_n=min_n)
    conv_adj = max(0.0, min(1.0, conv * mult))
    cand["conviction_adj"] = conv_adj
    et = str(cand.get("entry_type") or "")
    promote_n = int(strategy.get("sleeve_promote_n") or 8)
    stressed = False
    # cost: if cost_edge was weak, conviction already haircut — hard skip if below probe
    if conv_adj < probe_th:
        return "skip"
    if et in {"pullback", "mean_reversion"}:
        if not _sleeve_promoted(cand.get("playbook") or {}, promote_n):
            return "probe"
        if conv_adj >= take_th:
            return "take"
        return "probe"
    # Donchian: never fully skip a raw breakout that already passed entry.py —
    # worst case probe-size when conviction is soft.
    if et == "donchian_breakout":
        if conv_adj >= take_th:
            return "take"
        return "probe"
    if conv_adj >= take_th:
        return "take"
    if conv_adj >= probe_th:
        return "probe"
    return "skip"


def arbitrate_entry(candidates: list[dict], strategy: dict, policy: dict) -> dict | None:
    """Pick one winner with decision, or None."""
    scored: list[dict] = []
    for c in candidates:
        if not c:
            continue
        dec = meta_decision(c, strategy, policy)
        if dec == "skip":
            continue
        cc = dict(c)
        cc["decision"] = dec
        scored.append(cc)
    if not scored:
        return None
    scored.sort(
        key=lambda x: (
            -float(x.get("conviction_adj") or x.get("conviction") or 0),
            _PRIORITY.get(str(x.get("entry_type") or ""), 9),
        )
    )
    return scored[0]


def build_pullback_signal(cand: dict, strategy: dict, pair: str):
    from hermes_core.engines.entry import Signal

    size = float(strategy.get("position_size_r") or 0.15) * float(cand.get("size_mult") or 0.5)
    return Signal(
        "donchian_breakout",
        round(float(cand.get("quality") or 0.5), 4),
        size,
        pair,
        {
            "entry_type": "pullback",
            "entry_sleeve": "pullback",
            "sentient_entry": True,
            "conviction": cand.get("conviction_adj") or cand.get("conviction"),
            "entry_decision": cand.get("decision"),
            "source": cand.get("source"),
        },
    )


def apply_winner_to_signal(winner: dict, strategy: dict, pair: str):
    """Return (signal, decision) from arbiter winner."""
    et = str(winner.get("entry_type") or "")
    if et == "pullback":
        sig = build_pullback_signal(winner, strategy, pair)
    else:
        sig = winner.get("signal")
        if sig is None:
            return None, "skip"
        sig.meta = dict(sig.meta or {})
        sig.meta["entry_type"] = et
        if et == "mean_reversion":
            sig.meta["entry_sleeve"] = "mean_reversion"
            sig.meta["idle_sleeve"] = True  # compat
        sig.meta["sentient_entry"] = True
        sig.meta["conviction"] = winner.get("conviction_adj") or winner.get("conviction")
        sig.meta["entry_decision"] = winner.get("decision")
        if winner.get("decision") == "probe":
            try:
                sig.size = float(sig.size) * float(winner.get("size_mult") or 0.5)
            except (TypeError, ValueError):
                pass
    return sig, str(winner.get("decision") or "skip")


def run_sentient_entry(
    *,
    bot: str | None,
    pair: str,
    prices: list[float],
    strategy: dict,
    context: str = "",
    trad_sig=None,
    trad_skip: str = "",
    current_cycle: int = 0,
    ensemble_consensus: str = "neutral",
    vol_above: bool = False,
    reentry: dict | None = None,
    session_token: str = "LDN",
    regime: str | None = None,
) -> dict:
    """Main collector. Returns decision payload for the loop.

    Always-on pieces: conviction logging for Donchian when present.
    Alt opens require SENTIENT_ENTRY.
    """
    out: dict[str, Any] = {
        "signal": None,
        "skip": trad_skip or "",
        "decision": None,
        "conviction": None,
        "candidates": [],
        "blocked_by_regime": False,
        "event_pause": False,
        "world": None,
        "observe_only": not sentient_entry_enabled(),
    }
    if not is_btc_entry_bot(bot, pair):
        out["signal"] = trad_sig
        out["skip"] = "" if trad_sig is not None else (trad_skip or "no_signal")
        return out

    from hermes_core.engines import btc_regime as br
    from hermes_core.engines.chart_vision import chart_soft_reasons

    try:
        brd = br.classify_btc_regime(pair) if pair else {}
    except Exception:  # noqa: BLE001
        brd = {}
    d1 = str(brd.get("label") or "")

    soft = chart_soft_reasons(context, strategy_type="donchian_breakout")
    hard_soft, actionable = split_soft_reasons(soft)

    # Drop Donchian traditional signal during post-FB cooldown.
    if trad_sig is not None and failed_breakout_cooldown_active(
        bot,
        pair,
        current_cycle=int(current_cycle or 0),
        entry_type="donchian_breakout",
    ):
        trad_sig = None
        trad_skip = trad_skip or "sentient:fb_cooldown"

    bundle = build_context_bundle(
        bot=bot, pair=pair, prices=prices, strategy=strategy, context=context
    )
    out["world"] = bundle.get("world")
    if bundle.get("event_hard_pause"):
        out["event_pause"] = True
        out["skip"] = "event:hard_pause"
        out["signal"] = None
        return out

    had_breakout = trad_sig is not None and str(
        (getattr(trad_sig, "meta", None) or {}).get("entry_type") or ""
    ) == "donchian_breakout"
    # Also treat confirm_pending / no_breakout as quiet
    quiet = trad_sig is None and (
        str(trad_skip or "").startswith("donchian:no_breakout")
        or str(trad_skip or "").startswith("donchian:confirm")
        or not trad_skip
    )
    runtime_pair = bump_runtime_cycle(
        bot,
        pair,
        had_breakout_candidate=bool(had_breakout),
        current_cycle=current_cycle,
    )

    # Resolve forward shadows opportunistically
    try:
        resolve_pending_shadows(
            bot,
            pair,
            prices,
            horizon_bars=int(strategy.get("shadow_horizon_bars") or 8),
            cost_rt_pct=float(bundle.get("cost_rt") or 0.22),
        )
    except Exception:  # noqa: BLE001
        pass

    candidates: list[dict] = []
    alt_quota_blocked = False
    if trad_sig is not None:
        candidates.append(
            donchian_candidate_from_signal(
                trad_sig, bot=bot, pair=pair, d1=d1, bundle=bundle, strategy=strategy
            )
        )

    if sentient_entry_enabled() and not hard_soft:
        rt = load_entry_runtime(bot)
        max_alt = int(strategy.get("max_alt_entries_per_day") or 6)
        alt_today = int(rt.get("alt_entries_today") or 0)
        if alt_today >= max_alt:
            alt_quota_blocked = True
        else:
            pull = try_pullback_candidate(
                prices=prices,
                strategy=strategy,
                pair=pair,
                context=context,
                d1=d1,
                bot=bot,
                bundle=bundle,
                runtime_pair=runtime_pair,
                soft_actionable=actionable,
            )
            if pull:
                candidates.append(pull)
            if quiet or trad_sig is None:
                mr = try_mr_candidate(
                    prices=prices,
                    strategy=strategy,
                    pair=pair,
                    context=context,
                    d1=d1,
                    bot=bot,
                    bundle=bundle,
                    runtime_pair=runtime_pair,
                    ensemble_consensus=ensemble_consensus,
                    vol_above=vol_above,
                    reentry=reentry,
                    current_cycle=current_cycle,
                    session_token=session_token,
                    regime=regime,
                )
                if mr:
                    candidates.append(mr)

    out["candidates"] = [
        {"entry_type": c.get("entry_type"), "conviction": c.get("conviction")}
        for c in candidates
    ]
    policy = load_entry_policy(bot)

    if not sentient_entry_enabled():
        # Observe-only: keep traditional signal, stamp conviction if present
        out["signal"] = trad_sig
        if trad_sig is not None and candidates:
            out["conviction"] = candidates[0].get("conviction")
            out["decision"] = "observe"
            trad_sig.meta = dict(trad_sig.meta or {})
            trad_sig.meta["conviction"] = out["conviction"]
        out["skip"] = "" if trad_sig is not None else (trad_skip or "no_signal")
        return out

    winner = arbitrate_entry(candidates, strategy, policy)
    if winner is None:
        # Log best skipped candidate for shadow learning
        if candidates:
            best = max(candidates, key=lambda c: float(c.get("conviction") or 0))
            append_entry_shadow(
                bot,
                {
                    "ts": time.time(),
                    "pair": pair,
                    "cycle": current_cycle,
                    "entry_type": best.get("entry_type"),
                    "mark": float(prices[-1]) if prices else None,
                    "price_index": len(prices) - 1 if prices else -1,
                    "features": best.get("features") or {},
                    "reason": "meta_skip",
                    "labeled": False,
                },
            )
            # pullback cooldown on spam
            if best.get("entry_type") == "pullback":
                cd = int(strategy.get("pullback_cooldown_cycles") or 30)
                rt = load_entry_runtime(bot)
                pst = (rt.get("pairs") or {}).setdefault(pair, {})
                pst["pullback_cooldown_until"] = current_cycle + cd
                rt["pairs"][pair] = pst
                save_entry_runtime(bot, rt)
        out["signal"] = None
        if candidates:
            out["skip"] = trad_skip or "sentient:no_winner"
        elif alt_quota_blocked and "wait_for_pullback" in actionable:
            out["skip"] = "sentient:alt_quota"
        elif "wait_for_pullback" in actionable and prices:
            # Explain empty pullback: chase vs not near support/mid.
            price = float(prices[-1])
            if resistance_chase(
                price,
                support=bundle.get("support"),
                resistance=bundle.get("resistance"),
            ):
                out["skip"] = "sentient:resistance_chase"
            elif not near_support(
                price,
                support=bundle.get("support"),
                donchian_mid=bundle.get("donchian_mid"),
                max_dist_pct=float(strategy.get("pullback_max_dist_pct") or 2.0),
                resistance=bundle.get("resistance"),
            ):
                out["skip"] = "sentient:pullback_not_in_zone"
            else:
                out["skip"] = trad_skip or "sentient:no_winner"
        else:
            out["skip"] = trad_skip or "sentient:no_winner"
        out["blocked_by_regime"] = d1 in {br.CHOP, br.TREND_DOWN} and not had_breakout
        return out

    sig, decision = apply_winner_to_signal(winner, strategy, pair)
    out["signal"] = sig
    out["decision"] = decision
    out["conviction"] = winner.get("conviction_adj") or winner.get("conviction")
    out["skip"] = ""
    if sig is not None:
        sig.meta = dict(sig.meta or {})
        sig.meta["world"] = bundle.get("world")
        overlays = sleeve_risk_overlays(strategy, str(winner.get("entry_type") or ""))
        if overlays:
            sig.meta["sleeve_risk"] = overlays
        # Shadow-log would-be until promoted (do NOT burn daily alt quota here —
        # quota increments only after the position actually opens in loop).
        if (
            str(winner.get("entry_type") or "") in {"pullback", "mean_reversion"}
            and decision == "probe"
            and not _sleeve_promoted(
                winner.get("playbook") or {}, int(strategy.get("sleeve_promote_n") or 8)
            )
        ):
            append_entry_shadow(
                bot,
                {
                    "ts": time.time(),
                    "pair": pair,
                    "cycle": current_cycle,
                    "entry_type": winner.get("entry_type"),
                    "mark": float(prices[-1]) if prices else None,
                    "price_index": len(prices) - 1 if prices else -1,
                    "features": winner.get("features") or {},
                    "reason": "probe_open",
                    "labeled": False,
                },
            )
    return out
