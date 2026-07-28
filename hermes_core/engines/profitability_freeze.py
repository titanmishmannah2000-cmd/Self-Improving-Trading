"""Profitability Path Phase 0 — HIF freeze helpers.

Target soak profile: only ``BOOK_RISK`` enabled among HIF dormant flags.
``REFLECT_AUTO_DEPLOY`` must stay off; ``GP_PROMOTE`` off until Phase 4.
"""

from __future__ import annotations

from hermes_core.engines import hif_flags as hf
from hermes_core.env import get_env

# HIF flags that must be OFF during Phase 0–1 prove.
PHASE0_OFF_FLAGS: tuple[str, ...] = (
    "SOFT_WEIGHTS",
    "KELLY_SIZING",
    "REGIME_SIZING",
    "ENTRY_RANKING",
    "EXIT_INTEL",
    "PROBE_SIZING",
    "SKIP_SHADOW_REFLECT",
    "SKIP_SHADOW_PROMOTE",
    "CRISIS_RECOMMEND",
    "GP_PROMOTE",
)

PHASE0_ON_FLAGS: tuple[str, ...] = ("BOOK_RISK",)


def reflect_auto_deploy_off() -> bool:
    return get_env("REFLECT_AUTO_DEPLOY", "0") == "0"


def assert_phase0_freeze(*, snap: dict | None = None) -> dict:
    """Return a freeze report. ``ok`` is True only when Phase 0 profile holds.

    Does not raise — callers (tests, soak monitor, scorecard) decide policy.
    """
    snap = snap if snap is not None else hf.snapshot()
    flags = dict(snap.get("flags") or {})
    violations: list[str] = []

    for key in PHASE0_ON_FLAGS:
        if not flags.get(key):
            violations.append(f"{key}_should_be_on")
    for key in PHASE0_OFF_FLAGS:
        if flags.get(key):
            violations.append(f"{key}_should_be_off")

    enabled = sorted(k for k, v in flags.items() if v)
    if set(enabled) != set(PHASE0_ON_FLAGS):
        violations.append(f"enabled_set={enabled} expected={list(PHASE0_ON_FLAGS)}")

    if not reflect_auto_deploy_off():
        violations.append("REFLECT_AUTO_DEPLOY_should_be_0")

    # Deduplicate while preserving order.
    seen: set[str] = set()
    uniq: list[str] = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            uniq.append(v)

    return {
        "ok": len(uniq) == 0,
        "enabled": enabled,
        "n_enabled": len(enabled),
        "violations": uniq,
        "reflect_auto_deploy": get_env("REFLECT_AUTO_DEPLOY", "0"),
        "gp_promote": flags.get("GP_PROMOTE", False),
        "book_risk": flags.get("BOOK_RISK", False),
    }


def focus_pairs_for_bot(bot: str) -> list[str]:
    """Canonical Phase 0 focus universe."""
    b = (bot or "").lower()
    if b == "gold":
        return ["XAU/USD"]
    if b == "forex":
        return ["EUR/USD", "GBP/USD"]
    return []
