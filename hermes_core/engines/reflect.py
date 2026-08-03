"""Reflection engine (Session 9 / Phase 9) — Layer 1 rule-based self-review.

L1 is deterministic arithmetic over a batch of closed trades (NO LLM, NO network):
given recent trades for a pair and its strategy, it proposes exactly ONE parameter
change driven by drawdown / win-rate, or returns None.

Decision tree (blueprint Section 7 Engine 2 / line 544, line 730, L45):
  * DD > max_dd            -> tighten stop_loss_pct by -0.3 (floor 0.5)  [guard L45]
  * WR < 0.3               -> widen  stop_loss_pct (raise it)            [guard L45]
  * return < -0.5 & >=8t & >=10t -> widen stop_loss_pct (raise it)
  * one_variable_only:     at most ONE change per batch.
  * confidence gate:       self-assigned 0.40 (fixed for the pure rule tree).

Discipline (S9 contract, roadmap 8.1-8.2):
  * layer1_rule_based NEVER mutates the live strategy; combined_reflect only
    LOGS a proposal to state/hypotheses.jsonl.
  * Live deploy is gated by run_reflection_pipeline: L2 (when score>=65) then
    backtest_with_history; on approve, apply_strategy_change writes YAML +
    version. Set REFLECT_AUTO_DEPLOY=0 to stop at approved_pending_deploy.
  * Every proposal is reconstructable: hypotheses.jsonl records
    pair, variable, old -> new, reason, confidence, and the trade stats that
    produced it.

Functions (blueprint Phase 9 build target + live latch):
  layer1_rule_based(pair, trades, goal, strategy) -> tuple | None
  combined_reflect(pair, trades, goal, chart_context="", ...) -> list[dict]
  maybe_reflect_pair / run_reflection_pipeline / apply_strategy_change
  _is_reflection_done / _mark_reflection_done
"""

from __future__ import annotations

import contextlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from hermes_core.config import load_config, load_strategy_for_pair
from hermes_core.state.paths import hypotheses_path, reflection_latch_path

STOP_FLOOR = 0.5  # [GUARD L45] stop_loss_pct never goes below this
STOP_TIGHTEN = 0.3  # DD breach -> tighten by this much
STOP_WIDEN = 0.3  # low-WR / sustained-loss -> widen by this much
CONFIDENCE = 0.40  # L1 fixed confidence gate (legacy floor / fallback)

# ── Phase 2 multi-axis tuning knobs ─────────────────────────────────────────
# Bounds mirror hermes_core/config/schema.STRATEGY_PARAM_RANGES so a proposed
# value always survives validate_strategy_params.
STOP_CAP = 8.0  # widen ceiling (schema allows up to 10.0; stay conservative)
TARGET_STEP = 0.5  # profit_target_pct step
TARGET_FLOOR = 0.8  # never take profit-target below this
TRAIL_STEP = 0.4  # trailing_stop_pct step (0.0 -> capture giveback)
TRAIL_CAP = 3.0
ENTRY_THRESH_STEP = 5  # RSI entry threshold step (be more selective)
ENTRY_THRESH_CAP = 45  # do not over-tighten MR oversold entries
MAXHOLD_CUT = 0.75  # reduce time_exit_cycles to this fraction
MAXHOLD_FLOOR = 60
SIZE_CUT = 0.75  # reduce position_size_r to this fraction (soft de-risk)
SIZE_FLOOR = 0.05

GIVEBACK_CAPTURE_LO = 0.6  # winners capturing < 60% of peak MFE -> giveback problem
TIMEOUT_FRAC_HI = 0.5  # > half of closes are time-exits -> exit-timing problem
LOW_WR = 0.3  # win-rate below this is a low-WR pathology
MIN_SAMPLE = 5  # need at least this many closes before any change


# ── pure helpers (unit-tested, no I/O) ─────────────────────────────────────
def aggregate_trades(trades: list[dict]) -> dict:
    """Compute win_rate, pnl stats, drawdown proxy from a batch of closed trades.

    A trade record carries at least: pnl_pct, exit_price, entry_price.
    'drawdown' here = worst single-trade loss (blueprint uses max_dd vs the
    goal's max_drawdown, expressed in %). Both are in percent units.
    """
    if not trades:
        return {"count": 0, "win_rate": 0.0, "ret": 0.0, "worst_loss": 0.0, "drawdown": 0.0}
    pnls = [float(t.get("pnl_pct", 0.0)) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    worst = min(pnls) if pnls else 0.0
    return {
        "count": len(pnls),
        "win_rate": wins / len(pnls),
        "ret": sum(pnls),
        "worst_loss": worst,  # <= 0
        "drawdown": -worst,  # >= 0, percent
    }


def _trade_regime(t: dict) -> str | None:
    """Best-effort regime label stamped on a close (Phase 5.1)."""
    for key in ("entry_regime", "regime", "regime_label"):
        v = t.get(key)
        if v:
            return str(v).lower()
    return None


def same_regime_batch(trades: list[dict], every: int) -> tuple[list[dict], str | None]:
    """Pick a same-regime reflection batch (Phase 5.1).

    Prefer the dominant regime among the most recent ``every * 2`` closes, then
    return up to ``every`` trades from that regime. If no regime stamps are
    present (legacy books), fall back to the last ``every`` closes unchanged.
    """
    if not trades or every < 1:
        return [], None
    window = trades[-max(every * 2, every) :]
    labels = [_trade_regime(t) for t in window]
    present = [r for r in labels if r]
    if not present:
        return trades[-every:], None
    # Dominant regime (mode); ties → most recent label wins via reverse scan.
    counts: dict[str, int] = {}
    for r in present:
        counts[r] = counts.get(r, 0) + 1
    best_n = max(counts.values())
    candidates = {k for k, n in counts.items() if n == best_n}
    dominant = None
    for t in reversed(window):
        r = _trade_regime(t)
        if r in candidates:
            dominant = r
            break
    batch = [t for t in window if _trade_regime(t) == dominant][-every:]
    return batch, dominant


def _exit_reason(t: dict) -> str:
    return str(t.get("exit_reason") or t.get("reason") or "").lower()


def trade_pathology(trades: list[dict]) -> dict:
    """Summarise WHY the batch behaved as it did (Phase 2.1 feature extraction).

    Pure arithmetic over the closed batch + any excursion fields the loop stamps
    (mfe_pct / giveback_frac / mfe_capture). Fields that are absent simply yield
    ``None`` so downstream axes fail-soft (they will not fire on data they can't
    see — this keeps legacy rule tests intact).
    """
    n = len(trades)
    reasons = [_exit_reason(t) for t in trades]
    stop_n = sum(1 for r in reasons if r in ("sl", "stop_loss", "stop"))
    tp_n = sum(1 for r in reasons if r in ("tp", "take_profit", "profit_target"))
    soft_n = sum(
        1
        for t, r in zip(trades, reasons)
        if r in ("profit_bank", "soft_bank")
        or (isinstance(t, dict) and (t.get("soft_bank") or t.get("exit_class") == "soft_capture"))
    )
    # Soft banks must NOT count as timeouts (would lower TP).
    time_n = sum(
        1
        for t, r in zip(trades, reasons)
        if ("time" in r or r == "timeout")
        and r not in ("profit_bank",)
        and not (isinstance(t, dict) and t.get("exit_class") == "soft_capture")
    )

    caps = [float(t["mfe_capture"]) for t in trades if t.get("mfe_capture") is not None]
    gbs = [float(t["giveback_frac"]) for t in trades if t.get("giveback_frac") is not None]
    winners = [t for t in trades if float(t.get("pnl_pct", 0.0)) > 0]
    # MFE left on the table by time-exits (profit that existed but wasn't taken).
    time_mfe = [
        float(t.get("mfe_pct", 0.0))
        for t in trades
        if ("time" in _exit_reason(t)) and t.get("mfe_pct") is not None
    ]
    return {
        "count": n,
        "stop_frac": stop_n / n if n else 0.0,
        "tp_frac": tp_n / n if n else 0.0,
        "timeout_frac": time_n / n if n else 0.0,
        "soft_bank_frac": soft_n / n if n else 0.0,
        "winners": len(winners),
        "avg_capture": (sum(caps) / len(caps)) if caps else None,
        "avg_giveback": (sum(gbs) / len(gbs)) if gbs else None,
        "avg_time_mfe": (sum(time_mfe) / len(time_mfe)) if time_mfe else None,
    }


def _cortex_stability(cortex, pair: str, entry_type: str) -> float:
    """0..1 confidence contribution from Cortex evidence for (pair, entry_type)."""
    if cortex is None:
        return 0.0
    try:
        wr = cortex.entry_type_wr(entry_type, pair=pair)
        if wr is None:
            return 0.0
        n = 0
        with contextlib.suppress(Exception):
            n = int(cortex.evidence_n(pair, entry_type))
        return min(1.0, n / 20.0)
    except Exception:  # noqa: BLE001
        return 0.0


def dynamic_confidence(
    count: int, effect: float, stability: float, *, weights: dict | None = None
) -> float:
    """Confidence from sample size + effect size + Cortex stability (Phase 2.3).

    Calibrated so a thin batch (~5 closes, no Cortex support) stays BELOW the L2
    invocation bar (0.65) — only more evidence / stronger pathology / Cortex
    corroboration pushes a proposal into consensus review.

    ``weights`` (Phase 6) lets the blend be recalibrated from realized live
    outcomes; omitted → the default prior weights, i.e. unchanged behaviour.
    """
    w = weights or {}
    intercept = float(w.get("intercept", 0.30))
    w_size = float(w.get("size", 0.20))
    w_effect = float(w.get("effect", 0.20))
    w_stab = float(w.get("stability", 0.15))

    size_term = min(1.0, max(0, count) / 20.0)
    effect = max(0.0, min(1.0, effect))
    stability = max(0.0, min(1.0, stability))
    conf = intercept + w_size * size_term + w_effect * effect + w_stab * stability
    return round(max(0.25, min(0.95, conf)), 3)


def adaptive_bars(bot: str | None, pair: str, trades: list[dict]) -> dict:
    """Pathology bars for THIS pair, learned from its own distribution (Phase 6).

    Returns the same keys as the module-level priors. With a thin sample the
    priors are returned unchanged, so behaviour only shifts once the pair has
    actually shown the engine what "normal" looks like for it.
    """
    bars = {
        "low_wr": LOW_WR,
        "giveback_capture_lo": GIVEBACK_CAPTURE_LO,
        "timeout_frac_hi": TIMEOUT_FRAC_HI,
    }
    if not bot:
        return bars
    with contextlib.suppress(Exception):
        from hermes_core.engines.adaptive import adaptive_threshold

        closed = _closed_trades_for_pair(bot, pair) or list(trades or [])
        # Per-batch win-rates over rolling windows of 5 → the pair's own WR spread.
        wrs: list[float] = []
        window = 5
        for i in range(0, max(0, len(closed) - window + 1)):
            chunk = closed[i : i + window]
            wins = sum(1 for t in chunk if float(t.get("pnl_pct", 0.0)) > 0)
            wrs.append(wins / len(chunk))
        bars["low_wr"] = adaptive_threshold(
            LOW_WR, wrs, q=0.25, floor=0.10, cap=0.50
        )

        caps = [
            float(t["mfe_capture"]) for t in closed if t.get("mfe_capture") is not None
        ]
        bars["giveback_capture_lo"] = adaptive_threshold(
            GIVEBACK_CAPTURE_LO, caps, q=0.35, floor=0.30, cap=0.85
        )

        # Timeout share per rolling window → what "too many timeouts" means here.
        shares: list[float] = []
        for i in range(0, max(0, len(closed) - window + 1)):
            chunk = closed[i : i + window]
            n_to = sum(1 for t in chunk if "time" in _exit_reason(t))
            shares.append(n_to / len(chunk))
        bars["timeout_frac_hi"] = adaptive_threshold(
            TIMEOUT_FRAC_HI, shares, q=0.75, floor=0.25, cap=0.85
        )
    return bars


def _axis_candidates(
    strategy: dict,
    agg: dict,
    path: dict,
    goal: dict,
    *,
    blocked: set[str] | None = None,
    bot: str | None = None,
    pair: str | None = None,
    bars: dict | None = None,
    regime: str | None = None,
    closed_count: int | None = None,
) -> list[tuple]:
    """Pathology→axis mapping (Phase 2.2 / 2.4). Returns ordered candidate axes.

    Each candidate is (priority, variable, old, new, why, effect). The single
    highest-priority candidate whose change is non-degenerate is chosen upstream
    (one_variable_only). Only axes whose pathology signal is actually present
    produce a candidate — so on batches lacking excursion/exit data the tree
    reduces to the classic stop rules.

    ``blocked`` (Phase 3.4) drops axes whose variable is under a live-experiment
    cooldown, so reflection is FORCED onto a different lever after a revert.

    Phase 6: step sizes, pathology bars and the final ORDER are all adaptive.
    Priorities are priors — an axis with a live track record can outrank one that
    keeps failing. With no learned evidence this is byte-identical to the old
    fixed tree.

    Soft direction quarantine (#6) drops candidates whose (variable, direction)
    is still cooling after a live/pipeline reject.
    """
    out: list[tuple] = []
    max_dd = float((goal or {}).get("max_drawdown", 10.0))
    cur_stop = float(strategy.get("stop_loss_pct", 1.5))

    b = bars or {
        "low_wr": LOW_WR,
        "giveback_capture_lo": GIVEBACK_CAPTURE_LO,
        "timeout_frac_hi": TIMEOUT_FRAC_HI,
    }
    low_wr = float(b["low_wr"])
    capture_lo = float(b["giveback_capture_lo"])
    timeout_hi = float(b["timeout_frac_hi"])

    def _step(variable: str, base: float, effect: float) -> float:
        """Learned step magnitude for this axis (prior when no evidence)."""
        if not bot:
            return base
        try:
            from hermes_core.engines.adaptive import adaptive_step

            return adaptive_step(
                bot, pair, variable, base, effect=effect, regime=regime
            )
        except Exception:  # noqa: BLE001
            return base

    # P1 — drawdown breach → tighten stop (classic; always emitted even if the
    # floor clamps it, matching the legacy floor-enforced contract).
    if agg["drawdown"] > max_dd:
        effect = min(1.0, (agg["drawdown"] - max_dd) / max(1e-9, max_dd))
        step = _step("stop_loss_pct", STOP_TIGHTEN, effect)
        # STOP_FLOOR is a hard risk guard — never adaptive.
        new_stop = max(STOP_FLOOR, round(cur_stop - step, 4))
        out.append(
            (1, "stop_loss_pct", cur_stop, new_stop,
             f"tighten stop on drawdown breach; drawdown {agg['drawdown']:.2f}% > max_dd {max_dd:.2f}%",
             effect)
        )

    # P2 — winners give back too much of peak MFE → capture with a trailing stop.
    cap = path.get("avg_capture")
    if cap is not None and path["winners"] >= 3 and cap < capture_lo:
        cur_trail = float(strategy.get("trailing_stop_pct", 0.0) or 0.0)
        effect = min(1.0, (capture_lo - cap) / max(1e-9, capture_lo))
        step = _step("trailing_stop_pct", TRAIL_STEP, effect)
        new_trail = round(min(TRAIL_CAP, (cur_trail or 0.0) + step), 4)
        out.append(
            (2, "trailing_stop_pct", cur_trail, new_trail,
             f"winners capture only {cap:.0%} of peak MFE (<{capture_lo:.0%}); "
             f"add/raise trailing stop to lock gains",
             effect)
        )

    # P3 — most exits are timeouts leaving profit on the table → take profit sooner.
    if path["timeout_frac"] > timeout_hi and (path.get("avg_time_mfe") or 0.0) > 0:
        cur_tgt = float(strategy.get("profit_target_pct", 3.0))
        effect = min(1.0, path["timeout_frac"])
        step = _step("profit_target_pct", TARGET_STEP, effect)
        new_tgt = round(max(TARGET_FLOOR, cur_tgt - step), 4)
        out.append(
            (3, "profit_target_pct", cur_tgt, new_tgt,
             f"{path['timeout_frac']:.0%} time-exits with avg peak MFE "
             f"{path['avg_time_mfe']:.2f}% unrealised; lower profit target to capture it",
             effect)
        )

    # P4 — low win-rate → widen stop so noise doesn't stop us out (classic).
    if agg["win_rate"] < low_wr:
        effect = min(1.0, (low_wr - agg["win_rate"]) / max(1e-9, low_wr))
        step = _step("stop_loss_pct", STOP_WIDEN, effect)
        new_stop = min(STOP_CAP, round(cur_stop + step, 4))
        out.append(
            (4, "stop_loss_pct", cur_stop, new_stop,
             f"widen stop on low win-rate; win_rate {agg['win_rate']:.2f} < {low_wr:.2f}",
             effect)
        )

    # P5 — sustained bleed over a real sample → soft de-risk (shrink size).
    if agg["ret"] < -0.5 and agg["count"] >= 10 and agg["win_rate"] >= low_wr:
        cur_size = float(strategy.get("position_size_r", 0.4))
        effect = min(1.0, abs(agg["ret"]) / 5.0)
        # Cut fraction adapts: a reliable de-risk lever cuts harder, a shaky one less.
        cut = 1.0 - (1.0 - SIZE_CUT) * (_step("position_size_r", 1.0, effect))
        cut = max(0.4, min(0.95, cut))
        new_size = round(max(SIZE_FLOOR, cur_size * cut), 4)
        out.append(
            (5, "position_size_r", cur_size, new_size,
             f"sustained loss {agg['ret']:.2f}% over {agg['count']} trades at WR "
             f"{agg['win_rate']:.2f}; shrink size while edge is unproven",
             effect)
        )

    # P6 — legacy sustained-loss widen-stop (low WR path already covered by P4).
    if agg["ret"] < -0.5 and agg["count"] >= 10 and agg["win_rate"] < low_wr:
        effect = min(1.0, abs(agg["ret"]) / 5.0)
        step = _step("stop_loss_pct", STOP_WIDEN, effect)
        new_stop = min(STOP_CAP, round(cur_stop + step, 4))
        out.append(
            (6, "stop_loss_pct", cur_stop, new_stop,
             f"widen stop on sustained loss; ret {agg['ret']:.2f}% over {agg['count']} trades",
             effect)
        )

    if blocked:
        out = [c for c in out if c[1] not in blocked]

    # Soft direction / near-dupe quarantine (#6).
    if bot and pair is not None:
        with contextlib.suppress(Exception):
            from hermes_core.engines import experiment_control as _exp

            n_closed = closed_count
            if n_closed is None:
                n_closed = len(_closed_trades_for_pair(bot, pair))
            filtered = []
            for c in out:
                ban = _exp.soft_quarantined(bot, pair, c[1], c[2], c[3], int(n_closed))
                if ban is None:
                    filtered.append(c)
            out = filtered

    if bot:
        with contextlib.suppress(Exception):
            from hermes_core.engines.adaptive import sort_candidates

            return sort_candidates(bot, pair, out, regime=regime)
    out.sort(key=lambda c: c[0])
    return out


def layer1_rule_based(
    pair: str,
    trades: list[dict],
    goal: dict,
    strategy: dict,
    *,
    cortex=None,
    blocked_axes: set[str] | None = None,
    bot: str | None = None,
) -> tuple | None:
    """Multi-axis L1 rule tree (Phase 2). Returns (variable, old, new, reason,
    confidence) or None.

    No mutation. Picks exactly ONE variable (one_variable_only) — the
    highest-ranked axis whose pathology signal is present and whose change is
    non-degenerate. New axes (trailing capture, profit target, soft size) fire
    only when their excursion/exit signals appear.

    Phase 6: when ``bot`` is supplied the step sizes, pathology bars, axis order
    and confidence blend are all learned from that bot's realized live-experiment
    outcomes. Without ``bot`` the function stays pure and uses the priors.
    """
    if not trades or not strategy:
        return None
    agg = aggregate_trades(trades)
    min_sample = MIN_SAMPLE
    if agg["count"] < min_sample:
        return None  # [guard] need a minimum sample before anything changes

    path = trade_pathology(trades)
    entry_type = str(strategy.get("strategy_type") or "mean_reversion")
    stability = _cortex_stability(cortex, pair, entry_type)

    bars = adaptive_bars(bot, pair, trades) if bot else None
    conf_weights = None
    if bot:
        with contextlib.suppress(Exception):
            from hermes_core.engines.adaptive import confidence_weights

            conf_weights = confidence_weights(bot, pair)

    # Dominant regime of this batch → regime-conditioned credit (#3).
    regime = None
    with contextlib.suppress(Exception):
        from hermes_core.engines.live_verdict import dominant_regime

        regime = dominant_regime(trades)

    closed_count = None
    if bot:
        with contextlib.suppress(Exception):
            closed_count = len(_closed_trades_for_pair(bot, pair))

    cands = list(
        _axis_candidates(
            strategy,
            agg,
            path,
            goal,
            blocked=blocked_axes,
            bot=bot,
            pair=pair,
            bars=bars,
            regime=regime,
            closed_count=closed_count,
        )
    )

    # #9: prefer the next step of a saved plan when it is still a valid candidate.
    if bot and cands:
        with contextlib.suppress(Exception):
            from hermes_core.engines.experiment_control import next_plan_step

            step = next_plan_step(bot, pair)
            if step and step.get("variable"):
                for i, c in enumerate(cands):
                    if c[1] == step["variable"]:
                        cands = [cands[i]] + cands[:i] + cands[i + 1 :]
                        break

    chosen = None
    for priority, variable, old, new, why, effect in cands:
        # P1 (drawdown tighten) is allowed to be floor-clamped to a no-op and
        # still emitted (legacy contract). Every other axis skips no-ops.
        if priority != 1 and float(new) == float(old):
            continue
        conf = dynamic_confidence(agg["count"], effect, stability, weights=conf_weights)
        chosen = (variable, old, new, why, conf)
        break

    # Persist the remaining pathology-matched axes as a short plan (#9).
    if bot and chosen and len(cands) > 1:
        with contextlib.suppress(Exception):
            from hermes_core.engines.experiment_control import save_plan

            rest = []
            for priority, variable, old, new, why, effect in cands:
                if variable == chosen[0] and float(old) == float(chosen[1]) and float(new) == float(chosen[2]):
                    continue
                if priority != 1 and float(new) == float(old):
                    continue
                rest.append(
                    {
                        "priority": priority,
                        "variable": variable,
                        "old": old,
                        "new": new,
                        "why": why,
                    }
                )
            if rest:
                save_plan(bot, pair, rest, reason=f"follow_up_after_{chosen[0]}")

    return chosen


def _log_hypothesis(rec: dict) -> None:
    """Append a reflection hypothesis to state/hypotheses.jsonl (shadow log).

    Also mirror the note into the cortex reflection channel (Phase 0.4) so
    Cortex is aware of every reflection proposal/outcome. Both writes are
    fail-soft — reflection must never break on a logging error.
    """
    path = hypotheses_path(rec.get("bot"))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass
    _notify_cortex(rec)


def _notify_cortex(rec: dict) -> None:
    """Mirror a reflection hypothesis into the cortex reflection channel.

    The destination is derived from the (possibly test-patched) hypotheses log
    location so isolation of one isolates the other. In production this resolves
    to ``{bot}/state/cortex/reflection_log.jsonl`` — exactly what
    ``Cortex.recent_hypotheses`` reads.
    """
    try:
        from hermes_core.engines.decision_cortex import append_reflection_note

        bot = rec.get("bot")
        cortex_log = hypotheses_path(bot).parent / "cortex" / "reflection_log.jsonl"
        variable = rec.get("variable")
        text = rec.get("reason") or (
            f"{variable} {rec.get('old')}->{rec.get('new')}" if variable else rec.get("status", "")
        )
        append_reflection_note(
            bot,
            {
                "pair": rec.get("pair"),
                "type": "hypothesis",
                "text": text,
                "status": rec.get("status"),
                "variable": variable,
                "old": rec.get("old"),
                "new": rec.get("new"),
                "version": rec.get("version"),
                "ts": rec.get("ts") or __import__("time").time(),
            },
            path=cortex_log,
        )
    except Exception:  # noqa: BLE001 — never break reflection on cortex logging
        pass


def combined_reflect(
    pair: str,
    trades: list[dict],
    goal: dict | None = None,
    chart_context: str = "",
    skipped_json: str = "",
    strategy: dict | None = None,
    bot: str = "forex",
    cortex=None,
) -> list[dict]:
    """L1 orchestrator. Returns the list of proposed (shadow) changes.

    SHADOW-ONLY: it never mutates the live strategy. Each proposal is logged to
    state/hypotheses.jsonl with full provenance so you can approve it later.
    Exactly one variable may change per call (one_variable_only). ``cortex`` (or
    a lazily-built read-only Cortex) feeds per-entry-type WR into confidence.
    """
    if goal is None:
        goal = (load_config(bot) if bot else {}).get("goal", {})
    if strategy is None:
        from hermes_core.config import load_strategy_for_pair

        strategy = load_strategy_for_pair(pair, bot)

    if cortex is None:
        with contextlib.suppress(Exception):
            from hermes_core.engines.decision_cortex import Cortex

            cortex = Cortex(bot=bot)

    # Phase 3.4: axes under a live-experiment cooldown are off-limits — reflection
    # must try a different lever until the cooldown lapses.
    blocked: set[str] = set()
    total_closed = len(trades)
    with contextlib.suppress(Exception):
        from hermes_core.engines import experiment_control as _exp

        total_closed = len(_closed_trades_for_pair(bot, pair)) if bot else len(trades)
        blocked = _exp.blocked_axes(bot, pair, total_closed)

    change = layer1_rule_based(
        pair, trades, goal, strategy, cortex=cortex, blocked_axes=blocked, bot=bot
    )
    if change is None:
        # Phase 3.5: if the pathology DID produce candidate axes but every one is
        # blocked (all reverted/quarantined), reflection is stuck → escalate the
        # pair into safe mode (size-down, then pause).
        if blocked:
            with contextlib.suppress(Exception):
                agg = aggregate_trades(trades)
                raw = _axis_candidates(
                    strategy,
                    agg,
                    trade_pathology(trades),
                    goal,
                    bot=bot,
                    pair=pair,
                    bars=adaptive_bars(bot, pair, trades) if bot else None,
                )
                raw_vars = {c[1] for c in raw}
                if raw_vars and raw_vars.issubset(blocked):
                    from hermes_core.engines import experiment_control as _exp

                    _exp.escalate_safe_mode(
                        bot, pair, f"all reflection axes blocked: {sorted(raw_vars)}"
                    )
        return []

    variable, old, new, reason, confidence = change
    stats = aggregate_trades(trades)
    if skipped_json:
        reason = f"{reason} | skip_ctx: {skipped_json[:400]}"
    rec = {
        "pair": pair,
        "bot": bot,
        "variable": variable,
        "old": old,
        "new": new,
        "reason": reason,
        "confidence": confidence,
        "chart_context": chart_context,
        "stats": stats,
        "ts": __import__("time").time(),
        "status": "proposed",  # NOT applied — awaits approval + backtest (S10)
    }
    if skipped_json:
        rec["skip_context"] = skipped_json[:2000]
    _log_hypothesis(rec)
    return [rec]


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2 — three-model consensus (Session 11 / Phase 11)
#
# Corrected, tiered score gate (roadmap header correction is BINDING here):
#   score < 65          -> L2 is NEVER called; L1 stands/rejected on its own.
#   65 <= score < 75    -> 2/3 consensus required to apply.
#   score >= 75         -> UNANIMOUS 3/3 required to apply.
#   confidence >= 0.40   -> required to apply regardless of vote outcome.
#
# NOTE: the blueprint's documented 55 gate is a REGRESSION this rebuild corrects.
# Do NOT implement the gate at 55 as the standard — 65 is the standard, 75 the
# unanimous bar. See roadmap S11 DO-NOT.
# ═══════════════════════════════════════════════════════════════════════════
L2_MIN_SCORE = 65  # [GUARD L53] below this, L2 is never invoked
L2_UNANIMOUS_SCORE = 75  # at/above this, 3/3 unanimous required
APPLY_CONFIDENCE = 0.40  # [GUARD L53] min confidence to apply any change

# Ordered cascade: DeepSeek -> Gemini -> Groq. Tests inject fakes via ``callers``.
# Production callers use httpx (already a hermes dep) — NOT the openai /
# google.generativeai SDKs, which are absent from the bot image and caused
# live ModuleNotFoundError → false 0/3 L2 rejects.
DEFAULT_MODELS = ("deepseek", "gemini", "groq")
# Models that get the micro brief (small/fast); others get the full brief.
_L2_MICRO_MODELS = frozenset({"groq"})
# Matches experiment_control.l2_model_weight upper bound (cascade early-exit).
_L2_MAX_MODEL_WEIGHT = 1.75
_L2_MAX_TOKENS = 96

_DEEPSEEK_URL = "https://api.deepseek.com/v1/chat/completions"
_GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"


def _env(name: str, default: str | None = None) -> str | None:
    """Read env at call time via hermes_core.env (loads .env once).

    Never freeze API keys at import — same soak bug chart_vision hit when
    bots/_runner imported engines before load_env().
    """
    from hermes_core.env import get_env

    val = (get_env(name, "") or "").strip()
    if not val:
        return default
    return val


def l2_keys_status() -> dict[str, bool]:
    """Which L2 provider keys are visible right now (no secret values)."""
    return {
        "deepseek": bool(_env("DEEPSEEK_API_KEY")),
        "gemini": bool(_env("GEMINI_API_KEY")),
        "groq": bool(_env("GROQ_API_KEY")),
    }


def _openai_chat_completion(
    *,
    url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: float = 30.0,
) -> str:
    """POST an OpenAI-compatible chat completion; return assistant text."""
    import httpx

    resp = httpx.post(
        url,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": _L2_MAX_TOKENS,
        },
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return str((((data.get("choices") or [{}])[0].get("message") or {}).get("content")) or "")


def call_deepseek(prompt: str, api_key: str | None = None) -> str:
    """DeepSeek chat completion via httpx (OpenAI-compatible)."""
    key = api_key or _env("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY missing")
    model = _env("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL) or DEFAULT_DEEPSEEK_MODEL
    url = _env("DEEPSEEK_URL", _DEEPSEEK_URL) or _DEEPSEEK_URL
    return _openai_chat_completion(url=url, api_key=key, model=model, prompt=prompt)


def call_gemini(prompt: str, api_key: str | None = None) -> str:
    """Gemini generateContent via httpx (no google.generativeai SDK)."""
    import httpx

    key = api_key or _env("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing")
    model = _env("GEMINI_MODEL", DEFAULT_GEMINI_MODEL) or DEFAULT_GEMINI_MODEL
    url = _env(
        "GEMINI_URL",
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    ) or (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    )
    resp = httpx.post(
        url,
        params={"key": key},
        json={"contents": [{"parts": [{"text": prompt}]}]},
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict)]
    return "".join(texts)


def call_groq(prompt: str, api_key: str | None = None) -> str:
    """Groq chat completion via httpx (OpenAI-compatible)."""
    key = api_key or _env("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY missing")
    model = _env("GROQ_MODEL", DEFAULT_GROQ_MODEL) or DEFAULT_GROQ_MODEL
    url = _env("GROQ_URL", _GROQ_URL) or _GROQ_URL
    return _openai_chat_completion(url=url, api_key=key, model=model, prompt=prompt)


_MODEL_CALLERS = {
    "deepseek": call_deepseek,
    "gemini": call_gemini,
    "groq": call_groq,
}


def _parse_vote(text: str) -> bool:
    """Parse a model reply into a yes-vote (fail-closed).

    Prefer a last-line ``VOTE: YES|NO`` contract. Else accept whole-word
    YES / APPROVE / AGREE / ACCEPT. Hedging, silence, and NO are all NO.
    """
    raw = (text or "").strip()
    if not raw:
        return False
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if lines:
        m = re.match(r"^VOTE\s*:\s*(YES|NO)\b", lines[-1], flags=re.IGNORECASE)
        if m:
            return m.group(1).upper() == "YES"
        # Also accept VOTE on any line if last line was a reason-only fragment.
        for ln in reversed(lines):
            m = re.match(r"^VOTE\s*:\s*(YES|NO)\b", ln, flags=re.IGNORECASE)
            if m:
                return m.group(1).upper() == "YES"
    t = raw.upper()
    if re.search(r"\bVOTE\s*:\s*NO\b", t):
        return False
    return bool(re.search(r"\b(YES|APPROVE|AGREE|ACCEPT)\b", t))


def _l2_axis_kind(variable: str) -> str:
    """Map a proposal variable to an evidence bucket for adaptive context."""
    v = (variable or "").lower()
    if any(k in v for k in ("trail", "giveback", "mfe", "trailing")):
        return "trail"
    if any(
        k in v
        for k in (
            "session",
            "rsi",
            "entry",
            "threshold",
            "bb_",
            "bandwidth",
            "filter",
            "oversold",
            "overbought",
        )
    ):
        return "entry"
    if any(
        k in v
        for k in (
            "stop_loss",
            "profit_target",
            "risk_reward",
            "rr_",
            "target",
            "time_exit",
            "max_hold",
        )
    ):
        return "exit"
    return "generic"


def _l2_prompt_tier(model: str) -> str:
    return "micro" if model in _L2_MICRO_MODELS else "full"


def _context_bullets(context: str, *, limit: int = 5) -> list[str]:
    """Split assembled L2 context into short bullets for the micro brief."""
    bullets: list[str] = []
    for chunk in re.split(r"\s*\|\s*", (context or "").strip()):
        chunk = chunk.strip()
        if not chunk:
            continue
        if len(chunk) > 100:
            chunk = chunk[:97] + "..."
        bullets.append(chunk)
        if len(bullets) >= limit:
            break
    return bullets


def build_l2_prompt(proposal: dict, context: str = "", *, tier: str = "full") -> str:
    """Build a capability-tiered L2 critic prompt with a strict VOTE contract."""
    variable = proposal.get("variable")
    pair = proposal.get("pair")
    old = proposal.get("old")
    new = proposal.get("new")
    reason = proposal.get("reason") or ""
    header = (
        f"PROPOSAL: {variable} {old} -> {new} on {pair}\n"
        f"REASON: {reason}"
    )
    vote_rule = (
        "End your reply with exactly one line: VOTE: YES or VOTE: NO\n"
        "(YES = worth a backtest of this reversible paper-soak YAML change; "
        "NO = harmful, oscillating, or unsupported.)"
    )
    if tier == "micro":
        bullets = _context_bullets(context, limit=5)
        facts = "\n".join(f"- {b}" for b in bullets) if bullets else "- (no extra evidence)"
        return (
            "You are a fast trading-risk sanity voter (paper soak).\n"
            f"{header}\n"
            f"FACTS:\n{facts}\n"
            f"{vote_rule}"
        )
    evidence = (context or "").strip() or "(none)"
    return (
        "You are a senior trading-risk reviewer for a paper-soak HERMES bot.\n"
        "This is a reversible YAML parameter change; YES only advances the "
        "proposal to a backtest — it is not a claim of guaranteed profit.\n"
        "Vote NO if the change looks harmful, oscillates a failed axis, or "
        "lacks supporting evidence.\n"
        f"{header}\n"
        f"EVIDENCE:\n{evidence}\n"
        f"{vote_rule}"
    )


def _model_weight(bot: str | None, name: str) -> float:
    if not bot:
        return 1.0
    try:
        from hermes_core.engines.experiment_control import l2_model_weight

        return float(l2_model_weight(bot, name))
    except Exception:  # noqa: BLE001
        return 1.0


def _cascade_skip_third(
    *,
    first_two: list[str],
    vote_map: dict[str, bool],
    weights: dict[str, float],
    required: int,
    required_weight: float,
) -> bool:
    """True when the 3rd model call cannot change the weighted outcome."""
    if len(first_two) < 2:
        return True
    yeses = [n for n in first_two if vote_map.get(n)]
    yes_w = sum(weights.get(n, 1.0) for n in yeses)
    if len(yeses) == 2 and required <= 2:
        return True
    if yes_w + 1e-9 >= required_weight:
        return True
    # Both NO (or errors-as-NO): even max-weight YES from #3 cannot clear the bar.
    if len(yeses) == 0 and yes_w + _L2_MAX_MODEL_WEIGHT + 1e-9 < required_weight:
        return True
    if yes_w + _L2_MAX_MODEL_WEIGHT + 1e-9 < required_weight:
        return True
    return False


class ConsensusResult:
    """Outcome of the L2 consensus gate over a single proposal."""

    __slots__ = (
        "score",
        "threshold",
        "votes_yes",
        "votes_total",
        "required",
        "confidence",
        "decision",
        "reasons",
    )

    def __init__(
        self,
        score: float,
        threshold: float,
        votes_yes: int,
        votes_total: int,
        required: int,
        confidence: float,
        decision: bool,
        reasons: list[str],
    ):
        self.score = score
        self.threshold = threshold
        self.votes_yes = votes_yes
        self.votes_total = votes_total
        self.required = required
        self.confidence = confidence
        self.decision = decision
        self.reasons = reasons

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "threshold": self.threshold,
            "votes_yes": self.votes_yes,
            "votes_total": self.votes_total,
            "required": self.required,
            "confidence": self.confidence,
            "decision": self.decision,
            "reasons": self.reasons,
        }


def _required_votes(score: float, *, min_score: float = L2_MIN_SCORE, uni_score: float = L2_UNANIMOUS_SCORE) -> tuple[int, str]:
    """Return (required yes-votes, human label) for a given score (gate logic)."""
    if score >= uni_score:
        return 3, f"unanimous 3/3 (score>={uni_score:.0f})"
    if score >= min_score:
        return 2, f"2/3 majority ({min_score:.0f}<=score<{uni_score:.0f})"
    return 0, f"L2 not invoked (score<{min_score:.0f})"


def call_llm_consensus(
    proposal: dict,
    context: str = "",
    *,
    score: float | None = None,
    confidence: float | None = None,
    models: tuple[str, ...] = DEFAULT_MODELS,
    callers: dict[str, callable] | None = None,
    bot: str | None = None,
    min_score: float | None = None,
    uni_score: float | None = None,
) -> ConsensusResult:
    """Run the tiered three-model consensus gate over a proposal.

    `score` and `confidence` are normally taken from the L1 proposal; both are
    injectable so the gate logic is testable without producing a real proposal.
    `callers` lets tests inject fake model functions keyed by model name.

    Gate (fail-closed): below min_score the models are never consulted and the
    decision is REJECT (L1 must stand/rejected on its own). At/above the bar the
    required weighted vote count (2/3 or 3/3) must be met AND confidence >= APPLY.
    Model weights come from live outcome calibration (#10).

    Efficiency: models are ordered by trust weight; top-2 run in parallel; the
    3rd is skipped when it cannot change the weighted outcome. Each model gets
    a capability-tiered prompt (full vs micro).
    """
    score = float(score if score is not None else proposal.get("confidence", 0.0) * 100)
    confidence = float(confidence if confidence is not None else proposal.get("confidence", 0.0))
    min_s = float(min_score if min_score is not None else L2_MIN_SCORE)
    uni_s = float(uni_score if uni_score is not None else L2_UNANIMOUS_SCORE)
    required, label = _required_votes(score, min_score=min_s, uni_score=uni_s)

    if required == 0:
        return ConsensusResult(
            score,
            min_s,
            0,
            0,
            0,
            confidence,
            False,
            [f"score {score:.0f} < {min_s:.0f}: L2 not invoked; L1 stands on its own"],
        )

    callers = _MODEL_CALLERS if callers is None else callers
    key_status = l2_keys_status()
    missing = [n for n, ok in key_status.items() if n in models and not ok]
    if missing:
        print(
            f"[hermes][L2] missing API keys at call time: {', '.join(missing)} "
            f"(present={[n for n, ok in key_status.items() if ok]})",
            flush=True,
        )

    order_index = {n: i for i, n in enumerate(models)}
    available = [n for n in models if n in callers]
    weights = {n: _model_weight(bot, n) for n in available}
    ordered = sorted(available, key=lambda n: (-weights[n], order_index.get(n, 99)))

    votes_yes = 0
    reached = 0
    yes_weight = 0.0
    call_errors: list[str] = []
    cascade_note: str | None = None
    vote_map: dict[str, bool] = {}

    def _invoke(name: str) -> tuple[str, bool | None, str | None]:
        """Return (name, yes_or_None_on_error, error_or_None)."""
        tier = _l2_prompt_tier(name)
        prompt = build_l2_prompt(proposal, context, tier=tier)
        try:
            reply = callers[name](prompt)
        except Exception as exc:  # noqa: BLE001 — fail-closed: a model error = NO
            return name, None, f"{name}:{type(exc).__name__}"
        return name, _parse_vote(reply), None

    def _record(name: str, yes: bool | None, err: str | None) -> None:
        nonlocal votes_yes, reached, yes_weight
        reached += 1
        if err:
            call_errors.append(err)
            vote_map[name] = False
            return
        vote_map[name] = bool(yes)
        if yes:
            votes_yes += 1
            yes_weight += weights.get(name, 1.0)

    if not ordered:
        pass
    elif len(ordered) == 1:
        name, yes, err = _invoke(ordered[0])
        _record(name, yes, err)
    else:
        first_two = ordered[:2]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(_invoke, n) for n in first_two]
            for fut in as_completed(futs):
                name, yes, err = fut.result()
                _record(name, yes, err)

        required_weight = float(required)
        rest = ordered[2:]
        if rest and not _cascade_skip_third(
            first_two=first_two,
            vote_map=vote_map,
            weights=weights,
            required=required,
            required_weight=required_weight,
        ):
            name, yes, err = _invoke(rest[0])
            _record(name, yes, err)
        elif rest:
            cascade_note = f"cascade_skip:{rest[0]}"

    # Weighted bar: default weights=1 → identical to classic 2/3 or 3/3.
    required_weight = float(required)
    reasons = [
        f"score {score:.0f} -> {label}; votes {votes_yes}/{reached} "
        f"(required {required}, yes_w={yes_weight:.2f}/{required_weight:.2f})"
    ]
    if cascade_note:
        reasons.append(cascade_note)
    if call_errors:
        reasons.append("errors: " + ",".join(call_errors))

    decision = yes_weight + 1e-9 >= required_weight and confidence >= APPLY_CONFIDENCE
    if confidence < APPLY_CONFIDENCE:
        reasons.append(f"confidence {confidence:.2f} < {APPLY_CONFIDENCE}")
    reasons.append("CONSENSUS APPLY" if decision else "CONSENSUS REJECT")

    if bot and proposal.get("pair"):
        with contextlib.suppress(Exception):
            from hermes_core.engines.experiment_control import record_l2_votes

            record_l2_votes(
                bot,
                str(proposal.get("pair")),
                votes=vote_map,
                decision=decision,
            )

    return ConsensusResult(
        score,
        min_s if score < uni_s else uni_s,
        votes_yes,
        reached,
        required,
        confidence,
        decision,
        reasons,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Live latch + reflect → L2 → backtest → deploy (wired from the trade loop)
# ═══════════════════════════════════════════════════════════════════════════


def _load_reflection_latches(bot: str = "forex") -> dict:
    path = reflection_latch_path(bot)
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _save_reflection_latches(latches: dict, bot: str = "forex") -> None:
    path = reflection_latch_path(bot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(latches), encoding="utf-8")
    except OSError:
        pass


def _is_reflection_done(pair: str, closed_count: int, bot: str = "forex") -> bool:
    """True if we already reflected at this exact closed-trade count for `pair`."""
    entry = _load_reflection_latches(bot).get(pair)
    if entry is None:
        return False
    return entry.get("reflected_count") == closed_count


def _mark_reflection_done(pair: str, closed_count: int, bot: str = "forex") -> None:
    latches = _load_reflection_latches(bot)
    latches[pair] = {"reflected_count": closed_count}
    _save_reflection_latches(latches, bot)


def strategy_yaml_path(pair: str, bot: str = "forex") -> Path:
    """Canonical per-pair strategy file on the runtime volume."""
    from hermes_core.config.loader import strategy_yaml_path as _live

    return _live(pair, bot)


def _set_strategy_param(strategy: dict, variable: str, value) -> None:
    """Set a top-level or dotted param (e.g. entry.threshold) on a strategy dict."""
    if "." in variable:
        parts = variable.split(".")
        cur = strategy
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = value
    else:
        strategy[variable] = value


def apply_strategy_change(
    pair: str,
    variable: str,
    new_val,
    *,
    bot: str = "forex",
    version: str | None = None,
    strategy: dict | None = None,
) -> dict:
    """Atomically write the approved param (+ version) to the pair strategy YAML.

    Returns the written strategy dict. Validates ranges before writing; raises
    on validation failure so callers can refuse a bad deploy.
    """
    import copy

    import yaml

    from hermes_core.config import validate_strategy_params

    strat = copy.deepcopy(strategy if strategy is not None else load_strategy_for_pair(pair, bot))
    _set_strategy_param(strat, variable, new_val)
    if version is not None:
        strat["version"] = str(version)
    elif "version" not in strat:
        strat["version"] = "01"
    validate_strategy_params(strat, raise_on_fail=True)

    path = strategy_yaml_path(pair, bot)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(strat, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    return strat


def _build_l2_context(
    pair: str,
    bot: str,
    *,
    chart_context: str = "",
    skipped_json: str = "",
    variable: str = "",
) -> str:
    """Assemble a variable-aware critic brief for L2.

    Evidence is chosen by proposal axis so small models are not drowned in
    unrelated skips/chart. Hard caps keep prompts efficient.
    """
    kind = _l2_axis_kind(variable)
    parts: list[str] = []

    if chart_context:
        cap = 120 if kind == "trail" else 200
        parts.append(f"CHART: {chart_context[:cap]}")

    with contextlib.suppress(Exception):
        from hermes_core.engines.decision_cortex import Cortex

        cx = Cortex(bot=bot)
        summ = cx.summary() or {}
        s = summ.get("summary", {})
        type_wr = summ.get("type_wr", {})
        wr_compact = {
            k: (round(v, 2) if isinstance(v, (int, float)) else v)
            for k, v in (type_wr or {}).items()
        }
        parts.append(
            "CORTEX: best_entry="
            f"{s.get('best_entry_type')} exiled={s.get('exiled_indicators')} "
            f"type_wr={wr_compact}"
        )
        recent = cx.recent_hypotheses(pair=pair, limit=6) or []
        axis = _l2_axis_kind(variable)
        same = [h for h in recent if _l2_axis_kind(str(h.get("variable") or "")) == axis]
        picked = (same or recent)[:2]
        if picked:
            hist = "; ".join(
                f"{h.get('variable')} {h.get('old')}->{h.get('new')} [{h.get('status')}]"
                for h in picked
            )
            parts.append(f"LAST_HYPOTHESES: {hist}")

    # Skips matter for entry filters; omit noisy skip dumps for exit/trail axes.
    if skipped_json and kind in ("entry", "generic"):
        parts.append(f"SKIPS: {skipped_json[:120]}")

    return " | ".join(parts) if parts else chart_context


def run_reflection_pipeline(
    pair: str,
    trades: list[dict],
    *,
    bot: str = "forex",
    goal: dict | None = None,
    strategy: dict | None = None,
    chart_context: str = "",
    prices: list[float] | None = None,
    fetch_prices=None,
    llm_callers: dict | None = None,
    auto_deploy: bool = True,
    skipped_json: str = "",
) -> dict:
    """L1 → (optional L2) → backtest → deploy. Returns a status dict.

    Gate rules (roadmap S11):
      * score < 65  → L2 skipped; L1 proposal proceeds to backtest on its own.
      * score ≥ 65  → L2 consensus required before backtest.
      * backtest approve + auto_deploy → write strategy YAML + version bump.
    """
    from hermes_core.engines.backtest import backtest_with_history

    if goal is None:
        goal = (load_config(bot) or {}).get("goal", {})
    if strategy is None:
        strategy = load_strategy_for_pair(pair, bot)

    proposals = combined_reflect(
        pair,
        trades,
        goal=goal,
        chart_context=chart_context,
        skipped_json=skipped_json,
        strategy=strategy,
        bot=bot,
    )
    if not proposals:
        return {"status": "no_proposal", "pair": pair, "deployed": False}

    prop = proposals[0]
    score = float(prop.get("confidence", 0.0)) * 100.0
    # Allow an explicit numeric score on the proposal (tests / L2 escalation).
    if "score" in prop:
        score = float(prop["score"])

    l2_min, l2_uni = float(L2_MIN_SCORE), float(L2_UNANIMOUS_SCORE)
    with contextlib.suppress(Exception):
        from hermes_core.engines.adaptive import adaptive_l2_thresholds

        l2_min, l2_uni = adaptive_l2_thresholds(bot, pair)

    if score >= l2_min:
        critic_context = _build_l2_context(
            pair,
            bot,
            chart_context=chart_context,
            skipped_json=skipped_json,
            variable=str(prop.get("variable") or ""),
        )
        cons = call_llm_consensus(
            prop,
            context=critic_context,
            score=score,
            confidence=float(prop.get("confidence", 0.0)),
            callers=llm_callers,
            bot=bot,
            min_score=l2_min,
            uni_score=l2_uni,
        )
        _log_hypothesis(
            {
                **{k: prop.get(k) for k in ("pair", "bot", "variable", "old", "new")},
                "status": "l2_approved" if cons.decision else "l2_rejected",
                "l2": cons.to_dict(),
                "ts": __import__("time").time(),
            }
        )
        if not cons.decision:
            with contextlib.suppress(Exception):
                from hermes_core.engines import experiment_control as _exp
                from hermes_core.engines.live_verdict import dominant_regime

                _exp.record_pipeline_outcome(
                    bot,
                    pair,
                    variable=str(prop.get("variable")),
                    status="l2_reject",
                    old=prop.get("old"),
                    new=prop.get("new"),
                    regime=dominant_regime(trades),
                    reason="l2_consensus_rejected",
                )
            return {
                "status": "l2_reject",
                "pair": pair,
                "deployed": False,
                "proposal": prop,
                "l2": cons.to_dict(),
            }

    # Phase 1: reflection deploy proof is STRICT by default — a candidate must
    # strictly beat the last version on real data (escape hatch: REFLECT_STRICT=0).
    strict = __import__("os").getenv("REFLECT_STRICT", "1") != "0"
    kwargs = {
        "strategy": strategy,
        "prices": prices,
        "bot": bot,
        "strict": strict,
    }
    if fetch_prices is not None:
        kwargs["fetch_prices"] = fetch_prices
    try:
        from hermes_core.engines.cost_model import round_trip_pct, stress_mult

        kwargs["cost_pct"] = round_trip_pct(pair)
        kwargs["cost_stress_mult"] = 1.0  # primary gate at 1×; stress logged separately
        _stress = round_trip_pct(pair, stressed=True)
    except Exception:  # noqa: BLE001
        _stress = None
    verdict = backtest_with_history(
        pair,
        prop["variable"],
        prop["old"],
        prop["new"],
        **kwargs,
    )
    if _stress is not None and isinstance(verdict, dict):
        try:
            from hermes_core.engines.cost_model import estimate, stress_mult

            verdict["cost_model"] = estimate(pair).as_dict()
            stress_v = backtest_with_history(
                pair,
                prop["variable"],
                prop["old"],
                prop["new"],
                **{**kwargs, "cost_pct": round_trip_pct(pair), "cost_stress_mult": stress_mult()},
            )
            verdict["cost_stress"] = {
                "approved": stress_v.get("approved"),
                "reason": stress_v.get("reason"),
                "stressed_round_trip_pct": _stress,
            }
            # BTC/USDT Focus: fail reflect deploy if 2× cost stress does not pass.
            if verdict.get("approved") and not stress_v.get("approved"):
                verdict["approved"] = False
                verdict["reason"] = (
                    f"cost_stress_failed:{stress_v.get('reason') or '2x_cost'}"
                )
        except Exception:  # noqa: BLE001
            pass

    _log_hypothesis(
        {
            **{k: prop.get(k) for k in ("pair", "bot", "variable", "old", "new")},
            "status": "backtest_approved" if verdict.get("approved") else "backtest_rejected",
            "backtest": {
                "approved": verdict.get("approved"),
                "reason": verdict.get("reason"),
                "version_bumped": (verdict.get("phases") or {})
                .get("phase6_deploy", {})
                .get("version_bumped"),
                # Phase 1.5 provenance: prove it on the record.
                "strict": verdict.get("strict"),
                "old_pnl": verdict.get("old_pnl"),
                "new_pnl": verdict.get("new_pnl"),
                "improvement_full": verdict.get("improvement_full"),
                "improvement_oos": verdict.get("improvement_oos"),
                "data_bars": verdict.get("data_bars"),
                "version_from": verdict.get("version_from"),
                "version_to": verdict.get("version_to"),
            },
            "ts": __import__("time").time(),
        }
    )
    if not verdict.get("approved"):
        with contextlib.suppress(Exception):
            from hermes_core.engines import experiment_control as _exp
            from hermes_core.engines.live_verdict import dominant_regime

            _exp.record_pipeline_outcome(
                bot,
                pair,
                variable=str(prop.get("variable")),
                status="backtest_reject",
                old=prop.get("old"),
                new=prop.get("new"),
                regime=dominant_regime(trades),
                reason=str(verdict.get("reason") or "backtest_rejected"),
            )
        return {
            "status": "backtest_reject",
            "pair": pair,
            "deployed": False,
            "proposal": prop,
            "verdict": verdict,
        }

    # Profitability Path Phase 3 — verifier gates (tunables, min trades, OOS, MDD).
    with contextlib.suppress(Exception):
        from hermes_core.engines.reflect_verifier import (
            record_verifier_reject,
            verify_reflection_candidate,
        )

        vcheck = verify_reflection_candidate(
            pair=pair,
            proposal=prop,
            verdict=verdict,
            trades=trades,
            bot=bot,
        )
        if not vcheck.get("ok"):
            record_verifier_reject(
                bot,
                pair,
                proposal=prop,
                reason=str(vcheck.get("reason") or "verifier_reject"),
                details=vcheck.get("details") if isinstance(vcheck.get("details"), dict) else {},
            )
            _log_hypothesis(
                {
                    **{k: prop.get(k) for k in ("pair", "bot", "variable", "old", "new")},
                    "status": "verifier_reject",
                    "reason": vcheck.get("reason"),
                    "details": vcheck.get("details"),
                    "ts": __import__("time").time(),
                }
            )
            return {
                "status": "verifier_reject",
                "pair": pair,
                "deployed": False,
                "proposal": prop,
                "verdict": verdict,
                "verifier": vcheck,
            }

    bumped = (verdict.get("phases") or {}).get("phase6_deploy", {}).get("version_bumped")

    # Phase 5.3 staged deploy + Phase 5.2 cooldown.
    #   auto_deploy False → pending
    #   stage == prove    → pending (shadow even if auto_deploy True)
    #   cooldown/quiet    → pending
    #   canary/full       → live write (+ canary size_down)
    stage = "full"
    with contextlib.suppress(Exception):
        from hermes_core.engines import experiment_control as _exp

        stage = _exp.get_deploy_stage(bot)
    if not auto_deploy or stage == "prove":
        pending_reason = (
            "stage_prove_shadow_only" if stage == "prove" and auto_deploy else "auto_deploy_off"
        )
        with contextlib.suppress(Exception):
            from hermes_core.engines import experiment_control as _exp

            _exp.record_shadow_challenger(
                bot,
                pair,
                variable=str(prop.get("variable")),
                old=prop.get("old"),
                new=prop.get("new"),
                reason=pending_reason,
                backtest=verdict,
                version=bumped,
            )
        _log_hypothesis(
            {
                **{k: prop.get(k) for k in ("pair", "bot", "variable", "old", "new")},
                "status": "approved_pending_deploy",
                "version": bumped,
                "deploy_stage": stage,
                "reason": pending_reason,
                "deployable": True,
                "ts": __import__("time").time(),
            }
        )
        return {
            "status": "approved_pending_deploy",
            "pair": pair,
            "deployed": False,
            "proposal": prop,
            "verdict": verdict,
            "version": bumped,
            "deploy_stage": stage,
            "reason": pending_reason,
        }

    closed_now = 0
    with contextlib.suppress(Exception):
        closed_now = len(_closed_trades_for_pair(bot, pair))
    block = None
    with contextlib.suppress(Exception):
        from hermes_core.engines import experiment_control as _exp

        block = _exp.deploy_blocked(bot, pair, closed_count=closed_now)
    if block:
        _log_hypothesis(
            {
                **{k: prop.get(k) for k in ("pair", "bot", "variable", "old", "new")},
                "status": "deploy_cooldown",
                "reason": block.get("reason"),
                "block": block,
                "ts": __import__("time").time(),
            }
        )
        return {
            "status": "approved_pending_deploy",
            "pair": pair,
            "deployed": False,
            "proposal": prop,
            "verdict": verdict,
            "version": bumped,
            "deploy_stage": stage,
            "cooldown": block,
        }

    if stage == "canary":
        with contextlib.suppress(Exception):
            from hermes_core.engines import experiment_control as _exp

            if _exp.pair_safe_mode(bot, pair) is None:
                _exp.set_safe_mode(bot, pair, "size_down", "canary_deploy")

    import copy as _copy

    prior_strategy = _copy.deepcopy(strategy)
    written = apply_strategy_change(
        pair,
        prop["variable"],
        prop["new"],
        bot=bot,
        version=bumped,
        strategy=strategy,
    )
    _log_hypothesis(
        {
            **{k: prop.get(k) for k in ("pair", "bot", "variable", "old", "new")},
            "status": "deployed",
            "version": written.get("version"),
            "deploy_stage": stage,
            "ts": __import__("time").time(),
        }
    )
    with contextlib.suppress(Exception):
        from hermes_core.engines import experiment_control as _exp

        _exp.record_deployment(
            bot,
            pair,
            variable=prop["variable"],
            old=prop["old"],
            new=prop["new"],
            version_from=str(prior_strategy.get("version", "00")),
            version_to=written.get("version"),
            prior_strategy=prior_strategy,
            closed_count=closed_now,
        )
        _exp.clear_shadow_challenger(bot, pair)
    return {
        "status": "deployed",
        "pair": pair,
        "deployed": True,
        "proposal": prop,
        "verdict": verdict,
        "version": written.get("version"),
        "strategy": written,
        "deploy_stage": stage,
    }


def maybe_reflect_pair(
    bot: str,
    pair: str,
    *,
    goal: dict | None = None,
    chart_context: str = "",
    prices: list[float] | None = None,
    fetch_prices=None,
    llm_callers: dict | None = None,
    auto_deploy: bool = True,
) -> dict | None:
    """Fire reflection when closed-count hits reflection_every and latch is clear.

    Returns the pipeline result dict, or None if cadence/latch skipped the run.
    Always fail-soft at the caller — this function may raise only on logic bugs;
    I/O errors inside the pipeline are converted to status dicts where possible.
    """
    if goal is None:
        goal = (load_config(bot) or {}).get("goal", {})
    every = int(goal.get("reflection_every", 5) or 5)
    if every < 1:
        every = 5

    closed = _closed_trades_for_pair(bot, pair)
    total = len(closed)

    # #2: cadence adapts to noise / recent failure rate (prior = goal every).
    with contextlib.suppress(Exception):
        from hermes_core.engines.adaptive import adaptive_reflection_every

        every = adaptive_reflection_every(
            bot,
            pair,
            every,
            pnls=[float(t.get("pnl_pct", 0.0)) for t in closed if t.get("pnl_pct") is not None],
        )

    # Phase 3.1: judge any live experiment on EVERY close (independent of the
    # propose cadence) so a worsening version is reverted as soon as it has
    # enough evidence. Fail-soft — never break the loop on experiment control.
    revert_result: dict | None = None
    with contextlib.suppress(Exception):
        from hermes_core.engines import experiment_control as _exp

        rr = _exp.maybe_auto_revert(bot, pair)
        if rr.get("status") in ("reverted", "improved"):
            revert_result = rr

    # Phase 4.5: after a GP admit on a handoff pair, force an early reflection
    # pass so risk params can be retuned for the new entry behaviour.
    force_retune = False
    with contextlib.suppress(Exception):
        from hermes_core.engines import experiment_control as _exp

        if _exp.pending_reflection_retune(bot, pair) and total > 0:
            force_retune = True
            _exp.consume_reflection_retune(bot, pair)

    if not force_retune and (total <= 0 or total % every != 0):
        if revert_result is not None:
            return {
                "status": revert_result["status"],
                "pair": pair,
                "closed": total,
                "deployed": False,
                "experiment": revert_result,
            }
        return None
    if not force_retune and _is_reflection_done(pair, total, bot):
        result = {"status": "latched", "pair": pair, "closed": total, "deployed": False}
        if revert_result is not None:
            result["experiment"] = revert_result
        return result

    batch, batch_regime = same_regime_batch(closed, every)
    if len(batch) < MIN_SAMPLE and not force_retune:
        # Not enough same-regime evidence yet — wait for a cleaner batch.
        result = {
            "status": "no_proposal",
            "pair": pair,
            "closed": total,
            "deployed": False,
            "reason": "insufficient_same_regime_sample",
            "regime": batch_regime,
            "batch_n": len(batch),
        }
        if revert_result is not None:
            result["experiment"] = revert_result
        _mark_reflection_done(pair, total, bot)
        return result
    skipped_json = ""
    try:
        from hermes_core.engines.skip_shadow_learn import (
            analyze_skip_shadow,
            format_skip_shadow_context,
            load_pair_shadow,
            load_pair_skips,
            skip_shadow_reflect_enabled,
        )

        if skip_shadow_reflect_enabled():
            analysis = analyze_skip_shadow(
                load_pair_skips(bot, pair),
                load_pair_shadow(bot, pair),
            )
            skipped_json = format_skip_shadow_context(analysis)
    except Exception:  # noqa: BLE001
        skipped_json = ""

    try:
        result = run_reflection_pipeline(
            pair,
            batch,
            bot=bot,
            goal=goal,
            chart_context=chart_context,
            prices=prices,
            fetch_prices=fetch_prices,
            llm_callers=llm_callers,
            auto_deploy=auto_deploy,
            skipped_json=skipped_json,
        )
    except Exception as exc:  # noqa: BLE001 — never break the trade loop
        result = {
            "status": "error",
            "pair": pair,
            "deployed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    _mark_reflection_done(pair, total, bot)
    result["closed"] = total
    result["reflection_every"] = every
    if skipped_json:
        result["skip_context"] = skipped_json
    if revert_result is not None:
        result["experiment"] = revert_result
    if force_retune:
        result["retune_after_gp"] = True
    if batch_regime:
        result["regime"] = batch_regime
    return result


# ═══════════════════════════════════════════════════════════════════════════
# Reflection health / status taxonomy (Phase 0.2 / 0.3)
# ═══════════════════════════════════════════════════════════════════════════
#
# NOTE ON THE DASHBOARD "VERSIONS" TAB (read this before debugging "no new
# version every 5 trades"): the Reports → Versions tab groups CLOSED TRADES by
# the ``strategy_version`` string that was stamped on each trade AT OPEN TIME.
# It is NOT a live view of the reflection pipeline. A reflection run can fire,
# propose, pass L2, and pass backtest yet still NOT create a new version when
# ``REFLECT_AUTO_DEPLOY=0`` (soak default) — it stops at ``approved_pending_deploy``.
# Use ``reflection_health`` (below) — not the Versions tab — to answer
# "did reflection fire / prove / deploy?".

# Terminal statuses returned by the pipeline, grouped for the dashboard.
REFLECTION_STATUSES = {
    # Reflection fired but produced no change (healthy non-event).
    "no_proposal": "no_proposal",
    # Rejected before deploy.
    "l2_reject": "rejected",
    "backtest_reject": "rejected",
    "error": "error",
    # Proven but intentionally not deployed (auto-deploy off) — soak SUCCESS.
    "approved_pending_deploy": "proven",
    # Live version written.
    "deployed": "deployed",
    # Cadence/latch bookkeeping (not a real reflection run).
    "latched": "latched",
}

# Statuses that mean "reflection worked as intended" during a soak. A proven
# proposal that is intentionally not deployed (REFLECT_AUTO_DEPLOY=0) counts as
# success — it is the whole point of shadow-proving before live deploy.
SOAK_SUCCESS_STATUSES = frozenset({"approved_pending_deploy", "deployed"})


def status_class(status: str | None) -> str:
    """Map a raw pipeline status to a coarse class for the dashboard."""
    return REFLECTION_STATUSES.get(status or "", "unknown")


def is_soak_success(status: str | None) -> bool:
    """True if ``status`` means reflection proved a change (deployed or pending)."""
    return status in SOAK_SUCCESS_STATUSES


def _closed_trades_for_pair(bot: str, pair: str) -> list[dict]:
    """Closed trades for ``pair`` as reflection counts them (single source of truth).

    A close is any row carrying ``exit_reason``/``reason`` or a ``pnl_pct`` and
    matching ``pair``. Read from the SAME file the trade loop appends to
    (``append_trade`` → ``bot_state_dir(bot)/trades.jsonl``) so the health view
    and the live cadence can never disagree.

    Uses the process-local trades cache (#8) so health polls / experiment eval
    don't re-parse the whole jsonl on every call.
    """
    with contextlib.suppress(Exception):
        from hermes_core.engines.trades_cache import closed_trades

        return closed_trades(bot, pair)
    from hermes_core.state.paths import bot_state_dir

    trades_path = bot_state_dir(bot) / "trades.jsonl"
    closed: list[dict] = []
    if not trades_path.exists():
        return closed
    try:
        for line in trades_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if rec.get("pair") != pair:
                continue
            if rec.get("orphan"):
                continue
            if rec.get("exit_reason") or rec.get("reason") or "pnl_pct" in rec:
                closed.append(rec)
    except (OSError, json.JSONDecodeError):
        return []
    return closed


def _last_hypothesis_for_pair(bot: str, pair: str) -> dict | None:
    """Most recent hypotheses.jsonl record for ``pair`` (None if never reflected)."""
    path = hypotheses_path(bot)
    if not path.exists():
        return None
    last: dict | None = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("pair") == pair:
                last = rec
    except OSError:
        return None
    return last


# Strategy knobs the Reflections ledger surfaces per pair (risk / exit / size).
_REFLECTION_KNOB_KEYS = (
    "version",
    "strategy_type",
    "stop_loss_pct",
    "trailing_stop_pct",
    "profit_target_pct",
    "position_size_r",
    "time_exit_cycles",
    "rsi_entry",
    "rsi_exit",
    "session_filter",
)


def _strategy_knobs(bot: str, pair: str) -> dict:
    """Current live YAML knobs for ``pair`` (fail-soft → {})."""
    out: dict = {}
    with contextlib.suppress(Exception):
        strat = load_strategy_for_pair(pair, bot) or {}
        for k in _REFLECTION_KNOB_KEYS:
            if k in strat and strat[k] is not None:
                out[k] = strat[k]
        # Keep any other numeric risk-ish fields operators might have tuned.
        for k, v in strat.items():
            if k in out or k.startswith("_"):
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = v
    return out


def _recent_hypotheses_for_pair(bot: str, pair: str, *, limit: int = 40) -> list[dict]:
    """Newest-last hypothesis rows for one pair (timeline for Reflections tab)."""
    path = hypotheses_path(bot)
    if not path.exists():
        return []
    rows: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("pair") != pair:
                continue
            rows.append(
                {
                    "ts": rec.get("ts"),
                    "status": rec.get("status"),
                    "variable": rec.get("variable"),
                    "old": rec.get("old"),
                    "new": rec.get("new"),
                    "reason": rec.get("reason"),
                    "confidence": rec.get("confidence"),
                    "version": rec.get("version"),
                }
            )
    except OSError:
        return []
    return rows[-limit:]


def reflection_health(bot: str, pairs: list[str] | None = None, *, goal: dict | None = None) -> dict:
    """Per-pair reflection health + full ledger snapshot for the Reflections tab.

    Pure reads; never raises. Includes cadence, live experiment, champion/explore,
    cooldowns, plans, shadow, strategy knobs, adaptive learning, and a short
    hypothesis timeline so operators can see everything a pair has gone through.
    """
    import os

    if goal is None:
        goal = (load_config(bot) or {}).get("goal", {})
    every = int((goal or {}).get("reflection_every", 5) or 5)
    if every < 1:
        every = 5

    if pairs is None:
        cfg = load_config(bot) or {}
        pairs = list(cfg.get("pairs") or [])

    auto_deploy = os.getenv("REFLECT_AUTO_DEPLOY", "0") != "0"
    latches = _load_reflection_latches(bot)

    exp_summary: dict = {}
    exp_history: list = []
    with contextlib.suppress(Exception):
        from hermes_core.engines import experiment_control as _exp

        summ = _exp.experiments_summary(bot, pairs)
        exp_summary = summ.get("pairs", {})
        exp_history = list(summ.get("history") or [])

    out_pairs: dict[str, dict] = {}
    for pair in pairs:
        closed = _closed_trades_for_pair(bot, pair)
        total = len(closed)
        latch_entry = latches.get(pair) or {}
        latched_at = latch_entry.get("reflected_count")
        if total > 0 and total % every == 0 and latched_at != total:
            next_fire_at = total
        else:
            next_fire_at = ((total // every) + 1) * every
        last_hyp = _last_hypothesis_for_pair(bot, pair)
        last_status = last_hyp.get("status") if last_hyp else None
        info = exp_summary.get(pair) or {}
        out_pairs[pair] = {
            "closed": total,
            "reflection_every": every,
            "next_fire_at": next_fire_at,
            "trades_until_next": max(0, next_fire_at - total),
            "latched_at": latched_at,
            "last_status": last_status,
            "last_status_class": status_class(last_status),
            "last_reason": (last_hyp or {}).get("reason"),
            "last_ts": (last_hyp or {}).get("ts"),
            "proven": is_soak_success(last_status),
            "experiment": info.get("experiment"),
            "champion_status": info.get("champion_status"),
            "champion_version": info.get("champion_version"),
            "revert_count": info.get("revert_count", 0),
            "safe_mode": info.get("safe_mode"),
            "safe_mode_reason": info.get("safe_mode_reason"),
            "cooldown_axes": info.get("cooldown_axes", []),
            "axis_cooldown": info.get("axis_cooldown", {}),
            "direction_cooldown": info.get("direction_cooldown", {}),
            "gp_handoff": info.get("gp_handoff", False),
            "gp_handoff_reason": info.get("gp_handoff_reason"),
            "gp_handoff_variable": info.get("gp_handoff_variable"),
            "plan": info.get("plan"),
            "plan_reason": info.get("plan_reason"),
            "shadow": info.get("shadow"),
            "explore": info.get("explore", False),
            "explore_reason": info.get("explore_reason"),
            "deploy_cooldown": info.get("deploy_cooldown"),
            "strategy": _strategy_knobs(bot, pair),
            "timeline": _recent_hypotheses_for_pair(bot, pair, limit=40),
        }
        with contextlib.suppress(Exception):
            from hermes_core.engines.adaptive import summary as _adapt_summary

            learned = _adapt_summary(bot, pair)
            out_pairs[pair]["learned_axes"] = learned.get("axes", {})
            out_pairs[pair]["pathology_bars"] = adaptive_bars(bot, pair, closed)
            out_pairs[pair]["cadence"] = learned.get("cadence", {})

    deploy_stage = "full"
    with contextlib.suppress(Exception):
        from hermes_core.engines.experiment_control import get_deploy_stage

        deploy_stage = get_deploy_stage(bot)

    adaptive_state: dict = {}
    with contextlib.suppress(Exception):
        from hermes_core.engines.adaptive import summary as _adapt_summary

        adaptive_state = _adapt_summary(bot)

    pending_deploys: list[dict] = []
    with contextlib.suppress(Exception):
        from hermes_core.engines.experiment_control import list_pending_deploys

        pending_deploys = list_pending_deploys(bot, pairs)

    return {
        "bot": bot,
        "auto_deploy": auto_deploy,
        "reflection_every": every,
        "deploy_stage": deploy_stage,
        "pairs": out_pairs,
        "adaptive": adaptive_state,
        "history": [
            h for h in exp_history if not pairs or h.get("pair") in set(pairs)
        ][-40:],
        "gp_handoff_pairs": [
            p for p, info in out_pairs.items() if info.get("gp_handoff")
        ],
        "pending_deploys": pending_deploys,
    }
