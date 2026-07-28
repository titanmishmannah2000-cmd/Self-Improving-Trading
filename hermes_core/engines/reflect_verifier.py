"""Profitability Path Phase 3 — verifier gates before reflection deploy.

LLM/L1 proposals that pass backtest still need hard evidence gates:
  * min trades in the reflection batch
  * OOS expectancy not worse than incumbent (from backtest verdict)
  * MDD ≤ cap
  * param within schema / profitability tunables
Reject reasons are persisted via experiment_control.param quarantine helpers.
"""

from __future__ import annotations

import contextlib
from typing import Any

# Phase 1 locked primary tunables — reflection may only touch these.
PROFITABILITY_TUNABLES: frozenset[str] = frozenset(
    {
        "stop_loss_pct",
        "profit_target_pct",
        "mr_entry_rsi",
        "threshold",  # momentum RSI threshold (nested under entry in YAML)
        "entry.threshold",
        "entry.mr_entry_rsi",
        "entry.session_filter",
        "session_filter",
        "trailing_stop_pct",  # still one-variable; allowed for giveback fixes
    }
)

DEFAULT_MIN_TRADES = 15
DEFAULT_MAX_DD = 10.0


def verify_reflection_candidate(
    *,
    pair: str,
    proposal: dict,
    verdict: dict,
    trades: list[dict] | None = None,
    max_dd: float = DEFAULT_MAX_DD,
    min_trades: int = DEFAULT_MIN_TRADES,
    bot: str = "forex",
) -> dict[str, Any]:
    """Return ``{ok, reason, details}``. ``ok=False`` → do not deploy."""
    details: dict[str, Any] = {}
    variable = str(proposal.get("variable") or "")
    details["variable"] = variable

    if variable and variable not in PROFITABILITY_TUNABLES:
        # Allow bare entry.* aliases already listed; reject exotic axes.
        base = variable.split(".")[-1]
        if variable not in PROFITABILITY_TUNABLES and base not in {
            "stop_loss_pct",
            "profit_target_pct",
            "mr_entry_rsi",
            "threshold",
            "session_filter",
            "trailing_stop_pct",
        }:
            return {
                "ok": False,
                "reason": f"variable_not_in_profitability_tunables:{variable}",
                "details": details,
            }

    n = len(trades or [])
    details["n_trades"] = n
    if n < int(min_trades):
        return {
            "ok": False,
            "reason": f"insufficient_trades:{n}<{min_trades}",
            "details": details,
        }

    if not verdict.get("approved"):
        return {
            "ok": False,
            "reason": "backtest_not_approved",
            "details": details,
        }

    # OOS / full improvement from backtest verdict (strict path already gates).
    oos_imp = verdict.get("improvement_oos")
    full_imp = verdict.get("improvement_full")
    details["improvement_oos"] = oos_imp
    details["improvement_full"] = full_imp
    if oos_imp is not None:
        try:
            if float(oos_imp) < 0:
                return {
                    "ok": False,
                    "reason": "oos_expectancy_worse_than_incumbent",
                    "details": details,
                }
        except (TypeError, ValueError):
            pass

    # MDD from phases if present.
    phases = verdict.get("phases") or {}
    mdd = None
    for key in ("phase1_historical", "phase2_walkfwd", "phase0_oos"):
        block = phases.get(key) if isinstance(phases, dict) else None
        if isinstance(block, dict):
            for mk in ("max_dd", "new_max_dd", "mdd"):
                if block.get(mk) is not None:
                    try:
                        mdd = float(block[mk])
                    except (TypeError, ValueError):
                        mdd = None
                    break
        if mdd is not None:
            break
    details["max_dd"] = mdd
    if mdd is not None and mdd > float(max_dd):
        return {
            "ok": False,
            "reason": f"mdd_exceeds_cap:{mdd}>{max_dd}",
            "details": details,
        }

    # Schema range check via config validator when available.
    with contextlib.suppress(Exception):
        from hermes_core.config.schema import STRATEGY_PARAM_RANGES

        ranges = STRATEGY_PARAM_RANGES
        bare = variable.split(".")[-1]
        if bare in ranges:
            lo, hi = ranges[bare]
            try:
                new_v = float(proposal.get("new"))
            except (TypeError, ValueError):
                new_v = None
            details["new"] = new_v
            if new_v is not None and not (float(lo) <= new_v <= float(hi)):
                return {
                    "ok": False,
                    "reason": f"param_out_of_schema:{new_v} not in [{lo},{hi}]",
                    "details": details,
                }

    return {"ok": True, "reason": "verifier_pass", "details": details}


def record_verifier_reject(
    bot: str,
    pair: str,
    *,
    proposal: dict,
    reason: str,
    details: dict | None = None,
) -> None:
    """Persist reject into experiment_control quarantine (fail-soft)."""
    with contextlib.suppress(Exception):
        from hermes_core.engines import experiment_control as exp

        exp.record_pipeline_outcome(
            bot,
            pair,
            variable=str(proposal.get("variable")),
            status="verifier_reject",
            old=proposal.get("old"),
            new=proposal.get("new"),
            reason=reason,
        )
        # Prefer dedicated quarantine if available.
        if hasattr(exp, "quarantine_param"):
            exp.quarantine_param(
                bot,
                pair,
                variable=str(proposal.get("variable")),
                reason=reason,
                details=details or {},
            )
