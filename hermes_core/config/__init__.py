"""Configuration schema, loader, and validator (Session 1 / Phase 1)."""

from __future__ import annotations

from .loader import _seed_default_state, load_config, load_strategy_for_pair, repo_root, state_root
from .schema import (
    ALLOWED_SESSION_FILTERS,
    ALLOWED_STRATEGY_TYPES,
    STRATEGY_PARAM_RANGES,
    ValidationError,
    resolve_param,
)
from .validator import validate_strategy_params

# Phase-1 build-target function names (blueprint Section 7 Phase 1).
__all__ = [
    "load_config",
    "load_strategy_for_pair",
    "validate_strategy_params",
    "_seed_default_state",
    "repo_root",
    "state_root",
    "ValidationError",
    "STRATEGY_PARAM_RANGES",
    "ALLOWED_STRATEGY_TYPES",
    "ALLOWED_SESSION_FILTERS",
    "resolve_param",
]
