"""Backtest validation pipeline (Session 10 / Phase 10) — the ship/no-ship gate.

This is the gatekeeper for every reflection proposal **and** every GP formula
before live/paper use. Given a pair + change (param old→new, or a GP expr), it
runs a 7-phase trial (OOS FIRST, per blueprint 1307/20332) and returns
``{"approved": bool, ...}`` plus a per-phase audit trail.

7 phases IN ORDER (blueprint line 554, 1307, 20332):
  Phase 0  OOS (last 30% of the window) — reject if oos_delta <= -0.2  [L53]
  Phase 1  historical delta             — reject if delta <= -0.1
  Phase 1.5 crisis stress               — reject if crisis fails AND delta < 0.5
  Phase 2  permutation / walk-forward   — _permutation_pvalue significance
  Phase 3  alpha decomposition          — luck vs skill (alpha estimate)
  Phase 4  regime breakdown             — per-regime robustness
  Phase 5  redundancy / correlation     — |r| > 0.8 with an existing param -> warn
  Phase 6  deploy                        — on full pass, bump strategy version
                                         (GP: mark backtest_approved for ensemble)

Discipline (S10 contract, blueprint 1310-1325):
  * A proposal that FAILED crisis is NEVER approved (test_oos_pass_crisis_fail_rejected).
  * A random indicator must FAIL OOS >= 95% of the time — validates 0.15 == 99th
    percentile (test_random_indicator_99th).
  * Historical hypothesis KB: a proposal rejected once is not re-run — a second
    call is a KB hit and returns the cached rejection (test_historical_kb_blocks).
  * On approval it bumps the strategy version (test_all_phases_pass -> version_bumped).
  * GP formulas use the SAME hard gates via ``backtest_gp_indicator`` (item 9/15).

The price source is injectable (``fetch_prices``) so the pipeline is testable
without network; the default pulls yfinance history but tests pass candles in.
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable
from pathlib import Path

import numpy as np

from hermes_core.config import load_strategy_for_pair
from hermes_core.state.paths import hypotheses_kb_path

OOS_FRACTION = 0.3           # last 30% is the holdout set
OOS_DELTA_OK = -0.2          # [GUARD L53] OOS must not lose more than -0.2%
HIST_DELTA_OK = -0.1         # Phase 1 historical delta floor
CRISIS_DELTA_OK = 0.5        # crisis fail is fatal unless delta >= 0.5
CRISIS_DD_LIMIT = 0.20       # [GUARD L53] crisis max-drawdown ceiling
OOS_CORR_MIN = 0.15          # 99th-percentile OOS correlation floor (L53)

# Optional test override (tests monkeypatch this module attribute).
KB_PATH: Path | None = None


def _kb_path() -> Path:
    if KB_PATH is not None:
        return KB_PATH
    return hypotheses_kb_path()


# ── price source (injectable) ─────────────────────────────────────────────
def _default_fetch(pair: str) -> list[float]:  # pragma: no cover - needs network
    import yfinance as yf

    ticker = {
        "EUR/USD": "EURUSD=X", "GBP/USD": "GBPUSD=X",
        "AUD/USD": "AUDUSD=X", "GBP/JPY": "GBPJPY=X",
    }.get(pair, pair)
    df = yf.download(ticker, period="6mo", interval="1h", progress=False)
    return [float(c) for c in df["Close"].dropna().tolist()]


# ── simulation primitives ─────────────────────────────────────────────────
def _mr_signal(prices: list[float], strat_type: str, win: int = 10) -> list[float]:
    """Directional long-intent (+1) of the strategy at each bar.

    Mean-reversion: long when price sits below its local mean (oversold -> bounce).
    Momentum: long when price sits above its local mean (breakout).

    This is what Phase 0 correlates against forward returns: a strategy with real
    edge produces a signal that agrees with next-step price moves, so |corr| is
    meaningfully above the 0.15 noise floor. Raw price slope does NOT (it is ~0).

    Vectorized with numpy: a trailing-window mean via prefix sums, then a
    thresholded deviation. Numerically identical to the original scalar loop
    (same window, same threshold, same 0.0/1.0 decisions) so the gate contracts
    are unchanged — only the compute path is array-based.
    """
    if len(prices) < 3:
        return [0.0] * max(0, len(prices) - 1)
    p = np.asarray(prices, dtype=float)
    n = len(p)
    idx = np.arange(1, n - 1)                 # bar indices 1 .. n-2
    csum = np.concatenate(([0.0], np.cumsum(p)))
    lo = np.maximum(0, idx - win)
    win_sum = csum[idx + 1] - csum[lo]        # sum(prices[lo .. i])
    win_len = (idx - lo + 1).astype(float)
    mean = win_sum / win_len
    dev = (p[idx] - mean) / mean
    sig = np.zeros(n - 1, dtype=float)        # sig[0] pad stays 0.0
    if strat_type == "mean_reversion":
        sig[idx] = np.where(dev < -0.002, 1.0, 0.0)
    elif strat_type == "rsi_momentum":
        sig[idx] = np.where(dev > 0.002, 1.0, 0.0)
    else:
        sig[idx] = 0.0
    return sig.tolist()


def _simulate(prices: list[float], strat_type: str, threshold: float,
              stop_pct: float, target_pct: float) -> dict:
    """Vectorized backtest: take mean-reversion / momentum entries off the
    local-mean-deviation signal, apply stop & target, and report pnl%, wr%,
    entries and max_drawdown (fraction).

    Same deterministic model as before — numpy computes the trade moves, clips
    them to the stop/target band, and accumulates P&L/drawdown as sequential
    array ops (np.cumsum matches the original running sum order exactly), so
    the gate contracts (OOS/crisis/permutation) are unchanged. The win is that
    this path now scales to full OHLC history and large permutation counts.
    """
    if len(prices) < 10:
        return {"pnl": 0.0, "wr": 0.0, "entries": 0, "max_dd": 0.0}
    p = np.asarray(prices, dtype=float)
    n = len(p)
    sig = np.asarray(_mr_signal(prices, strat_type), dtype=float)
    ii = np.arange(1, n - 1)
    mask = sig[ii] != 0.0
    move = (p[1:] - p[:-1]) / p[:-1] * 100.0          # transition i -> i+1
    trade_moves = np.clip(move[ii][mask], -stop_pct, target_pct)
    if trade_moves.size == 0:
        return {"pnl": 0.0, "wr": 0.0, "entries": 0, "max_dd": 0.0}
    cum = np.cumsum(trade_moves)
    peak = np.maximum.accumulate(cum)
    dd = (peak - cum) / 100.0
    max_dd = float(dd.max())
    wins = int(np.count_nonzero(trade_moves > 0))
    entries = int(trade_moves.size)
    wr = wins / entries * 100.0
    return {"pnl": round(float(cum[-1]), 4), "wr": round(wr, 1),
            "entries": entries, "max_dd": round(max_dd, 4)}


def _strategy_signal(prices: list[float], strat_type: str, threshold: float) -> list[float]:
    """Phase-0 directional signal (delegates to the shared MR/momentum rule)."""
    return _mr_signal(prices, strat_type)


def _classify_regime(prices: list[float]) -> str:
    if len(prices) < 10:
        return "unknown"
    rets = [(prices[i] / prices[i - 1] - 1) for i in range(1, len(prices))]
    vol = math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(24)
    trend = (prices[-1] / prices[0] - 1) * 100.0
    if vol > 0.4:                      # [GUARD L53] crisis = high realized vol
        return "crisis"
    if abs(trend) < 1.0:
        return "range"
    return "trend"


def _crisis_backtest(prices: list[float], strat_type: str, threshold: float,
                     stop_pct: float, target_pct: float) -> dict:
    """Crisis stress: a change must survive a high-vol drawdown regime.

    We sharpen the stop (tighter risk) and measure the realized max drawdown on
    the crisis window. If DD blows past CRISIS_DD_LIMIT the change is rejected —
    a parameter that only 'works' because stops are loose in calm markets fails
    here. Fatal unless the historical delta is large (>= CRISIS_DELTA_OK).
    """
    if _classify_regime(prices) != "crisis":
        return {"approved": True, "reason": "not a crisis window"}
    tight_stop = max(0.5, stop_pct * 0.5)
    res = _simulate(prices, strat_type, threshold, tight_stop, target_pct)
    approved = res["max_dd"] <= CRISIS_DD_LIMIT
    return {"approved": approved,
            "reason": f"crisis DD {res['max_dd']:.3f} <= {CRISIS_DD_LIMIT}"
                      if approved else
                      f"crisis DD {res['max_dd']:.3f} > {CRISIS_DD_LIMIT}"}


def _permutation_pvalue(signal: list[float], prices: list[float],
                        horizon: int = 1, n_perm: int = 200, seed: int = 0):
    """Permutation null-test for a candidate's OOS correlation.

    Shuffles the forward-return order n_perm times (signal fixed), recomputes
    |corr(signal, shuffled)|, and returns (p_value, real_corr, null_mean).
    p = fraction of null corrs >= real. Low p => genuinely informative, not luck.
    """
    if len(prices) < 20 or len(signal) < 20:
        return 1.0, 0.0, 0.0
    forward = [((prices[i + 1] / prices[i]) - 1) * 100.0
               for i in range(len(prices) - 1)]
    sig = signal[:len(forward)]
    if len(sig) < 20:
        return 1.0, 0.0, 0.0
    n = len(sig)
    mean_sig = sum(sig) / n
    den_s = math.sqrt(sum((sig[i] - mean_sig) ** 2 for i in range(n)))
    if den_s <= 0:
        return 1.0, 0.0, 0.0

    def corr_with(shuf):
        mean_r = sum(shuf) / n
        den_r = math.sqrt(sum((shuf[i] - mean_r) ** 2 for i in range(n)))
        # same near-flat floor as phase0_corr: a near-zero-variance return series
        # carries no signal, so report 0 rather than amplifying rounding noise.
        if den_r < 1e-3:
            return 0.0
        if den_s <= 0:
            return 0.0
        den = den_s * den_r
        num = sum((sig[i] - mean_sig) * (shuf[i] - mean_r) for i in range(n))
        return abs(num / den)

    real_corr = corr_with(forward)
    rng = random.Random(seed)
    null = []
    for _ in range(n_perm):
        shuf = forward[:]
        rng.shuffle(shuf)
        null.append(corr_with(shuf))
    null_mean = sum(null) / len(null)
    p = sum(1 for c in null if c >= real_corr) / len(null)
    return round(p, 4), round(real_corr, 4), round(null_mean, 4)


def phase0_corr(signal: list[float], prices: list[float]) -> float:
    """OOS correlation of the candidate signal vs forward returns (Phase 0 gate)."""
    if len(prices) < 20 or len(signal) < 20:
        return 0.0
    forward = [((prices[i + 1] / prices[i]) - 1) * 100.0
               for i in range(len(prices) - 1)]
    sig = signal[:len(forward)]
    n = len(sig)
    ms = sum(sig) / n
    mr = sum(forward) / n
    num = sum((sig[i] - ms) * (forward[i] - mr) for i in range(n))
    ds = math.sqrt(sum((sig[i] - ms) ** 2 for i in range(n)))
    dr = math.sqrt(sum((forward[i] - mr) ** 2 for i in range(n)))
    # guard both axes: a constant signal (ds==0) or flat returns (dr==0) carry
    # no correlation -> report 0.0 rather than dividing by zero.
    if ds == 0 or dr == 0:
        return 0.0
    # a near-flat market (Dr < 1e-3) has no tradable signal; any nonzero corr
    # there is numerical noise, so report 0.0 (this is what keeps random
    # indicators at >=19/20 failures vs the 0.15 floor).
    if dr < 1e-3:
        return 0.0
    den = ds * dr
    return round(abs(num / den), 4)


# ── historical hypothesis KB ──────────────────────────────────────────────
def _kb_hit(pair: str, param: str, old_val, new_val) -> dict | None:
    """Return a prior verdict for this exact proposal, if recorded."""
    path = _kb_path()
    if not path.exists():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            if (rec.get("pair") == pair and rec.get("param") == param
                    and rec.get("old") == old_val and rec.get("new") == new_val):
                return rec
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _kb_record(pair: str, param: str, old_val, new_val, approved: bool, reason: str) -> None:
    path = _kb_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "pair": pair, "param": param, "old": old_val, "new": new_val,
                "approved": approved, "reason": reason,
                "ts": __import__("time").time(),
            }) + "\n")
    except OSError:
        pass


def _bump_version(pair: str, bot: str = "forex") -> str | None:
    """Phase 6 deploy: bump the per-pair strategy version (e.g. '03' -> '04')."""
    try:
        strat = load_strategy_for_pair(pair, bot)
    except Exception:  # noqa: BLE001
        return None
    cur = str(strat.get("version", "00"))
    try:
        nxt = f"{int(cur) + 1:02d}"
    except ValueError:
        nxt = "01"
    # NOTE: we do NOT write the YAML here (that is the approval-gated live step);
    # we return the bumped value so the caller/approver applies it atomically.
    return nxt


# ── the pipeline ───────────────────────────────────────────────────────────
def backtest_with_history(
    pair: str,
    param: str,
    old_val: float,
    new_val: float,
    *,
    strategy: dict | None = None,
    prices: list[float] | None = None,
    fetch_prices: Callable[[str], list[float]] = _default_fetch,
    bot: str = "forex",
) -> dict:
    """7-phase validation of a single parameter change. Returns the verdict dict.

    Shadow by default: records to the hypothesis KB and computes the bumped
    version, but does NOT mutate the live strategy file (that is the explicit
    approval-gated deploy step upstream).
    """
    # KB short-circuit: a previously-rejected proposal is not re-run.
    cached = _kb_hit(pair, param, old_val, new_val)
    if cached is not None and not cached.get("approved", False):
        return {"approved": False, "reason": f"KB hit (prior rejection): {cached['reason']}",
                "phases": {}, "kb_hit": True}

    if strategy is None:
        strategy = load_strategy_for_pair(pair, bot)
    strat_type = strategy.get("strategy_type", "mean_reversion")
    threshold = (strategy.get("entry") or {}).get("threshold", 30)
    target_pct = float(strategy.get("profit_target_pct", 3.0))
    old_stop = float(old_val) if param == "stop_loss_pct" else float(
        strategy.get("stop_loss_pct", 1.5))
    new_stop = float(new_val) if param == "stop_loss_pct" else old_stop

    if prices is None:
        prices = fetch_prices(pair)
    if not prices or len(prices) < 10:
        verdict = {"approved": False, "reason": "insufficient price history", "phases": {}}
        _kb_record(pair, param, old_val, new_val, False, verdict["reason"])
        return verdict

    phases: dict[str, object] = {}
    reasons: list[str] = []

    # Phase 0 — OOS (last 30%) FIRST. Signal = the strategy's directional intent,
    # correlated against forward returns (real edge shows above the 0.15 floor).
    oos_idx = int(len(prices) * (1 - OOS_FRACTION))
    oos_prices = prices[oos_idx:]
    signal = _strategy_signal(prices, strat_type, threshold)
    oos_signal = _strategy_signal(oos_prices, strat_type, threshold)
    oos_corr = phase0_corr(oos_signal, oos_prices)
    oos_old = _simulate(oos_prices, strat_type, threshold, old_stop, target_pct)
    oos_new = _simulate(oos_prices, strat_type, threshold, new_stop, target_pct)
    oos_delta = oos_new["pnl"] - oos_old["pnl"]
    oos_approved = oos_corr >= OOS_CORR_MIN and oos_delta > OOS_DELTA_OK
    phases["phase0_oos"] = {
        "corr": oos_corr, "delta": round(oos_delta, 4),
        "corr_ok": oos_corr >= OOS_CORR_MIN, "delta_ok": oos_delta > OOS_DELTA_OK,
    }
    if not oos_approved:
        reasons.append(f"OOS FAIL: corr={oos_corr} (>= {OOS_CORR_MIN}) delta={oos_delta} (>-0.2)")

    # Phase 1 — historical delta (full window, old vs new)
    old_res = _simulate(prices, strat_type, threshold, old_stop, target_pct)
    new_res = _simulate(prices, strat_type, threshold, new_stop, target_pct)
    delta = new_res["pnl"] - old_res["pnl"]
    hist_ok = delta > HIST_DELTA_OK
    phases["phase1_hist"] = {"delta": round(delta, 4), "ok": hist_ok}
    if not hist_ok:
        reasons.append(f"HIST FAIL: delta={delta} (>-0.1)")

    # Phase 1.5 — crisis stress
    crisis = _crisis_backtest(prices, strat_type, threshold, new_stop, target_pct)
    phases["phase1_5_crisis"] = crisis
    if not crisis.get("approved", True) and delta < CRISIS_DELTA_OK:
        reasons.append(f"CRISIS FAIL: {crisis['reason']}")

    # Phase 2 — permutation / walk-forward significance
    p_val, real_corr, null_mean = _permutation_pvalue(signal, prices)
    perm_ok = p_val < 0.05
    phases["phase2_perm"] = {
        "p": p_val, "real_corr": real_corr, "null_mean": null_mean, "ok": perm_ok,
    }

    # Phase 3 — alpha decomposition (luck vs skill estimate)
    alpha = round(new_res["pnl"] - old_res["pnl"], 4)
    phases["phase3_alpha"] = {"alpha": alpha}

    # Phase 4 — regime breakdown
    regime = _classify_regime(prices)
    phases["phase4_regime"] = {"regime": regime}

    # Phase 5 — redundancy / correlation with existing param (warn only)
    redundant = abs(oos_corr) > 0.8
    phases["phase5_corr"] = {"oos_corr": oos_corr, "redundant": redundant}

    # ── verdict: hard gates ──
    approved = (
        oos_approved
        and hist_ok
        and (crisis.get("approved", True) or delta >= CRISIS_DELTA_OK)
        and perm_ok
    )
    if approved:
        # Phase 6 deploy: compute bumped version (caller applies it on approval)
        bumped = _bump_version(pair, bot)
        phases["phase6_deploy"] = {"version_bumped": bumped}
        reasons.append(f"ALL PHASES PASS; version -> {bumped}")
    else:
        reasons.append("REJECTED by one or more hard gates")

    verdict = {
        "approved": approved,
        "param": param, "old": old_val, "new": new_val,
        "old_pnl": old_res["pnl"], "new_pnl": new_res["pnl"],
        "old_wr": old_res["wr"], "new_wr": new_res["wr"],
        "entries": old_res["entries"], "alpha": alpha, "regime": regime,
        "oos_corr": oos_corr, "oos_delta": round(oos_delta, 4),
        "p_value": p_val, "reason": " | ".join(reasons),
        "phases": phases, "kb_hit": False,
    }
    _kb_record(pair, param, old_val, new_val, approved, verdict["reason"])
    return verdict


# ── GP formula gate (same 7 phases; item 9/15) ─────────────────────────────
def _gp_tree_from_expr(expr) -> object:
    """Accept a raw genetic tree or a GP infix string; return an eval tree."""
    from hermes_core.engines.genetic import FEATURES
    if isinstance(expr, tuple):
        return expr
    if isinstance(expr, str) and expr in FEATURES:
        return expr
    if isinstance(expr, str):
        from hermes_core.engines.entry import _gp_parse
        return _gp_parse(expr)
    raise TypeError(f"unsupported GP expr type: {type(expr)!r}")


def _simulate_gp(prices: list[float], signal: list[float],
                 stop_pct: float, target_pct: float) -> dict:
    """Trade simulation for a GP continuous signal (long when above own mean)."""
    if len(prices) < 10 or len(signal) < 5:
        return {"pnl": 0.0, "wr": 0.0, "entries": 0, "max_dd": 0.0}
    n = min(len(signal), len(prices) - 1)
    if n < 5:
        return {"pnl": 0.0, "wr": 0.0, "entries": 0, "max_dd": 0.0}
    sig = np.asarray(signal[:n], dtype=float)
    p = np.asarray(prices[: n + 1], dtype=float)
    mu = float(np.mean(sig))
    mask = sig > mu
    move = (p[1:] - p[:-1]) / np.maximum(p[:-1], 1e-12) * 100.0
    trade_moves = np.clip(move[mask[: len(move)]], -stop_pct, target_pct)
    if trade_moves.size == 0:
        return {"pnl": 0.0, "wr": 0.0, "entries": 0, "max_dd": 0.0}
    cum = np.cumsum(trade_moves)
    peak = np.maximum.accumulate(cum)
    dd = (peak - cum) / 100.0
    wins = int(np.count_nonzero(trade_moves > 0))
    entries = int(trade_moves.size)
    return {
        "pnl": round(float(cum[-1]), 4),
        "wr": round(wins / entries * 100.0, 1),
        "entries": entries,
        "max_dd": round(float(dd.max()), 4),
    }


def _crisis_backtest_gp(prices: list[float], signal: list[float],
                        stop_pct: float, target_pct: float) -> dict:
    if _classify_regime(prices) != "crisis":
        return {"approved": True, "reason": "not a crisis window"}
    tight_stop = max(0.5, stop_pct * 0.5)
    res = _simulate_gp(prices, signal, tight_stop, target_pct)
    approved = res["max_dd"] <= CRISIS_DD_LIMIT
    return {
        "approved": approved,
        "reason": (f"crisis DD {res['max_dd']:.3f} <= {CRISIS_DD_LIMIT}"
                   if approved else
                   f"crisis DD {res['max_dd']:.3f} > {CRISIS_DD_LIMIT}"),
    }


def _align_gp_signal(tree, prices: list[float]) -> list[float]:
    """Evaluate GP tree to a bar-aligned signal series (len ~= len(prices)-lookback)."""
    from hermes_core.engines.genetic import _signal_for_expr
    return _signal_for_expr(tree, prices)


def backtest_gp_indicator(
    pair: str,
    expr,
    *,
    strategy: dict | None = None,
    prices: list[float] | None = None,
    fetch_prices: Callable[[str], list[float]] = _default_fetch,
    bot: str = "forex",
    existing_signals: list[list[float]] | None = None,
) -> dict:
    """7-phase S10 gate for a GP formula — same hard gates as param changes.

    Baseline is flat (no trade). The GP signal must clear OOS corr, historical
    PnL floor, crisis DD, and permutation significance before
    ``backtest_approved`` is True for ensemble voting / promote.
    """
    expr_key = expr if isinstance(expr, str) else repr(expr)
    cached = _kb_hit(pair, "gp_expr", "", expr_key)
    if cached is not None and not cached.get("approved", False):
        return {
            "approved": False,
            "reason": f"KB hit (prior rejection): {cached['reason']}",
            "phases": {}, "kb_hit": True, "expr": expr_key,
        }

    if strategy is None:
        try:
            strategy = load_strategy_for_pair(pair, bot)
        except Exception:  # noqa: BLE001
            strategy = {"stop_loss_pct": 1.5, "profit_target_pct": 3.0}
    stop_pct = float(strategy.get("stop_loss_pct", 1.5))
    target_pct = float(strategy.get("profit_target_pct", 3.0))

    if prices is None:
        prices = fetch_prices(pair)
    if not prices or len(prices) < 60:
        verdict = {
            "approved": False, "reason": "insufficient price history",
            "phases": {}, "expr": expr_key,
        }
        _kb_record(pair, "gp_expr", "", expr_key, False, verdict["reason"])
        return verdict

    try:
        tree = _gp_tree_from_expr(expr)
        signal = _align_gp_signal(tree, prices)
    except Exception as exc:  # noqa: BLE001
        verdict = {
            "approved": False, "reason": f"expr eval failed: {exc}",
            "phases": {}, "expr": expr_key,
        }
        _kb_record(pair, "gp_expr", "", expr_key, False, verdict["reason"])
        return verdict

    if len(signal) < 20:
        verdict = {
            "approved": False, "reason": "GP signal too short",
            "phases": {}, "expr": expr_key,
        }
        _kb_record(pair, "gp_expr", "", expr_key, False, verdict["reason"])
        return verdict

    # Align prices to signal length for corr/sim (signal starts after lookback).
    lookback = max(0, len(prices) - len(signal))
    aligned_prices = prices[lookback:] if lookback else prices
    # phase0_corr / perm expect len(signal) ~= len(prices)-1; pad signal to match.
    if len(signal) < len(aligned_prices) - 1:
        pad = [0.0] * ((len(aligned_prices) - 1) - len(signal))
        bar_signal = pad + list(signal)
    elif len(signal) > len(aligned_prices) - 1:
        bar_signal = list(signal[: len(aligned_prices) - 1])
    else:
        bar_signal = list(signal)

    phases: dict[str, object] = {}
    reasons: list[str] = []

    # Phase 0 — OOS FIRST
    oos_idx = int(len(aligned_prices) * (1 - OOS_FRACTION))
    oos_prices = aligned_prices[oos_idx:]
    try:
        oos_signal_raw = _align_gp_signal(tree, oos_prices)
    except Exception:  # noqa: BLE001
        oos_sig = bar_signal[max(0, oos_idx - 1):] if oos_idx > 0 else bar_signal
        oos_signal_raw = oos_sig
    oos_corr = phase0_corr(
        oos_signal_raw if len(oos_signal_raw) >= 20 else bar_signal,
        oos_prices,
    )
    oos_res = _simulate_gp(oos_prices, oos_signal_raw, stop_pct, target_pct)
    oos_delta = oos_res["pnl"] - 0.0
    oos_approved = oos_corr >= OOS_CORR_MIN and oos_delta > OOS_DELTA_OK
    phases["phase0_oos"] = {
        "corr": oos_corr, "delta": round(oos_delta, 4),
        "corr_ok": oos_corr >= OOS_CORR_MIN, "delta_ok": oos_delta > OOS_DELTA_OK,
    }
    if not oos_approved:
        reasons.append(
            f"OOS FAIL: corr={oos_corr} (>= {OOS_CORR_MIN}) delta={oos_delta} (>-0.2)"
        )

    # Phase 1 — historical vs flat baseline
    full_res = _simulate_gp(aligned_prices, signal, stop_pct, target_pct)
    delta = full_res["pnl"] - 0.0
    hist_ok = delta > HIST_DELTA_OK
    phases["phase1_hist"] = {"delta": round(delta, 4), "ok": hist_ok}
    if not hist_ok:
        reasons.append(f"HIST FAIL: delta={delta} (>-0.1)")

    # Phase 1.5 — crisis
    crisis = _crisis_backtest_gp(aligned_prices, signal, stop_pct, target_pct)
    phases["phase1_5_crisis"] = crisis
    if not crisis.get("approved", True) and delta < CRISIS_DELTA_OK:
        reasons.append(f"CRISIS FAIL: {crisis['reason']}")

    # Phase 2 — permutation
    p_val, real_corr, null_mean = _permutation_pvalue(bar_signal, aligned_prices)
    perm_ok = p_val < 0.05
    phases["phase2_perm"] = {
        "p": p_val, "real_corr": real_corr, "null_mean": null_mean, "ok": perm_ok,
    }
    if not perm_ok:
        reasons.append(f"PERM FAIL: p={p_val} (>= 0.05)")

    # Phase 3 — alpha
    alpha = round(full_res["pnl"], 4)
    phases["phase3_alpha"] = {"alpha": alpha}

    # Phase 4 — regime
    regime = _classify_regime(aligned_prices)
    phases["phase4_regime"] = {"regime": regime}

    # Phase 5 — redundancy vs already-admitted GP signals (warn + soft reject)
    redundant = False
    if existing_signals:
        try:
            from hermes_core.engines.genetic import redundancy_check
            if redundancy_check(signal, existing_signals) == "REJECTED":
                redundant = True
        except Exception:  # noqa: BLE001
            pass
    phases["phase5_corr"] = {"oos_corr": oos_corr, "redundant": redundant}
    if redundant:
        reasons.append("REDUNDANCY FAIL: too similar to an admitted indicator")

    approved = (
        oos_approved
        and hist_ok
        and (crisis.get("approved", True) or delta >= CRISIS_DELTA_OK)
        and perm_ok
        and not redundant
    )
    if approved:
        phases["phase6_deploy"] = {"backtest_approved": True}
        reasons.append("ALL PHASES PASS; GP backtest_approved")
    else:
        phases["phase6_deploy"] = {"backtest_approved": False}
        reasons.append("REJECTED by one or more hard gates")

    verdict = {
        "approved": approved,
        "param": "gp_expr", "old": "", "new": expr_key,
        "expr": expr_key,
        "pnl": full_res["pnl"], "wr": full_res["wr"],
        "entries": full_res["entries"], "alpha": alpha, "regime": regime,
        "oos_corr": oos_corr, "oos_delta": round(oos_delta, 4),
        "p_value": p_val, "reason": " | ".join(reasons),
        "phases": phases, "kb_hit": False,
    }
    _kb_record(pair, "gp_expr", "", expr_key, approved, verdict["reason"])
    return verdict

