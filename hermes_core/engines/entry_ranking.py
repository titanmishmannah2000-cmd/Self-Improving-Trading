"""HIF Layer B — entry ranking (pick best candidate by expected edge).

When ``ENTRY_RANKING=1``, the live loop may gather more than one entry candidate
(traditional + GP) and open the higher-scoring one. Ranking never hard-blocks:
if only one candidate exists, it wins; if none, the loop still logs ``no_signal``.

When the flag is off, callers keep legacy behaviour (traditional always wins;
GP only as fallback when traditional is None).
"""

from __future__ import annotations

from hermes_core.env import get_env

# Score blend weights (sum = 1.0)
W_EDGE = 0.45  # Bayesian p or raw WR
W_QUALITY = 0.35  # Signal.quality 0..1
W_EXPERT = 0.20  # soft expert weight 0..1 (1.0 if soft weights off)


def entry_ranking_enabled() -> bool:
    return get_env("ENTRY_RANKING", "0") == "1"


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def score_candidate(
    *,
    entry_type: str,
    quality: float | None = None,
    wr: float | None = None,
    p_bayes: float | None = None,
    expert_weight: float | None = None,
    gp_strength: float | None = None,
) -> dict:
    """Return edge score in [0, 1] plus component breakdown for the dashboard."""
    edge = p_bayes if p_bayes is not None else wr
    if edge is None:
        edge = 0.5
        edge_src = "neutral"
    else:
        edge = _clamp01(edge)
        edge_src = "p_bayes" if p_bayes is not None else "wr"

    q = _clamp01(quality) if quality is not None else 0.5
    ew = _clamp01(expert_weight) if expert_weight is not None else 1.0

    score = W_EDGE * edge + W_QUALITY * q + W_EXPERT * ew
    # Small GP conviction tilt (does not dominate).
    if gp_strength is not None:
        try:
            g = abs(float(gp_strength))
            score = _clamp01(score + min(0.05, g * 0.05))
        except (TypeError, ValueError):
            pass

    return {
        "entry_type": entry_type,
        "score": round(_clamp01(score), 4),
        "components": {
            "edge": round(edge, 4),
            "edge_src": edge_src,
            "quality": round(q, 4),
            "expert_weight": round(ew, 4),
            "gp_strength": gp_strength,
        },
    }


def rank_candidates(candidates: list[dict]) -> dict:
    """Pick the best scored candidate. ``candidates`` items need ``score`` + ``sig``.

    Tie-break: higher quality, then prefer non-GP (stable traditional) on equal score.
    """
    if not candidates:
        return {
            "winner": None,
            "ranked": [],
            "reason": "no_candidates",
        }

    def _key(c: dict) -> tuple:
        sig = c.get("sig")
        q = float(getattr(sig, "quality", 0.0) or 0.0) if sig is not None else 0.0
        et = c.get("entry_type") or ""
        trad_bonus = 0 if et == "gp_ensemble" else 1
        return (float(c.get("score") or 0.0), q, trad_bonus)

    ranked = sorted(candidates, key=_key, reverse=True)
    winner = ranked[0]
    others = [
        {
            "entry_type": c.get("entry_type"),
            "score": c.get("score"),
        }
        for c in ranked[1:]
    ]
    reason = f"best_score={winner.get('score')}"
    if others:
        reason += f" > {others[0].get('entry_type')}={others[0].get('score')}"
    return {
        "winner": winner,
        "ranked": [
            {
                "entry_type": c.get("entry_type"),
                "score": c.get("score"),
                "components": c.get("components"),
            }
            for c in ranked
        ],
        "reason": reason,
        "n_candidates": len(ranked),
    }
