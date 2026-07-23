"""Config schema constants and validation contract (Session 1 / Phase 1).

Single source of truth for what a bot config and a per-pair strategy are allowed
to contain. Implements the blueprint Section 6 config schema and the
STRATEGY_PARAM_RANGES hard-gate table (blueprint line ~4652).

This module deliberately contains NO I/O and NO network access — pure constants
and the error type. The loader (loader.py) reads files; the validator
(validator.py) enforces these constants. Keeping the contract here means the
validator and any future dashboard export cannot drift apart silently.
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------
# The blueprint Phase-1 test block asserts `pytest.raises(ValidationError)` while
# the build-target prose says `raises ValueError`. We subclass ValueError so both
# hold: a ValidationError IS-A ValueError, so either assertion passes. This is the
# one place the blueprint is internally inconsistent; we satisfy the stricter,
# test-binding form.
class ValidationError(ValueError):
    """Raised when a config or per-pair strategy violates the schema/ranges."""


# ---------------------------------------------------------------------------
# Allowed enums
# ---------------------------------------------------------------------------
ALLOWED_STRATEGY_TYPES: tuple[str, ...] = ("mean_reversion", "rsi_momentum")

# Blueprint line 934 session-filter vocabulary.
ALLOWED_SESSION_FILTERS: tuple[str, ...] = (
    "london_only",
    "24h",
    "ny_only",
    "asian_only",
)

# ---------------------------------------------------------------------------
# STRATEGY_PARAM_RANGES — hard gate (blueprint line ~4652)
# ---------------------------------------------------------------------------
# Numeric params (top-level) and one dotted nested param (entry.threshold).
# Values outside [lo, hi] are rejected by validate_strategy_params.
STRATEGY_PARAM_RANGES: dict[str, tuple[float, float]] = {
    "stop_loss_pct": (0.5, 10.0),
    "profit_target_pct": (0.5, 20.0),
    "trailing_stop_pct": (0.0, 5.0),
    "position_size_r": (0.05, 1.0),
    "time_exit_cycles": (60, 2880),
    "trailing_atr_mult": (0.5, 5.0),
    "mfe_giveback_min_pct": (0.1, 5.0),
    "mfe_giveback_frac": (0.1, 1.0),
    "entry.threshold": (5, 95),
    "entry.min_oversold_pairs": (1, 10),
}

# Nested params are addressed by dotted path; the resolver maps them to a getter.
_DOTTED_RESOLVERS = {
    "entry.threshold": lambda s: (s.get("entry") or {}).get("threshold"),
    "entry.min_oversold_pairs": lambda s: (s.get("entry") or {}).get("min_oversold_pairs"),
}

# Minimum stop-loss, also referenced independently by some guards (L40 floor).
MIN_STOP_LOSS_PCT = STRATEGY_PARAM_RANGES["stop_loss_pct"][0]


def resolve_param(strategy: dict, param: str):
    """Return the value for ``param`` from ``strategy`` (dotted paths supported).

    Returns ``None`` if the param is absent — absent params are skipped by the
    range gate (they are validated elsewhere, e.g. required-field checks).
    """
    if param in _DOTTED_RESOLVERS:
        return _DOTTED_RESOLVERS[param](strategy)
    return strategy.get(param)
