"""Per-bot GP invent profiles — candle TF, horizon, size, timeout.

Invent TF must equal live GP eval TF. Profiles keep FX/gold on daily with
different forward horizons, and give crypto its own shorter-horizon regime
(not FX-style daily mean-reversion invent).

``bots/<bot>/config.yaml`` may override via an ``invent:`` block; code
defaults remain the source of truth when keys are missing.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from hermes_core.state.paths import bot_for_pair

# Shared fallback if bot name is unknown.
_BASE: dict[str, Any] = {
    "interval": "1d",
    "period": "2y",
    "max_candles": 500,
    "horizon": 20,
    "generations": 40,
    "pop_size": 40,
    "n_islands": 2,
    "timeout_s": 90,
    "min_bars": 200,
}

# Forex: daily, shorter ahead (was hard-coded h=60 for everyone).
# Gold: daily, medium ahead.
# Crypto: hourly invent (not daily MR), shorter ahead, smaller search + longer
# timeout so invent can finish and land on Discovered.
BOT_INVENT_DEFAULTS: dict[str, dict[str, Any]] = {
    "forex": {
        "interval": "1d",
        "period": "2y",
        "max_candles": 500,
        "horizon": 10,
        "generations": 40,
        "pop_size": 40,
        "n_islands": 2,
        "timeout_s": 300,
        "min_bars": 200,
    },
    "gold": {
        "interval": "1d",
        "period": "2y",
        "max_candles": 500,
        "horizon": 20,
        "generations": 25,
        "pop_size": 30,
        "n_islands": 1,
        "timeout_s": 300,
        "min_bars": 200,
    },
    "crypto": {
        "interval": "1h",
        "period": "60d",
        "max_candles": 800,
        "horizon": 12,
        "generations": 20,
        "pop_size": 24,
        "n_islands": 1,
        "timeout_s": 300,
        "min_bars": 200,
    },
}

_INT_KEYS = {
    "max_candles",
    "horizon",
    "generations",
    "pop_size",
    "n_islands",
    "timeout_s",
    "min_bars",
}
_STR_KEYS = {"interval", "period"}


def _coerce(key: str, value: Any, fallback: Any) -> Any:
    if value is None:
        return fallback
    try:
        if key in _INT_KEYS:
            return int(value)
        if key in _STR_KEYS:
            return str(value).strip() or fallback
    except (TypeError, ValueError):
        return fallback
    return value


def invent_profile(bot: str | None = None, *, pair: str | None = None) -> dict[str, Any]:
    """Resolved invent settings for ``bot`` (or the bot that owns ``pair``)."""
    name = (bot or (bot_for_pair(pair) if pair else None) or "forex").strip().lower()
    # Unknown bot labels (e.g. legacy "goldbot" in tests) must resolve via pair
    # so invent TF/horizon match the formulas on disk.
    if name not in BOT_INVENT_DEFAULTS:
        name = (bot_for_pair(pair) if pair else None) or "forex"
        name = str(name).strip().lower()
        if name not in BOT_INVENT_DEFAULTS:
            name = "forex"
    out = deepcopy(_BASE)
    out.update(deepcopy(BOT_INVENT_DEFAULTS[name]))

    try:
        from hermes_core.config import load_config

        cfg = load_config(name)
        override = cfg.get("invent") if isinstance(cfg, dict) else None
        if isinstance(override, dict):
            for key in list(out.keys()):
                if key in override:
                    out[key] = _coerce(key, override[key], out[key])
    except Exception:  # noqa: BLE001 — config optional; defaults always work
        pass

    # Hard floors so a bad override cannot zero the search.
    out["horizon"] = max(1, int(out["horizon"]))
    out["generations"] = max(1, int(out["generations"]))
    out["pop_size"] = max(4, int(out["pop_size"]))
    out["n_islands"] = max(1, int(out["n_islands"]))
    out["timeout_s"] = max(30, int(out["timeout_s"]))
    out["min_bars"] = max(50, int(out["min_bars"]))
    out["max_candles"] = max(out["min_bars"], int(out["max_candles"]))
    out["interval"] = str(out["interval"] or "1d")
    out["period"] = str(out["period"] or "2y")
    out["bot"] = name
    return out


def regime_key(interval: str, horizon: int) -> str:
    return f"{str(interval).strip()}|h{int(horizon)}"


def indicator_matches_regime(
    ind: dict,
    *,
    interval: str,
    horizon: int,
) -> bool:
    """True when a formula was invented on the same candle TF + horizon."""
    ind_iv = str(ind.get("interval") or "1d").strip()
    try:
        ind_h = int(ind.get("horizon") if ind.get("horizon") is not None else -1)
    except (TypeError, ValueError):
        ind_h = -1
    return ind_iv == str(interval).strip() and ind_h == int(horizon)


def has_votable_for_regime(
    indicators: list[dict],
    *,
    interval: str,
    horizon: int,
    expr_fn=None,
    approved_fn=None,
) -> bool:
    """Whether any own indicator is S10-approved on this invent regime."""
    from hermes_core.engines.genetic import indicator_expr, is_backtest_approved

    expr_fn = expr_fn or indicator_expr
    approved_fn = approved_fn or is_backtest_approved
    for ind in indicators:
        if not expr_fn(ind) or not approved_fn(ind):
            continue
        if indicator_matches_regime(ind, interval=interval, horizon=horizon):
            return True
    return False
