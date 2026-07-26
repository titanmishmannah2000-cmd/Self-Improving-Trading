"""Richer + sequential live experiment verdicts.

Replaces the thin ``mean(challenger) > mean(champion)`` gate with a scorecard
that accounts for noise, win-rate, and drawdown, and allows EARLY ABORT of
clearly doomed challengers before the full evaluation window fills.

Promotion stays conservative (full window + composite pass). Early stop is
asymmetric: only losers can be aborted early — never promote on thin data.
"""

from __future__ import annotations

import math
import os

# Minimum challenger closes before an early-abort is even considered.
EARLY_MIN = int(os.environ.get("REFLECT_EARLY_ABORT_MIN", "3") or 3)
# How many SEs below baseline counts as "clearly doomed" for early abort.
EARLY_Z = float(os.environ.get("REFLECT_EARLY_ABORT_Z", "1.0") or 1.0)
# Absolute mean gap (pct) that alone can trigger early abort (catastrophic).
EARLY_GAP = float(os.environ.get("REFLECT_EARLY_ABORT_GAP", "0.35") or 0.35)
# Challenger max-DD may exceed champion DD by this many pct points and still pass.
DD_SLACK = float(os.environ.get("REFLECT_VERDICT_DD_SLACK", "2.0") or 2.0)
# Noise-adjusted edge (diff / SE) required to promote at full window.
PROMOTE_Z = float(os.environ.get("REFLECT_PROMOTE_Z", "0.25") or 0.25)


def _pnls(rows: list[dict]) -> list[float]:
    out: list[float] = []
    for r in rows or []:
        v = r.get("pnl_pct")
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def trade_stats(rows: list[dict]) -> dict:
    """Mean / SE / win-rate / equity max-DD for a close sample."""
    vals = _pnls(rows)
    n = len(vals)
    if n == 0:
        return {
            "n": 0,
            "mean": None,
            "std": None,
            "se": None,
            "win_rate": None,
            "max_dd": None,
            "total": 0.0,
        }
    mean = sum(vals) / n
    if n > 1:
        var = sum((v - mean) ** 2 for v in vals) / (n - 1)
        std = math.sqrt(max(0.0, var))
    else:
        std = 0.0
    se = std / math.sqrt(n) if n else None
    wins = sum(1 for v in vals if v > 0)
    # Equity curve max drawdown in pct points of cumulative pnl.
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for v in vals:
        eq += v
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    return {
        "n": n,
        "mean": mean,
        "std": std,
        "se": se,
        "win_rate": wins / n,
        "max_dd": max_dd,
        "total": sum(vals),
    }


def dominant_regime(rows: list[dict]) -> str | None:
    """Most common entry_regime stamp in ``rows`` (None if unmarked)."""
    counts: dict[str, int] = {}
    for r in rows or []:
        for key in ("entry_regime", "regime", "regime_label"):
            v = r.get(key)
            if v:
                label = str(v).lower()
                counts[label] = counts.get(label, 0) + 1
                break
    if not counts:
        return None
    best = max(counts.values())
    # Prefer most recent among ties.
    tied = {k for k, n in counts.items() if n == best}
    for r in reversed(rows or []):
        for key in ("entry_regime", "regime", "regime_label"):
            v = r.get(key)
            if v and str(v).lower() in tied:
                return str(v).lower()
    return next(iter(tied), None)


def change_direction(old, new) -> str | None:
    """``up`` / ``down`` / ``set`` (non-numeric) / None (no change)."""
    if old == new:
        return None
    try:
        o = float(old)
        n = float(new)
    except (TypeError, ValueError):
        return "set"
    if n > o + 1e-12:
        return "up"
    if n < o - 1e-12:
        return "down"
    return None


def _edge(diff: float, se: float | None) -> float:
    """Signed noise-adjusted edge. Large |diff| with tiny SE → large |edge|."""
    noise = max(float(se or 0.0), 1e-6)
    return diff / noise


def judge_live(
    challenger: list[dict],
    champion: list[dict],
    *,
    need: int,
    margin: float = 0.0,
    early_min: int = EARLY_MIN,
) -> dict:
    """Return ``pending`` / ``improved`` / ``worsened`` with a full scorecard.

    * ``len(challenger) < early_min`` → always pending
    * ``early_min <= n < need`` → only early-ABORT if clearly doomed; else pending
    * ``n >= need`` → full composite (mean + noise edge + DD guard)
    """
    need = max(1, int(need))
    early_min = max(1, min(int(early_min), need))
    chal_rows = list(challenger or [])
    n = len(chal_rows)

    empty = {
        "status": "pending",
        "have": n,
        "need": need,
        "early_min": early_min,
        "regime": dominant_regime(chal_rows),
    }
    if n < early_min:
        return empty

    # Use all available challenger closes up to need (don't ignore extras).
    used = chal_rows[: max(need, n)]
    chal = trade_stats(used[: max(n, 1)])
    champ = trade_stats(champion or [])
    baseline = champ["mean"] if champ["mean"] is not None else 0.0
    chal_mean = chal["mean"] if chal["mean"] is not None else 0.0
    diff = chal_mean - baseline - float(margin)
    edge = _edge(diff, chal["se"])
    wr_delta = None
    if chal["win_rate"] is not None and champ["win_rate"] is not None:
        wr_delta = chal["win_rate"] - champ["win_rate"]
    elif chal["win_rate"] is not None:
        wr_delta = chal["win_rate"] - 0.5

    dd_ok = True
    if chal["max_dd"] is not None:
        champ_dd = champ["max_dd"] if champ["max_dd"] is not None else 0.0
        dd_ok = chal["max_dd"] <= champ_dd + DD_SLACK

    scorecard = {
        "challenger_avg": round(chal_mean, 5),
        "champion_avg": (round(champ["mean"], 5) if champ["mean"] is not None else None),
        "baseline": round(baseline, 5),
        "diff": round(diff, 5),
        "edge": round(edge, 4),
        "challenger_se": (round(chal["se"], 5) if chal["se"] is not None else None),
        "challenger_wr": (round(chal["win_rate"], 4) if chal["win_rate"] is not None else None),
        "champion_wr": (round(champ["win_rate"], 4) if champ["win_rate"] is not None else None),
        "wr_delta": (round(wr_delta, 4) if wr_delta is not None else None),
        "challenger_max_dd": (round(chal["max_dd"], 4) if chal["max_dd"] is not None else None),
        "champion_max_dd": (round(champ["max_dd"], 4) if champ["max_dd"] is not None else None),
        "dd_ok": dd_ok,
        "n_challenger": n,
        "n_champion": champ["n"],
        "have": n,
        "need": need,
        "early_min": early_min,
        "regime": dominant_regime(used),
    }

    clearly_doomed = False
    if diff < 0:
        catastrophic_gap = diff <= -2.0 * EARLY_GAP
        significant_loss = diff <= -EARLY_GAP and edge <= -EARLY_Z
        bad_dd = (chal["max_dd"] or 0.0) >= max(
            3.0, (champ["max_dd"] or 0.0) + DD_SLACK + 1.0
        )
        clearly_doomed = catastrophic_gap or significant_loss or bad_dd

    if n < need:
        if clearly_doomed:
            return {"status": "worsened", "early_abort": True, **scorecard}
        return {"status": "pending", "early_abort": False, **scorecard}

    # Full window: richer composite. Mean must clear margin; noise-adjusted edge
    # must not be noise; drawdown must not blow past the champion by DD_SLACK.
    # Tiny clear edges (large |diff|, tiny SE) still promote via PROMOTE_Z.
    improved = diff > 0 and edge >= PROMOTE_Z and dd_ok
    # Fallback for ultra-quiet identical samples (SE≈0, clear positive diff):
    if diff > 0 and dd_ok and (chal["se"] or 0.0) < 1e-9:
        improved = True
    return {
        "status": "improved" if improved else "worsened",
        "early_abort": False,
        **scorecard,
    }
