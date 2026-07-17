"""Genetic programming discovery engine (Session 13 / Phase 13).

Evolves small symbolic indicator expressions over price/volume features,
scores each by fitness = |corr(signal, forward_return)| - 0.001*complexity,
and admits only those that survive an OOS-first check (>= OOS_FLOOR) AND pass
the novelty + redundancy gates. Discovered indicators persist to
state/discovered/{pair}.json so they survive a restart.

Hard isolation (D8): the feature/operator set contains ONLY market-data
primitives (price, returns, simple moving averages, RSI, volatility). There is
NO path to crypto-specific signals (fear-&-greed, on-chain, BTC feeds) — none
are imported, referenced, or reachable from this module's dependency chain.

Functions (blueprint Phase 13 build target):
  discover(pair, prices, volumes=None) -> list[Indicator]
  _compute_fitness(signal, prices) -> float
  redundancy_check(new_ind, existing) -> str          # "OK" | "REJECTED"
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

from hermes_core.config import repo_root

# ── gates ──────────────────────────────────────────────────────────────────
OOS_FLOOR = 0.15         # [GUARD L53] indicator admitted only if OOS corr >= 0.15
COMPLEXITY_PENALTY = 0.001
REDUNDANCY_R = 0.8       # |pearson| > this vs an existing indicator -> REJECTED

DISCOVERED_DIR = repo_root() / "state" / "discovered"

# Safe primitive feature set (D8: market-data only, no crypto feeds).
# Each feature fn maps a price window -> scalar. Tree depth is bounded.
FEATURES = ("price", "ret", "sma5", "sma20", "rsi", "vol")
OPERATORS = ("add", "sub", "mul", "div")


# ── safe expression primitives (no eval) ───────────────────────────────────
def _feature(name: str, win: list[float]) -> float:
    if not win:
        return 0.0
    last = win[-1]
    if name == "price":
        return last
    if name == "ret":
        return (win[-1] / win[-2] - 1.0) * 100.0 if len(win) >= 2 and win[-2] else 0.0
    if name == "sma5":
        s = win[-5:]
        return sum(s) / len(s)
    if name == "sma20":
        s = win[-20:]
        return sum(s) / len(s)
    if name == "rsi":
        return _wilder_rsi(win)
    if name == "vol":
        if len(win) < 2:
            return 0.0
        trs = [abs(win[i] - win[i - 1]) for i in range(1, len(win))]
        return (sum(trs[-14:]) / 14) / last * 100.0 if last else 0.0
    return 0.0


def _wilder_rsi(win: list[float], period: int = 14) -> float:
    if len(win) < period + 1:
        return 50.0
    deltas = [win[i] - win[i - 1] for i in range(1, len(win))]
    gains = sum(d for d in deltas[-period:] if d > 0) / period
    losses = sum(-d for d in deltas[-period:] if d < 0) / period
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100.0 - (100.0 / (1.0 + rs))


def _eval_expr(expr, win: list[float]) -> float:
    """Evaluate a safe expression tree over a price window.

    expr is either a feature string, or (op, left, right). No eval/exec. Div
    by zero -> 0.0. Bounded so a runaway tree can't explode the runtime.
    """
    if isinstance(expr, str):
        return _feature(expr, win)
    op, a, b = expr
    x, y = _eval_expr(a, win), _eval_expr(b, win)
    if op == "add":
        return x + y
    if op == "sub":
        return x - y
    if op == "mul":
        return x * y
    if op == "div":
        return x / y if y not in (0.0, -0.0) else 0.0
    return 0.0


def _complexity(expr) -> int:
    if isinstance(expr, str):
        return 1
    return 1 + _complexity(expr[1]) + _complexity(expr[2])


def _expr_to_str(expr) -> str:
    if isinstance(expr, str):
        return expr
    op = {"add": "+", "sub": "-", "mul": "*", "div": "/"}[expr[0]]
    return f"({_expr_to_str(expr[1])}{op}{_expr_to_str(expr[2])})"


# ── signal / fitness ───────────────────────────────────────────────────────
def _signal_for_expr(expr, prices: list[float], lookback: int = 20) -> list[float]:
    """Directional signal series: evaluate the expr on each trailing window."""
    out = []
    for i in range(lookback, len(prices) + 1):
        out.append(_eval_expr(expr, prices[i - lookback:i]))
    return out


def _pearson(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    a, b = a[:n], b[:n]
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((a[i] - ma) ** 2 for i in range(n)))
    db = math.sqrt(sum((b[i] - mb) ** 2 for i in range(n)))
    if da == 0 or db == 0:
        return 0.0
    return max(-1.0, min(1.0, num / (da * db)))


def _forward_returns(prices: list[float]) -> list[float]:
    return [(prices[i + 1] / prices[i] - 1.0) * 100.0
            for i in range(len(prices) - 1)]


def _compute_fitness(signal: list[float], prices: list[float]) -> float:
    """fitness = |corr(signal, forward_return)| - 0.001*complexity.

    Complexity penalty is applied by the caller (it owns the expr); this pure
    form takes a pre-built signal + prices and returns the |corr| component.
    """
    fwd = _forward_returns(prices)
    # align lengths: signal[i] predicts fwd[i]
    m = min(len(signal), len(fwd))
    if m < 10:
        return 0.0
    corr = _pearson(signal[:m], fwd[:m])
    return abs(corr)


def _oos_corr(signal: list[float], prices: list[float], frac: float = 0.3) -> float:
    """Out-of-sample correlation: train on first (1-frac), test on last frac."""
    fwd = _forward_returns(prices)
    m = min(len(signal), len(fwd))
    if m < 20:
        return 0.0
    cut = int(m * (1 - frac))
    if cut < 10 or m - cut < 10:
        return 0.0
    return abs(_pearson(signal[:cut], fwd[:cut]))


# ── GP core ─────────────────────────────────────────────────────────────────
def _random_expr(rng: random.Random, depth: int = 2) -> object:
    if depth <= 0 or (depth < 2 and rng.random() < 0.5):
        return rng.choice(FEATURES)
    op = rng.choice(OPERATORS)
    return (op, _random_expr(rng, depth - 1), _random_expr(rng, depth - 1))


def _fitness_with_penalty(expr, prices: list[float]) -> float:
    sig = _signal_for_expr(expr, prices)
    corr = _compute_fitness(sig, prices)
    return corr - COMPLEXITY_PENALTY * _complexity(expr)


def _novelty_ok(expr, population: list[object]) -> bool:
    """Reject near-duplicate expressions; admit genuinely new shapes.

    A candidate is a duplicate (reject) if it sits CLOSER to an existing member
    than members sit to each other on average. We measure the typical
    intra-population spacing (median pairwise distance) and require the
    candidate's nearest distance to meet/exceed it. An exact clone (distance 0)
    is always rejected.
    """
    if not population:
        return True

    def dist(a, b) -> float:
        sa, sb = _expr_to_str(a), _expr_to_str(b)
        if sa == sb:
            return 0.0
        ta, tb = sa.split(), sb.split()
        inter = set(ta) & set(tb)
        union = set(ta) | set(tb)
        return 1.0 - (len(inter) / len(union) if union else 1.0)

    nearest = min(dist(expr, p) for p in population)

    if len(population) < 2:
        return nearest > 0.0          # only reject an exact clone

    intra = sorted(
        dist(a, b) for i, a in enumerate(population)
        for b in population[i + 1:]
    )
    median_intra = intra[len(intra) // 2]
    return nearest >= median_intra     # not closer than typical spacing


def redundancy_check(new_signal: list[float], existing_signals: list[list[float]]) -> str:
    """REJECTED if new signal correlates |r|>0.8 with any existing indicator."""
    for ex in existing_signals:
        if abs(_pearson(new_signal, ex)) > REDUNDANCY_R:
            return "REJECTED"
    return "OK"


def discover(pair: str, prices: list[float], volumes: list[float] | None = None,
             *, generations: int = 40, pop_size: int = 30, seed: int = 7,
             top_k: int = 5) -> list[dict]:
    """Evolve and admit indicators for `pair`.

    Returns the list of admitted indicator dicts (also persisted to
    state/discovered/{pair}.json). OOS-first: an expr must clear OOS_FLOOR and
    pass novelty + redundancy gates before admission.
    """
    rng = random.Random(seed)
    prices = list(prices)
    if len(prices) < 40:
        return []

    population: list[object] = []
    best: list[tuple[float, object]] = []
    for _ in range(generations):
        expr = _random_expr(rng, depth=2)
        fit = _fitness_with_penalty(expr, prices)
        best.append((fit, expr))
        if len(best) > pop_size:
            best.sort(reverse=True)
            best.pop()
    best.sort(reverse=True)

    admitted: list[dict] = []
    existing_signals: list[list[float]] = []
    for fit, expr in best:
        if fit < OOS_FLOOR:
            continue
        if not _novelty_ok(expr, population):
            continue
        sig = _signal_for_expr(expr, prices)
        if redundancy_check(sig, existing_signals) == "REJECTED":
            continue
        oos = _oos_corr(sig, prices)
        if oos < OOS_FLOOR:
            continue
        ind = {
            "pair": pair,
            "expr": _expr_to_str(expr),
            "fitness": round(fit, 4),
            "oos_corr": round(oos, 4),
            "complexity": _complexity(expr),
        }
        admitted.append(ind)
        existing_signals.append(sig)
        population.append(expr)
        if len(admitted) >= top_k:
            break

    if admitted:
        _save_discovered(pair, admitted)
    return admitted


# ── persistence (survives restart) ──────────────────────────────────────────
def _discovered_path(pair: str) -> Path:
    DISCOVERED_DIR.mkdir(parents=True, exist_ok=True)
    safe = pair.replace("/", "_")
    return DISCOVERED_DIR / f"{safe}.json"


def _save_discovered(pair: str, inds: list[dict]) -> None:
    _discovered_path(pair).write_text(json.dumps(inds, indent=2), encoding="utf-8")


def load_discovered_indicators(pair: str) -> list[dict]:
    p = _discovered_path(pair)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


class GeneticEngine:
    """Roadmap S13 contract wrapper."""

    def discover(self, pair: str, prices: list[float],
                 volumes: list[float] | None = None) -> list[dict]:
        return discover(pair, prices, volumes)

    def load(self, pair: str) -> list[dict]:
        return load_discovered_indicators(pair)
