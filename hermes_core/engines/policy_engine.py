"""Policy engine (Session 15 / Phase 15).

Autonomous policy: suppresses entry types in BOTH directions (MR can suppress
GP, GP can suppress MR — whichever is underperforming), triggers priority
discovery when the fleet has >=2 exiled indicators, accelerates probes when
cortex is empty, and flags rollback candidates. Result persists to
state/policy.json so the dashboard + loop read a stable policy across restarts.

Suppression rules (blueprint ENGINE 9 / Phase 15):
  * suppress GP if MR WR >= 40% AND GP WR < 30%   [GUARD L35]
  * suppress MR if GP WR >= 50%
  * WRs are evaluated PER PAIR (a bleeding pair must not bench the fleet)
  * priority_discovery = True if >=2 indicators exiled fleet-wide
  * probe_interval = 10 if cortex has <5 entries
  * rollback flag if MR WR < 30% AND >=10 trades (fleet-level)

HIF Phase-2: when SOFT_WEIGHTS=1 the loop treats suppressions as size weights
(see expert_weights.py) instead of hard skips. Policy still records the L35
suppression set + a per-pair ``allocation`` map for the dashboard.
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_core.engines.decision_cortex import Cortex
from hermes_core.engines.expert_weights import pair_expert_weights
from hermes_core.env import get_env
from hermes_core.state.paths import current_bot, policy_path

# ── gates ──────────────────────────────────────────────────────────────────
SUPPRESS_GP_MR_WR = 0.40     # [GUARD L35] MR strong enough to bench GP
SUPPRESS_GP_GP_WR = 0.30     # GP weak enough to be benched by MR
SUPPRESS_MR_GP_WR = 0.50     # GP strong enough to bench MR
PRIORITY_DISCOVERY_EXILES = 2  # >=2 exiled fleet-wide -> discover
PROBE_CORTEX_THRESHOLD = 5    # cortex <5 entries -> probe every 10
ROLLBACK_MR_WR = 0.30         # MR WR < this + >=10 trades -> rollback flag
ROLLBACK_MIN_TRADES = 10

# Optional test override (tests monkeypatch this module attribute).
POLICY_PATH: Path | None = None


def _policy_file(bot: str | None = None) -> Path:
    if POLICY_PATH is not None:
        return POLICY_PATH
    return policy_path(bot)


def _save_policy(policy: dict, bot: str | None = None) -> None:
    path = _policy_file(bot)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(policy, indent=2), encoding="utf-8")


def soft_weights_enabled() -> bool:
    """HIF Phase-2 flag — default OFF (hard L35 suppress unchanged)."""
    return get_env("SOFT_WEIGHTS", "0") == "1"


class Policy:
    """Immutable-ish view of the evaluated policy."""

    def __init__(
        self,
        suppressions: dict,
        priority_discovery: bool,
        probe_interval: int,
        rollback: bool,
        allocation: dict | None = None,
        soft_weights: bool = False,
    ) -> None:
        self.suppressions = suppressions          # {pair: set(entry_type)}
        self.priority_discovery = priority_discovery
        self.probe_interval = probe_interval
        self.rollback = rollback
        self.allocation = allocation or {}        # {pair: {etype: weight_info}}
        self.soft_weights = soft_weights

    def is_suppressed(self, pair: str, entry_type: str) -> bool:
        return entry_type in self.suppressions.get(pair, set())

    def expert_weight_for(self, pair: str, entry_type: str) -> dict | None:
        """Return soft-weight info for (pair, entry_type) if allocation present."""
        by_type = self.allocation.get(pair) or {}
        return by_type.get(entry_type)

    def to_dict(self) -> dict:
        return {
            "suppressions": {p: sorted(t) for p, t in self.suppressions.items()},
            "priority_discovery": self.priority_discovery,
            "probe_interval": self.probe_interval,
            "rollback": self.rollback,
            "soft_weights": self.soft_weights,
            "allocation": {
                p: {
                    et: {
                        "weight": info.get("weight"),
                        "mode": info.get("mode"),
                        "suppressed_soft": info.get("suppressed_soft"),
                        "evidence_n": info.get("evidence_n"),
                        "wr": info.get("wr"),
                        "reasons": info.get("reasons"),
                    }
                    for et, info in (types or {}).items()
                }
                for p, types in self.allocation.items()
            },
        }


class PolicyEngine:
    """Evaluates and persists the live policy."""

    def evaluate(self, cycle: int, pairs: list[str],
                 cortex: Cortex | None = None,
                 current_strategies: dict | None = None) -> Policy:
        cortex = cortex or Cortex()
        suppressions: dict[str, set[str]] = {p: set() for p in pairs}

        for pair in pairs:
            # Per-pair WRs (not fleet-wide) so one bleeding pair cannot bench
            # GP/MR on healthy pairs. Sparse pairs simply do not suppress.
            mr_wr = cortex.entry_type_wr("mean_reversion", pair=pair)
            gp_wr = cortex.entry_type_wr("gp_ensemble", pair=pair)
            # GP suppressed only when MR is clearly better AND GP is poor
            if (mr_wr is not None and mr_wr >= SUPPRESS_GP_MR_WR
                    and gp_wr is not None and gp_wr < SUPPRESS_GP_GP_WR):
                suppressions[pair].add("gp_ensemble")          # [GUARD L35]
            # MR suppressed when GP is clearly better
            if gp_wr is not None and gp_wr >= SUPPRESS_MR_GP_WR:
                suppressions[pair].add("mean_reversion")

        exiled = cortex.get_exiled_indicators()
        priority_discovery = len(exiled) >= PRIORITY_DISCOVERY_EXILES

        n_entries = len(cortex._entries) if hasattr(cortex, "_entries") else 0
        probe_interval = 10 if n_entries < PROBE_CORTEX_THRESHOLD else 50

        # Rollback remains fleet-level (overall MR health).
        mr_wr = cortex.entry_type_wr("mean_reversion")
        n_trades = sum(1 for e in getattr(cortex, "_entries", [])
                       if e.get("type") == "mean_reversion"
                       and e.get("outcome") is not None)
        rollback = (mr_wr is not None and mr_wr < ROLLBACK_MR_WR
                    and n_trades >= ROLLBACK_MIN_TRADES)

        soft = soft_weights_enabled()
        allocation: dict[str, dict] = {}
        for pair in pairs:
            try:
                allocation[pair] = pair_expert_weights(
                    pair, cortex, suppressions.get(pair, set()), enabled=soft,
                )
            except Exception:  # noqa: BLE001 — never break policy on alloc
                allocation[pair] = {}

        policy = Policy(
            suppressions, priority_discovery, probe_interval, rollback,
            allocation=allocation, soft_weights=soft,
        )
        _save_policy(policy.to_dict(), current_bot())
        return policy

    def get_policy(self, bot: str | None = None) -> Policy | None:
        path = _policy_file(bot)
        if not path.exists():
            return None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return Policy(
            {p: set(t) for p, t in d.get("suppressions", {}).items()},
            d.get("priority_discovery", False),
            d.get("probe_interval", 50),
            d.get("rollback", False),
            allocation=d.get("allocation") or {},
            soft_weights=bool(d.get("soft_weights", False)),
        )
