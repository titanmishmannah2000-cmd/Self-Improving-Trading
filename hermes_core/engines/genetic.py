"""Genetic programming discovery engine (Session 13 / Phase 13).

Evolves small symbolic indicator expressions over price/volume features,
scores each by fitness = |corr(signal, forward_return)| - 0.001*complexity,
and admits only those that survive real out-of-sample checks:

  * genuine OOS correlation on a held-out split (NOT the in-sample bug the
    earlier version had),
  * a permutation null-test (reject candidates that only "work" by luck —
    p >= 0.05 means the correlation is indistinguishable from label-shuffle
    noise),
  * the novelty + redundancy gates.

Discovered indicators persist to state/discovered/{pair}.json so they survive
a restart.

Real GP search (ported from the older hermes_trading.genetic_discovery):
  * population of expression trees, elitist survival (top 10%), crossover +
    mutation, depth cap, complexity penalty,
  * richer primitive set (rolling window ops: sma/ema/roc/min/max/stdev/mom),
  * optional multi-candle horizon objective (cumulative forward return over H
    candles) which the old engine proved captures serial structure that a
    single next-candle return cannot.

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
from datetime import datetime, timezone
from pathlib import Path

from hermes_core.state.paths import discovered_path as _state_discovered_path
from hermes_core.env import get_env

# ── gates ──────────────────────────────────────────────────────────────────
OOS_FLOOR = 0.15         # admit if held-out |corr| >= floor. Restored to the
                          # older engine's value (0.15) — the low 0.08 floor let
                          # permutation-cleared noise slip through the Sharpe/kfold
                          # OR-gate at ~30% FDR. On the daily/horizon-60 regime we
                          # actually reach 0.28-0.85, so 0.15 is real, not unreachable.
COMPLEXITY_PENALTY = 0.001
REDUNDANCY_R = 0.8       # |pearson| > this vs an existing indicator -> REJECTED
PERM_PVALUE_FLOOR = 0.05  # reject candidates whose OOS corr is not better than shuffled-label noise

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
OPERATORS = ("add", "sub", "mul", "div")


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
    return 0.0


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
def _signal_for_expr(expr, prices: list[float], lookback: int = 60) -> list[float]:
    """Directional signal series: evaluate the expr on each trailing window.

    lookback must cover the longest window feature (sma50) so those operators
    have enough history to compute.
    """
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
    return nodes


def _replace_node(tree, target, replacement):
    """Return a copy of `tree` with `target` subtree replaced by `replacement`."""
    if tree is target:
        return replacement
    if isinstance(tree, tuple):
        return (tree[0], _replace_node(tree[1], target, replacement),
                _replace_node(tree[2], target, replacement))
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
    return _replace_node(e1, src, copy.deepcopy(tgt))


def _mutate(expr, rng: random.Random):
    """Replace a random subtree with a fresh random one."""
    nodes = _get_nodes(expr)
    if not nodes:
        return _random_expr(rng, 2)
    e = copy.deepcopy(expr)
    node = rng.choice(nodes)
    return _replace_node(e, node, _random_expr(rng, rng.randint(0, 2)))


# ── GP core ─────────────────────────────────────────────────────────────────
def _random_expr(rng: random.Random, depth: int = 2) -> object:
    if depth <= 0 or (depth < 2 and rng.random() < 0.5):
        return rng.choice(FEATURES)
    op = rng.choice(OPERATORS)
    return (op, _random_expr(rng, depth - 1), _random_expr(rng, depth - 1))


def _fitness_with_penalty(expr, prices: list[float], horizon: int = 1) -> float:
    sig = _signal_for_expr(expr, prices)
    if len(sig) < 20:
        return 0.0
    corr = _compute_fitness(sig, prices, horizon)
    return corr - COMPLEXITY_PENALTY * _complexity(expr)


def _evolve_population(prices: list[float], pop_size: int, generations: int,
                       horizon: int, rng: random.Random) -> list[object]:
    """Real GA evolution on `prices`. Elitist survival (top 10%, min 15),
    crossover/mutation refill, depth cap. Returns the final population."""
    pop = [_random_expr(rng, 2) for _ in range(pop_size)]
    for _gen in range(generations):
        scored = []
        for expr in pop:
            try:
                fit = _fitness_with_penalty(expr, prices, horizon)
            except Exception:
                fit = 0.0
            scored.append((fit, expr))
        scored.sort(key=lambda x: x[0], reverse=True)
        keep = max(15, pop_size // 10)
        survivors = [s[1] for s in scored[:keep]]
        new_pop = list(survivors)
        while len(new_pop) < pop_size:
            parent = rng.choice(survivors)
            if rng.random() < 0.6 and len(survivors) >= 2:
                child = _crossover(parent, rng.choice(survivors), rng)
            else:
                child = _mutate(parent, rng)
            if _complexity(child) > 60:
                child = _random_expr(rng, 2)
            new_pop.append(child)
        pop = new_pop
    return pop


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
             *, generations: int = 60, pop_size: int = 40, seed: int = 7,
             top_k: int = 5, horizon: int = 1) -> list[dict]:
    """Evolve and admit indicators for `pair`.

    Returns the list of admitted indicator dicts (also persisted to
    state/discovered/{pair}.json). Genuine OOS-first (ported from the older
    engine): an expr must clear OOS_FLOOR on a held-out split, pass a
    permutation null-test (p < 0.05), and pass novelty + redundancy gates
    before admission. `horizon` sets the forward-return objective (1 = single
    next-candle; >1 = cumulative move over H candles, which the older engine
    proved is far more predictable on real data).
    """
    rng = random.Random(seed)
    prices = list(prices)
    if len(prices) < 60:
        return []

    # Train/test split for GENUINE out-of-sample evaluation (fixes the earlier
    # in-sample OOS bug).
    cut = int(len(prices) * 0.6)
    train = prices[:cut]
    test = prices[cut:]
    if len(train) < 40 or len(test) < 20:
        return []

    # 1) Evolve on the TRAIN portion only.
    pop = _evolve_population(train, pop_size, generations, horizon, rng)

    # 2) Evaluate each unique candidate on the held-out TEST portion + gates.
    admitted: list[dict] = []
    seen: set[str] = set()
    existing_signals: list[list[float]] = []
    population: list[object] = []
    for expr in pop:
        es = _expr_to_str(expr)
        if es in seen:
            continue
        seen.add(es)
        try:
            sig_test = _signal_for_expr(expr, test)
            if len(sig_test) < 20:
                continue
            oos = _compute_fitness(sig_test, test, horizon)
            if oos < OOS_FLOOR:
                continue
            # Permutation null-test: reject lucky-noise candidates (mandatory
            # firewall — also what keeps test_random_low_rate FDR <5% on noise).
            p_val, _real_c, null_mean = _permutation_pvalue(
                sig_test, test, horizon, n_perm=200, seed=seed)
            if p_val >= PERM_PVALUE_FLOOR:
                continue
            # ── Ported from the older engine: walk-forward majority gate ──
            # The old engine's real noise control was walk-forward: a candidate
            # had to clear the OOS bar in a MAJORITY of disjoint folds, so a
            # single lucky 60/40 split could not admit it. The GA evolves
            # thousands of candidates specifically hunting for that one lucky
            # split, which is why a bare OOS>=0.15 + permutation still admits
            # ~20% noise under full evolution. _honest_oos returns
            # (median_corr, frac_folds_passing); we require the candidate to
            # pass in >= half the folds (the old `clear >= (n_folds+1)//2`
            # rule). Sharpe is a SECONDARY escape (old engine: corr OR sharpe)
            # but only when the split is too short for honest k-fold.
            _n = min(len(sig_test), len(test) - horizon)
            if _n >= N_FOLDS * 15:
                kfold_med, frac = _honest_oos(sig_test, test, horizon)
                sh = _sharpe(sig_test, test, horizon)
                # Old-engine walk-forward rule, tightened for shorter series:
                # require BOTH a strong majority of folds clearing OOS_FLOOR
                # (>=4 of 5; the old engine's n_windows=4 -> >=2 of 3 on 2y daily
                # where folds are large and 0.15 is genuinely hard by chance)
                # AND the median fold-corr >= OOS_FLOOR. On our ~400pt test the
                # folds are small, so a stricter majority is needed to kill the
                # GA's lucky-split hunt. (The old engine's secondary Sharpe OR is
                # intentionally NOT used here: on noise it admits false positives
                # and the k-fold majority already captures tradeable signals.)
                _kfold_ok = (frac >= 0.8) and (kfold_med >= OOS_FLOOR)
                if not _kfold_ok:
                    continue
            # OOS signal stats for the dashboard (honest, held-out).
            win_rate, total_pnl = _compute_signal_stats(sig_test, test, horizon)
            if redundancy_check(sig_test, existing_signals) == "REJECTED":
                continue
            if not _novelty_ok(expr, population):
                continue
            ind = {
                "pair": pair,
                "name": es,
                "expr": es,
                "_expr": expr,                       # raw tree for live re-eval
                "fitness": round(oos - COMPLEXITY_PENALTY * _complexity(expr), 4),
                "oos_corr": round(oos, 4),
                "perm_pvalue": round(p_val, 4),
                "null_mean_corr": null_mean,
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "complexity": _complexity(expr),
                "nodes": _complexity(expr),
                "horizon": horizon,
                "interval": "1d",                     # GP discovery runs on daily bars
                "source": "genetic",
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            admitted.append(ind)
            existing_signals.append(sig_test)
            population.append(expr)
            if len(admitted) >= top_k:
                break
        except Exception:
            continue

    if admitted:
        _save_discovered(pair, admitted)
    return admitted


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
        own = load_discovered_indicators(pair, include_shared=False)
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


# ── persistence (survives restart) ──────────────────────────────────────────
def _discovered_path(pair: str) -> Path:
    if DISCOVERED_DIR is not None:
        DISCOVERED_DIR.mkdir(parents=True, exist_ok=True)
        safe = pair.replace("/", "_")
        return DISCOVERED_DIR / f"{safe}.json"
    return _state_discovered_path(pair)


def _save_discovered(pair: str, inds: list[dict]) -> None:
    # Drop the raw tree (not JSON-serialisable) before persisting.
    clean = [{k: v for k, v in ind.items() if k != "_expr"} for ind in inds]
    _discovered_path(pair).write_text(json.dumps(clean, indent=2), encoding="utf-8")


def load_discovered_indicators(pair: str, include_shared: bool = True) -> list[dict]:
    p = _discovered_path(pair)
    own: list[dict] = []
    if p.exists():
        try:
            own = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            own = []
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
                 volumes: list[float] | None = None) -> list[dict]:
        return discover(pair, prices, volumes)

    def load(self, pair: str) -> list[dict]:
        return load_discovered_indicators(pair)
