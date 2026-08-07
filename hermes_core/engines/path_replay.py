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


def _policy_ev(path: list[dict], trade: dict, strategy: dict, *, challenger: dict | None) -> float:
    """EV under bank/giveback policies with optional challenger overrides."""
    s = dict(strategy or {})
    if challenger:
        var = str(challenger.get("variable") or "")
        if var in s or var:
            s[var] = challenger.get("new")
    min_bank = _f(s.get("min_bank_net_pct"), 0.10)
    tp = _f(s.get("profit_target_pct"), _f(trade.get("profit_target_pct"), 1.5))
    cost = _cost(trade)
    ev = counterfactual_evs(path, tp=tp, cost_pct=cost, min_bank_net=min_bank)
    # Prefer giveback policy when that knob is the proposal
    var = str((challenger or {}).get("variable") or "")
    if "giveback" in var:
        return _f(ev.get("giveback"), _f(ev.get("best")))
    if var == "min_bank_net_pct":
        return _f(ev.get("bank_first_green"), _f(ev.get("best")))
    if var == "profit_target_pct":
        return _f(ev.get("hold_to_tp"), _f(ev.get("best")))
    return _f(ev.get("best"))


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

    inc_nets = []
    ch_nets = []
    for t, p in paths:
        realized = _realized_net(t)
        # Incumbent: max(realized, CF under current strategy)
        inc_cf = _policy_ev(p, t, strategy, challenger=None)
        inc = max(realized, inc_cf)
        ch = _policy_ev(p, t, strategy, challenger=proposal)
        # Size proposals: scale realized by size ratio (path EV unchanged shape)
        var = str(proposal.get("variable") or "")
        if var == "position_size_r":
            old = _f(proposal.get("old"), 1.0) or 1.0
            new = _f(proposal.get("new"), old)
            ch = realized * (new / old)
            inc = realized
        inc_nets.append(inc)
        ch_nets.append(ch)

    mean_inc = sum(inc_nets) / len(inc_nets)
    mean_ch = sum(ch_nets) / len(ch_nets)
    improvement = mean_ch - mean_inc
    details["mean_incumbent"] = round(mean_inc, 6)
    details["mean_challenger"] = round(mean_ch, 6)
    details["improvement"] = round(improvement, 6)

    # MDD proxy: worst path net under challenger
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
        "reason": "path_replay_ok",
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
