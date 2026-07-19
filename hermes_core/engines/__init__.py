"""Engines package — entry, exit, risk, chart vision, reflection, backtest, loop (S4-S10)."""

from __future__ import annotations

from hermes_core.engines.backtest import (
    _permutation_pvalue,
    backtest_with_history,
    phase0_corr,
)
from hermes_core.engines.chart_vision import (
    analyze_chart,
    get_all_chart_contexts,
    get_chart_context,
    hard_block,
    soft_block,
)
from hermes_core.engines.crisis_learning import (
    CrisisLearning,
    check_novel_regime,
    find_nearest_crisis,
    get_crisis_recommendation,
    save_lived_crisis,
)
from hermes_core.engines.decision_cortex import Cortex
from hermes_core.engines.entry import Signal, evaluate_entry
from hermes_core.engines.entry import (
    gp_ensemble_signal,
    simulate_gp_paper_pnl,
    _gp_eval_last,
    _gp_parse,
)
from hermes_core.engines.exit import Exit, evaluate_exit, should_circuit_break
from hermes_core.engines.genetic import (
    GeneticEngine,
    discover,
    load_discovered_indicators,
    redundancy_check,
)
from hermes_core.engines.gp_intelligence import (
    GPIntelligence,
    get_label,
    gp_entry_score,
    is_locked,
    record_loss,
    record_win,
    should_suppress,
    weight_for,
)
from hermes_core.engines.loop import (
    CIRCUIT_SLEEP_S,
    MAX_CONSECUTIVE_FAILURES,
    maybe_circuit_break,
    run_cycle,
    write_heartbeat,
)
from hermes_core.engines.policy_engine import Policy, PolicyEngine
from hermes_core.engines.reflect import (
    aggregate_trades,
    call_deepseek,
    call_gemini,
    call_groq,
    call_llm_consensus,
    combined_reflect,
    layer1_rule_based,
)
from hermes_core.engines.risk import (
    MAX_POSITION_SIZE,
    check_rr_guard,
    compute_atr_stop,
    compute_position_size,
    param_range_gate,
    size,
)

__all__ = [
    "Signal", "evaluate_entry",
    "Exit", "evaluate_exit", "should_circuit_break",
    "MAX_POSITION_SIZE", "check_rr_guard", "compute_atr_stop",
    "compute_position_size", "param_range_gate", "size",
    "get_chart_context", "get_all_chart_contexts", "analyze_chart",
    "hard_block", "soft_block",
    "aggregate_trades", "layer1_rule_based", "combined_reflect",
    "call_deepseek", "call_gemini", "call_groq", "call_llm_consensus",
    "backtest_with_history", "phase0_corr", "_permutation_pvalue",
    "CrisisLearning", "check_novel_regime", "find_nearest_crisis",
    "get_crisis_recommendation", "save_lived_crisis",
    "GeneticEngine", "discover", "load_discovered_indicators", "redundancy_check",
    "GPIntelligence", "get_label", "gp_entry_score", "is_locked",
    "record_loss", "record_win", "should_suppress", "weight_for",
    "Cortex", "Policy", "PolicyEngine",
    "run_cycle", "write_heartbeat", "maybe_circuit_break",
    "MAX_CONSECUTIVE_FAILURES", "CIRCUIT_SLEEP_S",
]
