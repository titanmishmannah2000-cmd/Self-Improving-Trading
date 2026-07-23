"""Engines package — entry, exit, risk, chart vision, reflection, backtest, loop (S4-S10)."""

from __future__ import annotations

from hermes_core.engines.backtest import (
    _permutation_pvalue,
    backtest_gp_indicator,
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
from hermes_core.engines.kelly_sizing import (
    apply_kelly_sizing,
    kelly_size_mult,
    kelly_sizing_enabled,
)
from hermes_core.engines.entry_ranking import (
    entry_ranking_enabled,
    rank_candidates,
    score_candidate,
)
from hermes_core.engines.exit_intel import apply_exit_intel, exit_intel_enabled
from hermes_core.engines.book_risk import apply_book_risk, book_risk_enabled
from hermes_core.engines.excursion import (
    excursion_from_position,
    mfe_tracking_enabled,
    update_position_excursions,
)
from hermes_core.engines.regime_sizing import (
    apply_regime_sizing,
    regime_size_mult,
    regime_sizing_enabled,
)
from hermes_core.engines.mom_range_guard import (
    apply_mom_range_guard,
    gp_agree_bullish,
    mom_range_guard_enabled,
)
from hermes_core.engines.policy_engine import Policy, PolicyEngine, soft_weights_enabled
from hermes_core.engines.expert_weights import (
    EXPERT_TYPES,
    SOFT_SUPPRESS_MULT,
    apply_expert_weight,
    expert_weight,
    pair_expert_weights,
)
from hermes_core.engines.skip_shadow_learn import (
    analyze_skip_shadow,
    format_skip_shadow_context,
    maybe_promote_skip_shadow,
    maybe_skip_shadow_learn,
    promote_skip_shadow_proposal,
    propose_skip_shadow_notes,
    skip_shadow_promote_enabled,
    skip_shadow_reflect_enabled,
)
from hermes_core.engines.gp_promote_gate import (
    is_promote_allowed,
    observe_shadow,
    record_pnl as gp_gate_record_pnl,
    refresh_from_pnls,
)
from hermes_core.engines.reflect import (
    _is_reflection_done,
    _mark_reflection_done,
    aggregate_trades,
    apply_strategy_change,
    call_deepseek,
    call_gemini,
    call_groq,
    call_llm_consensus,
    combined_reflect,
    layer1_rule_based,
    maybe_reflect_pair,
    run_reflection_pipeline,
)
from hermes_core.engines.risk import (
    MAX_POSITION_SIZE,
    PROBE_EVIDENCE_MIN,
    PROBE_SIZE_FRACTION,
    apply_probe_sizing,
    check_rr_guard,
    compute_atr_stop,
    compute_position_size,
    evidence_state_for,
    param_range_gate,
    size,
    size_regime_from_market,
)

__all__ = [
    "Signal", "evaluate_entry",
    "Exit", "evaluate_exit", "should_circuit_break",
    "MAX_POSITION_SIZE", "check_rr_guard", "compute_atr_stop",
    "compute_position_size", "param_range_gate", "size",
    "size_regime_from_market",
    "apply_probe_sizing", "PROBE_EVIDENCE_MIN", "PROBE_SIZE_FRACTION",
    "evidence_state_for",
    "get_chart_context", "get_all_chart_contexts", "analyze_chart",
    "hard_block", "soft_block",
    "aggregate_trades", "layer1_rule_based", "combined_reflect",
    "call_deepseek", "call_gemini", "call_groq", "call_llm_consensus",
    "_is_reflection_done", "_mark_reflection_done",
    "run_reflection_pipeline", "maybe_reflect_pair", "apply_strategy_change",
    "backtest_with_history", "backtest_gp_indicator",
    "phase0_corr", "_permutation_pvalue",
    "CrisisLearning", "check_novel_regime", "find_nearest_crisis",
    "get_crisis_recommendation", "save_lived_crisis",
    "GeneticEngine", "discover", "load_discovered_indicators", "redundancy_check",
    "GPIntelligence", "get_label", "gp_entry_score", "is_locked",
    "record_loss", "record_win", "should_suppress", "weight_for",
    "Cortex", "Policy", "PolicyEngine", "soft_weights_enabled",
    "EXPERT_TYPES", "SOFT_SUPPRESS_MULT", "apply_expert_weight",
    "expert_weight", "pair_expert_weights",
    "apply_regime_sizing", "regime_size_mult", "regime_sizing_enabled",
    "apply_mom_range_guard", "gp_agree_bullish", "mom_range_guard_enabled",
    "apply_kelly_sizing", "kelly_size_mult", "kelly_sizing_enabled",
    "entry_ranking_enabled", "score_candidate", "rank_candidates",
    "apply_exit_intel", "exit_intel_enabled",
    "apply_book_risk", "book_risk_enabled",
    "mfe_tracking_enabled", "update_position_excursions", "excursion_from_position",
    "skip_shadow_reflect_enabled", "maybe_skip_shadow_learn",
    "skip_shadow_promote_enabled", "maybe_promote_skip_shadow",
    "promote_skip_shadow_proposal",
    "analyze_skip_shadow", "format_skip_shadow_context", "propose_skip_shadow_notes",
    "is_promote_allowed", "gp_gate_record_pnl", "refresh_from_pnls", "observe_shadow",
    "run_cycle", "write_heartbeat", "maybe_circuit_break",
    "MAX_CONSECUTIVE_FAILURES", "CIRCUIT_SLEEP_S",
]
