"""Multi-hypothesis exit experts + weighted arbiter (L5)."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_core.engines.exit import (
    Exit,
    _mfe_giveback_hit,
    _unrealised_pct,
    compute_hold_score,
    dual_slope_path,
    effective_stall_bars,
    min_net_floor,
    net_unreal,
    _i,
    _f,
    _layered_hold_bank,
)


def _vote(action: str, confidence: float, reason: str) -> dict:
    return {"action": action, "confidence": float(confidence), "reason": reason}


def giveback_expert(trade: dict, unreal: float) -> dict:
    if _mfe_giveback_hit(trade, unreal):
        return _vote("bank", 0.9, "mfe_giveback")
    return _vote("hold", 0.4, "no_giveback")


def trail_expert(trade: dict, unreal: float) -> dict:
    peak = _f(trade, "peak_mfe_pct", 0.0)
    trail = _f(trade, "trailing_stop_pct", 0.0)
    if trail > 0 and peak > trail and unreal <= peak - trail:
        return _vote("trail", 0.7, "pct_trail")
    return _vote("hold", 0.3, "no_trail")


def bank_expert(trade: dict, unreal: float) -> dict:
    score = compute_hold_score(trade, unreal)
    path = dual_slope_path(trade, unreal, trade.get("profit_target_pct"))
    stalled = _i(trade, "exit_bars_since_peak", 0) >= effective_stall_bars(trade)
    net = net_unreal(trade, unreal)
    if net >= min_net_floor(trade) and (score < 0.35 or (stalled and path["path_fail"])):
        return _vote("bank", 0.85, "bank_expert")
    if score >= 0.55 and path["path_ok"]:
        return _vote("hold", 0.8, "runner")
    return _vote("protect", 0.5, "mid")


def tp_ladder_expert(trade: dict, price: float) -> dict:
    entry = float(trade["entry_price"])
    tp = trade.get("profit_target_pct")
    if tp is None:
        return _vote("hold", 0.2, "no_tp")
    soft = _f(trade, "soft_partial_tp_frac", 0.4)
    if (
        trade.get("partial_enabled")
        and not trade.get("soft_partial_done")
        and price >= entry * (1 + float(tp) * soft / 100.0)
    ):
        return _vote("partial", 0.9, "soft_partial")
    if price >= entry * (1 + float(tp) / 100.0):
        return _vote("bank", 0.95, "full_tp")
    return _vote("hold", 0.3, "below_tp")


def failed_breakout_expert(trade: dict, unreal: float) -> dict:
    fb = _i(trade, "failed_breakout_bars", 0)
    held_bars = _i(trade, "exit_bars_held", 0)
    if fb > 0 and held_bars >= fb and unreal <= 0 and net_unreal(trade, unreal) < min_net_floor(trade):
        return _vote("bank", 0.9, "failed_breakout")
    return _vote("hold", 0.2, "ok")


def load_weights(path: Path | None) -> dict[str, float]:
    base = {
        "giveback": 1.0,
        "trail": 1.0,
        "bank": 1.0,
        "tp_ladder": 1.0,
        "failed_breakout": 1.0,
    }
    if path is None or not path.exists():
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base.update({k: float(v) for k, v in data.items() if k in base})
    except Exception:  # noqa: BLE001
        pass
    return base


def save_weights(path: Path, weights: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(weights, indent=2), encoding="utf-8")


def credit_experts(weights: dict, votes: list[dict], best_action: str, lr: float = 0.05) -> dict:
    out = dict(weights)
    for v in votes:
        name = v.get("name")
        if not name or name not in out:
            continue
        if v.get("action") == best_action:
            out[name] = min(3.0, out[name] * (1 + lr))
        else:
            out[name] = max(0.2, out[name] * (1 - lr * 0.5))
    return out


def collect_votes(trade: dict, price: float) -> list[dict]:
    unreal = _unrealised_pct(trade, price)
    named = [
        ("giveback", giveback_expert(trade, unreal)),
        ("trail", trail_expert(trade, unreal)),
        ("bank", bank_expert(trade, unreal)),
        ("tp_ladder", tp_ladder_expert(trade, price)),
        ("failed_breakout", failed_breakout_expert(trade, unreal)),
    ]
    return [{"name": n, **v} for n, v in named]


def arbitrate_exit(trade: dict, price: float, prices: list[float] | None = None) -> Exit | None:
    """Weighted vote; SL still handled by caller/rules first via evaluate_exit order.

    When experts disagree mildly, fall through to layered rule brain.
    """
    # Hard SL path stays in evaluate_exit before experts — experts only for soft actions.
    weights = trade.get("exit_expert_weights") or load_weights(None)
    votes = collect_votes(trade, price)
    trade["exit_votes"] = votes
    scores: dict[str, float] = {}
    for v in votes:
        w = float(weights.get(v["name"], 1.0)) * float(v.get("confidence") or 0)
        act = v.get("action") or "hold"
        scores[act] = scores.get(act, 0.0) + w
    if not scores:
        return _layered_hold_bank(
            trade,
            price,
            float(trade["entry_price"]),
            _unrealised_pct(trade, price),
            trade.get("profit_target_pct"),
        )
    best = max(scores, key=scores.get)
    entry = float(trade["entry_price"])
    unreal = _unrealised_pct(trade, price)
    if best == "bank":
        # map failed_breakout vs profit_bank
        if any(v["name"] == "failed_breakout" and v["action"] == "bank" for v in votes):
            if unreal <= 0:
                return Exit("failed_breakout", price)
        return Exit("profit_bank", price)
    if best == "partial":
        return Exit(
            "partial_close",
            price,
            new_stop=entry,
            partial_close_fraction=0.5,
        )
    if best == "trail" or best == "protect":
        from hermes_core.engines.exit import _protect_stop

        return Exit("trailing", price, new_stop=_protect_stop(trade, entry, unreal))
    # hold — let layered brain still cut losers at soft clock
    return _layered_hold_bank(
        trade, price, entry, unreal, trade.get("profit_target_pct")
    )
