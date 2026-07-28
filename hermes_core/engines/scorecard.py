"""Profitability Path scorecard — cost-adjusted expectancy by (pair, entry_type).

North-star metric for weekly kill/continue reviews. Pure helpers + jsonl reader;
no network. CLI entry: ``python -m tools.scorecard``.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import Any

from hermes_core.env import get_env
from hermes_core.state.paths import bot_state_dir

# Default round-trip cost haircut (% of notional) applied to each closed trade.
DEFAULT_COST_PCT = 0.05  # 0.05% ≈ FX-ish; override via SCORECARD_COST_PCT


def cost_pct() -> float:
    raw = get_env("SCORECARD_COST_PCT", "")
    if not raw.strip():
        return DEFAULT_COST_PCT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_COST_PCT


def _pnl_of(rec: dict) -> float | None:
    for key in ("pnl_pct", "pnl", "return_pct"):
        if key in rec and rec[key] is not None:
            try:
                return float(rec[key])
            except (TypeError, ValueError):
                return None
    return None


def load_closed_trades(bot: str, *, path: Path | None = None) -> list[dict]:
    """Load closed trade rows from ``trades.jsonl`` (fail-soft)."""
    p = path or (bot_state_dir(bot) / "trades.jsonl")
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        # Skip open / adjustment stubs (no real exit).
        if rec.get("exit_reason") is None and rec.get("exit_price") is None:
            continue
        if _pnl_of(rec) is None:
            continue
        out.append(rec)
    return out


def _max_drawdown(pnls: list[float]) -> float:
    """Max peak-to-trough drawdown on cumulative % equity (absolute, >=0)."""
    if not pnls:
        return 0.0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return round(max_dd, 4)


def summarize_bucket(
    trades: list[dict],
    *,
    cost: float | None = None,
) -> dict[str, Any]:
    """Aggregate one (pair, entry_type) bucket."""
    c = float(cost_pct() if cost is None else cost)
    raw_pnls = [_pnl_of(t) for t in trades]
    pnls = [float(p) for p in raw_pnls if p is not None]
    adj = [p - c for p in pnls]
    n = len(adj)
    if n == 0:
        return {
            "n": 0,
            "wr": 0.0,
            "expectancy_raw": 0.0,
            "expectancy": 0.0,
            "profit_factor": 0.0,
            "max_dd": 0.0,
            "avg_hold_cycles": None,
            "cost_pct": c,
            "kill": True,
            "verdict": "no_trades",
        }
    wins = [p for p in adj if p > 0]
    losses = [p for p in adj if p <= 0]
    wr = len(wins) / n
    exp = sum(adj) / n
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 1e-12 else (999.0 if gross_win > 0 else 0.0)
    holds: list[float] = []
    for t in trades:
        for key in ("hold_cycles", "cycles_held", "duration_cycles"):
            if t.get(key) is not None:
                with contextlib.suppress(TypeError, ValueError):
                    holds.append(float(t[key]))
                break
    avg_hold = round(sum(holds) / len(holds), 2) if holds else None
    verdict = "continue" if exp > 0 else "kill"
    return {
        "n": n,
        "wr": round(wr, 4),
        "expectancy_raw": round(sum(pnls) / n, 4),
        "expectancy": round(exp, 4),
        "profit_factor": round(min(pf, 999.0), 4),
        "max_dd": _max_drawdown(adj),
        "avg_hold_cycles": avg_hold,
        "cost_pct": c,
        "kill": exp <= 0,
        "verdict": verdict,
    }


def build_scorecard(
    bot: str,
    *,
    trades: list[dict] | None = None,
    cost: float | None = None,
    min_n: int = 20,
) -> dict[str, Any]:
    """Per-(pair, entry_type) scorecard + fleet rollup."""
    rows = trades if trades is not None else load_closed_trades(bot)
    buckets: dict[tuple[str, str], list[dict]] = {}
    for t in rows:
        pair = str(t.get("pair") or t.get("symbol") or "UNKNOWN")
        et = str(t.get("entry_type") or t.get("strategy_type") or "unknown")
        buckets.setdefault((pair, et), []).append(t)

    by_pair_type: dict[str, dict[str, Any]] = {}
    for (pair, et), ts in sorted(buckets.items()):
        key = f"{pair}|{et}"
        s = summarize_bucket(ts, cost=cost)
        s["pair"] = pair
        s["entry_type"] = et
        s["sample_ok"] = s["n"] >= int(min_n)
        by_pair_type[key] = s

    fleet_pnls_adj: list[float] = []
    c = float(cost_pct() if cost is None else cost)
    for t in rows:
        p = _pnl_of(t)
        if p is not None:
            fleet_pnls_adj.append(p - c)
    fleet = summarize_bucket(rows, cost=cost)
    fleet["pair"] = "*"
    fleet["entry_type"] = "*"
    fleet["sample_ok"] = fleet["n"] >= int(min_n)

    return {
        "bot": bot,
        "cost_pct": c,
        "min_n": int(min_n),
        "buckets": by_pair_type,
        "fleet": fleet,
        "n_trades": len(rows),
    }


def phase1_gate(
    scorecard: dict[str, Any],
    *,
    focus_pairs: list[str] | None = None,
    max_dd: float = 10.0,
    min_n: int = 20,
) -> dict[str, Any]:
    """Kill/continue per focus pair using Phase 1 exit gates."""
    focus = [p.upper() for p in (focus_pairs or [])]
    decisions: dict[str, Any] = {}
    buckets = scorecard.get("buckets") or {}
    for key, s in buckets.items():
        pair = str(s.get("pair") or "")
        if focus and pair.upper() not in focus:
            continue
        n = int(s.get("n") or 0)
        exp = float(s.get("expectancy") or 0.0)
        dd = float(s.get("max_dd") or 0.0)
        if n < min_n:
            decisions[key] = {
                "verdict": "wait",
                "reason": f"n={n}<{min_n}",
                "expectancy": exp,
                "max_dd": dd,
            }
        elif exp <= 0:
            decisions[key] = {
                "verdict": "kill",
                "reason": "expectancy_le_0_after_costs",
                "expectancy": exp,
                "max_dd": dd,
            }
        elif dd > max_dd:
            decisions[key] = {
                "verdict": "kill",
                "reason": f"max_dd={dd}>{max_dd}",
                "expectancy": exp,
                "max_dd": dd,
            }
        else:
            decisions[key] = {
                "verdict": "continue",
                "reason": "phase1_exit_gate_ok",
                "expectancy": exp,
                "max_dd": dd,
            }
    return {"bot": scorecard.get("bot"), "decisions": decisions}
