"""Per-strategy parameter validation (Session 1 / Phase 1).

Implements the blueprint's ``validate_strategy_params`` hard gate:
- every numeric param inside STRATEGY_PARAM_RANGES (blueprint line ~4652);
- strategy_type is one of the allowed enum values;
- entry.session_filter (if present) is in the allowed vocabulary (blueprint 934).

Design note (blueprint discipline 1.5): this is pure logic, no I/O. The loader
calls it after reading YAML; callers may choose raise-on-fail (default) or a
(bool, errors) tuple for batch use.
"""

from __future__ import annotations

from .schema import (
    ALLOWED_SESSION_FILTERS,
    ALLOWED_STRATEGY_TYPES,
    STRATEGY_PARAM_RANGES,
    ValidationError,
    resolve_param,
)


def _validate_ranges(strategy: dict) -> list[str]:
    errors: list[str] = []
    for param, (lo, hi) in STRATEGY_PARAM_RANGES.items():
        val = resolve_param(strategy, param)
        if val is None:
            continue  # absent -> not a range violation; required-ness checked elsewhere
        try:
            v = float(val)
        except (TypeError, ValueError):
            errors.append(f"{param}={val!r} (not numeric)")
            continue
        if v < lo or v > hi:
            errors.append(f"{param}={v} outside safe range [{lo}, {hi}]")
    return errors


def _validate_enums(strategy: dict) -> list[str]:
    errors: list[str] = []
    stype = strategy.get("strategy_type")
    if stype is not None and stype not in ALLOWED_STRATEGY_TYPES:
        errors.append(f"strategy_type={stype!r} not in {ALLOWED_STRATEGY_TYPES}")
    session = (strategy.get("entry") or {}).get("session_filter")
    if session is not None and session not in ALLOWED_SESSION_FILTERS:
        errors.append(f"entry.session_filter={session!r} not in {ALLOWED_SESSION_FILTERS}")
    return errors


def validate_strategy_params(strategy: dict, raise_on_fail: bool = True) -> tuple[bool, list[str]]:
    """Hard gate: validate ALL strategy params are within safe ranges/enums.

    Returns (valid, errors). When ``raise_on_fail`` is True (default) a non-empty
    error list raises ``ValidationError`` carrying the joined messages.

    Matches the blueprint build-target signature exactly:
        validate_strategy_params(strategy: dict) -> None  # raises ValueError
    while the stricter Phase-1 test asserts ``pytest.raises(ValidationError)``.
    ValidationError subclasses ValueError, so both forms are satisfied.
    """
    errors = _validate_ranges(strategy) + _validate_enums(strategy)
    if errors and raise_on_fail:
        raise ValidationError("; ".join(errors))
    return (not errors, errors)
