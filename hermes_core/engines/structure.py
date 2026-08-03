"""Deterministic market structure (continuous, no LLM)."""

from __future__ import annotations


def _swings(prices: list[float], left: int = 2, right: int = 2) -> tuple[list[float], list[float]]:
    highs: list[float] = []
    lows: list[float] = []
    n = len(prices)
    for i in range(left, n - right):
        window = prices[i - left : i + right + 1]
        p = prices[i]
        if p >= max(window):
            highs.append(p)
        if p <= min(window):
            lows.append(p)
    return highs, lows


def analyze_structure(
    prices: list[float],
    *,
    donchian_period: int = 20,
    entry_price: float | None = None,
) -> dict:
    """Return structure digest for patience / failed-auction / level distance.

    Pure + fail-open: never raises.
    """
    out = {
        "failed_auction": False,
        "dist_to_resistance_pct": None,
        "dist_to_support_pct": None,
        "donchian_upper": None,
        "donchian_lower": None,
        "structure_hash": "",
    }
    try:
        if not prices or len(prices) < max(10, donchian_period + 2):
            return out
        px = [float(p) for p in prices if p is not None]
        if len(px) < max(10, donchian_period + 2):
            return out
        last = px[-1]
        period = max(5, int(donchian_period))
        prior = px[-(period + 1) : -1]
        if len(prior) < period:
            return out
        upper = max(prior)
        lower = min(prior)
        out["donchian_upper"] = round(upper, 6)
        out["donchian_lower"] = round(lower, 6)
        # Failed auction: broke above channel then closed back inside.
        broke = len(px) >= 2 and px[-2] > upper
        back_inside = last <= upper
        out["failed_auction"] = bool(broke and back_inside)
        highs, lows = _swings(px)
        res = min((h for h in highs if h > last), default=upper if upper > last else None)
        sup = max((lo for lo in lows if lo < last), default=lower if lower < last else None)
        if res and last > 0:
            out["dist_to_resistance_pct"] = round((res - last) / last * 100.0, 4)
        if sup and last > 0:
            out["dist_to_support_pct"] = round((last - sup) / last * 100.0, 4)
        out["structure_hash"] = (
            f"fa={int(out['failed_auction'])}|u={out['donchian_upper']}|"
            f"r={out['dist_to_resistance_pct']}"
        )
        if entry_price is not None:
            out["entry_price"] = float(entry_price)
    except Exception:  # noqa: BLE001
        pass
    return out


def structure_patience_mult(structure: dict | None) -> float:
    """Haircut patience when structure says failed auction / tight resistance."""
    if not structure:
        return 1.0
    mult = 1.0
    if structure.get("failed_auction"):
        mult *= 0.6
    try:
        d = structure.get("dist_to_resistance_pct")
        if d is not None and float(d) < 0.15:
            mult *= 0.85
    except (TypeError, ValueError):
        pass
    return max(0.4, min(1.2, mult))
