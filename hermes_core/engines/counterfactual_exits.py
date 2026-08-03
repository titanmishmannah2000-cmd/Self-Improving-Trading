"""Counterfactual exit EV along an MFE path (L3)."""

from __future__ import annotations


def _net(gross: float, cost: float) -> float:
    return float(gross) - max(0.0, float(cost))


def counterfactual_evs(
    path: list[dict],
    *,
    tp: float = 1.5,
    cost_pct: float = 0.0,
    min_bank_net: float = 0.10,
    soft_clock_idx: int | None = None,
) -> dict:
    """Hindsight net EV for simple alternate policies along ``path``.

    Each path point needs ``unreal`` (gross %). Returns dict of policy -> final net.
    """
    if not path:
        return {}
    cost = float(cost_pct or 0.0)
    results: dict[str, float] = {}

    # bank at first net-green above min
    bank = None
    for p in path:
        try:
            u = float(p.get("unreal") or 0.0)
        except (TypeError, ValueError):
            continue
        if _net(u, cost) >= min_bank_net:
            bank = _net(u, cost)
            break
    results["bank_first_green"] = bank if bank is not None else _net(
        float(path[-1].get("unreal") or 0.0), cost
    )

    # hold to TP if ever reached else last
    hit_tp = None
    for p in path:
        try:
            u = float(p.get("unreal") or 0.0)
        except (TypeError, ValueError):
            continue
        if u >= tp:
            hit_tp = _net(tp, cost)
            break
    results["hold_to_tp"] = hit_tp if hit_tp is not None else _net(
        float(path[-1].get("unreal") or 0.0), cost
    )

    # soft clock exit
    if soft_clock_idx is not None and 0 <= soft_clock_idx < len(path):
        u = float(path[soft_clock_idx].get("unreal") or 0.0)
        results["soft_clock"] = _net(u, cost)
    else:
        mid = len(path) // 2
        results["soft_clock"] = _net(float(path[mid].get("unreal") or 0.0), cost)

    # giveback-ish: exit when unreal <= 0.5 * peak after peak>=0.35
    gb = None
    peak = 0.0
    for p in path:
        try:
            u = float(p.get("unreal") or 0.0)
            pk = float(p.get("peak") or u)
        except (TypeError, ValueError):
            continue
        peak = max(peak, pk)
        if peak >= 0.35 and u <= peak * 0.5:
            gb = _net(u, cost)
            break
    results["giveback"] = gb if gb is not None else _net(
        float(path[-1].get("unreal") or 0.0), cost
    )

    results["best"] = max(results.values()) if results else 0.0
    results["best_policy"] = max(results, key=results.get) if results else ""
    return results


def label_hold_vs_bank(path: list[dict], *, cost_pct: float = 0.0, min_bank_net: float = 0.10) -> list[dict]:
    """Per-step labels: 1 if holding to best CF beats banking immediately."""
    ev = counterfactual_evs(path, cost_pct=cost_pct, min_bank_net=min_bank_net)
    best = float(ev.get("best") or 0.0)
    labels = []
    for i, p in enumerate(path):
        try:
            u = float(p.get("unreal") or 0.0)
        except (TypeError, ValueError):
            u = 0.0
        immediate = u - max(0.0, cost_pct)
        # hold better if eventual best > banking now by margin
        y = 1.0 if best > immediate + 0.05 else 0.0
        labels.append({"i": i, "y_hold": y, "unreal": u, "best": best})
    return labels
