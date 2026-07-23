"""GP promote gate — automatic per-pair ban/unban for GP Brain entries.

Replaces the static ``GP_EXCLUDE_PAIRS``-only check with expectancy-driven
promote gating. Invent / shadow keep running while a pair is banned; only
``promote=True`` (live GP paper entries) is blocked.

State lives in ``{bot}/state/gp_promote_gate.json``. ``GP_EXCLUDE_PAIRS`` still
seeds initial bans (once per pair) so deploy env remains the cold-start lever.

Gates (env-overridable):
  * min samples before any flip
  * ban threshold (expectancy too low) + unban threshold (hysteresis band)
  * cooldown after a ban/unban so the gate cannot thrash
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from hermes_core.env import get_env
from hermes_core.state.paths import bot_state_dir

STATE_NAME = "gp_promote_gate.json"

# Defaults chosen to match the measured daily paper book that seeded
# GP_EXCLUDE_PAIRS (BTC ~−26% cumulative / many bars → clearly negative mean).
DEFAULT_MIN_SAMPLES = 30
DEFAULT_BAN_EXPECTANCY = -0.05   # mean % per sample → ban if below
DEFAULT_UNBAN_EXPECTANCY = 0.05  # mean % per sample → unban if above
DEFAULT_COOLDOWN_S = 86_400      # 24h after a state flip
DEFAULT_WINDOW = 100             # rolling PnL samples kept per pair
DEFAULT_SHADOW_HORIZON_S = 3_600 # settle a pending shadow after 1h


def _fenv(name: str, default: float) -> float:
    raw = get_env(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _ienv(name: str, default: int) -> int:
    raw = get_env(name, "")
    if not raw.strip():
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def min_samples() -> int:
    return max(1, _ienv("GP_PROMOTE_GATE_MIN_SAMPLES", DEFAULT_MIN_SAMPLES))


def ban_expectancy() -> float:
    return _fenv("GP_PROMOTE_GATE_BAN", DEFAULT_BAN_EXPECTANCY)


def unban_expectancy() -> float:
    return _fenv("GP_PROMOTE_GATE_UNBAN", DEFAULT_UNBAN_EXPECTANCY)


def cooldown_s() -> float:
    return float(max(0, _ienv("GP_PROMOTE_GATE_COOLDOWN_S", DEFAULT_COOLDOWN_S)))


def sample_window() -> int:
    return max(min_samples(), _ienv("GP_PROMOTE_GATE_WINDOW", DEFAULT_WINDOW))


def shadow_horizon_s() -> float:
    return float(max(60, _ienv("GP_PROMOTE_GATE_SHADOW_HORIZON_S", DEFAULT_SHADOW_HORIZON_S)))


def normalize_pair(pair: str) -> str:
    return (pair or "").strip().upper()


def env_seed_bans() -> set[str]:
    """Pairs listed in ``GP_EXCLUDE_PAIRS`` (deploy cold-start bans)."""
    raw = get_env("GP_EXCLUDE_PAIRS", "GBP/JPY,BTC/USD")
    return {normalize_pair(p) for p in raw.split(",") if p.strip()}


def state_path(bot: str) -> Path:
    return bot_state_dir(bot) / STATE_NAME


def _empty_pair(banned: bool = False, *, seeded: bool = False) -> dict:
    return {
        "banned": bool(banned),
        "seeded_from_env": bool(seeded),
        "n": 0,
        "expectancy": 0.0,
        "samples": [],
        "last_change_ts": 0.0,
        "last_reason": "init",
        "pending_shadow": None,
    }


def load_state(bot: str) -> dict:
    path = state_path(bot)
    if not path.exists():
        return {"pairs": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"pairs": {}}
    if not isinstance(data, dict):
        return {"pairs": {}}
    pairs = data.get("pairs")
    if not isinstance(pairs, dict):
        data["pairs"] = {}
    return data


def save_state(bot: str, state: dict) -> None:
    path = state_path(bot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass


def ensure_seeded(bot: str, *, state: dict | None = None) -> dict:
    """Apply ``GP_EXCLUDE_PAIRS`` as initial bans for pairs not yet tracked."""
    st = state if state is not None else load_state(bot)
    pairs = st.setdefault("pairs", {})
    dirty = False
    for pair in env_seed_bans():
        if pair not in pairs:
            pairs[pair] = _empty_pair(banned=True, seeded=True)
            pairs[pair]["last_reason"] = "seeded_from_env"
            dirty = True
    if dirty:
        save_state(bot, st)
    return st


def compute_expectancy(pnls: list[float]) -> float:
    """Mean % PnL per sample (0.0 when empty)."""
    if not pnls:
        return 0.0
    return sum(float(x) for x in pnls) / len(pnls)


def _in_cooldown(rec: dict, now: float) -> bool:
    last = float(rec.get("last_change_ts") or 0.0)
    if last <= 0:
        return False
    return (now - last) < cooldown_s()


def decide(
    banned: bool,
    expectancy: float,
    n: int,
    *,
    now: float | None = None,
    last_change_ts: float = 0.0,
    min_n: int | None = None,
    ban_thr: float | None = None,
    unban_thr: float | None = None,
) -> tuple[bool, str]:
    """Pure ban/unban decision with min-samples, hysteresis, and cooldown.

    Returns ``(banned, reason)``.
    """
    now = time.time() if now is None else float(now)
    min_n = min_samples() if min_n is None else int(min_n)
    ban_thr = ban_expectancy() if ban_thr is None else float(ban_thr)
    unban_thr = unban_expectancy() if unban_thr is None else float(unban_thr)

    if n < min_n:
        return banned, "insufficient_samples"

    fake = {"last_change_ts": last_change_ts}
    if _in_cooldown(fake, now):
        return banned, "cooldown"

    if banned:
        if expectancy >= unban_thr:
            return False, "unban_expectancy"
        return True, "hold_banned"
    if expectancy <= ban_thr:
        return True, "ban_expectancy"
    return False, "hold_allowed"


def _pair_rec(state: dict, pair: str) -> dict:
    key = normalize_pair(pair)
    pairs = state.setdefault("pairs", {})
    rec = pairs.get(key)
    if rec is None:
        rec = _empty_pair(banned=False)
        pairs[key] = rec
    return rec


def _apply_samples(
    bot: str,
    pair: str,
    samples: list[float],
    *,
    now: float | None = None,
    state: dict | None = None,
    replace: bool = False,
) -> dict:
    """Update rolling samples and re-evaluate ban status. Persists."""
    now = time.time() if now is None else float(now)
    st = ensure_seeded(bot, state=state)
    rec = _pair_rec(st, pair)
    window = sample_window()
    cleaned = [float(x) for x in samples]
    if replace:
        rec["samples"] = cleaned[-window:]
    else:
        merged = list(rec.get("samples") or []) + cleaned
        rec["samples"] = merged[-window:]
    rec["n"] = len(rec["samples"])
    rec["expectancy"] = round(compute_expectancy(rec["samples"]), 6)

    new_banned, reason = decide(
        bool(rec.get("banned")),
        float(rec["expectancy"]),
        int(rec["n"]),
        now=now,
        last_change_ts=float(rec.get("last_change_ts") or 0.0),
    )
    if new_banned != bool(rec.get("banned")):
        rec["banned"] = new_banned
        rec["last_change_ts"] = now
        rec["last_reason"] = reason
        # Once evidence drives a flip, it is no longer just an env seed.
        if reason in ("ban_expectancy", "unban_expectancy"):
            rec["seeded_from_env"] = False
    else:
        rec["last_reason"] = reason
    save_state(bot, st)
    return {
        "pair": normalize_pair(pair),
        "banned": bool(rec["banned"]),
        "n": int(rec["n"]),
        "expectancy": float(rec["expectancy"]),
        "reason": rec["last_reason"],
    }


def record_pnl(
    bot: str,
    pair: str,
    pnl: float,
    *,
    now: float | None = None,
    state: dict | None = None,
) -> dict:
    """Append one paper/shadow PnL sample and re-evaluate the gate."""
    return _apply_samples(bot, pair, [pnl], now=now, state=state, replace=False)


def refresh_from_pnls(
    bot: str,
    pair: str,
    pnls: list[float],
    *,
    now: float | None = None,
    state: dict | None = None,
) -> dict:
    """Replace the rolling window with ``pnls`` and re-evaluate."""
    return _apply_samples(bot, pair, pnls, now=now, state=state, replace=True)


def refresh_from_sim(
    bot: str,
    pair: str,
    sim: dict,
    *,
    now: float | None = None,
) -> dict:
    """Ingest ``simulate_gp_paper_pnl``-style result (trades + total_pnl).

    Expands to ``trades`` equal-sized samples of ``total_pnl / trades`` so
    min-samples / expectancy math stays consistent with live closes.
    """
    n = int(sim.get("trades") or 0)
    if n <= 0:
        return {
            "pair": normalize_pair(pair),
            "banned": is_banned(bot, pair),
            "n": 0,
            "expectancy": 0.0,
            "reason": "empty_sim",
        }
    total = float(sim.get("total_pnl") or 0.0)
    per = total / n
    return refresh_from_pnls(bot, pair, [per] * n, now=now)


def is_banned(bot: str, pair: str, *, state: dict | None = None) -> bool:
    st = ensure_seeded(bot, state=state)
    key = normalize_pair(pair)
    rec = st.get("pairs", {}).get(key)
    if rec is None:
        return False
    return bool(rec.get("banned"))


def is_promote_allowed(bot: str, pair: str, *, state: dict | None = None) -> bool:
    """True when GP Brain promote entries are allowed for ``pair``."""
    return not is_banned(bot, pair, state=state)


def observe_shadow(
    bot: str,
    pair: str,
    price: float,
    *,
    direction: int | None = None,
    now: float | None = None,
    state: dict | None = None,
) -> dict | None:
    """Settle pending shadow forward-PnL and optionally open a new pending.

    ``direction``: +1 long / -1 short / None to settle only.
    Called from the always-on shadow logger so banned pairs still accumulate
    expectancy evidence while invent/shadow keep running.
    """
    now = time.time() if now is None else float(now)
    try:
        px = float(price)
    except (TypeError, ValueError):
        return None
    if px <= 0:
        return None

    st = ensure_seeded(bot, state=state)
    rec = _pair_rec(st, pair)
    pending = rec.get("pending_shadow")
    result = None

    if isinstance(pending, dict):
        age = now - float(pending.get("ts") or 0.0)
        entry = float(pending.get("price") or 0.0)
        direc = int(pending.get("direction") or 0)
        if entry > 0 and direc in (-1, 1) and age >= shadow_horizon_s():
            pnl = (px / entry - 1.0) * 100.0 * direc
            rec["pending_shadow"] = None
            save_state(bot, st)
            result = record_pnl(bot, pair, pnl, now=now, state=st)

    if direction in (-1, 1):
        # Reload after possible record_pnl persist.
        st = load_state(bot)
        ensure_seeded(bot, state=st)
        rec = _pair_rec(st, pair)
        if rec.get("pending_shadow") is None:
            rec["pending_shadow"] = {
                "ts": now,
                "price": px,
                "direction": int(direction),
            }
            save_state(bot, st)

    return result
