"""HIF Layer C — book-level risk (cap total size + tilt by posterior edge).

When ``BOOK_RISK=1``, after per-pair Kelly, soft-cap the new entry against
``BOOK_RISK_CAP`` and tilt size toward pairs with better Bayesian edge vs the
open book. Never skips. Flag off / errors → passthrough.
"""

from __future__ import annotations

from hermes_core.engines.kelly_sizing import bayesian_p
from hermes_core.env import get_env

BOOK_RISK_CAP = 1.0  # 2× MAX_POSITION_SIZE (0.5)
TILT_MIN = 0.50
TILT_MAX = 1.15
TILT_K = 1.25  # sensitivity to (edge_i - mean_open)


def book_risk_enabled() -> bool:
    return get_env("BOOK_RISK", "0") == "1"


def _edge_p(cortex, pair: str, entry_type: str) -> float | None:
    try:
        st = cortex.edge_stats(pair, entry_type) if cortex is not None else None
    except Exception:  # noqa: BLE001
        return None
    if not st or not st.get("n"):
        return None
    return bayesian_p(int(st.get("wins") or 0), int(st.get("losses") or 0))


def apply_book_risk(
    base_size: float,
    *,
    enabled: bool,
    open_positions: dict | None,
    pair: str,
    entry_type: str,
    cortex=None,
    book_cap: float = BOOK_RISK_CAP,
) -> dict:
    """Scale/cap ``base_size`` for book risk. Fail-open to unchanged size."""
    base = max(0.0, float(base_size))
    out = {
        "size": base,
        "book_mode": "disabled",
        "book_mult": 1.0,
        "book_tilt": 1.0,
        "book_used": 0.0,
        "book_cap": float(book_cap),
        "book_remaining": float(book_cap),
        "book_reasons": [],
    }
    if not enabled:
        return out

    try:
        positions = open_positions or {}
        used = 0.0
        open_ps: list[float] = []
        for p, pos in positions.items():
            if p == pair:
                continue
            try:
                used += float(pos.get("size") or 0.0)
            except (TypeError, ValueError):
                continue
            et = pos.get("entry_type") or "mean_reversion"
            ep = _edge_p(cortex, p, et)
            if ep is not None:
                open_ps.append(ep)

        remaining = max(0.0, float(book_cap) - used)
        cand_p = _edge_p(cortex, pair, entry_type)
        tilt = 1.0
        reasons: list[str] = []

        if cand_p is not None and open_ps:
            mean_open = sum(open_ps) / len(open_ps)
            tilt = 0.5 + TILT_K * (cand_p - mean_open)
            tilt = max(TILT_MIN, min(TILT_MAX, tilt))
            reasons.append(f"tilt_vs_book={tilt:.2f}")
        elif cand_p is not None:
            reasons.append("solo_book_no_tilt")
        else:
            reasons.append("no_edge_passthrough_tilt")

        sized = base * tilt
        if sized > remaining:
            reasons.append("book_cap")
            sized = remaining
        mult = (sized / base) if base > 1e-12 else 1.0

        return {
            "size": max(0.0, float(sized)),
            "book_mode": "soft",
            "book_mult": round(mult, 4),
            "book_tilt": round(tilt, 4),
            "book_used": round(used, 4),
            "book_cap": float(book_cap),
            "book_remaining": round(remaining, 4),
            "book_reasons": reasons or ["soft"],
            "p_bayes": cand_p,
        }
    except Exception:  # noqa: BLE001 — fail-open
        return {
            **out,
            "book_mode": "passthrough",
            "book_reasons": ["book_error"],
        }
