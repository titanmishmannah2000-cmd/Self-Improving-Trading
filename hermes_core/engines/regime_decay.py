"""Profitability Path Phase 5 — regime / edge decay detector (2-of-3).

Signals:
  S1 — Bayesian win-rate decay: P(WR < breakeven) > threshold
  S2 — Live drawdown exceeds ``dd_mult`` × reference backtest MDD
  S3 — Feature OOD: recent ATR/trend z vs training baseline

Trip when ≥2 signals fire (after ``min_trades``). Callers suppress new entries
for the pair / expert. Pure + small state file; fail-soft.
"""

from __future__ import annotations

import contextlib
import math
import threading
import time
from pathlib import Path
from typing import Any

from hermes_core.env import get_env
from hermes_core.state.atomic_json import atomic_write_json, load_json
from hermes_core.state.paths import bot_state_dir

STATE_NAME = "regime_decay.json"
_LOCK = threading.RLock()

DEFAULT_MIN_TRADES = 20
DEFAULT_BREAKEVEN = 0.45
DEFAULT_WR_PROB = 0.80
DEFAULT_DD_MULT = 1.5
DEFAULT_OOD_Z = 2.5


def regime_decay_enabled() -> bool:
    return get_env("REGIME_DECAY", "0") == "1"


def _fenv(name: str, default: float) -> float:
    raw = get_env(name, "")
    if not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _ienv(name: str, default: int) -> int:
    raw = get_env(name, "")
    if not str(raw).strip():
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def state_path(bot: str) -> Path:
    return bot_state_dir(bot) / STATE_NAME


def _beta_cdf_below(a: float, b: float, x: float, *, n_steps: int = 200) -> float:
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    if a <= 0 or b <= 0:
        return 0.5
    try:
        log_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    except ValueError:
        return 0.5
    steps = max(20, int(n_steps))
    dx = x / steps
    acc = 0.0
    for i in range(steps + 1):
        t = i * dx
        if t <= 0.0 or t >= 1.0:
            dens = 0.0
        else:
            dens = math.exp((a - 1.0) * math.log(t) + (b - 1.0) * math.log(1.0 - t) - log_beta)
        w = 0.5 if i in (0, steps) else 1.0
        acc += w * dens
    return max(0.0, min(1.0, acc * dx))


def signal_wr_decay(wins: int, losses: int) -> tuple[bool, float]:
    a = 1.0 + max(0, wins)
    b = 1.0 + max(0, losses)
    be = _fenv("REGIME_DECAY_BREAKEVEN", DEFAULT_BREAKEVEN)
    thr = _fenv("REGIME_DECAY_WR_PROB", DEFAULT_WR_PROB)
    p = _beta_cdf_below(a, b, be)
    return p >= thr, p


def signal_dd_exceed(live_dd: float, backtest_mdd: float) -> tuple[bool, float]:
    mult = _fenv("REGIME_DECAY_DD_MULT", DEFAULT_DD_MULT)
    if backtest_mdd <= 0:
        return False, 0.0
    ratio = float(live_dd) / float(backtest_mdd)
    return ratio > mult, ratio


def signal_ood(feature_z: float | None) -> tuple[bool, float | None]:
    if feature_z is None:
        return False, None
    thr = _fenv("REGIME_DECAY_OOD_Z", DEFAULT_OOD_Z)
    z = abs(float(feature_z))
    return z >= thr, z


def evaluate_decay(
    *,
    wins: int,
    losses: int,
    live_dd: float,
    backtest_mdd: float,
    feature_z: float | None = None,
    min_trades: int | None = None,
) -> dict[str, Any]:
    """Pure 2-of-3 evaluation."""
    need = _ienv("REGIME_DECAY_MIN_TRADES", DEFAULT_MIN_TRADES) if min_trades is None else int(min_trades)
    n = int(wins) + int(losses)
    s1, p_wr = signal_wr_decay(int(wins), int(losses))
    s2, dd_ratio = signal_dd_exceed(float(live_dd), float(backtest_mdd))
    s3, z = signal_ood(feature_z)
    votes = int(s1) + int(s2) + int(s3)
    ready = n >= need
    tripped = ready and votes >= 2
    return {
        "ready": ready,
        "n": n,
        "s1_wr_decay": s1,
        "s1_p": round(p_wr, 4),
        "s2_dd": s2,
        "s2_ratio": round(dd_ratio, 4),
        "s3_ood": s3,
        "s3_z": z,
        "votes": votes,
        "tripped": tripped,
    }


def load_state(bot: str) -> dict:
    data = load_json(state_path(bot), default={"pairs": {}}, quarantine=True)
    if not isinstance(data, dict):
        return {"pairs": {}}
    if not isinstance(data.get("pairs"), dict):
        data["pairs"] = {}
    return data


def save_state(bot: str, state: dict) -> None:
    with contextlib.suppress(OSError):
        atomic_write_json(state_path(bot), state, indent=2)


def is_pair_suppressed(bot: str, pair: str) -> bool:
    if not regime_decay_enabled():
        return False
    st = load_state(bot)
    rec = (st.get("pairs") or {}).get(pair.upper()) or (st.get("pairs") or {}).get(pair)
    if not isinstance(rec, dict):
        return False
    return bool(rec.get("suppressed"))


def update_pair_decay(
    bot: str,
    pair: str,
    *,
    wins: int,
    losses: int,
    live_dd: float,
    backtest_mdd: float,
    feature_z: float | None = None,
) -> dict[str, Any]:
    """Evaluate and persist suppress flag for ``pair``."""
    result = evaluate_decay(
        wins=wins,
        losses=losses,
        live_dd=live_dd,
        backtest_mdd=backtest_mdd,
        feature_z=feature_z,
    )
    with _LOCK:
        st = load_state(bot)
        key = pair.upper()
        rec = st.setdefault("pairs", {}).setdefault(key, {})
        rec.update(result)
        rec["ts"] = time.time()
        if result["tripped"]:
            rec["suppressed"] = True
            rec["suppress_reason"] = "regime_decay_2of3"
        save_state(bot, st)
    result["suppressed"] = bool(rec.get("suppressed"))
    result["pair"] = key
    return result


def clear_pair_suppress(bot: str, pair: str) -> None:
    with _LOCK:
        st = load_state(bot)
        key = pair.upper()
        rec = (st.get("pairs") or {}).get(key)
        if isinstance(rec, dict):
            rec["suppressed"] = False
            rec["cleared_ts"] = time.time()
            save_state(bot, st)
