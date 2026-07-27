"""HIF feature flags — wired in the loop, dormant at env default 0.

Single source of truth for soak-conservative toggles. Enabling any flag is a
policy decision (changes trade behavior); this module never flips defaults.
Heartbeat / self-audit call ``snapshot()`` so dormancy is visible, not silent.
"""

from __future__ import annotations

from hermes_core.env import get_env

# (env_key, short_label) — order matches .env.example HIF block.
DORMANT_FLAGS: tuple[tuple[str, str], ...] = (
    ("PROBE_SIZING", "25% probe size on thin evidence"),
    ("SOFT_WEIGHTS", "soft expert benches"),
    ("REGIME_SIZING", "regime size multiplier"),
    ("KELLY_SIZING", "fractional Kelly sizing"),
    ("ENTRY_RANKING", "trad vs GP entry rank"),
    ("EXIT_INTEL", "cortex trail/BE/partial knobs"),
    ("BOOK_RISK", "book soft-cap + tilt"),
    ("SKIP_SHADOW_REFLECT", "skip/GP-shadow → reflection fuel"),
    ("SKIP_SHADOW_PROMOTE", "gated skip-shadow promote"),
    ("GP_PROMOTE", "live GP entries (shadow still runs)"),
    ("CRISIS_RECOMMEND", "soft-widen stop from nearest crisis"),
)


def flag_on(name: str, default: str = "0") -> bool:
    """True only when env is exactly ``1`` (missing/empty/other → off)."""
    return get_env(name, default) == "1"


def probe_sizing_enabled() -> bool:
    return flag_on("PROBE_SIZING")


def crisis_recommend_enabled() -> bool:
    return flag_on("CRISIS_RECOMMEND")


def gp_promote_enabled() -> bool:
    return flag_on("GP_PROMOTE")


def snapshot() -> dict:
    """Per-flag on/off map + counts for heartbeat / self-audit.

    ``dormant`` lists flags that are wired but currently off (expected soak).
    """
    flags: dict[str, bool] = {}
    for key, _label in DORMANT_FLAGS:
        flags[key] = flag_on(key)
    enabled = sorted(k for k, v in flags.items() if v)
    dormant = sorted(k for k, v in flags.items() if not v)
    return {
        "flags": flags,
        "enabled": enabled,
        "dormant": dormant,
        "n_enabled": len(enabled),
        "n_dormant": len(dormant),
        "n_total": len(flags),
    }
