"""Closed mfe_path replay — primary reflection prove for live exit knobs."""

from __future__ import annotations

from typing import Any

from hermes_core.engines.counterfactual_exits import counterfactual_evs
from hermes_core.engines.size_stamp import ensure_mfe_path


LAYERED_PROVE_VARS = frozenset(
    {
        "min_bank_net_pct",
        "mfe_giveback_frac",
        "mfe_giveback_min_pct",
        "mfe_stall_bars",
        "failed_breakout_min_mae_pct",
        "failed_breakout_bars",
        "soft_partial_tp_frac",
        "early_reeval_cycles",
        "trailing_stop_pct",
    }
)

CLASSIC_PROVE_VARS = frozenset(
    {
        "stop_loss_pct",
        "profit_target_pct",
        "position_size_r",
        "session_filter",
        "mr_entry_rsi",
        "threshold",
        "entry.threshold",
        "entry.mr_entry_rsi",
        "entry.session_filter",
    }
)

# Knobs path-replay can differentiate on closed mfe_path points.
PATH_SCORABLE_VARS = frozenset(
    {
        "min_bank_net_pct",
        "mfe_giveback_frac",
        "mfe_giveback_min_pct",
        "failed_breakout_min_mae_pct",
        "failed_breakout_bars",
        "profit_target_pct",
        "position_size_r",
        "trailing_stop_pct",
    }
)


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _path_for(trade: dict) -> list[dict]:
    t = ensure_mfe_path(trade)
    path = t.get("mfe_path") or []
    return path if isinstance(path, list) else []


def _realized_net(trade: dict) -> float:
    if trade.get("net_pnl_pct") is not None:
        return _f(trade.get("net_pnl_pct"))
    return _f(trade.get("pnl_pct"))


def _cost(trade: dict) -> float:
    if trade.get("fees_pct") is not None:
        return _f(trade.get("fees_pct"))
    cm = trade.get("cost_model") or {}
    if isinstance(cm, dict) and cm.get("round_trip_pct") is not None:
        return _f(cm.get("round_trip_pct"))
    return 0.22


def _cf_cost(trade: dict, path: list[dict]) -> float:
    """Fee for CF: 0 when path already ends at net PnL (avoid double-counting)."""
    if not path:
        return _cost(trade)
    last = _f(path[-1].get("unreal"))
    realized = _realized_net(trade)
    if abs(last - realized) < 1e-5:
        return 0.0
    if trade.get("mfe_path_synthetic"):
        # Synthetic paths are built from mae/mfe/net pnl — already net-shaped.
        return 0.0
    return _cost(trade)


def _merge_strategy(strategy: dict, proposal: dict | None) -> dict:
    s = dict(strategy or {})
    if not proposal:
        return s
    var = str(proposal.get("variable") or "")
    if var:
        s[var] = proposal.get("new")
    return s


def _net_at(u: float, cost: float) -> float:
    return float(u) - max(0.0, float(cost))


def _fb_exit_net(
    path: list[dict],
    *,
    min_mae_pct: float,
    bars: int,
    cost: float,
) -> float:
    """Replay failed-breakout: exit when trough MAE hits floor within early bars."""
    if not path:
        return 0.0
    floor = -abs(float(min_mae_pct or 0.0))
    lim = max(1, int(bars or 1))
    trough = 0.0
    for i, p in enumerate(path):
        u = _f(p.get("unreal"))
        trough = min(trough, u)
        if i + 1 <= lim and floor < 0 and trough <= floor:
            return _net_at(u, cost)
    return _net_at(_f(path[-1].get("unreal")), cost)


def _giveback_exit_net(
    path: list[dict],
    *,
    giveback_frac: float,
    giveback_min: float,
    cost: float,
) -> float:
    """Exit when unreal falls to (1-frac)*peak after peak clears giveback_min."""
    if not path:
        return 0.0
    frac = max(0.0, min(1.0, float(giveback_frac or 0.0)))
    need = max(0.0, float(giveback_min or 0.0))
    peak = 0.0
    for p in path:
        u = _f(p.get("unreal"))
        pk = _f(p.get("peak"), u)
        peak = max(peak, pk, u)
        if peak >= need and peak > 1e-9 and u <= peak * (1.0 - frac):
            return _net_at(u, cost)
    return _net_at(_f(path[-1].get("unreal")), cost)


def _trail_exit_net(path: list[dict], *, trail_pct: float, cost: float) -> float:
    if not path:
        return 0.0
    trail = max(0.0, float(trail_pct or 0.0))
    peak = 0.0
    for p in path:
        u = _f(p.get("unreal"))
        peak = max(peak, _f(p.get("peak"), u), u)
        if trail > 0 and peak > trail and u <= peak - trail:
            return _net_at(u, cost)
    return _net_at(_f(path[-1].get("unreal")), cost)


def _policy_ev(
    path: list[dict],
    trade: dict,
    strategy: dict,
    *,
    proposal: dict | None,
) -> float:
    """EV under the proposal's knob (or incumbent strategy when proposal restores old)."""
    s = _merge_strategy(strategy, proposal)
    var = str((proposal or {}).get("variable") or "")
    cost = _cf_cost(trade, path)

    if var in {"failed_breakout_min_mae_pct", "failed_breakout_bars"}:
        return _fb_exit_net(
            path,
            min_mae_pct=_f(s.get("failed_breakout_min_mae_pct"), 0.40),
            bars=int(_f(s.get("failed_breakout_bars"), 2) or 2),
            cost=cost,
        )

    if "giveback" in var:
        return _giveback_exit_net(
            path,
            giveback_frac=_f(s.get("mfe_giveback_frac"), 0.4),
            giveback_min=_f(s.get("mfe_giveback_min_pct"), 0.35),
            cost=cost,
        )

    if var == "trailing_stop_pct":
        return _trail_exit_net(path, trail_pct=_f(s.get("trailing_stop_pct")), cost=cost)

    min_bank = _f(s.get("min_bank_net_pct"), 0.10)
    tp = _f(s.get("profit_target_pct"), _f(trade.get("profit_target_pct"), 1.5))
    ev = counterfactual_evs(path, tp=tp, cost_pct=cost, min_bank_net=min_bank)
    if var == "min_bank_net_pct":
        return _f(ev.get("bank_first_green"), _f(ev.get("best")))
    if var == "profit_target_pct":
        return _f(ev.get("hold_to_tp"), _f(ev.get("best")))
    return _f(ev.get("best"))


def _incumbent_proposal(proposal: dict) -> dict:
    """Proposal dict that restores the old knob value for paired comparison."""
    return {
        "variable": proposal.get("variable"),
        "old": proposal.get("old"),
        "new": proposal.get("old"),
    }


def replay_prove(
    trades: list[dict],
    *,
    strategy: dict,
    proposal: dict,
    min_paths: int = 5,
) -> dict:
    """Compare challenger vs incumbent mean net on closed paths.

    Returns ``{approved, reason, improvement, n, details}``.
    """
    details: dict[str, Any] = {}
    var = str(proposal.get("variable") or "")
    bare = var.split(".")[-1]
    if var and var not in PATH_SCORABLE_VARS and bare not in PATH_SCORABLE_VARS:
        return {
            "approved": False,
            "reason": f"path_replay_not_applicable:{var}",
            "improvement": None,
            "n": 0,
            "details": {"not_applicable": True},
            "method": "path_replay",
        }

    paths = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        p = _path_for(t)
        if len(p) >= 3:
            paths.append((t, p))
    details["n_paths"] = len(paths)
    if len(paths) < int(min_paths):
        return {
            "approved": False,
            "reason": f"insufficient_paths:{len(paths)}<{min_paths}",
            "improvement": None,
            "n": len(paths),
            "details": details,
            "method": "path_replay",
        }

    inc_prop = _incumbent_proposal(proposal)
    # Ensure incumbent strategy sees the old value even if live YAML drifted.
    strat = dict(strategy or {})
    if proposal.get("old") is not None and var:
        strat[var] = proposal.get("old")

    inc_nets = []
    ch_nets = []
    for t, p in paths:
        realized = _realized_net(t)
        if var == "position_size_r":
            old = _f(proposal.get("old"), 1.0) or 1.0
            new = _f(proposal.get("new"), old)
            inc_nets.append(realized)
            ch_nets.append(realized * (new / old))
            continue
        # Paired old vs new under the same fee/path conventions — never
        # max(realized, CF) vs CF (that structurally rejects equal-EV knobs).
        inc = _policy_ev(p, t, strat, proposal=inc_prop)
        ch = _policy_ev(p, t, strat, proposal=proposal)
        inc_nets.append(inc)
        ch_nets.append(ch)

    mean_inc = sum(inc_nets) / len(inc_nets)
    mean_ch = sum(ch_nets) / len(ch_nets)
    improvement = mean_ch - mean_inc
    details["mean_incumbent"] = round(mean_inc, 6)
    details["mean_challenger"] = round(mean_ch, 6)
    details["improvement"] = round(improvement, 6)

    worst = min(ch_nets) if ch_nets else 0.0
    details["worst_challenger"] = round(worst, 6)
    max_dd = abs(min(0.0, worst))
    details["max_dd"] = round(max_dd, 6)

    if improvement < -1e-9:
        return {
            "approved": False,
            "reason": "path_replay_worse_than_incumbent",
            "improvement": improvement,
            "n": len(paths),
            "details": details,
            "method": "path_replay",
        }
    return {
        "approved": True,
        "reason": "path_replay_ok" if improvement > 1e-9 else "path_replay_neutral_ok",
        "improvement": improvement,
        "improvement_full": improvement,
        "improvement_oos": improvement,
        "n": len(paths),
        "details": details,
        "method": "path_replay",
        "phases": {"path_replay": details},
    }


def is_layered_var(variable: str) -> bool:
    return str(variable or "") in LAYERED_PROVE_VARS


def is_classic_var(variable: str) -> bool:
    v = str(variable or "")
    return v in CLASSIC_PROVE_VARS or v.split(".")[-1] in {
        "stop_loss_pct",
        "profit_target_pct",
        "position_size_r",
        "session_filter",
        "mr_entry_rsi",
        "threshold",
        "trailing_stop_pct",
    }
