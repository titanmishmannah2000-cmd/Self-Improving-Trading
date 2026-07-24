"""HIF Phase-5 — Bayesian fractional Kelly size multiplier.

When ``KELLY_SIZING=1``, scale position size by a quarter-Kelly fraction derived
from a Beta posterior over win-rate (cortex closed outcomes) and payoff odds
(avg win / avg loss, or strategy TP/SL). Never blocks a trade.

Default OFF → multiplier 1.0 (passthrough). No evidence → passthrough (fail-open).
"""

from __future__ import annotations

import math

from hermes_core.env import get_env

PRIOR_ALPHA = 1.0  # Beta(1,1) uniform prior
PRIOR_BETA = 1.0
KELLY_FRACTION = 0.25  # quarter-Kelly safety
MIN_MULT = 0.15  # never starve completely when edge is weak
MAX_MULT = 1.0
REF_KELLY = KELLY_FRACTION  # map f=0.25 → mult≈1.0


def kelly_sizing_enabled() -> bool:
    return get_env("KELLY_SIZING", "0") == "1"


def bayesian_p(
    wins: int, losses: int, alpha: float = PRIOR_ALPHA, beta: float = PRIOR_BETA
) -> float:
    """Posterior mean of Beta(alpha+wins, beta+losses)."""
    w = max(0, int(wins))
    losses_n = max(0, int(losses))
    return (w + float(alpha)) / (w + losses_n + float(alpha) + float(beta))


def bayesian_ci(
    wins: int,
    losses: int,
    *,
    alpha: float = PRIOR_ALPHA,
    beta: float = PRIOR_BETA,
    z: float = 1.645,
) -> tuple[float, float]:
    """Rough 90% CI on posterior mean via normal approx on Beta variance."""
    a = float(alpha) + max(0, int(wins))
    b = float(beta) + max(0, int(losses))
    mean = a / (a + b)
    var = (a * b) / (((a + b) ** 2) * (a + b + 1.0))
    sd = math.sqrt(max(var, 0.0))
    lo = max(0.0, mean - z * sd)
    hi = min(1.0, mean + z * sd)
    return round(lo, 4), round(hi, 4)


def kelly_f(p: float, b: float, *, fraction: float = KELLY_FRACTION) -> float:
    """Fractional Kelly: f = fraction * (b*p - q) / b. Clamped to >= 0."""
    if b is None or b <= 0:
        return 0.0
    p = max(0.0, min(1.0, float(p)))
    q = 1.0 - p
    full = (float(b) * p - q) / float(b)
    return max(0.0, full * float(fraction))


def kelly_size_mult(
    *,
    wins: int,
    losses: int,
    avg_win: float | None = None,
    avg_loss: float | None = None,
    rr_b: float | None = None,
) -> dict:
    """Compute Kelly multiplier metadata (does not scale size)."""
    n = max(0, int(wins)) + max(0, int(losses))
    if n <= 0:
        return {
            "kelly_mult": 1.0,
            "kelly_f": None,
            "p_bayes": None,
            "ci_low": None,
            "ci_high": None,
            "b": None,
            "n": 0,
            "reasons": ["no_evidence_passthrough"],
        }

    p = bayesian_p(wins, losses)
    lo, hi = bayesian_ci(wins, losses)
    b = None
    try:
        if avg_win is not None and avg_loss is not None and float(avg_loss) > 0:
            b = abs(float(avg_win) / float(avg_loss))
    except (TypeError, ValueError):
        b = None
    if b is None and rr_b is not None:
        try:
            b = float(rr_b)
        except (TypeError, ValueError):
            b = None
    if b is None or b <= 0:
        b = 1.0

    f = kelly_f(p, b)
    # Map fractional Kelly onto a size multiplier centered so ref Kelly → 1.0
    mult = f / REF_KELLY if REF_KELLY > 0 else 0.0
    mult = max(MIN_MULT, min(MAX_MULT, mult))
    reasons = [f"p={p:.3f}", f"b={b:.3f}", f"f={f:.3f}"]
    if hi < 0.5:
        reasons.append("ci_below_coin_flip")
    return {
        "kelly_mult": round(mult, 4),
        "kelly_f": round(f, 4),
        "p_bayes": round(p, 4),
        "ci_low": lo,
        "ci_high": hi,
        "b": round(b, 4),
        "n": n,
        "wins": int(wins),
        "losses": int(losses),
        "reasons": reasons,
    }


def apply_kelly_sizing(
    base_size: float,
    *,
    enabled: bool,
    wins: int = 0,
    losses: int = 0,
    avg_win: float | None = None,
    avg_loss: float | None = None,
    rr_b: float | None = None,
) -> dict:
    """Scale size by Kelly mult when enabled; else passthrough."""
    base = float(base_size)
    if not enabled:
        return {
            "size": base,
            "base_size": base,
            "kelly_mult": 1.0,
            "kelly_mode": "disabled",
            "kelly_f": None,
            "p_bayes": None,
            "ci_low": None,
            "ci_high": None,
            "reasons": ["disabled"],
        }
    info = kelly_size_mult(
        wins=wins,
        losses=losses,
        avg_win=avg_win,
        avg_loss=avg_loss,
        rr_b=rr_b,
    )
    sized = max(0.0, base * float(info["kelly_mult"]))
    return {
        "size": sized,
        "base_size": base,
        "kelly_mult": info["kelly_mult"],
        "kelly_mode": "soft",
        "kelly_f": info.get("kelly_f"),
        "p_bayes": info.get("p_bayes"),
        "ci_low": info.get("ci_low"),
        "ci_high": info.get("ci_high"),
        "b": info.get("b"),
        "n": info.get("n"),
        "reasons": info.get("reasons") or [],
    }
