#!/usr/bin/env python3
"""G-contract: assert engine function signatures match blueprint Section 6."""

from __future__ import annotations

import inspect
import sys
from typing import Any

# (module_path, callable_name, min_positional_args)
CONTRACTS: list[tuple[str, str, int]] = [
    ("hermes_core.adapters.price", "fetch", 1),
    ("hermes_core.indicators", "compute_all", 1),
    ("hermes_core.engines.entry", "evaluate_entry", 3),
    ("hermes_core.engines.exit", "evaluate_exit", 2),
    ("hermes_core.engines.risk", "size", 4),
    ("hermes_core.engines.reflect", "layer1_rule_based", 4),
    ("hermes_core.engines.backtest", "backtest_with_history", 4),
    ("hermes_core.engines.genetic", "discover", 2),
    ("hermes_core.engines.gp_intelligence", "gp_entry_score", 1),
    ("hermes_core.engines.chart_vision", "get_chart_context", 1),
    ("hermes_core.engines.crisis_learning", "get_crisis_recommendation", 1),
    ("hermes_core.engines.decision_cortex", "Cortex", 0),
    ("hermes_core.engines.policy_engine", "PolicyEngine", 0),
    ("hermes_core.engines.self_audit", "run", 0),
    ("hermes_core.engines.loop", "run_cycle", 2),
    ("hermes_core.engines.loop", "write_heartbeat", 4),
]


def _import_attr(module_path: str, name: str) -> Any:
    import importlib

    mod = importlib.import_module(module_path)
    return getattr(mod, name)


def main() -> int:
    failures: list[str] = []
    for module_path, name, min_args in CONTRACTS:
        try:
            obj = _import_attr(module_path, name)
        except (ImportError, AttributeError) as exc:
            failures.append(f"{module_path}.{name}: missing ({exc})")
            continue
        if inspect.isclass(obj):
            if (
                not hasattr(obj, "run")
                and name in ("Cortex", "PolicyEngine", "SelfAudit")
                and name == "Cortex"
                and not callable(getattr(obj, "record_entry", None))
            ):
                failures.append(f"{module_path}.{name}: missing record_entry")
            continue
        sig = inspect.signature(obj)
        n_params = len(
            [
                p
                for p in sig.parameters.values()
                if p.default is inspect.Parameter.empty
                and p.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            ]
        )
        if n_params < min_args:
            failures.append(
                f"{module_path}.{name}: expected >={min_args} required args, got {n_params}"
            )

    if failures:
        print("G-contract FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(f"G-contract OK: {len(CONTRACTS)} contracts verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
