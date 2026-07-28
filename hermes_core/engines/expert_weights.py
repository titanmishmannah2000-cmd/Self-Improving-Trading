"""HIF Phase-2 — Bayesian soft expert weights (Profitability Path Phase 2).

When ``SOFT_WEIGHTS=1``:
  * ``evidence_n < MIN_N`` → passthrough weight 1.0 (no size change on thin data)
  * else Beta(1+wins, 1+losses) posterior → size weight from E[WR]
  * soft L35 suppress still multiplies
  * retire (floor weight) when P(WR < breakeven) > RETIRE_PROB

When the flag is off, callers keep hard ``policy.is_suppressed`` skips.
Pure helpers — no I/O except optional weight-update jsonl.
"""

from __future__ import annotations

import json
import math
import threading
import time

# Types the meta-allocator knows about (momentum included for dashboard).
EXPERT_TYPES = ("mean_reversion", "rsi_momentum", "gp_ensemble")

SOFT_SUPPRESS_MULT = 0.25  # L35 "bench" → 25% size, still allow entry
MIN_N = 15  # closed outcomes before any non-passthrough weight
EXPLORE_FLOOR = 0.40  # legacy name kept for imports; used post-min_n only
EXPLORE_MIN_N = MIN_N  # alias — thin evidence now means passthrough
MIN_WEIGHT = 0.05  # absolute floor — never zero (no hard ban in soft mode)
BREAKEVEN_WR = 0.45  # P(WR < this) triggers retire when high
RETIRE_PROB = 0.80  # retire if P(WR < breakeven) exceeds this
PRIOR_A = 1.0  # Beta prior wins
PRIOR_B = 1.0  # Beta prior losses

_WEIGHT_LOG_LOCK = threading.Lock()
_WEIGHT_LOG_NAME = "expert_weights.jsonl"


def _ienv(name: str, default: int) -> int:
    import os

    raw = os.environ.get(name, "")
    if not str(raw).strip():
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def min_n() -> int:
    return max(1, _ienv("EXPERT_WEIGHT_MIN_N", MIN_N))


def _beta_cdf_below(a: float, b: float, x: float, *, n_steps: int = 200) -> float:
    """P(Beta(a,b) < x) via trapezoid on the density (no scipy)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    if a <= 0 or b <= 0:
        return 0.5
    # log B(a,b) = lgamma(a)+lgamma(b)-lgamma(a+b)
    try:
        log_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    except ValueError:
        return 0.5
    steps = max(20, int(n_steps))
    dx = x / steps
    acc = 0.0
    for i in range(steps + 1):
        t = i * dx
        if t <= 0.0 or t >= 1.0:
            dens = 0.0
        else:
            dens = math.exp((a - 1.0) * math.log(t) + (b - 1.0) * math.log(1.0 - t) - log_beta)
        w = 0.5 if i in (0, steps) else 1.0
        acc += w * dens
    return max(0.0, min(1.0, acc * dx))


def beta_posterior(
    wins: int,
    losses: int,
    *,
    prior_a: float = PRIOR_A,
    prior_b: float = PRIOR_B,
) -> tuple[float, float, float, float]:
    """Return (alpha, beta, mean_wr, p_below_breakeven)."""
    a = float(prior_a) + max(0, int(wins))
    b = float(prior_b) + max(0, int(losses))
    mean = a / (a + b)
    p_bad = _beta_cdf_below(a, b, BREAKEVEN_WR)
    return a, b, mean, p_bad


def _wins_losses_from_wr(evidence_n: int, wr: float | None) -> tuple[int, int]:
    n = max(0, int(evidence_n))
    if wr is None or n <= 0:
        return 0, 0
    w = int(round(float(wr) * n))
    w = max(0, min(n, w))
    return w, n - w


def expert_weight(
    *,
    enabled: bool,
    suppressed: bool,
    evidence_n: int | None = None,
    wr: float | None = None,
    soft_suppress_mult: float = SOFT_SUPPRESS_MULT,
    explore_floor: float = EXPLORE_FLOOR,
    explore_min_n: int | None = None,
    wins: int | None = None,
    losses: int | None = None,
) -> dict:
    """Compute a single expert's size weight in (MIN_WEIGHT, 1.0].

    ``enabled=False`` → weight 1.0 (legacy full size; hard suppress elsewhere).
    Thin evidence (n < min_n) → passthrough 1.0 even when enabled.
    """
    if not enabled:
        return {
            "weight": 1.0,
            "mode": "disabled",
            "suppressed_soft": False,
            "evidence_n": evidence_n,
            "wr": wr,
            "retired": False,
            "p_below_be": None,
            "reasons": ["disabled"],
        }

    need = int(explore_min_n) if explore_min_n is not None else min_n()
    n = None
    if evidence_n is not None:
        try:
            n = int(evidence_n)
        except (TypeError, ValueError):
            n = None

    if n is None or n < need:
        return {
            "weight": 1.0,
            "mode": "passthrough",
            "suppressed_soft": False,
            "evidence_n": n,
            "wr": wr,
            "retired": False,
            "p_below_be": None,
            "reasons": ["passthrough_thin_evidence"],
        }

    if wins is None or losses is None:
        wins, losses = _wins_losses_from_wr(n, wr)
    _a, _b, mean_wr, p_bad = beta_posterior(int(wins), int(losses))
    wr = mean_wr

    reasons: list[str] = [f"beta_mean={mean_wr:.3f}", f"p_be={p_bad:.3f}"]
    retired = p_bad >= RETIRE_PROB
    if retired:
        w = float(MIN_WEIGHT)
        reasons.append("retired")
    else:
        # Map mean WR → weight: 0.35 at 0%, 1.0 at 100% (same shape as before).
        w = max(0.35, min(1.0, 0.35 + mean_wr * 0.65))
        # After min_n, allow a soft explore floor only if not retired.
        if w < float(explore_floor):
            w = float(explore_floor)
            reasons.append("explore_floor")

    soft = False
    if suppressed:
        w *= float(soft_suppress_mult)
        soft = True
        reasons.append("soft_suppress")

    w = max(float(MIN_WEIGHT), min(1.0, float(w)))
    return {
        "weight": round(w, 4),
        "mode": "bayesian",
        "suppressed_soft": soft,
        "evidence_n": n,
        "wr": round(float(wr), 4) if wr is not None else None,
        "retired": retired,
        "p_below_be": round(p_bad, 4),
        "reasons": reasons,
    }


def apply_expert_weight(base_size: float, weight_info: dict) -> dict:
    """Scale ``base_size`` by expert weight; return size + metadata for logs/UI."""
    base = float(base_size)
    w = float(weight_info.get("weight", 1.0) or 1.0)
    sized = max(0.0, base * w)
    return {
        "size": sized,
        "base_size": base,
        "expert_weight": w,
        "expert_mode": weight_info.get("mode", "disabled"),
        "suppressed_soft": bool(weight_info.get("suppressed_soft")),
        "expert_reasons": list(weight_info.get("reasons") or []),
        "evidence_n": weight_info.get("evidence_n"),
        "wr": weight_info.get("wr"),
        "retired": bool(weight_info.get("retired")),
    }


def append_weight_update(bot: str, pair: str, allocation: dict[str, dict]) -> None:
    """Append-only log of allocator updates (fail-soft)."""
    try:
        from hermes_core.state.paths import bot_state_dir

        path = bot_state_dir(bot) / _WEIGHT_LOG_NAME
        rec = {
            "ts": time.time(),
            "bot": bot,
            "pair": pair,
            "allocation": allocation,
        }
        line = json.dumps(rec, default=str)
        with _WEIGHT_LOG_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        return


def pair_expert_weights(
    pair: str,
    cortex,
    suppressed_types: set[str] | list[str] | None,
    *,
    enabled: bool,
    bot: str | None = None,
    log: bool = False,
) -> dict[str, dict]:
    """Per-entry-type weight map for one pair (dashboard + policy allocation)."""
    suppressed = set(suppressed_types or ())
    out: dict[str, dict] = {}
    for etype in EXPERT_TYPES:
        n = None
        wr = None
        if cortex is not None:
            try:
                n = int(cortex.evidence_n(pair, etype))
            except Exception:  # noqa: BLE001 — fail-soft
                n = None
            try:
                wr = cortex.entry_type_wr(etype, pair=pair)
            except Exception:  # noqa: BLE001
                wr = None
        out[etype] = expert_weight(
            enabled=enabled,
            suppressed=etype in suppressed,
            evidence_n=n,
            wr=wr,
        )
    if log and bot and enabled:
        append_weight_update(bot, pair, out)
    return out
