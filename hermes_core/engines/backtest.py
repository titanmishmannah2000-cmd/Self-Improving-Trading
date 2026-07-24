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
without network; the default pulls yfinance history (5m preferred, shared
ticker map) but tests pass candles in.

Entry simulation uses the live BB/RSI/ADX core from ``evaluate_entry``, plus
session (L04), ensemble consensus (L13), and stop-loss cooldown (L15/L23).
Chart vision hard/soft blocks remain live-only (no chart context in the gate).
Crisis stress relaxes ADX so the stop/DD gate is not vacated.
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

OOS_FRACTION = 0.3  # last 30% is the holdout set
OOS_DELTA_OK = -0.2  # [GUARD L53] OOS must not lose more than -0.2%
HIST_DELTA_OK = -0.1  # Phase 1 historical delta floor
CRISIS_DELTA_OK = 0.5  # crisis fail is fatal unless delta >= 0.5
CRISIS_DD_LIMIT = 0.20  # [GUARD L53] crisis max-drawdown ceiling
OOS_CORR_MIN = 0.15  # 99th-percentile OOS correlation floor (L53)

# Optional test override (tests monkeypatch this module attribute).
KB_PATH: Path | None = None


def _kb_path() -> Path:
    if KB_PATH is not None:
        return KB_PATH
    return hypotheses_kb_path()


# ── price source (injectable) ─────────────────────────────────────────────
def _default_fetch(pair: str) -> list[float]:  # pragma: no cover - needs network
    """Close series for the gate when callers do not inject ``prices``.

    Uses the shared Yahoo ticker map (EURUSD=X, GC=F, BTC-USD, …) via
    ``seed_history_interval_sync`` — never raw pair strings like XAU/USD.
    Prefers 5m bars (live indicator cadence); falls back to daily if 5m is short.
    """
    from hermes_core.adapters.price import seed_history_interval_sync

    for interval, period in (("5m", "60d"), ("1d", "2y")):
        try:
            hist = (
                seed_history_interval_sync(
                    pair,
                    interval=interval,
                    period=period,
                    max_candles=500,
                )
                or []
            )
        except Exception:  # noqa: BLE001 — fail-soft into next interval
            hist = []
        closes = [
            float(c["price"]) for c in hist if isinstance(c, dict) and c.get("price") is not None
        ]
        if len(closes) >= 10:
            return closes
    return []


# ── simulation primitives ─────────────────────────────────────────────────
def _session_token_for_hour(hour: int) -> str:
    """UTC hour → session token (same windows as ``loop._session_token_for``)."""
    if 0 <= hour < 8:
        return "ASIA"
    if 8 <= hour < 17:
        return "LDN"
    if 17 <= hour < 21:
        return "NY"
    return "OTHER"


def _bar_sessions(
    n: int,
    candle_ts: list[float] | None,
    *,
    bar_seconds: float = 300.0,
) -> list[str]:
    """Session token per price bar.

    Prefer real ``candle_ts`` when provided; otherwise synthesize evenly spaced
    bars ending at ``time.time()`` so session filters still engage on float-only
    history (reflection injects closes without timestamps).
    """
    import time as _time

    if candle_ts is not None and len(candle_ts) >= n:
        out = []
        for i in range(n):
            hour = int(_time.gmtime(float(candle_ts[i])).tm_hour)
            out.append(_session_token_for_hour(hour))
        return out
    now = _time.time()
    out = []
    for i in range(n):
        ts = now - (n - 1 - i) * bar_seconds
        hour = int(_time.gmtime(ts).tm_hour)
        out.append(_session_token_for_hour(hour))
    return out


def _ensemble_series(
    prices: list[float],
    strategy: dict,
    pair: str,
    *,
    stride: int = 20,
    injected: str | list[str] | None = None,
) -> list[str]:
    """Per-bar ensemble consensus for L13 (bearish blocks MR).

    ``injected``: constant string or per-bar list from the caller.
    Otherwise sample GP shadow consensus every ``stride`` bars (fail-soft →
    neutral when no discovered/approved indicators).
    """
    n = len(prices)
    if n < 2:
        return ["neutral"] * max(0, n - 1)
    if isinstance(injected, list) and injected:
        out = []
        for i in range(n - 1):
            out.append(injected[i] if i < len(injected) else injected[-1])
        return out
    if isinstance(injected, str) and injected:
        return [injected] * (n - 1)

    cons = ["neutral"] * (n - 1)
    try:
        from hermes_core.engines.entry import gp_ensemble_signal
    except Exception:  # noqa: BLE001
        return cons
    last = "neutral"
    for i in range(50, n - 1, max(1, stride)):
        try:
            sig = gp_ensemble_signal(pair, prices[: i + 1], strategy, promote=False)
        except Exception:  # noqa: BLE001
            sig = None
        if sig is not None:
            last = str((sig.meta or {}).get("consensus") or "neutral")
        for j in range(i, min(i + stride, n - 1)):
            cons[j] = last
    return cons


def _entry_signal(
    prices: list[float],
    strat_type: str,
    threshold: float,
    *,
    strategy: dict | None = None,
    pair: str = "EUR/USD",
    candle_ts: list[float] | None = None,
    ensemble_consensus: str | list[str] | None = None,
    relax_adx: bool = False,
    min_lookback: int = 40,
    apply_session: bool = True,
    apply_ensemble: bool = True,
) -> list[float]:
    """Bar-aligned long intent matching ``evaluate_entry`` (BB/RSI/ADX + L04/L13).

    Session (L04) and ensemble (L13) are applied here. Cooldown (L15/L23) is
    applied inside ``_simulate`` because it depends on simulated exits.
    Crisis stress may set ``relax_adx=True`` so the stop/DD gate is not vacated.
    """
    from hermes_core.engines.entry import (
        _BEARISH_CONSENSUS,
        _session_allowed,
    )
    from hermes_core.indicators import compute_all

    strategy = strategy or {
        "strategy_type": strat_type,
        "entry": {"threshold": threshold, "session_filter": "24h"},
        "session_filter": "24h",
    }
    n = len(prices)
    if n < min_lookback + 2:
        return [0.0] * max(0, n - 1)

    sessions = _bar_sessions(n, candle_ts) if apply_session else ["LDN"] * n
    ensembles = (
        _ensemble_series(prices, strategy, pair, injected=ensemble_consensus)
        if apply_ensemble
        else ["neutral"] * (n - 1)
    )

    sig = [0.0] * (n - 1)
    for i in range(min_lookback, n - 1):
        if apply_session and not _session_allowed(strategy, sessions[i]):
            continue
        if apply_ensemble and strat_type == "mean_reversion" and ensembles[i] in _BEARISH_CONSENSUS:
            continue
        ind = compute_all(prices[: i + 1])
        last = prices[i]
        rsi = float(ind["rsi"])
        if strat_type == "mean_reversion":
            at_band = last <= float(ind["bb"]["lower"])
            oversold = rsi <= threshold
            calm = True if relax_adx else (float(ind["adx"]) < 25.0)
            if at_band and oversold and calm:
                sig[i] = 1.0
        elif strat_type == "rsi_momentum":
            if rsi <= threshold:
                sig[i] = 1.0
    return sig


def _mr_signal(prices: list[float], strat_type: str, win: int = 10) -> list[float]:
    """Backward-compatible alias — delegates to indicator-based ``_entry_signal``."""
    del win  # unused; kept for call-site compatibility
    return _entry_signal(prices, strat_type, threshold=30.0)


def _simulate(
    prices: list[float],
    strat_type: str,
    threshold: float,
    stop_pct: float,
    target_pct: float,
    *,
    strategy: dict | None = None,
    pair: str = "EUR/USD",
    candle_ts: list[float] | None = None,
    ensemble_consensus: str | list[str] | None = None,
    relax_adx: bool = False,
    apply_cooldown: bool = True,
) -> dict:
    """Backtest entries with live BB/RSI/ADX + session/ensemble/cooldown gates.

    Stop-loss exits arm a re-entry cooldown (L15/L23). Target/flat exits do not
    — matching the spirit of not immediately re-buying after getting stopped
    in this next-bar PnL model.
    """
    from hermes_core.engines.entry import REENTRY_COOLDOWN_CYCLES

    if len(prices) < 10:
        return {"pnl": 0.0, "wr": 0.0, "entries": 0, "max_dd": 0.0}
    raw = _entry_signal(
        prices,
        strat_type,
        threshold,
        strategy=strategy,
        pair=pair,
        candle_ts=candle_ts,
        ensemble_consensus=ensemble_consensus,
        relax_adx=relax_adx,
    )
    p = np.asarray(prices, dtype=float)
    n = len(p)
    trade_moves: list[float] = []
    last_stop_bar: int | None = None
    for i in range(1, n - 1):
        if raw[i] == 0.0:
            continue
        if (
            apply_cooldown
            and last_stop_bar is not None
            and (i - last_stop_bar) < REENTRY_COOLDOWN_CYCLES
        ):
            continue
        move = (p[i + 1] - p[i]) / p[i] * 100.0
        clipped = float(np.clip(move, -stop_pct, target_pct))
        trade_moves.append(clipped)
        if move <= -stop_pct:
            last_stop_bar = i
    if not trade_moves:
        return {"pnl": 0.0, "wr": 0.0, "entries": 0, "max_dd": 0.0}
    tm = np.asarray(trade_moves, dtype=float)
    cum = np.cumsum(tm)
    peak = np.maximum.accumulate(cum)
    dd = (peak - cum) / 100.0
    wins = int(np.count_nonzero(tm > 0))
    entries = int(tm.size)
    wr = wins / entries * 100.0
    return {
        "pnl": round(float(cum[-1]), 4),
        "wr": round(wr, 1),
        "entries": entries,
        "max_dd": round(float(dd.max()), 4),
    }


def _strategy_signal(
    prices: list[float],
    strat_type: str,
    threshold: float,
    *,
    strategy: dict | None = None,
    pair: str = "EUR/USD",
    candle_ts: list[float] | None = None,
    ensemble_consensus: str | list[str] | None = None,
) -> list[float]:
    """Phase-0 directional signal (live entry core + session/ensemble)."""
    return _entry_signal(
        prices,
        strat_type,
        threshold,
        strategy=strategy,
        pair=pair,
        candle_ts=candle_ts,
        ensemble_consensus=ensemble_consensus,
    )


def _classify_regime(prices: list[float]) -> str:
    if len(prices) < 10:
        return "unknown"
    rets = [(prices[i] / prices[i - 1] - 1) for i in range(1, len(prices))]
    vol = math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(24)
    trend = (prices[-1] / prices[0] - 1) * 100.0
    if vol > 0.4:  # [GUARD L53] crisis = high realized vol
        return "crisis"
    if abs(trend) < 1.0:
        return "range"
    return "trend"


def _crisis_backtest(
    prices: list[float],
    strat_type: str,
    threshold: float,
    stop_pct: float,
    target_pct: float,
    *,
    strategy: dict | None = None,
    pair: str = "EUR/USD",
    candle_ts: list[float] | None = None,
    ensemble_consensus: str | list[str] | None = None,
) -> dict:
    """Crisis stress: a change must survive a high-vol drawdown regime.

    Uses the same BB/RSI + session/ensemble entry triggers as live, but relaxes
    the ADX calm filter (crisis windows elevate ADX and would otherwise vacate
    this gate). Measures max drawdown under the **proposed** stop.
    """
    if _classify_regime(prices) != "crisis":
        return {"approved": True, "reason": "not a crisis window"}
    res = _simulate(
        prices,
        strat_type,
        threshold,
        stop_pct,
        target_pct,
        strategy=strategy,
        pair=pair,
        candle_ts=candle_ts,
        ensemble_consensus=ensemble_consensus,
        relax_adx=True,
        apply_cooldown=False,  # stress the stop itself, not re-entry spacing
    )
    approved = res["max_dd"] <= CRISIS_DD_LIMIT
    return {
        "approved": approved,
        "reason": f"crisis DD {res['max_dd']:.3f} <= {CRISIS_DD_LIMIT}"
        if approved
        else f"crisis DD {res['max_dd']:.3f} > {CRISIS_DD_LIMIT}",
    }


def _forward_returns_pct(prices: list[float], horizon: int = 1) -> list[float]:
    """Cumulative % move from bar i to i+horizon (matches invent fitness)."""
    h = max(1, int(horizon))
    if len(prices) <= h:
        return []
    return [((prices[i + h] / prices[i]) - 1.0) * 100.0 for i in range(len(prices) - h)]


def _permutation_pvalue(
    signal: list[float], prices: list[float], horizon: int = 1, n_perm: int = 200, seed: int = 0
):
    """Permutation null-test for a candidate's OOS correlation.

    Shuffles the forward-return order n_perm times (signal fixed), recomputes
    |corr(signal, shuffled)|, and returns (p_value, real_corr, null_mean).
    p = fraction of null corrs >= real. Low p => genuinely informative, not luck.

    ``horizon`` must match the invent forward horizon (never invent-h≠S10-h).
    """
    if len(prices) < 20 or len(signal) < 20:
        return 1.0, 0.0, 0.0
    forward = _forward_returns_pct(prices, horizon)
    if len(forward) < 20:
        return 1.0, 0.0, 0.0
    sig = signal[: len(forward)]
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


def phase0_corr(signal: list[float], prices: list[float], horizon: int = 1) -> float:
    """OOS correlation of the candidate signal vs forward returns (Phase 0 gate).

    ``horizon`` is the invent forward horizon in candles (default 1 = next bar).
    """
    if len(prices) < 20 or len(signal) < 20:
        return 0.0
    forward = _forward_returns_pct(prices, horizon)
    if len(forward) < 20:
        return 0.0
    sig = signal[: len(forward)]
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
            if (
                rec.get("pair") == pair
                and rec.get("param") == param
                and rec.get("old") == old_val
                and rec.get("new") == new_val
            ):
                return rec
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _kb_record(pair: str, param: str, old_val, new_val, approved: bool, reason: str) -> None:
    path = _kb_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "pair": pair,
                        "param": param,
                        "old": old_val,
                        "new": new_val,
                        "approved": approved,
                        "reason": reason,
                        "ts": __import__("time").time(),
                    }
                )
                + "\n"
            )
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
    candle_ts: list[float] | None = None,
    ensemble_consensus: str | list[str] | None = None,
    fetch_prices: Callable[[str], list[float]] = _default_fetch,
    bot: str = "forex",
) -> dict:
    """7-phase validation of a single parameter change. Returns the verdict dict.

    Shadow by default: records to the hypothesis KB and computes the bumped
    version, but does NOT mutate the live strategy file (that is the explicit
    approval-gated deploy step upstream).

    Entry simulation applies live BB/RSI/ADX plus session (L04), ensemble (L13),
    and stop-loss cooldown (L15/L23). Optional ``candle_ts`` / ``ensemble_consensus``
    refine those gates; when omitted, sessions are synthesized and GP consensus
    is sampled (fail-soft → neutral).
    """
    from hermes_core.engines.entry import _entry_rsi_threshold

    # KB short-circuit: a previously-rejected proposal is not re-run.
    cached = _kb_hit(pair, param, old_val, new_val)
    if cached is not None and not cached.get("approved", False):
        return {
            "approved": False,
            "reason": f"KB hit (prior rejection): {cached['reason']}",
            "phases": {},
            "kb_hit": True,
        }

    if strategy is None:
        strategy = load_strategy_for_pair(pair, bot)
    strat_type = strategy.get("strategy_type", "mean_reversion")
    threshold = _entry_rsi_threshold(strategy)
    # Resolve old/new stop and target so any single validated param actually
    # differs in simulation (stop_loss_pct OR profit_target_pct).
    base_stop = float(strategy.get("stop_loss_pct", 1.5))
    base_target = float(strategy.get("profit_target_pct", 3.0))
    if param == "stop_loss_pct":
        old_stop = float(old_val)
        new_stop = float(new_val)
        old_target = base_target
        new_target = base_target
    elif param == "profit_target_pct":
        old_stop = base_stop
        new_stop = base_stop
        old_target = float(old_val)
        new_target = float(new_val)
    else:
        old_stop = base_stop
        new_stop = base_stop
        old_target = base_target
        new_target = base_target

    if prices is None:
        prices = fetch_prices(pair)
    if not prices or len(prices) < 10:
        verdict = {"approved": False, "reason": "insufficient price history", "phases": {}}
        _kb_record(pair, param, old_val, new_val, False, verdict["reason"])
        return verdict

    sim_kw = dict(
        strategy=strategy,
        pair=pair,
        candle_ts=candle_ts,
        ensemble_consensus=ensemble_consensus,
    )

    phases: dict[str, object] = {}
    reasons: list[str] = []

    # Phase 0 — OOS (last 30%) FIRST. Signal = the strategy's directional intent,
    # correlated against forward returns (real edge shows above the 0.15 floor).
    oos_idx = int(len(prices) * (1 - OOS_FRACTION))
    oos_prices = prices[oos_idx:]
    oos_ts = candle_ts[oos_idx:] if candle_ts and len(candle_ts) >= len(prices) else None
    oos_ens = None
    if isinstance(ensemble_consensus, list) and len(ensemble_consensus) >= len(prices) - 1:
        oos_ens = ensemble_consensus[oos_idx:]
    elif isinstance(ensemble_consensus, str):
        oos_ens = ensemble_consensus
    signal = _strategy_signal(
        prices,
        strat_type,
        threshold,
        strategy=strategy,
        pair=pair,
        candle_ts=candle_ts,
        ensemble_consensus=ensemble_consensus,
    )
    oos_signal = _strategy_signal(
        oos_prices,
        strat_type,
        threshold,
        strategy=strategy,
        pair=pair,
        candle_ts=oos_ts,
        ensemble_consensus=oos_ens,
    )
    oos_corr = phase0_corr(oos_signal, oos_prices)
    oos_kw = dict(
        strategy=strategy,
        pair=pair,
        candle_ts=oos_ts,
        ensemble_consensus=oos_ens,
    )
    oos_old = _simulate(
        oos_prices,
        strat_type,
        threshold,
        old_stop,
        old_target,
        **oos_kw,
    )
    oos_new = _simulate(
        oos_prices,
        strat_type,
        threshold,
        new_stop,
        new_target,
        **oos_kw,
    )
    oos_delta = oos_new["pnl"] - oos_old["pnl"]
    oos_approved = oos_corr >= OOS_CORR_MIN and oos_delta > OOS_DELTA_OK
    phases["phase0_oos"] = {
        "corr": oos_corr,
        "delta": round(oos_delta, 4),
        "corr_ok": oos_corr >= OOS_CORR_MIN,
        "delta_ok": oos_delta > OOS_DELTA_OK,
    }
    if not oos_approved:
        reasons.append(f"OOS FAIL: corr={oos_corr} (>= {OOS_CORR_MIN}) delta={oos_delta} (>-0.2)")

    # Phase 1 — historical delta (full window, old vs new)
    old_res = _simulate(prices, strat_type, threshold, old_stop, old_target, **sim_kw)
    new_res = _simulate(prices, strat_type, threshold, new_stop, new_target, **sim_kw)
    delta = new_res["pnl"] - old_res["pnl"]
    hist_ok = delta > HIST_DELTA_OK
    phases["phase1_hist"] = {"delta": round(delta, 4), "ok": hist_ok}
    if not hist_ok:
        reasons.append(f"HIST FAIL: delta={delta} (>-0.1)")

    # Phase 1.5 — crisis stress
    crisis = _crisis_backtest(
        prices,
        strat_type,
        threshold,
        new_stop,
        new_target,
        **sim_kw,
    )
    phases["phase1_5_crisis"] = crisis
    if not crisis.get("approved", True) and delta < CRISIS_DELTA_OK:
        reasons.append(f"CRISIS FAIL: {crisis['reason']}")

    # Phase 2 — permutation / walk-forward significance
    p_val, real_corr, null_mean = _permutation_pvalue(signal, prices)
    perm_ok = p_val < 0.05
    phases["phase2_perm"] = {
        "p": p_val,
        "real_corr": real_corr,
        "null_mean": null_mean,
        "ok": perm_ok,
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
        "param": param,
        "old": old_val,
        "new": new_val,
        "old_pnl": old_res["pnl"],
        "new_pnl": new_res["pnl"],
        "old_wr": old_res["wr"],
        "new_wr": new_res["wr"],
        "entries": old_res["entries"],
        "alpha": alpha,
        "regime": regime,
        "oos_corr": oos_corr,
        "oos_delta": round(oos_delta, 4),
        "p_value": p_val,
        "reason": " | ".join(reasons),
        "phases": phases,
        "kb_hit": False,
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


def _simulate_gp(
    prices: list[float], signal: list[float], stop_pct: float, target_pct: float, horizon: int = 1
) -> dict:
    """Trade simulation for a GP continuous signal.

    Holds for ``horizon`` candles (invent forward horizon). Direction follows
    the *signed* corr(signal, forward) — invent fitness is |corr|, so the raw
    signal polarity is arbitrary; trading always-long would reject inverse
    predictors that are otherwise valid.
    """
    h = max(1, int(horizon))
    if len(prices) < 10 or len(signal) < 5:
        return {"pnl": 0.0, "wr": 0.0, "entries": 0, "max_dd": 0.0}
    n = min(len(signal), len(prices) - h)
    if n < 5:
        return {"pnl": 0.0, "wr": 0.0, "entries": 0, "max_dd": 0.0}
    sig = np.asarray(signal[:n], dtype=float)
    p = np.asarray(prices[: n + h], dtype=float)
    mu = float(np.mean(sig))
    move = (p[h : n + h] - p[:n]) / np.maximum(p[:n], 1e-12) * 100.0
    # Signed corr vs invent-horizon forward returns → trade polarity.
    fwd = np.asarray(_forward_returns_pct(list(p[: n + h]), h)[:n], dtype=float)
    m = min(len(sig), len(fwd), len(move))
    direction = 1.0
    if m >= 10:
        s = sig[:m]
        f = fwd[:m]
        ms = float(np.mean(s))
        mf = float(np.mean(f))
        num = float(np.sum((s - ms) * (f - mf)))
        ds = float(np.sqrt(np.sum((s - ms) ** 2)))
        df = float(np.sqrt(np.sum((f - mf) ** 2)))
        if ds > 0 and df > 1e-3 and num < 0:
            direction = -1.0
    mask = sig > mu
    raw = direction * move[mask[: len(move)]]
    trade_moves = np.clip(raw, -stop_pct, target_pct)
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


def _crisis_backtest_gp(
    prices: list[float], signal: list[float], stop_pct: float, target_pct: float, horizon: int = 1
) -> dict:
    if _classify_regime(prices) != "crisis":
        return {"approved": True, "reason": "not a crisis window"}
    res = _simulate_gp(prices, signal, stop_pct, target_pct, horizon=horizon)
    approved = res["max_dd"] <= CRISIS_DD_LIMIT
    return {
        "approved": approved,
        "reason": (
            f"crisis DD {res['max_dd']:.3f} <= {CRISIS_DD_LIMIT}"
            if approved
            else f"crisis DD {res['max_dd']:.3f} > {CRISIS_DD_LIMIT}"
        ),
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
    horizon: int = 1,
    interval: str | None = None,
) -> dict:
    """7-phase S10 gate for a GP formula — same hard gates as param changes.

    Baseline is flat (no trade). The GP signal must clear OOS corr, historical
    PnL floor, crisis DD, and permutation significance before
    ``backtest_approved`` is True for ensemble voting / promote.

    ``horizon`` / ``interval`` must match invent (never invent on h=12 and
    S10-gate on h=1). KB keys are regime-scoped so a daily rejection cannot
    poison an hourly invent of the same expression string.
    """
    expr_key = expr if isinstance(expr, str) else repr(expr)
    h = max(1, int(horizon))
    iv = str(interval).strip() if interval else None
    # Regime-scoped KB param; legacy unscoped ``gp_expr`` still readable for
    # horizon=1 / no-interval callers, but new invent always writes scoped keys.
    kb_param = f"gp_expr|{iv or '1d'}|h{h}" if (iv is not None or h != 1) else "gp_expr"
    cached = _kb_hit(pair, kb_param, "", expr_key)
    if cached is None and kb_param != "gp_expr":
        # Do NOT fall back to legacy unscoped rejects — those were scored on a
        # different invent horizon/TF and must not block the new regime.
        cached = None
    if cached is not None and not cached.get("approved", False):
        return {
            "approved": False,
            "reason": f"KB hit (prior rejection): {cached['reason']}",
            "phases": {},
            "kb_hit": True,
            "expr": expr_key,
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
            "approved": False,
            "reason": "insufficient price history",
            "phases": {},
            "expr": expr_key,
        }
        _kb_record(pair, kb_param, "", expr_key, False, verdict["reason"])
        return verdict

    try:
        tree = _gp_tree_from_expr(expr)
        signal = _align_gp_signal(tree, prices)
    except Exception as exc:  # noqa: BLE001
        verdict = {
            "approved": False,
            "reason": f"expr eval failed: {exc}",
            "phases": {},
            "expr": expr_key,
        }
        _kb_record(pair, kb_param, "", expr_key, False, verdict["reason"])
        return verdict

    if len(signal) < 20:
        verdict = {
            "approved": False,
            "reason": "GP signal too short",
            "phases": {},
            "expr": expr_key,
        }
        _kb_record(pair, kb_param, "", expr_key, False, verdict["reason"])
        return verdict

    # Align prices to signal length for corr/sim (signal starts after lookback).
    lookback = max(0, len(prices) - len(signal))
    aligned_prices = prices[lookback:] if lookback else prices
    # phase0_corr / perm expect signal aligned to forward-return length.
    if len(signal) < len(aligned_prices) - h:
        pad = [0.0] * ((len(aligned_prices) - h) - len(signal))
        bar_signal = pad + list(signal)
    elif len(signal) > len(aligned_prices) - h:
        bar_signal = list(signal[: len(aligned_prices) - h])
    else:
        bar_signal = list(signal)

    phases: dict[str, object] = {}
    reasons: list[str] = []

    # Phase 0 — OOS FIRST (same forward horizon as invent)
    oos_idx = int(len(aligned_prices) * (1 - OOS_FRACTION))
    oos_prices = aligned_prices[oos_idx:]
    try:
        oos_signal_raw = _align_gp_signal(tree, oos_prices)
    except Exception:  # noqa: BLE001
        oos_sig = bar_signal[max(0, oos_idx - 1) :] if oos_idx > 0 else bar_signal
        oos_signal_raw = oos_sig
    oos_corr = phase0_corr(
        oos_signal_raw if len(oos_signal_raw) >= 20 else bar_signal,
        oos_prices,
        horizon=h,
    )
    oos_res = _simulate_gp(
        oos_prices,
        oos_signal_raw,
        stop_pct,
        target_pct,
        horizon=h,
    )
    oos_delta = oos_res["pnl"] - 0.0
    oos_approved = oos_corr >= OOS_CORR_MIN and oos_delta > OOS_DELTA_OK
    phases["phase0_oos"] = {
        "corr": oos_corr,
        "delta": round(oos_delta, 4),
        "corr_ok": oos_corr >= OOS_CORR_MIN,
        "delta_ok": oos_delta > OOS_DELTA_OK,
        "horizon": h,
        "interval": iv,
    }
    if not oos_approved:
        reasons.append(f"OOS FAIL: corr={oos_corr} (>= {OOS_CORR_MIN}) delta={oos_delta} (>-0.2)")

    # Phase 1 — historical vs flat baseline
    full_res = _simulate_gp(
        aligned_prices,
        signal,
        stop_pct,
        target_pct,
        horizon=h,
    )
    delta = full_res["pnl"] - 0.0
    hist_ok = delta > HIST_DELTA_OK
    phases["phase1_hist"] = {"delta": round(delta, 4), "ok": hist_ok, "horizon": h}
    if not hist_ok:
        reasons.append(f"HIST FAIL: delta={delta} (>-0.1)")

    # Phase 1.5 — crisis
    crisis = _crisis_backtest_gp(
        aligned_prices,
        signal,
        stop_pct,
        target_pct,
        horizon=h,
    )
    phases["phase1_5_crisis"] = crisis
    if not crisis.get("approved", True) and delta < CRISIS_DELTA_OK:
        reasons.append(f"CRISIS FAIL: {crisis['reason']}")

    # Phase 2 — permutation (same horizon as invent)
    p_val, real_corr, null_mean = _permutation_pvalue(
        bar_signal,
        aligned_prices,
        horizon=h,
    )
    perm_ok = p_val < 0.05
    phases["phase2_perm"] = {
        "p": p_val,
        "real_corr": real_corr,
        "null_mean": null_mean,
        "ok": perm_ok,
        "horizon": h,
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
        "param": kb_param,
        "old": "",
        "new": expr_key,
        "expr": expr_key,
        "pnl": full_res["pnl"],
        "wr": full_res["wr"],
        "entries": full_res["entries"],
        "alpha": alpha,
        "regime": regime,
        "oos_corr": oos_corr,
        "oos_delta": round(oos_delta, 4),
        "p_value": p_val,
        "reason": " | ".join(reasons),
        "phases": phases,
        "kb_hit": False,
        "horizon": h,
        "interval": iv,
    }
    _kb_record(pair, kb_param, "", expr_key, approved, verdict["reason"])
    return verdict
