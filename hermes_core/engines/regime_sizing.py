"""HIF Phase-3 — soft regime size multiplier.

When ``REGIME_SIZING=1``, scale position size by market regime (trend/range) and
short-horizon direction (fast_regime up/down/flat). Never blocks a trade.
When the flag is off, multiplier is 1.0 (size unchanged).

Uses indicator labels from ``compute_all``:
  regime = 'trend' | 'range'
  fast_regime = 'up' | 'down' | 'flat'
"""

from __future__ import annotations

from hermes_core.env import get_env

# Soft multipliers (long-biased book: cut size in adverse trend, ease in range).
MULT_TREND_UP = 1.00
MULT_TREND_DOWN = 0.40
MULT_TREND_FLAT = 0.70
MULT_RANGE = 0.85
MULT_UNKNOWN = 1.00  # fail-open


def regime_sizing_enabled() -> bool:
    return get_env("REGIME_SIZING", "0") == "1"


def regime_size_mult(
    regime: str | None,
    fast_regime: str | None = None,
    *,
    adx: float | None = None,
) -> dict:
    """Return soft size multiplier + labels for dashboard.

    Optional ``adx`` blends range vs trend multipliers (soft, not a hard gate).
    """
    reg = (regime or "").strip().lower() or "unknown"
    fast = (fast_regime or "").strip().lower() or "flat"

    if reg == "trend":
        if fast == "up":
            hard = MULT_TREND_UP
            label = "trend_up"
        elif fast == "down":
            hard = MULT_TREND_DOWN
            label = "trend_down"
        else:
            hard = MULT_TREND_FLAT
            label = "trend_flat"
        range_like = MULT_RANGE
    elif reg == "range":
        hard = MULT_RANGE
        label = f"range_{fast}" if fast in ("up", "down", "flat") else "range"
        range_like = MULT_RANGE
    else:
        return {
            "mult": MULT_UNKNOWN,
            "label": "unknown",
            "regime": reg,
            "fast_regime": fast,
            "reasons": ["unknown_regime"],
        }

    reasons = [label]
    mult = float(hard)
    if adx is not None and reg == "trend":
        try:
            a = float(adx)
            # Soft blend: at ADX 15 ≈ fully range-like, at 40 ≈ full trend mult.
            strength = max(0.0, min(1.0, (a - 15.0) / 25.0))
            mult = range_like * (1.0 - strength) + hard * strength
            reasons.append(f"adx_blend={a:.1f}")
        except (TypeError, ValueError):
            pass

    mult = max(0.05, min(1.0, float(mult)))
    return {
        "mult": round(mult, 4),
        "label": label,
        "regime": reg,
        "fast_regime": fast,
        "reasons": reasons,
    }


def apply_regime_sizing(
    base_size: float,
    *,
    enabled: bool,
    regime: str | None = None,
    fast_regime: str | None = None,
    adx: float | None = None,
) -> dict:
    """Scale size by regime mult when enabled; else pass-through (mult=1)."""
    base = float(base_size)
    if not enabled:
        return {
            "size": base,
            "base_size": base,
            "regime_mult": 1.0,
            "regime_label": "disabled",
            "regime": regime,
            "fast_regime": fast_regime,
            "regime_mode": "disabled",
            "reasons": ["disabled"],
        }
    info = regime_size_mult(regime, fast_regime, adx=adx)
    sized = max(0.0, base * float(info["mult"]))
    return {
        "size": sized,
        "base_size": base,
        "regime_mult": info["mult"],
        "regime_label": info["label"],
        "regime": info["regime"],
        "fast_regime": info["fast_regime"],
        "regime_mode": "soft",
        "reasons": info["reasons"],
    }
