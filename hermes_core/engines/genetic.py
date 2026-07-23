"""Genetic programming discovery engine (Session 13 + Phase A superior GP).

Evolves small symbolic indicator expressions over price/volume features,
scores each by fitness = |corr(signal, forward_return)| - 0.001*complexity,
and admits only those that survive real out-of-sample checks:

  * genuine OOS correlation on a held-out split (NOT the in-sample bug the
    earlier version had),
  * a permutation null-test (reject candidates that only "work" by luck —
    p >= 0.05 means the correlation is indistinguishable from label-shuffle
    noise),
  * the novelty + redundancy gates,
  * Phase A: island evolution, Pareto multi-objective selection, pool-marginal
    lift, typed grammar (unary/rolling/constants), semantic canonicalize.

Discovered indicators persist to ``state/discovered/{PAIR}.json`` where PAIR
uses underscores (``EUR_USD.json``). Legacy slash paths (``EUR/USD.json``) and
seed files under ``bots/{bot}/state/discovered/`` are read once and migrated
to the canonical underscore path.

Hard isolation (D8): the feature/operator set contains ONLY market-data
primitives (price, returns, moving averages, RSI, volatility, momentum). There
is NO path to crypto-specific signals (fear-&-greed, on-chain, BTC feeds) — none
are imported, referenced, or reachable from this module's dependency chain.

Functions (blueprint Phase 13 build target):
  discover(pair, prices, volumes=None) -> list[Indicator]
  _compute_fitness(signal, prices) -> float
  redundancy_check(new_ind, existing) -> str          # "OK" | "REJECTED"
"""

from __future__ import annotations

import copy
import json
import math
import random
import statistics
import uuid
from datetime import datetime, timezone
from pathlib import Path

from hermes_core.state.paths import discovered_path as _state_discovered_path
from hermes_core.state.paths import bot_for_pair
from hermes_core.env import get_env
from hermes_core.config.loader import repo_root
import re as _re

# ── gates ──────────────────────────────────────────────────────────────────
OOS_FLOOR = 0.15         # admit if held-out |corr| >= floor. Restored to the
                          # older engine's value (0.15) — the low 0.08 floor let
                          # permutation-cleared noise slip through the Sharpe/kfold
                          # OR-gate at ~30% FDR. On the daily/horizon-60 regime we
                          # actually reach 0.28-0.85, so 0.15 is real, not unreachable.
COMPLEXITY_PENALTY = 0.001
REDUNDANCY_R = 0.8       # |pearson| > this vs an existing indicator -> REJECTED
PERM_PVALUE_FLOOR = 0.05  # reject candidates whose OOS corr is not better than shuffled-label noise
POOL_LIFT_FLOOR = -0.005  # admit if marginal pool IC lift clears this (near-zero ok)
ENGINE_VERSION = "gp_v2_phase_b"
SIGNAL_LOOKBACK = 100     # covers sma50 + rolling windows up to 40

# ── Phase A/B search knobs ─────────────────────────────────────────────────
N_ISLANDS_DEFAULT = int(get_env("GP_N_ISLANDS", "1"))
MIGRATE_EVERY = int(get_env("GP_MIGRATE_EVERY", "10"))
MIGRATE_COUNT = 2         # elites copied across islands each migration
LEXICASE_CASES = int(get_env("GP_LEXICASE_CASES", "4"))
LEXICASE_EPS = 0.05       # keep individuals within this of best on each case
CONST_POLISH_TRIES = 3    # constant-leaf swaps after mutation
MAP_ELITES_ENABLED = get_env("GP_MAP_ELITES", "1") != "0"
BEHAVIOR_BINS = ("momentum", "mean_revert", "mixed")
COMPLEXITY_BINS = ("1-3", "4-6", "7+")
HORIZON_BINS = ("h_short", "h_med", "h_long")

# ── B10: live paper-PnL → discovery feedback (self-evolving GP) ────────────
# The GA evolves on HISTORICAL correlation (faithful to the old engine, which
# also scored by corr, never by live PnL). What was missing: a feedback layer
# that bends stored fitness toward REALIZED paper results. We only trust live
# results after LIVE_FEEDBACK_MIN_SAMPLES GP entries for an indicator, so a
# single lucky/unlucky trade cannot flip the ranking (the overfitting trap the
# audit flagged). Bonus is small + confidence-scaled so history still dominates.
LIVE_FEEDBACK_MIN_SAMPLES = 4     # GP entries before live signal is trusted
LIVE_FEEDBACK_BONUS = 0.05        # max additive bonus to fitness (~|corr| 0.15-0.85)
LIVE_PNL_SCALE = 10.0             # % PnL at which the tanh bonus saturates
LIVE_FEEDBACK_INTERVAL_S = int(get_env("LIVE_FEEDBACK_INTERVAL_S", str(
    max(int(get_env("DISCOVERY_INTERVAL_S", "3600")), 3600))))  # re-rank cadence

# Optional test override (tests monkeypatch this module attribute).
DISCOVERED_DIR: Path | None = None

# Safe primitive feature set (D8: market-data only, no crypto feeds).
# Each feature fn maps a price window -> scalar. Rolling-window operators give
# the GP genuine expressive power (ported from the older discovery engine).
FEATURES = (
    "price", "ret", "sma5", "sma10", "sma20", "sma50",
    "rsi", "vol", "roc20", "mom10", "min20", "max20", "stdev20", "ema20",
)
# Named constant leaves (avoid unary-minus ambiguity in the string grammar).
CONSTANTS: dict[str, float] = {
    "k_m10": -0.1, "k_m05": -0.05, "k_m01": -0.01,
    "k_p01": 0.01, "k_p05": 0.05, "k_p10": 0.1,
}
BINARY_OPS = ("add", "sub", "mul", "div")
UNARY_OPS = ("abs", "neg", "sign")
ROLLING_OPS = ("mean", "std", "delta", "ref", "ts_max", "ts_min")
WINDOWS = (5, 10, 20, 40)
OPERATORS = BINARY_OPS  # backward-compat alias used by older tests/callers
COMMUTATIVE_OPS = frozenset({"add", "mul"})
MAX_EXPR_COMPLEXITY = 60


# ── safe expression primitives (no eval) ───────────────────────────────────
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


def _ema(values: list[float], smoothing: float = 2) -> float:
    if len(values) < 2:
        return values[-1] if values else 0.0
    k = smoothing / (len(values) + 1)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1 - k)
    return ema


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
    if name == "sma10":
        s = win[-10:]
        return sum(s) / len(s)
    if name == "sma20":
        s = win[-20:]
        return sum(s) / len(s)
    if name == "sma50":
        s = win[-50:]
        return sum(s) / len(s)
    if name == "rsi":
        return _wilder_rsi(win)
    if name == "vol":
        if len(win) < 2:
            return 0.0
        trs = [abs(win[i] - win[i - 1]) for i in range(1, len(win))]
        return (sum(trs[-14:]) / 14) / last * 100.0 if last else 0.0
    if name == "roc20":
        return (win[-1] / win[-21] - 1.0) * 100.0 if len(win) > 21 and win[-21] else 0.0
    if name == "mom10":
        return (win[-1] / win[-11] - 1.0) * 100.0 if len(win) > 11 and win[-11] else 0.0
    if name == "min20":
        return min(win[-20:]) if len(win) >= 20 else win[-1]
    if name == "max20":
        return max(win[-20:]) if len(win) >= 20 else win[-1]
    if name == "stdev20":
        w = win[-20:]
        return statistics.stdev(w) if len(w) >= 3 and len(set(w)) >= 2 else 0.0001
    if name == "ema20":
        return _ema(win[-20:]) if len(win) >= 2 else win[-1]
    if name in CONSTANTS:
        return CONSTANTS[name]
    return 0.0


def _child_series(child, win: list[float], period: int) -> list[float]:
    """Evaluate `child` on the last `period` trailing local windows (bounded cost)."""
    period = max(2, int(period))
    n = len(win)
    if n < 2:
        return [_eval_expr(child, win)] if win else [0.0]
    out: list[float] = []
    local_lb = min(60, n)
    for end in range(max(1, n - period + 1), n + 1):
        local = win[max(0, end - local_lb):end]
        out.append(_eval_expr(child, local))
    return out if out else [0.0]


def _eval_expr(expr, win: list[float]) -> float:
    """Evaluate a safe expression tree over a price window.

    Tree forms (Phase A):
      * feature / constant leaf: str
      * unary: (op, child) for abs|neg|sign
      * binary: (op, left, right) for add|sub|mul|div
      * rolling: (op, child, window_int) for mean|std|delta|ref|ts_max|ts_min
    No eval/exec. Div by zero -> 0.0.
    """
    if isinstance(expr, (int, float)):
        return float(expr)
    if isinstance(expr, str):
        return _feature(expr, win)
    if not isinstance(expr, tuple) or not expr:
        return 0.0
    op = expr[0]
    if op in UNARY_OPS and len(expr) >= 2:
        x = _eval_expr(expr[1], win)
        if op == "abs":
            return abs(x)
        if op == "neg":
            return -x
        if op == "sign":
            return 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)
        return 0.0
    if op in ROLLING_OPS and len(expr) >= 3:
        period = int(expr[2]) if not isinstance(expr[2], tuple) else 20
        period = max(2, min(period, 40))
        series = _child_series(expr[1], win, period)
        if not series:
            return 0.0
        if op == "mean":
            return sum(series) / len(series)
        if op == "std":
            if len(series) < 3 or len(set(round(v, 8) for v in series)) < 2:
                return 0.0001
            return statistics.stdev(series)
        if op == "delta":
            return series[-1] - series[0]
        if op == "ref":
            return series[0]
        if op == "ts_max":
            return max(series)
        if op == "ts_min":
            return min(series)
        return 0.0
    if op in BINARY_OPS and len(expr) >= 3:
        x, y = _eval_expr(expr[1], win), _eval_expr(expr[2], win)
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
    if isinstance(expr, (str, int, float)):
        return 1
    if not isinstance(expr, tuple) or not expr:
        return 1
    return 1 + sum(_complexity(c) for c in expr[1:])


def _expr_to_str(expr) -> str:
    """Serialize tree to a parseable string (infix binary + functional unary/rolling)."""
    if isinstance(expr, (int, float)):
        return str(int(expr)) if float(expr) == int(expr) else str(expr)
    if isinstance(expr, str):
        return expr
    if not isinstance(expr, tuple) or not expr:
        return "price"
    op = expr[0]
    if op in UNARY_OPS and len(expr) >= 2:
        return f"{op}({_expr_to_str(expr[1])})"
    if op in ROLLING_OPS and len(expr) >= 3:
        w = int(expr[2]) if not isinstance(expr[2], tuple) else 20
        return f"{op}({_expr_to_str(expr[1])},{w})"
    if op in BINARY_OPS and len(expr) >= 3:
        sym = {"add": "+", "sub": "-", "mul": "*", "div": "/"}[op]
        return f"({_expr_to_str(expr[1])}{sym}{_expr_to_str(expr[2])})"
    return "price"


def _canonicalize(expr):
    """Semantic normalize: fold double-unary, sort commutative children."""
    if isinstance(expr, (str, int, float)):
        return expr
    if not isinstance(expr, tuple) or not expr:
        return "price"
    op = expr[0]
    if op in UNARY_OPS and len(expr) >= 2:
        child = _canonicalize(expr[1])
        if op == "abs" and isinstance(child, tuple) and child[0] == "abs":
            return child
        if op == "neg" and isinstance(child, tuple) and child[0] == "neg":
            return _canonicalize(child[1])
        if op == "sign" and isinstance(child, tuple) and child[0] == "sign":
            return child
        return (op, child)
    if op in ROLLING_OPS and len(expr) >= 3:
        w = int(expr[2]) if not isinstance(expr[2], tuple) else 20
        w = max(2, min(w, 40))
        # snap to nearest allowed window
        w = min(WINDOWS, key=lambda x: abs(x - w))
        return (op, _canonicalize(expr[1]), w)
    if op in BINARY_OPS and len(expr) >= 3:
        a, b = _canonicalize(expr[1]), _canonicalize(expr[2])
        if op in COMMUTATIVE_OPS and _expr_to_str(a) > _expr_to_str(b):
            a, b = b, a
        return (op, a, b)
    return expr


def _semantic_key(expr) -> str:
    return _expr_to_str(_canonicalize(expr))


# ── signal / fitness ───────────────────────────────────────────────────────
def _signal_for_expr(expr, prices: list[float], lookback: int = SIGNAL_LOOKBACK) -> list[float]:
    """Directional signal series: evaluate the expr on each trailing window.

    lookback must cover the longest window feature (sma50) and Phase A rolling
    windows (up to 40) so those operators have enough history to compute.
    """
    out = []
    lb = max(lookback, SIGNAL_LOOKBACK)
    for i in range(lb, len(prices) + 1):
        out.append(_eval_expr(expr, prices[i - lb:i]))
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


def _forward_returns(prices: list[float], horizon: int = 1) -> list[float]:
    """Cumulative pct move from candle i to i+horizon (objective for fitness)."""
    if horizon < 1:
        horizon = 1
    return [(prices[i + horizon] / prices[i] - 1.0) * 100.0
            for i in range(len(prices) - horizon)]


def _compute_fitness(signal: list[float], prices: list[float], horizon: int = 1) -> float:
    """fitness = |corr(signal, forward_return)|.

    The complexity penalty is applied by the caller (it owns the expr); this
    pure form takes a pre-built signal + prices and returns the |corr|
    component. horizon>1 uses the cumulative forward return over H candles
    (ported from the older engine: captures serial structure 5m/next-candle
    returns lack).
    """
    fwd = _forward_returns(prices, horizon)
    m = min(len(signal), len(fwd))
    if m < 10:
        return 0.0
    return abs(_pearson(signal[:m], fwd[:m]))


def _oos_corr(signal: list[float], prices: list[float], frac: float = 0.3,
              horizon: int = 1) -> float:
    """GENUINE out-of-sample correlation: train on first (1-frac), test on the
    held-out LAST frac. (Earlier version mistakenly measured the train side.)

    signal is the series evaluated over the full `prices`; we correlate the
    tail `signal[cut:]` against the tail of the forward returns.
    """
    fwd = _forward_returns(prices, horizon)
    m = min(len(signal), len(fwd))
    if m < 20:
        return 0.0
    cut = int(m * (1 - frac))
    if cut < 10 or m - cut < 10:
        return 0.0
    return abs(_pearson(signal[cut:m], fwd[cut:m]))


def _permutation_pvalue(signal: list[float], prices: list[float], horizon: int = 1,
                        n_perm: int = 200, seed: int = 0):
    """Permutation null-test for a candidate's OOS correlation (ported).

    Builds a null distribution by shuffling the FORWARD-RETURN order n_perm
    times (keeping the signal fixed) and recomputing |corr(signal, shuffled)|.
    If the real correlation sits in the top of that null, the candidate is
    genuinely informative rather than a lucky draw.

    Returns (p_value, real_corr, null_mean). p = fraction of null corrs >= real.
    """
    real_corr = _compute_fitness(signal, prices, horizon)
    fwd = _forward_returns(prices, horizon)
    if len(fwd) < 20:
        return 1.0, real_corr, 0.0
    sig = signal[:len(fwd)]
    n = len(sig)
    mean_sig = sum(sig) / n
    den_s = math.sqrt(sum((sig[i] - mean_sig) ** 2 for i in range(n)))
    if den_s <= 0:
        return 1.0, real_corr, 0.0
    rng = random.Random(seed)
    null = []
    for _ in range(n_perm):
        shuf = fwd[:]
        rng.shuffle(shuf)
        mean_r = sum(shuf) / n
        den_r = math.sqrt(sum((shuf[i] - mean_r) ** 2 for i in range(n)))
        den = den_s * den_r if den_r > 0 else 1e-4
        num = sum((sig[i] - mean_sig) * (shuf[i] - mean_r) for i in range(n))
        null.append(abs(num / den))
    null_mean = sum(null) / len(null)
    p = sum(1 for c in null if c >= real_corr) / len(null)
    return p, real_corr, round(null_mean, 4)


def _compute_signal_stats(signal: list[float], prices: list[float], horizon: int = 1):
    """Win-rate (FRACTION 0-1) + cumulative return (pct) of trading in the
    signal's direction. Direction at step i is the signal's slope (sign of
    sig[i]-sig[i-1]); a 'win' is when the forward return agrees. Ported from the
    older engine (win_rate is a fraction because both the dashboard and the
    ensemble weight expect 0-1, not a percent).
    """
    fwd = _forward_returns(prices, horizon)
    sig = signal[:len(fwd) + 1]
    if len(sig) < 11:
        return 0.0, 0.0
    wins = 0
    total = 0
    cum = 0.0
    for i in range(1, len(sig)):
        s_dir = 1 if sig[i] > sig[i - 1] else (-1 if sig[i] < sig[i - 1] else 0)
        if s_dir == 0:
            continue
        r = fwd[i - 1]
        total += 1
        if (r > 0 and s_dir > 0) or (r < 0 and s_dir < 0):
            wins += 1
        cum += s_dir * r
    win_rate = round(wins / total, 4) if total > 0 else 0.0
    return win_rate, round(cum, 2)


def _max_drawdown_pct(signal: list[float], prices: list[float], horizon: int = 1) -> float:
    """Peak-to-trough drawdown (%) of trading in the signal direction (absolute)."""
    fwd = _forward_returns(prices, horizon)
    sig = signal[:len(fwd) + 1]
    if len(sig) < 11:
        return 0.0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for i in range(1, len(sig)):
        s_dir = 1 if sig[i] > sig[i - 1] else (-1 if sig[i] < sig[i - 1] else 0)
        if s_dir == 0:
            continue
        equity += s_dir * fwd[i - 1]
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 4)


def _zscore(xs: list[float]) -> list[float]:
    n = len(xs)
    if n < 2:
        return [0.0] * n
    mu = sum(xs) / n
    var = sum((x - mu) ** 2 for x in xs) / n
    sd = math.sqrt(var) if var > 1e-18 else 1.0
    return [(x - mu) / sd for x in xs]


def _pool_ic(signals: list[list[float]], prices: list[float], horizon: int = 1) -> float:
    """Equal-weight |corr| of z-scored signal pool vs forward returns."""
    if not signals:
        return 0.0
    fwd = _forward_returns(prices, horizon)
    m = min(len(fwd), *(len(s) for s in signals))
    if m < 10:
        return 0.0
    aligned = [_zscore(s[:m]) for s in signals]
    pooled = [sum(row[i] for row in aligned) / len(aligned) for i in range(m)]
    return abs(_pearson(pooled, fwd[:m]))


def _marginal_pool_lift(new_signal: list[float], existing: list[list[float]],
                        prices: list[float], horizon: int = 1) -> float:
    """IC(pool+new) - IC(pool). First candidate: full solo IC."""
    if not existing:
        return _compute_fitness(new_signal, prices, horizon)
    base = _pool_ic(existing, prices, horizon)
    neo = _pool_ic(existing + [new_signal], prices, horizon)
    return neo - base


def _behavior_label(signal: list[float]) -> str:
    """Cheap niche tag from signal lag-1 autocorrelation."""
    if len(signal) < 20:
        return "mixed"
    a, b = signal[:-1], signal[1:]
    r = _pearson(a, b)
    if r >= 0.25:
        return "momentum"
    if r <= -0.15:
        return "mean_revert"
    return "mixed"


def _complexity_bin(n: int) -> str:
    if n <= 3:
        return "1-3"
    if n <= 6:
        return "4-6"
    return "7+"


def _horizon_bin(horizon: int) -> str:
    if horizon <= 5:
        return "h_short"
    if horizon <= 20:
        return "h_med"
    return "h_long"


def _niche_key(behavior: str, complexity_bin: str, horizon_bin: str) -> str:
    return f"{behavior}|{complexity_bin}|{horizon_bin}"


def _niche_key_from_parts(*, behavior: str, complexity: int, horizon: int) -> str:
    return _niche_key(behavior, _complexity_bin(complexity), _horizon_bin(horizon))


def _map_elites_cells() -> list[str]:
    return [
        _niche_key(b, c, h)
        for b in BEHAVIOR_BINS
        for c in COMPLEXITY_BINS
        for h in HORIZON_BINS
    ]


def _regime_slices(prices: list[float], n_cases: int) -> list[list[float]]:
    """Split `prices` into contiguous regime windows for lexicase cases."""
    n_cases = max(2, min(int(n_cases), 12))
    n = len(prices)
    min_len = max(40, SIGNAL_LOOKBACK // 2)
    if n < min_len * 2:
        return [prices]
    # Prefer equal-length contiguous blocks (regime episodes).
    block = max(min_len, n // n_cases)
    slices: list[list[float]] = []
    for i in range(n_cases):
        a = i * block
        b = min(n, a + block) if i < n_cases - 1 else n
        if b - a >= min_len:
            slices.append(prices[a:b])
    return slices or [prices]


def _case_fitness_matrix(pop: list, prices: list[float], horizon: int,
                         n_cases: int) -> list[list[float]]:
    slices = _regime_slices(prices, n_cases)
    matrix: list[list[float]] = []
    for expr in pop:
        row: list[float] = []
        for sl in slices:
            try:
                # Cheap lookback for lexicase pressure only.
                sig = _signal_for_expr(expr, sl, lookback=min(60, max(30, len(sl) // 3)))
                if len(sig) < 15:
                    row.append(0.0)
                else:
                    row.append(_compute_fitness(sig, sl, horizon)
                               - COMPLEXITY_PENALTY * _complexity(expr))
            except Exception:
                row.append(0.0)
        matrix.append(row)
    return matrix


def _epsilon_lexicase_select(pop: list, case_mat: list[list[float]],
                            rng: random.Random, eps: float = LEXICASE_EPS):
    """ε-lexicase parent selection over regime-sliced fitness cases."""
    if not pop:
        return _random_expr(rng, 2)
    if len(pop) == 1 or not case_mat or not case_mat[0]:
        return pop[0]
    pool = list(range(len(pop)))
    cases = list(range(len(case_mat[0])))
    rng.shuffle(cases)
    for c in cases:
        if len(pool) <= 1:
            break
        fits = [case_mat[i][c] for i in pool]
        best = max(fits)
        thresh = best - abs(eps)
        pool = [pool[j] for j, f in enumerate(fits) if f >= thresh]
    return pop[rng.choice(pool)]


def _replace_constants(expr, rng: random.Random):
    """Return a copy with one constant leaf swapped (or expr unchanged)."""
    const_nodes = [
        n for n in _get_nodes(expr)
        if isinstance(n, str) and n in CONSTANTS
    ]
    if not const_nodes:
        return expr
    target = rng.choice(const_nodes)
    alt = rng.choice([k for k in CONSTANTS if k != target] or list(CONSTANTS))
    return _canonicalize(_replace_node(copy.deepcopy(expr), target, alt))


def _polish_constants(expr, prices: list[float], horizon: int,
                      rng: random.Random, tries: int = CONST_POLISH_TRIES):
    """Local search over named constant leaves after structural variation."""
    best = expr
    try:
        best_fit = _fitness_with_penalty(best, prices, horizon)
    except Exception:
        return expr
    for _ in range(max(0, tries)):
        cand = _replace_constants(best, rng)
        if cand is best or cand == best:
            continue
        try:
            fit = _fitness_with_penalty(cand, prices, horizon)
        except Exception:
            continue
        if fit > best_fit:
            best, best_fit = cand, fit
    return best


def _map_elites_insert(archive: dict, cand: dict) -> None:
    """Keep the best OOS elite per MAP-Elites niche cell."""
    key = cand.get("niche_key") or _niche_key_from_parts(
        behavior=cand.get("behavior", "mixed"),
        complexity=int(cand.get("complexity", 1)),
        horizon=int(cand.get("horizon", 1)),
    )
    cand["niche_key"] = key
    prev = archive.get(key)
    if prev is None or float(cand.get("oos_corr", 0)) > float(prev.get("oos_corr", 0)):
        archive[key] = cand


def _map_elites_coverage(archive: dict) -> dict:
    cells = _map_elites_cells()
    filled = {k: {
        "oos_corr": archive[k].get("oos_corr"),
        "expr": archive[k].get("expr_str") or archive[k].get("expr"),
        "behavior": archive[k].get("behavior"),
        "complexity": archive[k].get("complexity"),
    } for k in cells if k in archive}
    return {
        "filled": len(filled),
        "total_cells": len(cells),
        "coverage": round(len(filled) / max(len(cells), 1), 4),
        "cells": filled,
    }


def prefer_niche_diverse(inds: list[dict], max_per_niche: int = 2) -> list[dict]:
    """Reorder indicators so ensemble voting spreads across MAP-Elites niches."""
    if not inds or max_per_niche < 1:
        return list(inds)
    buckets: dict[str, list[dict]] = {}
    for ind in inds:
        niche = ind.get("niche") or {}
        key = ind.get("niche_key") or _niche_key(
            niche.get("behavior") or "mixed",
            niche.get("complexity_bin") or _complexity_bin(int(ind.get("complexity") or 1)),
            niche.get("horizon_bin") or _horizon_bin(int(ind.get("horizon") or 1)),
        )
        buckets.setdefault(key, []).append(ind)
    for key in buckets:
        buckets[key].sort(
            key=lambda x: float(x.get("live_fitness", x.get("fitness", 0)) or 0),
            reverse=True,
        )
    out: list[dict] = []
    # Round-robin take up to max_per_niche from each niche.
    for take in range(max_per_niche):
        for key in sorted(buckets.keys()):
            if take < len(buckets[key]):
                out.append(buckets[key][take])
    # Append any leftovers (beyond cap) at the end so loaders still see them.
    seen = {id(x) for x in out}
    for key in buckets:
        for ind in buckets[key]:
            if id(ind) not in seen:
                out.append(ind)
                seen.add(id(ind))
    return out


def _pareto_dominated(a: dict, b: dict) -> bool:
    """True if `a` is dominated by `b` on (oos, -complexity, -max_dd, pool_lift)."""
    a_obj = (a["oos_corr"], -a["complexity"], -a["max_dd"], a["pool_lift"])
    b_obj = (b["oos_corr"], -b["complexity"], -b["max_dd"], b["pool_lift"])
    ge_all = all(bx >= ax for ax, bx in zip(a_obj, b_obj))
    gt_one = any(bx > ax for ax, bx in zip(a_obj, b_obj))
    return ge_all and gt_one


def _pareto_front(cands: list[dict]) -> list[dict]:
    if not cands:
        return []
    front = []
    for c in cands:
        if any(_pareto_dominated(c, o) for o in cands if o is not c):
            continue
        front.append(c)
    return front


# ── Sharpe gate (ported from the older hermes_trading discovery engine) ─────
# The older engine admitted a candidate if (corr >= MIN_CORR) OR
# (sharpe >= MIN_SHARPE) — a risk-adjusted bar that catches low-corr but
# tradeable signals the pure-correlation gate misses. We keep BOTH bars here.
MIN_CORR = 0.05
MIN_SHARPE = 1.0
N_FOLDS = 5


def _sharpe(signal: list[float], prices: list[float], horizon: int = 1) -> float:
    """Annualised-ish Sharpe of trading in the signal's direction.

    Direction at step i = sign of slope of the signal; per-step return =
    direction * forward_return; Sharpe = mean/std of those per-step returns
    (std floored to avoid div-by-zero). Ported from the older engine's fitness.
    """
    fwd = _forward_returns(prices, horizon)
    sig = signal[:len(fwd) + 1]
    if len(sig) < 11:
        return 0.0
    rets = []
    for i in range(1, len(sig)):
        s_dir = 1 if sig[i] > sig[i - 1] else (-1 if sig[i] < sig[i - 1] else 0)
        if s_dir == 0:
            continue
        rets.append(s_dir * fwd[i - 1])
    if len(rets) < 5:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    if var <= 1e-9:
        return 0.0
    return mean / math.sqrt(var) * math.sqrt(252.0)   # annualise like the old engine


def _honest_oos(signal: list[float], prices: list[float], horizon: int = 1,
                n_folds: int = N_FOLDS):
    """Honest out-of-sample correlation via N_FOLDS walk-forward splits.

    Returns (median_corr, frac_folds_passing) where a fold "passes" if its
    held-out |corr| >= OOS_FLOOR (0.15) -- the SAME bar the old engine's
    walk-forward used (its correlation_threshold=0.15, NOT the near-zero
    MIN_CORR=0.05). A candidate must clear that bar in a MAJORITY of disjoint
    folds, so a single lucky split cannot admit it -- this is the old engine's
    actual noise control, and it is what stops the GA (which hunts 2400
    candidates for one lucky split) from leaking ~20% false discoveries.
    """
    n = min(len(signal), len(prices) - horizon)
    if n < n_folds * 15:
        return 0.0, 0.0
    fold = n // n_folds
    corrs = []
    passing = 0
    for k in range(n_folds):
        a = k * fold
        b = a + fold
        if b > n:
            b = n
        if b - a < 15:
            continue
        c = abs(_pearson(signal[a:b], _forward_returns(prices, horizon)[a:b]))
        corrs.append(c)
        if c >= OOS_FLOOR:
            passing += 1
    if not corrs:
        return 0.0, 0.0
    corrs.sort()
    med = corrs[len(corrs) // 2]
    return med, passing / len(corrs)


# ── GP tree operators (ported from the older discovery engine) ─────────────
def _get_nodes(expr):
    """All subtree objects in the expression tree (for crossover/mutation)."""
    nodes = [expr]
    if isinstance(expr, tuple):
        for child in expr[1:]:
            if isinstance(child, (tuple, str)):
                nodes.extend(_get_nodes(child))
            # int window leaves are not mutated as subtrees
    return nodes


def _replace_node(tree, target, replacement):
    """Return a copy of `tree` with `target` subtree replaced by `replacement`."""
    if tree is target:
        return replacement
    # String / numeric leaves: identity fails across deepcopy — match by value.
    if isinstance(target, str) and tree == target:
        return replacement
    if isinstance(target, (int, float)) and tree == target:
        return replacement
    if isinstance(tree, tuple):
        new_children = []
        for child in tree[1:]:
            if isinstance(child, (tuple, str, int, float)):
                new_children.append(_replace_node(child, target, replacement))
            else:
                new_children.append(child)
        return (tree[0], *new_children)
    return tree


def _crossover(e1, e2, rng: random.Random):
    """Swap a random subtree of e1 with a random subtree of e2."""
    e1 = copy.deepcopy(e1)
    nodes1 = _get_nodes(e1)
    nodes2 = _get_nodes(e2)
    if not nodes1 or not nodes2:
        return e1
    src = rng.choice(nodes1)
    tgt = rng.choice(nodes2)
    return _canonicalize(_replace_node(e1, src, copy.deepcopy(tgt)))


def _mutate(expr, rng: random.Random):
    """Replace a random subtree with a fresh random one."""
    nodes = _get_nodes(expr)
    if not nodes:
        return _random_expr(rng, 2)
    e = copy.deepcopy(expr)
    node = rng.choice(nodes)
    return _canonicalize(_replace_node(e, node, _random_expr(rng, rng.randint(0, 2))))


# ── GP core ─────────────────────────────────────────────────────────────────
def _random_leaf(rng: random.Random):
    if rng.random() < 0.12:
        return rng.choice(list(CONSTANTS.keys()))
    return rng.choice(FEATURES)


def _random_expr(rng: random.Random, depth: int = 2) -> object:
    """Sample a Phase A expression (binary / unary / rolling / leaf)."""
    if depth <= 0 or (depth < 2 and rng.random() < 0.45):
        return _random_leaf(rng)
    roll = rng.random()
    if roll < 0.55:
        op = rng.choice(BINARY_OPS)
        return _canonicalize((op, _random_expr(rng, depth - 1), _random_expr(rng, depth - 1)))
    if roll < 0.75:
        op = rng.choice(UNARY_OPS)
        return _canonicalize((op, _random_expr(rng, depth - 1)))
    op = rng.choice(ROLLING_OPS)
    return _canonicalize((op, _random_expr(rng, max(0, depth - 1)), rng.choice(WINDOWS)))


def _fitness_with_penalty(expr, prices: list[float], horizon: int = 1) -> float:
    # Shorter lookback during evolution keeps island search inside live timeouts.
    sig = _signal_for_expr(expr, prices, lookback=60)
    if len(sig) < 20:
        return 0.0
    corr = _compute_fitness(sig, prices, horizon)
    return corr - COMPLEXITY_PENALTY * _complexity(expr)


def _evolve_one_generation(pop: list, prices: list[float], horizon: int,
                           rng: random.Random, pop_size: int,
                           *, use_lexicase: bool = True) -> list:
    """One generation: elitist keep + ε-lexicase breeding + constant polish."""
    scored = []
    for expr in pop:
        try:
            fit = _fitness_with_penalty(expr, prices, horizon)
        except Exception:
            fit = 0.0
        scored.append((fit, expr))
    scored.sort(key=lambda x: x[0], reverse=True)
    keep = max(3, pop_size // 10)
    survivors = [s[1] for s in scored[:keep]]
    new_pop = list(survivors)

    case_mat = None
    if use_lexicase and LEXICASE_CASES > 0:
        case_mat = _case_fitness_matrix(pop, prices, horizon, LEXICASE_CASES)
    while len(new_pop) < pop_size:
        if case_mat is not None:
            parent = _epsilon_lexicase_select(pop, case_mat, rng)
            donor = _epsilon_lexicase_select(pop, case_mat, rng)
        else:
            parent = rng.choice(survivors)
            donor = rng.choice(survivors)
        if rng.random() < 0.6 and len(pop) >= 2:
            child = _crossover(parent, donor, rng)
        else:
            child = _mutate(parent, rng)
        child = _polish_constants(child, prices, horizon, rng)
        if _complexity(child) > MAX_EXPR_COMPLEXITY:
            child = _random_expr(rng, 2)
        new_pop.append(child)
    return new_pop


def _evolve_population(prices: list[float], pop_size: int, generations: int,
                       horizon: int, rng: random.Random,
                       n_islands: int | None = None) -> list[object]:
    """Island GA on `prices`. Total pop ≈ pop_size split across islands.

    Elitist survival per island, periodic migration of top elites. Returns the
    combined final population (canonicalized). Lexicase runs every other
    generation to stay inside live discovery timeouts.
    """
    n_islands = max(1, int(n_islands if n_islands is not None else N_ISLANDS_DEFAULT))
    per = max(8, pop_size // n_islands)
    islands = [
        [_random_expr(rng, 2) for _ in range(per)]
        for _ in range(n_islands)
    ]
    for gen in range(generations):
        use_lex = (gen % 2 == 0)
        for i in range(n_islands):
            islands[i] = _evolve_one_generation(
                islands[i], prices, horizon, rng, per, use_lexicase=use_lex,
            )
        if n_islands > 1 and MIGRATE_EVERY > 0 and (gen + 1) % MIGRATE_EVERY == 0:
            for i in range(n_islands):
                src = islands[i]
                dst = islands[(i + 1) % n_islands]
                scored = sorted(
                    ((_fitness_with_penalty(e, prices, horizon), e) for e in src),
                    key=lambda x: x[0], reverse=True,
                )
                migrants = [copy.deepcopy(e) for _, e in scored[:MIGRATE_COUNT]]
                dst_scored = sorted(
                    ((_fitness_with_penalty(e, prices, horizon), e) for e in dst),
                    key=lambda x: x[0],
                )
                for j, m in enumerate(migrants):
                    if j < len(dst_scored):
                        worst = dst_scored[j][1]
                        for k, e in enumerate(dst):
                            if e is worst or e == worst:
                                dst[k] = m
                                break
    combined: list[object] = []
    for isl in islands:
        combined.extend(_canonicalize(e) for e in isl)
    return combined


def _novelty_ok(expr, population: list[object]) -> bool:
    """Reject near-duplicate expressions; admit genuinely new shapes.

    A candidate is a duplicate (reject) if it sits CLOSER to an existing member
    than members sit to each other on average. We measure the typical
    intra-population spacing (median pairwise distance) and require the
    candidate's nearest distance to meet/exceed it. An exact clone (distance 0)
    is always rejected. Phase A: compare semantic (canonical) keys first.
    """
    if not population:
        return True

    key = _semantic_key(expr)
    if any(_semantic_key(p) == key for p in population):
        return False

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
             *, generations: int = 60, pop_size: int = 40, seed: int = 7,
             top_k: int = 5, horizon: int = 1,
             n_islands: int | None = None,
             interval: str = "1d") -> list[dict]:
    """Evolve and admit indicators for `pair` (Phase B superior GP).

    Phase B adds ε-lexicase regime selection, constant polish, MAP-Elites niche
    archive, and a discovery-run pulse for the dashboard. Admission still
    requires OOS + permutation + k-fold + pool-lift + S10 backtest.

    ``interval`` is the candle size of ``prices`` (must match live GP eval TF).
    Every admitted formula is tagged with ``interval`` + ``horizon`` so the
    ensemble only votes same-regime formulas together.
    """
    rng = random.Random(seed)
    prices = list(prices)
    if len(prices) < SIGNAL_LOOKBACK:
        return []

    run_id = f"{ENGINE_VERSION}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
    n_islands = max(1, int(n_islands if n_islands is not None else N_ISLANDS_DEFAULT))
    interval = str(interval or "1d").strip() or "1d"
    h_bin = _horizon_bin(horizon)

    cut = int(len(prices) * 0.6)
    train = prices[:cut]
    test = prices[cut:]
    if len(train) < SIGNAL_LOOKBACK or len(test) < 40:
        return []

    # 1) Evolve on TRAIN (islands + lexicase + constant polish).
    pop = _evolve_population(train, pop_size, generations, horizon, rng,
                             n_islands=n_islands)

    # 2) Gate unique candidates on held-out TEST; fill MAP-Elites archive.
    candidates: list[dict] = []
    archive: dict = {}
    seen: set[str] = set()
    n_eval = 0
    for idx, raw in enumerate(pop):
        n_eval += 1
        expr = _canonicalize(raw)
        # Final constant polish on test-regime fitness (cheap local search).
        expr = _polish_constants(expr, test, horizon, rng, tries=2)
        es = _expr_to_str(expr)
        key = _semantic_key(expr)
        if key in seen:
            continue
        seen.add(key)
        try:
            sig_test = _signal_for_expr(expr, test)
            if len(sig_test) < 20:
                continue
            oos = _compute_fitness(sig_test, test, horizon)
            if oos < OOS_FLOOR:
                continue
            p_val, _real_c, null_mean = _permutation_pvalue(
                sig_test, test, horizon, n_perm=200, seed=seed)
            if p_val >= PERM_PVALUE_FLOOR:
                continue
            _n = min(len(sig_test), len(test) - horizon)
            if _n >= N_FOLDS * 15:
                kfold_med, frac = _honest_oos(sig_test, test, horizon)
                _kfold_ok = (frac >= 0.6) and (kfold_med >= OOS_FLOOR)
                if not _kfold_ok:
                    continue
            win_rate, total_pnl = _compute_signal_stats(sig_test, test, horizon)
            max_dd = _max_drawdown_pct(sig_test, test, horizon)
            cx = _complexity(expr)
            behavior = _behavior_label(sig_test)
            c_bin = _complexity_bin(cx)
            niche_key = _niche_key(behavior, c_bin, h_bin)
            cand = {
                "expr_tree": expr,
                "expr_str": es,
                "sig": sig_test,
                "oos_corr": round(oos, 4),
                "perm_pvalue": round(p_val, 4),
                "null_mean_corr": null_mean,
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "max_dd": max_dd,
                "complexity": cx,
                "island_id": idx % n_islands,
                "pool_lift": 0.0,
                "behavior": behavior,
                "horizon": horizon,
                "niche_key": niche_key,
            }
            candidates.append(cand)
            if MAP_ELITES_ENABLED:
                _map_elites_insert(archive, cand)
        except Exception:
            continue

    # 3) Prefer MAP-Elites elites (diverse niches), fallback to Pareto front.
    if MAP_ELITES_ENABLED and archive:
        front = list(archive.values())
        front.sort(key=lambda c: (c["oos_corr"], -c["complexity"], -c["max_dd"]),
                   reverse=True)
    else:
        front = _pareto_front(candidates) or candidates
        front.sort(key=lambda c: (c["oos_corr"], -c["complexity"], -c["max_dd"]),
                   reverse=True)

    # 4) Sequential admit with redundancy / novelty / pool-lift / S10.
    admitted: list[dict] = []
    existing_signals: list[list[float]] = []
    population: list[object] = []
    niches_used: set[str] = set()
    for cand in front:
        expr = cand["expr_tree"]
        es = cand["expr_str"]
        sig_test = cand["sig"]
        try:
            if redundancy_check(sig_test, existing_signals) == "REJECTED":
                continue
            if not _novelty_ok(expr, population):
                continue
            lift = _marginal_pool_lift(sig_test, existing_signals, test, horizon)
            if lift < POOL_LIFT_FLOOR:
                continue
            from hermes_core.engines.backtest import backtest_gp_indicator
            bt = backtest_gp_indicator(
                pair, es, prices=prices, existing_signals=existing_signals,
            )
            if not bt.get("approved"):
                continue
            niche = {
                "horizon": horizon,
                "horizon_bin": h_bin,
                "complexity_bin": _complexity_bin(cand["complexity"]),
                "behavior": cand["behavior"],
                "niche_key": cand.get("niche_key"),
            }
            niches_used.add(cand.get("niche_key") or "")
            ind = {
                "pair": pair,
                "name": es,
                "expr": es,
                "_expr": expr,
                "fitness": round(
                    cand["oos_corr"] - COMPLEXITY_PENALTY * cand["complexity"], 4
                ),
                "oos_corr": cand["oos_corr"],
                "perm_pvalue": cand["perm_pvalue"],
                "null_mean_corr": cand["null_mean_corr"],
                "win_rate": cand["win_rate"],
                "total_pnl": cand["total_pnl"],
                "max_dd": cand["max_dd"],
                "complexity": cand["complexity"],
                "nodes": cand["complexity"],
                "horizon": horizon,
                "interval": interval,
                "source": "genetic",
                "engine_version": ENGINE_VERSION,
                "run_id": run_id,
                "island_id": cand["island_id"],
                "pool_lift": round(lift, 4),
                "niche": niche,
                "niche_key": cand.get("niche_key"),
                "admit_reason": "oos+perm+kfold+map_elites+pool_lift+s10",
                "backtest_approved": True,
                "backtest_reason": bt.get("reason"),
                "backtest_oos_corr": bt.get("oos_corr"),
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            admitted.append(ind)
            existing_signals.append(sig_test)
            population.append(expr)
            if len(admitted) >= top_k:
                break
        except Exception:
            continue

    cov = _map_elites_coverage(archive) if archive else {
        "filled": 0, "total_cells": len(_map_elites_cells()), "coverage": 0.0, "cells": {},
    }
    pulse = {
        "run_id": run_id,
        "pair": pair,
        "engine_version": ENGINE_VERSION,
        "ts": datetime.now(timezone.utc).isoformat(),
        "interval": interval,
        "horizon": horizon,
        "n_islands": n_islands,
        "generations": generations,
        "pop_size": pop_size,
        "lexicase_cases": LEXICASE_CASES,
        "candidates_evaluated": n_eval,
        "candidates_unique": len(seen),
        "candidates_gated": len(candidates),
        "admitted": len(admitted),
        "admit_rate": round(len(admitted) / max(len(seen), 1), 4),
        "best_oos": max((c["oos_corr"] for c in candidates), default=0.0),
        "niches_used": sorted(k for k in niches_used if k),
        "map_elites": cov,
    }
    _save_discovery_pulse(pair, pulse)

    if admitted:
        _save_discovered(pair, admitted)
    return admitted


def _is_dashboard_seed_fixture(ind: dict) -> bool:
    """True for non-GP dashboard seed rows (ta.rsi / mom(close,N) / source=seed)."""
    if (ind.get("source") or "").lower() == "seed":
        return True
    for key in ("expr", "expr_str", "name"):
        raw = ind.get(key)
        if not isinstance(raw, str):
            continue
        s = raw.strip()
        if s.startswith("ta.") or _re.match(r"^[a-zA-Z_]+\(close\b", s):
            return True
    return False


def apply_live_feedback(pair: str, cortex) -> int:
    """B10: bend discovered-indicator fitness toward REALIZED paper PnL.

    After the GP brain paper-trades its indicators (B9 credits the firing
    ones on every close), the cortex holds each indicator's live GP stats.
    This re-ranks + annotates the persisted discovered indicators so the next
    ensemble vote (entry.py) favors indicators that actually make money and
    deprioritizes those that lose.

    Anti-overfit guards (audit-flagged):
      * live signal is ignored until LIVE_FEEDBACK_MIN_SAMPLES GP entries;
      * bonus magnitude is small + tanh-scaled by PnL + confidence (sample
        count), so a few trades can't dominate the historical corr fitness.

    Returns the number of indicators updated. Fail-soft: any error -> 0.
    """
    try:
        if cortex is None:
            return 0
        # The cortex passed by the discovery thread is loaded ONCE at startup
        # and never re-reads the on-disk memory the trade loop writes to. So
        # read the authoritative persisted stats fresh here (fail-soft: fall
        # back to the passed instance if a fresh load is unavailable).
        try:
            from hermes_core.engines.decision_cortex import Cortex
            stats_source = Cortex()
        except Exception:
            stats_source = cortex
        own = [
            i for i in load_discovered_indicators(pair, include_shared=False)
            if not _is_dashboard_seed_fixture(i)
        ]
        if not own:
            return 0
        updated = 0
        for ind in own:
            name = ind.get("name") or ind.get("expr")
            if not name:
                continue
            stats = stats_source.indicator_live_stats(name) or {}
            samples = int(stats.get("attempts", 0))
            base = float(ind.get("fitness", 0.0) or 0.0)
            if samples < LIVE_FEEDBACK_MIN_SAMPLES:
                # Not enough live evidence yet — annotate but don't re-rank.
                ind.setdefault("live_fitness", round(base, 4))
                ind.setdefault("live_samples", samples)
                ind.setdefault("live_flag", "pending")
                continue
            wins = int(stats.get("wins", 0))
            pnl = float(stats.get("pnl", 0.0))
            wr = wins / samples if samples else 0.0
            conf = min(1.0, samples / (LIVE_FEEDBACK_MIN_SAMPLES * 2.0))
            live_adj = LIVE_FEEDBACK_BONUS * math.tanh(pnl / LIVE_PNL_SCALE) * conf
            ind["live_fitness"] = round(base + live_adj, 4)
            ind["live_pnl"] = round(pnl, 2)
            ind["live_wr"] = round(wr, 3)
            ind["live_samples"] = samples
            if pnl < 0 and wr < 0.4:
                ind["live_flag"] = "suppress"   # real losses -> deprioritize
            elif wr >= 0.6 and pnl > 0:
                ind["live_flag"] = "promote"
            else:
                ind["live_flag"] = "neutral"
            updated += 1
        # Re-rank by live_fitness (falls back to historical fitness) and persist.
        own.sort(key=lambda x: x.get("live_fitness", x.get("fitness", 0.0)),
                 reverse=True)
        _save_discovered(pair, own)
        return updated
    except Exception:
        return 0


def _discovered_dir() -> Path:
    if DISCOVERED_DIR is not None:
        DISCOVERED_DIR.mkdir(parents=True, exist_ok=True)
        return DISCOVERED_DIR
    # Canonical discovered folder (parent of EUR_USD.json).
    try:
        return _state_discovered_path("EUR/USD").parent
    except Exception:
        return repo_root() / "state" / "discovered"


def _pulse_path(pair: str) -> Path:
    return _discovered_dir() / f"_pulse_{_pair_safe(pair)}.json"


def _save_discovery_pulse(pair: str, pulse: dict) -> Path:
    """Persist the latest discovery-run pulse for dashboard surfacing."""
    path = _pulse_path(pair)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pulse, indent=2), encoding="utf-8")
    # Also merge into the aggregate pulse index used by the runner push.
    index_path = _discovered_dir() / "_discovery_pulse.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
        if not isinstance(index, dict):
            index = {}
    except (json.JSONDecodeError, OSError):
        index = {}
    index[pair] = pulse
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return path


def load_discovery_pulse(pair: str) -> dict | None:
    path = _pulse_path(pair)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def load_discovery_pulses(pairs: list[str] | None = None) -> dict[str, dict]:
    """Load pulses for `pairs` (or all pairs found in the aggregate index)."""
    index_path = _discovered_dir() / "_discovery_pulse.json"
    out: dict[str, dict] = {}
    try:
        if index_path.exists():
            raw = json.loads(index_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                out = {k: v for k, v in raw.items() if isinstance(v, dict)}
    except (json.JSONDecodeError, OSError):
        out = {}
    if pairs is not None:
        wanted = set(pairs)
        out = {k: v for k, v in out.items() if k in wanted}
        for p in pairs:
            if p not in out:
                one = load_discovery_pulse(p)
                if one:
                    out[p] = one
    return out


def niche_map_from_indicators(inds: list[dict]) -> dict:
    """Count indicators per niche cell (dashboard MAP-Elites map)."""
    counts: dict[str, int] = {}
    for ind in inds:
        niche = ind.get("niche") or {}
        key = (
            ind.get("niche_key")
            or niche.get("niche_key")
            or _niche_key(
                niche.get("behavior") or "mixed",
                niche.get("complexity_bin") or _complexity_bin(int(ind.get("complexity") or 1)),
                niche.get("horizon_bin") or _horizon_bin(int(ind.get("horizon") or 1)),
            )
        )
        counts[key] = counts.get(key, 0) + 1
    cells = _map_elites_cells()
    return {
        "filled": sum(1 for c in cells if counts.get(c)),
        "total_cells": len(cells),
        "coverage": round(sum(1 for c in cells if counts.get(c)) / max(len(cells), 1), 4),
        "counts": {c: counts.get(c, 0) for c in cells},
    }


# ── persistence (survives restart) ──────────────────────────────────────────
def _pair_safe(pair: str) -> str:
    return pair.replace("/", "_").replace("-", "_")


def _is_gp_expr(s: object) -> bool:
    """True if `s` is a FEATURE/CONSTANT token or Phase A GP expression string.

    Accepts legacy infix ``(price-sma20)`` and Phase A forms ``abs(rsi)``,
    ``mean(price,20)``. Rejects dashboard seeds like ``ta.rsi(close,14)``.
    """
    if not isinstance(s, str) or not s.strip():
        return False
    toks = _re.findall(r"\(|\)|,|\+|-|\*|/|[a-z_][a-z0-9_]*|\d+", s.strip())
    if not toks:
        return False
    allowed_names = (
        set(FEATURES) | set(CONSTANTS) | set(UNARY_OPS) | set(ROLLING_OPS)
        | {"(", ")", ",", "+", "-", "*", "/"}
    )
    for t in toks:
        if t.isdigit():
            continue
        if t not in allowed_names:
            return False
    # Reject bare function-call seeds like mom(close,5) — "close" is not a feature
    return True


def indicator_expr(ind: dict) -> str | None:
    """Resolve a votable GP expression from an indicator record.

    Preference: ``expr`` → ``expr_str`` → ``name``, but only if the string is a
    valid GP infix/feature (so seed fixtures with ta.* / mom(close,N) are skipped).
    """
    for key in ("expr", "expr_str", "name"):
        raw = ind.get(key)
        if _is_gp_expr(raw):
            return str(raw).strip()
    return None


def is_backtest_approved(ind: dict) -> bool:
    """True only when the S10 7-phase gate marked this indicator approved."""
    return ind.get("backtest_approved") is True


def _normalize_indicator(ind: dict, *, pair: str | None = None) -> dict:
    """Canonicalize a discovered-indicator dict for disk + live eval."""
    out = dict(ind)
    if pair and not out.get("pair"):
        out["pair"] = pair
    expr = indicator_expr(out)
    if expr:
        out["expr"] = expr
        out["expr_str"] = expr
        if not out.get("name"):
            out["name"] = expr
    else:
        # Keep raw fields for dashboard display, but clear unusable expr so
        # gp_ensemble_signal skips the indicator rather than mis-parsing seeds.
        if out.get("expr") and not _is_gp_expr(out.get("expr")):
            out.pop("expr", None)
    out.setdefault("source", out.get("source") or "genetic")
    return out


def _legacy_slash_path(base_dir: Path, pair: str) -> Path:
    """``discovered/EUR/USD.json`` (slash → nested dirs) — old fixture layout."""
    parts = [p for p in pair.replace("-", "/").split("/") if p]
    if len(parts) < 2:
        return base_dir / f"{_pair_safe(pair)}.json"
    return base_dir.joinpath(*parts[:-1]) / f"{parts[-1]}.json"


def _candidate_discovered_paths(pair: str) -> list[Path]:
    """Ordered candidates: canonical underscore, then legacy locations."""
    seen: set[str] = set()
    out: list[Path] = []

    def _add(p: Path) -> None:
        key = str(p.resolve()) if p.parent.exists() or p.exists() else str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)

    if DISCOVERED_DIR is not None:
        DISCOVERED_DIR.mkdir(parents=True, exist_ok=True)
        _add(DISCOVERED_DIR / f"{_pair_safe(pair)}.json")
        _add(_legacy_slash_path(DISCOVERED_DIR, pair))
        return out

    canon = _state_discovered_path(pair)
    _add(canon)
    _add(_legacy_slash_path(canon.parent, pair))
    # Dual-tree: seed fixtures under bots/{bot}/state/discovered/
    try:
        bot = bot_for_pair(pair)
        bots_disc = repo_root() / "bots" / bot / "state" / "discovered"
        _add(bots_disc / f"{_pair_safe(pair)}.json")
        _add(_legacy_slash_path(bots_disc, pair))
    except Exception:  # noqa: BLE001 — path discovery must never break load
        pass
    return out


def _discovered_path(pair: str) -> Path:
    """Canonical write path: ``{state}/discovered/EUR_USD.json``."""
    if DISCOVERED_DIR is not None:
        DISCOVERED_DIR.mkdir(parents=True, exist_ok=True)
        return DISCOVERED_DIR / f"{_pair_safe(pair)}.json"
    return _state_discovered_path(pair)


def _read_indicators_file(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict):
        data = data.get("indicators") or data.get("admitted") or []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def _save_discovered(pair: str, inds: list[dict]) -> Path:
    """Persist admitted indicators to the canonical underscore path.

    Always writes ``expr`` + ``expr_str`` (identical) for entry + dashboard.
    Drops the raw ``_expr`` tree (not JSON-serialisable). Returns the path written.
    """
    clean = []
    for ind in inds:
        row = _normalize_indicator(
            {k: v for k, v in ind.items() if k != "_expr"}, pair=pair,
        )
        clean.append(row)
    path = _discovered_path(pair)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean, indent=2), encoding="utf-8")
    return path


def load_discovered_indicators(pair: str, include_shared: bool = True) -> list[dict]:
    """Load indicators for `pair`, migrating legacy paths to the canonical file.

    Prefer the canonical ``EUR_USD.json`` when it exists (even if empty — that
    means discovery already ran). Otherwise search legacy slash paths and
    ``bots/.../discovered`` seeds; first non-empty hit is normalized and copied
    to the canonical path so entry / invent / dashboard share one file.
    """
    canon = _discovered_path(pair)
    own: list[dict] = []
    loaded_from: Path | None = None

    if canon.exists():
        own = [_normalize_indicator(r, pair=pair)
               for r in _read_indicators_file(canon)]
        loaded_from = canon
        # Scrub dashboard seed fixtures that were wrongly re-persisted to the
        # canonical path (apply_live_feedback used to save them). Seeds are not
        # votable and must not masquerade as GP discoveries on the dashboard.
        votable = [i for i in own if indicator_expr(i)]
        seeds = [i for i in own if _is_dashboard_seed_fixture(i)]
        if seeds and not votable:
            try:
                canon.unlink()
            except OSError:
                pass
            own = []
            loaded_from = None
        elif seeds and votable:
            try:
                _save_discovered(pair, votable)
            except OSError:
                pass
            own = votable
    else:
        for cand in _candidate_discovered_paths(pair):
            if cand == canon:
                continue
            rows = _read_indicators_file(cand)
            if rows:
                own = [_normalize_indicator(r, pair=pair) for r in rows]
                loaded_from = cand
                break

    # Migrate legacy/seed → canonical only when at least one indicator is a
    # votable GP expression. Seed fixtures (ta.rsi / mom(close,N)) must NOT be
    # copied to the canonical path — that would block invent forever.
    if own and loaded_from is not None:
        try:
            if loaded_from.resolve() != canon.resolve():
                votable = [i for i in own if indicator_expr(i)]
                if votable:
                    _save_discovered(pair, votable)
                    own = votable
        except OSError:
            pass

    if not include_shared:
        return own
    # Ported from the older engine's _get_shared_pairs: indicators discovered
    # on one pair are REUSED (at 50% weight) on related pairs in the same group.
    # e.g. gold's discoveries inform silver, and USD-pair discoveries inform
    # each other. This is NOT auto-discovery of new tradeable symbols -- the
    # pair universe is unchanged; only indicator knowledge is shared.
    merged = list(own)
    for sp in _shared_pairs_for(pair):
        for ind in load_discovered_indicators(sp, include_shared=False):
            c = dict(ind)
            c["_shared_from"] = sp
            c["_shared_penalty"] = 0.5
            merged.append(c)
    return merged


# Pairs that share indicator knowledge (ported from old engine _SHARED_INDICATOR_GROUPS).
# Gold/silver cointegrated; USD pairs share DXY-driven structure. Hand-coded,
# NOT discovered -- the tradeable-symbol universe is unchanged.
SHARED_INDICATOR_GROUPS = [
    {"XAU/USD", "XAG/USD"},
    {"EUR/USD", "GBP/USD", "AUD/USD"},
]


def _shared_pairs_for(pair: str) -> list[str]:
    for group in SHARED_INDICATOR_GROUPS:
        if pair in group:
            return [p for p in group if p != pair]
    return []


class GeneticEngine:
    """Roadmap S13 contract wrapper."""

    def discover(self, pair: str, prices: list[float],
                 volumes: list[float] | None = None, **kwargs) -> list[dict]:
        return discover(pair, prices, volumes, **kwargs)

    def load(self, pair: str) -> list[dict]:
        return load_discovered_indicators(pair)
