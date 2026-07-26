"""Adaptive tuning for the reflection engine — priors that learn from outcomes.

The reflection engine used to be governed by hand-set constants (step sizes,
pathology thresholds, axis priority, cooldown length, evaluation window). Those
numbers were guesses. This module turns each of them into a POSTERIOR: the old
constant becomes a prior, and realized live-experiment outcomes move it.

Governing principle — evidence-weighted blending::

    value = (1 - w) * prior + w * observed        w = n / (n + k)

With no evidence (``n = 0``) every function returns exactly its prior, so the
engine's behaviour is unchanged until it has actually learned something. As
outcomes accumulate the observed term takes over.

What adapts:
  * step size        — bigger for severe pathology + reliable axes, backs off
                       after failures (learning-rate style)
  * pathology bars   — derived from THIS pair's own distribution, not a global
                       "win rate < 30%" rule
  * axis order       — reliability-ranked (bandit-ish credit assignment) instead
                       of a fixed 1..6 priority list
  * cooldown         — exponential backoff on repeated failure, decays on success
  * evaluation window— variance-aware statistical power, not a flat 10 closes
  * confidence blend — recalibrated against whether confident proposals actually
                       improved
  * regime credit    — reliability scoped to the regime the batch was in (#3)
  * pipeline memory  — l2/backtest rejects soft-penalise an axis before live (#4)

What does NOT adapt (deliberate, these are risk-of-ruin guards, not knobs):
  * ``STRATEGY_PARAM_RANGES`` schema bounds
  * the stop-loss floor
  * one-variable-per-fire and the reflection thread cap

Fail-soft: every public function returns its prior on any error.
"""

from __future__ import annotations

import contextlib
import math
import os

# Evidence half-weight: at n == k the observed term carries 50% of the blend.
EVIDENCE_K = float(os.environ.get("REFLECT_ADAPT_K", "6") or 6)
# How hard reliability is allowed to reorder axes (0 disables reordering).
AXIS_REORDER_GAIN = float(os.environ.get("REFLECT_AXIS_REORDER_GAIN", "0.9") or 0.9)
# Bounds on any learned step multiplier.
STEP_SCALE_MIN = 0.25
STEP_SCALE_MAX = 2.5
# Bounds on the learned cooldown multiplier.
COOLDOWN_SCALE_MAX = 8.0
# Soft weight of a pipeline reject relative to a live revert.
PIPELINE_LOSS_WEIGHT = float(os.environ.get("REFLECT_PIPELINE_LOSS_WEIGHT", "0.5") or 0.5)
# How strongly regime-specific evidence overrides the global prior.
REGIME_BLEND_K = float(os.environ.get("REFLECT_REGIME_BLEND_K", "4") or 4)
# How strongly other pairs on the SAME bot warm-start a cold pair (#7).
CROSS_PAIR_K = float(os.environ.get("REFLECT_CROSS_PAIR_K", "8") or 8)
CROSS_PAIR_MAX_W = float(os.environ.get("REFLECT_CROSS_PAIR_MAX_W", "0.35") or 0.35)
# Cadence adaptation bounds (#2).
REFLECT_EVERY_MIN = 3
REFLECT_EVERY_MAX = 20
L2_MIN_FLOOR, L2_MIN_CAP = 55.0, 80.0
L2_UNI_FLOOR, L2_UNI_CAP = 70.0, 90.0
CHAMPION_WINDOW_MIN, CHAMPION_WINDOW_MAX = 8, 40
DEPLOY_COOLDOWN_MIN_S, DEPLOY_COOLDOWN_MAX_S = 6 * 3600, 3 * 86400


def evidence_weight(n: int | float, k: float = EVIDENCE_K) -> float:
    """Blend weight for ``n`` observations. 0 at no evidence → 1 asymptotically."""
    n = max(0.0, float(n))
    return n / (n + max(1e-9, k))


def blend(prior: float, observed: float | None, n: int | float, k: float = EVIDENCE_K) -> float:
    """Evidence-weighted blend of a prior and an observation."""
    if observed is None or n <= 0:
        return float(prior)
    w = evidence_weight(n, k)
    return (1.0 - w) * float(prior) + w * float(observed)


# ── outcome history ─────────────────────────────────────────────────────────
def _history(bot: str) -> list[dict]:
    """Closed live experiments (newest last). Empty on any read failure."""
    with contextlib.suppress(Exception):
        from hermes_core.engines import experiment_control as ec

        hist = ec._load(bot, ec._EXPERIMENTS).get("_history")
        if isinstance(hist, list):
            return [h for h in hist if isinstance(h, dict)]
    return []


def _pipeline(bot: str) -> list[dict]:
    with contextlib.suppress(Exception):
        from hermes_core.engines import experiment_control as ec

        return ec.pipeline_outcomes(bot)
    return []


def _row_regime(h: dict) -> str | None:
    r = h.get("regime")
    if r:
        return str(r).lower()
    v = (h.get("verdict") or {}).get("regime")
    return str(v).lower() if v else None


def axis_outcomes(
    bot: str, pair: str | None = None, *, regime: str | None = None
) -> dict[str, dict]:
    """Per-variable live results: attempts / improved / reverted / recent streak.

    When ``regime`` is set, only rows stamped with that regime are counted for
    the primary stats (regime-conditioned credit, #3). Unscoped rows still
    contribute to ``global_*`` fields used as a hierarchical prior.
    """
    out: dict[str, dict] = {}
    rows = [h for h in _history(bot) if pair is None or h.get("pair") == pair]
    regime_l = str(regime).lower() if regime else None

    def _bucket(var: str) -> dict:
        return out.setdefault(
            var,
            {
                "attempts": 0,
                "improved": 0,
                "reverted": 0,
                "streak": 0,
                "gains": [],
                "global_attempts": 0,
                "global_improved": 0,
                "global_reverted": 0,
                "pipeline_rejects": 0,
            },
        )

    for h in rows:
        var = h.get("variable")
        if not var:
            continue
        st = _bucket(var)
        st["global_attempts"] += 1
        if h.get("status") == "improved":
            st["global_improved"] += 1
        elif h.get("status") == "reverted":
            st["global_reverted"] += 1

        row_reg = _row_regime(h)
        # Regime filter: when asking for a specific regime, skip mismatched
        # stamped rows. Unstamped legacy rows count everywhere (weak prior).
        if regime_l and row_reg and row_reg != regime_l:
            continue

        st["attempts"] += 1
        verdict = h.get("verdict") or {}
        chal = verdict.get("challenger_avg")
        base = verdict.get("baseline")
        if chal is not None and base is not None:
            with contextlib.suppress(TypeError, ValueError):
                st["gains"].append(float(chal) - float(base))
        if h.get("status") == "improved":
            st["improved"] += 1
            st["streak"] = 0
        elif h.get("status") == "reverted":
            st["reverted"] += 1
            st["streak"] += 1

    # Pipeline negative evidence (#4) — soft losses before a live burn.
    for h in _pipeline(bot):
        if pair is not None and h.get("pair") != pair:
            continue
        var = h.get("variable")
        if not var:
            continue
        status = str(h.get("status") or "")
        if "reject" not in status:
            continue
        row_reg = _row_regime(h)
        if regime_l and row_reg and row_reg != regime_l:
            continue
        st = _bucket(var)
        st["pipeline_rejects"] += 1

    return out


def axis_reliability(
    bot: str, pair: str | None, variable: str, *, regime: str | None = None
) -> float:
    """P(this axis improves live) — Beta posterior, regime-aware + pipeline-soft.

    Returns 0.5 with no evidence. With regime evidence, blends regime-specific
    posterior against the global (all-regime) posterior so a cold regime still
    inherits something without erasing RANGE wins when TREND fails.

    Cold pairs (#7) also inherit a WEAK prior from other pairs on the same bot
    (never cross-bot — gold must not learn crypto lessons).
    """
    st = axis_outcomes(bot, pair, regime=regime).get(variable)
    if not st:
        st = {
            "attempts": 0,
            "improved": 0,
            "reverted": 0,
            "pipeline_rejects": 0,
            "global_attempts": 0,
            "global_improved": 0,
            "global_reverted": 0,
        }

    def _beta(wins: float, losses: float) -> float:
        return (1.0 + wins) / (2.0 + wins + losses)

    # Soft-count pipeline rejects as fractional losses.
    pipe_loss = PIPELINE_LOSS_WEIGHT * float(st.get("pipeline_rejects") or 0)
    regime_rel = _beta(float(st["improved"]), float(st["reverted"]) + pipe_loss)

    if not regime or st["attempts"] <= 0:
        if st["attempts"] <= 0 and st.get("global_attempts", 0) <= 0 and pipe_loss <= 0:
            local = 0.5
        elif st["attempts"] <= 0 and pipe_loss > 0:
            local = _beta(0.0, pipe_loss)
        else:
            local = regime_rel
    else:
        global_rel = _beta(
            float(st.get("global_improved") or 0),
            float(st.get("global_reverted") or 0) + pipe_loss,
        )
        local = blend(global_rel, regime_rel, st["attempts"], k=REGIME_BLEND_K)

    # Cross-pair warm-start: only when THIS pair is still thin on evidence.
    pair_n = int(st.get("attempts", 0) or 0) + int(st.get("pipeline_rejects", 0) or 0)
    if pair and pair_n < CROSS_PAIR_K:
        fleet = axis_outcomes(bot, None, regime=regime).get(variable)
        if fleet and int(fleet.get("attempts", 0) or 0) > pair_n:
            # Subtract this pair's own counts if they were included in fleet.
            fleet_imp = max(0, int(fleet["improved"]) - int(st.get("improved", 0) or 0))
            fleet_rev = max(0, int(fleet["reverted"]) - int(st.get("reverted", 0) or 0))
            fleet_n = fleet_imp + fleet_rev
            if fleet_n > 0:
                fleet_rel = _beta(float(fleet_imp), float(fleet_rev))
                # Cap transfer weight so a cold pair never becomes a clone.
                w = min(CROSS_PAIR_MAX_W, evidence_weight(fleet_n, k=CROSS_PAIR_K))
                # Further shrink transfer as the pair itself accumulates evidence.
                w *= 1.0 - evidence_weight(pair_n, k=CROSS_PAIR_K)
                local = (1.0 - w) * local + w * fleet_rel

    return local


# ── adaptive step size ──────────────────────────────────────────────────────
def step_scale(
    bot: str,
    pair: str | None,
    variable: str,
    *,
    effect: float = 0.0,
    severity_gain: float = 0.8,
    regime: str | None = None,
) -> float:
    """Multiplier applied to a base step size."""
    try:
        eff = max(0.0, min(1.0, float(effect)))
        st = axis_outcomes(bot, pair, regime=regime).get(variable) or {}
        n = int(st.get("attempts", 0) or 0) + int(st.get("pipeline_rejects", 0) or 0)
        rel = axis_reliability(bot, pair, variable, regime=regime)

        severity = 1.0 + severity_gain * eff  # 1.0 .. 1.8
        reliability = blend(1.0, 0.5 + rel, n)  # rel 0.5 → 1.0 (neutral)
        backoff = 0.5 ** int(st.get("streak", 0) or 0)

        scale = severity * reliability * backoff
        return max(STEP_SCALE_MIN, min(STEP_SCALE_MAX, scale))
    except Exception:  # noqa: BLE001
        return 1.0


def adaptive_step(
    bot: str,
    pair: str | None,
    variable: str,
    base_step: float,
    *,
    effect: float = 0.0,
    regime: str | None = None,
) -> float:
    """Base step size scaled by learned behaviour for (pair, variable)."""
    return float(base_step) * step_scale(
        bot, pair, variable, effect=effect, regime=regime
    )


# ── adaptive pathology thresholds ───────────────────────────────────────────
def _percentile(values: list[float], q: float) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return float(vals[0])
    pos = max(0.0, min(1.0, q)) * (len(vals) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(vals[lo])
    frac = pos - lo
    return float(vals[lo] * (1 - frac) + vals[hi] * frac)


def adaptive_threshold(
    prior: float,
    samples: list[float],
    *,
    q: float = 0.3,
    floor: float,
    cap: float,
    min_n: int = 8,
) -> float:
    """Pathology bar derived from THIS pair's own distribution."""
    try:
        vals = [float(v) for v in (samples or []) if v is not None]
        if len(vals) < max(1, min_n):
            return float(prior)
        observed = _percentile(vals, q)
        value = blend(prior, observed, len(vals))
        return max(floor, min(cap, value))
    except Exception:  # noqa: BLE001
        return float(prior)


# ── adaptive axis ordering ──────────────────────────────────────────────────
def axis_order_key(
    bot: str,
    pair: str | None,
    priority: int,
    variable: str,
    *,
    regime: str | None = None,
) -> float:
    """Sort key blending the prior priority with learned reliability."""
    try:
        st = axis_outcomes(bot, pair, regime=regime).get(variable) or {}
        n = int(st.get("attempts", 0) or 0) + int(st.get("pipeline_rejects", 0) or 0)
        if n <= 0:
            return float(priority)
        rel = axis_reliability(bot, pair, variable, regime=regime)
        adjust = AXIS_REORDER_GAIN * (rel - 0.5) * 2.0 * evidence_weight(n)
        return float(priority) - adjust
    except Exception:  # noqa: BLE001
        return float(priority)


def sort_candidates(
    bot: str,
    pair: str | None,
    candidates: list[tuple],
    *,
    regime: str | None = None,
) -> list[tuple]:
    """Order ``(priority, variable, ...)`` tuples by learned axis reliability."""
    return sorted(
        candidates,
        key=lambda c: (axis_order_key(bot, pair, c[0], c[1], regime=regime), c[0]),
    )


# ── adaptive cooldown ───────────────────────────────────────────────────────
def adaptive_cooldown(
    bot: str,
    pair: str | None,
    variable: str,
    base_closes: int,
    *,
    regime: str | None = None,
) -> int:
    """Cooldown length with exponential backoff on repeated failures."""
    try:
        st = axis_outcomes(bot, pair, regime=regime).get(variable) or {}
        streak = max(0, int(st.get("streak", 0) or 0))
        scale = min(COOLDOWN_SCALE_MAX, 2.0 ** max(0, streak - 1)) if streak else 1.0
        rel = axis_reliability(bot, pair, variable, regime=regime)
        n = int(st.get("attempts", 0) or 0)
        discount = blend(1.0, 1.5 - rel, n)
        return max(1, int(round(float(base_closes) * scale * discount)))
    except Exception:  # noqa: BLE001
        return int(base_closes)


# ── adaptive evaluation window ──────────────────────────────────────────────
def adaptive_eval_closes(
    base: int,
    pnls: list[float] | None = None,
    *,
    min_closes: int = 3,
    max_closes: int = 60,
) -> int:
    """Closes required to judge an experiment, sized by observed noise."""
    try:
        vals = [float(p) for p in (pnls or []) if p is not None]
        if len(vals) < 4:
            return int(base)
        mean = sum(vals) / len(vals)
        var = sum((v - mean) ** 2 for v in vals) / max(1, len(vals) - 1)
        sigma = math.sqrt(max(0.0, var))
        scale_ref = sum(abs(v) for v in vals) / len(vals)
        if scale_ref <= 1e-9:
            return int(base)
        ratio = sigma / scale_ref
        needed = float(base) * max(0.5, min(4.0, ratio * ratio))
        blended = blend(float(base), needed, len(vals))
        return max(min_closes, min(max_closes, int(round(blended))))
    except Exception:  # noqa: BLE001
        return int(base)


# ── adaptive confidence calibration ─────────────────────────────────────────
DEFAULT_CONF_WEIGHTS = {
    "intercept": 0.30,
    "size": 0.20,
    "effect": 0.20,
    "stability": 0.15,
}


def confidence_weights(bot: str, pair: str | None = None) -> dict:
    """Recalibrate the confidence blend against realized outcomes."""
    w = dict(DEFAULT_CONF_WEIGHTS)
    try:
        rows = [h for h in _history(bot) if pair is None or h.get("pair") == pair]
        n = len(rows)
        if n <= 0:
            return w
        improved = sum(1 for h in rows if h.get("status") == "improved")
        hit_rate = improved / n
        observed = DEFAULT_CONF_WEIGHTS["intercept"] + 0.30 * (hit_rate - 0.5)
        w["intercept"] = max(0.10, min(0.50, blend(w["intercept"], observed, n)))
        return w
    except Exception:  # noqa: BLE001
        return w


def adaptive_reflection_every(
    bot: str,
    pair: str | None,
    base: int,
    *,
    pnls: list[float] | None = None,
) -> int:
    """How often to fire reflection (#2) — grows with noise / recent failure rate.

    Never fires *more* often than the prior solely because the book is quiet —
    quietness is not a reason to thrash. Only evidence of failure / noise stretches
    the interval (think less often, wait for cleaner samples).
    """
    try:
        every = max(1, int(base))
        rows = [h for h in _history(bot) if pair is None or h.get("pair") == pair]
        n = len(rows)
        scale = 1.0
        if n >= 3:
            hit = sum(1 for h in rows if h.get("status") == "improved") / n
            # Lots of reverts → think less often / wait for more evidence.
            if hit < 0.5:
                scale *= 1.0 + 0.5 * (0.5 - hit)
        if pnls and len(pnls) >= 4:
            mean = sum(pnls) / len(pnls)
            var = sum((p - mean) ** 2 for p in pnls) / max(1, len(pnls) - 1)
            sigma = math.sqrt(max(0.0, var))
            ref = sum(abs(p) for p in pnls) / len(pnls) or 1.0
            noise = min(2.0, sigma / ref)
            # Only stretch on noise (never compress below 1.0 from quiet books).
            if noise > 1.0:
                scale *= 0.75 + 0.5 * noise
        needed = float(every) * max(1.0, min(2.0, scale))
        blended = blend(float(every), needed, max(n, len(pnls or [])))
        return max(REFLECT_EVERY_MIN, min(REFLECT_EVERY_MAX, int(round(blended))))
    except Exception:  # noqa: BLE001
        return max(1, int(base))


def adaptive_l2_thresholds(bot: str, pair: str | None = None) -> tuple[float, float]:
    """Adaptive L2 invoke / unanimous bars (#2 / #10 feedback).

    Defaults stay at the legacy (65, 75). If live experiments that passed L2
    keep reverting, bars rise (harder to invoke / need more agreement). If they
    mostly improve, bars ease slightly — never below the floors.
    """
    min_s, uni_s = 65.0, 75.0
    try:
        rows = [h for h in _history(bot) if pair is None or h.get("pair") == pair]
        # Prefer L2-tagged trust stats when present.
        trust_n = 0
        trust_hit = 0.5
        with contextlib.suppress(Exception):
            from hermes_core.engines import experiment_control as ec

            t = ec.l2_trust_summary(bot)
            trust_n = int(t.get("decisions") or 0)
        if trust_n > 0:
            raw_hit = t.get("hit_rate")
            trust_hit = float(raw_hit) if raw_hit is not None else 0.5
        if trust_n <= 0 and rows:
            trust_n = len(rows)
            trust_hit = sum(1 for h in rows if h.get("status") == "improved") / trust_n
        if trust_n <= 0:
            return min_s, uni_s
        # hit_rate 0.5 → no move; 0.0 → +10 pts; 1.0 → -5 pts
        delta = 10.0 * (0.5 - trust_hit)
        if trust_hit > 0.5:
            delta = 5.0 * (0.5 - trust_hit)
        min_s = max(L2_MIN_FLOOR, min(L2_MIN_CAP, blend(65.0, 65.0 + delta, trust_n)))
        uni_s = max(
            L2_UNI_FLOOR,
            min(L2_UNI_CAP, blend(75.0, 75.0 + delta, trust_n)),
            min_s + 5.0,
        )
        return float(min_s), float(uni_s)
    except Exception:  # noqa: BLE001
        return 65.0, 75.0


def adaptive_champion_window(bot: str, pair: str | None, base: int) -> int:
    """Champion comparison window sized by noise (#2)."""
    try:
        rows = [h for h in _history(bot) if pair is None or h.get("pair") == pair]
        gains = []
        for h in rows:
            v = h.get("verdict") or {}
            if v.get("challenger_avg") is not None and v.get("baseline") is not None:
                with contextlib.suppress(TypeError, ValueError):
                    gains.append(abs(float(v["challenger_avg"]) - float(v["baseline"])))
        if len(gains) < 3:
            return int(base)
        mean = sum(gains) / len(gains)
        var = sum((g - mean) ** 2 for g in gains) / max(1, len(gains) - 1)
        sigma = math.sqrt(max(0.0, var))
        scale = 1.0 + min(1.0, sigma / max(mean, 1e-6))
        return max(
            CHAMPION_WINDOW_MIN,
            min(CHAMPION_WINDOW_MAX, int(round(blend(float(base), float(base) * scale, len(gains))))),
        )
    except Exception:  # noqa: BLE001
        return int(base)


def adaptive_deploy_cooldown_s(bot: str, pair: str | None, base_s: int) -> int:
    """Deploy cooldown seconds — longer after repeated reverts (#2)."""
    try:
        rows = [h for h in _history(bot) if pair is None or h.get("pair") == pair]
        recent = rows[-5:]
        if not recent:
            return int(base_s)
        revert_frac = sum(1 for h in recent if h.get("status") == "reverted") / len(recent)
        scale = 1.0 + revert_frac  # 1.0 .. 2.0
        return max(
            DEPLOY_COOLDOWN_MIN_S,
            min(DEPLOY_COOLDOWN_MAX_S, int(round(float(base_s) * scale))),
        )
    except Exception:  # noqa: BLE001
        return int(base_s)


def summary(bot: str, pair: str | None = None, *, regime: str | None = None) -> dict:
    """What the engine has learned so far (dashboard / debugging surface)."""
    axes = axis_outcomes(bot, pair, regime=regime)
    l2_min, l2_uni = adaptive_l2_thresholds(bot, pair)
    return {
        "bot": bot,
        "pair": pair,
        "regime": regime,
        "evidence_k": EVIDENCE_K,
        "confidence_weights": confidence_weights(bot, pair),
        "cadence": {
            "l2_min_score": round(l2_min, 1),
            "l2_unanimous_score": round(l2_uni, 1),
        },
        "axes": {
            var: {
                "attempts": st["attempts"],
                "improved": st["improved"],
                "reverted": st["reverted"],
                "pipeline_rejects": st.get("pipeline_rejects", 0),
                "fail_streak": st["streak"],
                "reliability": round(
                    axis_reliability(bot, pair, var, regime=regime), 3
                ),
                "step_scale": round(step_scale(bot, pair, var, regime=regime), 3),
                "avg_gain": (
                    round(sum(st["gains"]) / len(st["gains"]), 5) if st["gains"] else None
                ),
            }
            for var, st in axes.items()
        },
    }
