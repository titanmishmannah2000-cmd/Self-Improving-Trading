"""Reflection engine (Session 9 / Phase 9) — Layer 1 rule-based self-review.

L1 is deterministic arithmetic over a batch of closed trades (NO LLM, NO network):
given recent trades for a pair and its strategy, it proposes exactly ONE parameter
change driven by drawdown / win-rate, or returns None.

Decision tree (blueprint Section 7 Engine 2 / line 544, line 730, L45):
  * DD > max_dd            -> tighten stop_loss_pct by -0.3 (floor 0.5)  [guard L45]
  * WR < 0.3               -> widen  stop_loss_pct (raise it)            [guard L45]
  * return < -0.5 & >=8t & >=10t -> widen stop_loss_pct (raise it)
  * one_variable_only:     at most ONE change per batch.
  * confidence gate:       self-assigned 0.40 (fixed for the pure rule tree).

Discipline (S9 contract, roadmap 8.1-8.2):
  * SHADOW-ONLY / APPROVAL-GATED. layer1_rule_based NEVER mutates the live
    strategy; combined_reflect only LOGS a proposal to state/hypotheses.jsonl.
    The actual YAML edit + version bump is a separate, user-approved step
    (Phase 10 backtest validates before anything ships live).
  * Every proposal is reconstructable: hypotheses.jsonl records
    pair, variable, old -> new, reason, confidence, and the trade stats that
    produced it.

Functions (blueprint Phase 9 build target):
  layer1_rule_based(pair, trades, goal, strategy) -> tuple | None
  combined_reflect(pair, trades, goal, chart_context="", ...) -> list[dict]
"""

from __future__ import annotations

import json

from hermes_core.config import load_config, repo_root

STOP_FLOOR = 0.5          # [GUARD L45] stop_loss_pct never goes below this
STOP_TIGHTEN = 0.3        # DD breach -> tighten by this much
CONFIDENCE = 0.40         # L1 fixed confidence gate
HYPOTHESES_PATH = repo_root() / "state" / "hypotheses.jsonl"


# ── pure helpers (unit-tested, no I/O) ─────────────────────────────────────
def aggregate_trades(trades: list[dict]) -> dict:
    """Compute win_rate, pnl stats, drawdown proxy from a batch of closed trades.

    A trade record carries at least: pnl_pct, exit_price, entry_price.
    'drawdown' here = worst single-trade loss (blueprint uses max_dd vs the
    goal's max_drawdown, expressed in %). Both are in percent units.
    """
    if not trades:
        return {"count": 0, "win_rate": 0.0, "ret": 0.0,
                "worst_loss": 0.0, "drawdown": 0.0}
    pnls = [float(t.get("pnl_pct", 0.0)) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    worst = min(pnls) if pnls else 0.0
    return {
        "count": len(pnls),
        "win_rate": wins / len(pnls),
        "ret": sum(pnls),
        "worst_loss": worst,                # <= 0
        "drawdown": -worst,                 # >= 0, percent
    }


def layer1_rule_based(
    pair: str,
    trades: list[dict],
    goal: dict,
    strategy: dict,
) -> tuple | None:
    """L1 rule tree. Returns (variable, old, new, reason, confidence) or None.

    Pure: no I/O, no mutation. The caller decides whether to log/apply it.
    """
    if not trades or not strategy:
        return None
    agg = aggregate_trades(trades)
    if agg["count"] < 5:
        return None  # [guard] need a minimum sample before anything changes

    cur_stop = float(strategy.get("stop_loss_pct", 1.5))
    reason_parts: list[str] = []
    decision = None  # (new_stop, why)

    max_dd = float((goal or {}).get("max_drawdown", 10.0))
    # drawdown is in %; goal max_drawdown is in % (10.0 == 10%)
    if agg["drawdown"] > max_dd:
        # tighten stop to cut further damage, but never below floor
        new_stop = max(STOP_FLOOR, round(cur_stop - STOP_TIGHTEN, 4))
        reason_parts.append(f"drawdown {agg['drawdown']:.2f}% > max_dd {max_dd:.2f}%")
        decision = (new_stop, "tighten stop on drawdown breach")

    if decision is None and agg["win_rate"] < 0.3:
        # widen stop so noise doesn't stop us out; only if still above floor
        new_stop = max(STOP_FLOOR, round(cur_stop + 0.3, 4))
        reason_parts.append(f"win_rate {agg['win_rate']:.2f} < 0.30")
        decision = (new_stop, "widen stop on low win-rate")

    if decision is None and agg["ret"] < -0.5 and agg["count"] >= 10:
        # sustained bleed (>=10 trades, aggregate return < -0.5%): widen stop
        new_stop = max(STOP_FLOOR, round(cur_stop + 0.3, 4))
        reason_parts.append(f"ret {agg['ret']:.2f}% < -0.5 over {agg['count']} trades")
        decision = (new_stop, "widen stop on sustained loss")

    if decision is None:
        return None  # no rule fired -> no change

    new_stop, why = decision
    # The floor (STOP_FLOOR) may have clamped new_stop == cur_stop. That is NOT a
    # no-op: it is a legitimate "attempted to tighten/widen but already at the
    # floor" reflection signal (blueprint test_floor_enforced asserts new >= 0.5,
    # not that we suppress it). Always return a fired rule's proposal.
    return ("stop_loss_pct", cur_stop, new_stop, f"{why}; {'; '.join(reason_parts)}", CONFIDENCE)


def _log_hypothesis(rec: dict) -> None:
    """Append a reflection hypothesis to state/hypotheses.jsonl (shadow log)."""
    HYPOTHESES_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(HYPOTHESES_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


def combined_reflect(
    pair: str,
    trades: list[dict],
    goal: dict | None = None,
    chart_context: str = "",
    skipped_json: str = "",
    strategy: dict | None = None,
    bot: str = "forex",
) -> list[dict]:
    """L1 orchestrator. Returns the list of proposed (shadow) changes.

    SHADOW-ONLY: it never mutates the live strategy. Each proposal is logged to
    state/hypotheses.jsonl with full provenance so you can approve it later.
    Exactly one variable may change per call (one_variable_only).
    """
    if goal is None:
        goal = (load_config(bot) if bot else {}).get("goal", {})
    if strategy is None:
        from hermes_core.config import load_strategy_for_pair
        strategy = load_strategy_for_pair(pair, bot)

    change = layer1_rule_based(pair, trades, goal, strategy)
    if change is None:
        return []

    variable, old, new, reason, confidence = change
    stats = aggregate_trades(trades)
    rec = {
        "pair": pair,
        "bot": bot,
        "variable": variable,
        "old": old,
        "new": new,
        "reason": reason,
        "confidence": confidence,
        "chart_context": chart_context,
        "stats": stats,
        "ts": __import__("time").time(),
        "status": "proposed",  # NOT applied — awaits approval + backtest (S10)
    }
    _log_hypothesis(rec)
    return [rec]


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2 — three-model consensus (Session 11 / Phase 11)
#
# Corrected, tiered score gate (roadmap header correction is BINDING here):
#   score < 65          -> L2 is NEVER called; L1 stands/rejected on its own.
#   65 <= score < 75    -> 2/3 consensus required to apply.
#   score >= 75         -> UNANIMOUS 3/3 required to apply.
#   confidence >= 0.40   -> required to apply regardless of vote outcome.
#
# NOTE: the blueprint's documented 55 gate is a REGRESSION this rebuild corrects.
# Do NOT implement the gate at 55 as the standard — 65 is the standard, 75 the
# unanimous bar. See roadmap S11 DO-NOT.
# ═══════════════════════════════════════════════════════════════════════════
L2_MIN_SCORE = 65          # [GUARD L53] below this, L2 is never invoked
L2_UNANIMOUS_SCORE = 75    # at/above this, 3/3 unanimous required
APPLY_CONFIDENCE = 0.40    # [GUARD L53] min confidence to apply any change

# Ordered cascade: DeepSeek -> Gemini -> Groq. Each is a backup if the prior
# call fails (network/quota/empty). Tests inject fakes; prod lazy-imports.
DEFAULT_MODELS = ("deepseek", "gemini", "groq")


def call_deepseek(prompt: str, api_key: str | None = None) -> str:  # pragma: no cover
    """DeepSeek chat completion. Lazy import; network. Monkeypatch in tests."""
    from openai import OpenAI
    client = OpenAI(api_key=api_key or _env("DEEPSEEK_API_KEY"),
                    base_url="https://api.deepseek.com")
    resp = client.chat.completions.create(
        model="deepseek-chat", messages=[{"role": "user", "content": prompt}])
    return resp.choices[0].message.content or ""


def call_gemini(prompt: str, api_key: str | None = None) -> str:  # pragma: no cover
    """Gemini chat generation. Lazy import; network. Monkeypatch in tests."""
    import google.generativeai as genai
    genai.configure(api_key=api_key or _env("GEMINI_API_KEY"))
    model = genai.GenerativeModel("gemini-1.5-flash")
    resp = model.generate_content(prompt)
    return resp.text or ""


def call_groq(prompt: str, api_key: str | None = None) -> str:  # pragma: no cover
    """Groq chat completion (fallback). Lazy import; network. Monkeypatch in tests."""
    from openai import OpenAI
    client = OpenAI(api_key=api_key or _env("GROQ_API_KEY"),
                    base_url="https://api.groq.com/openai/v1")
    resp = client.chat.completions.create(
        model="llama-3.1-8b-instant", messages=[{"role": "user", "content": prompt}])
    return resp.choices[0].message.content or ""


def _env(name: str) -> str | None:
    import os
    return os.environ.get(name)


_MODEL_CALLERS = {
    "deepseek": call_deepseek,
    "gemini": call_gemini,
    "groq": call_groq,
}


def _parse_vote(text: str) -> bool:
    """A model 'votes yes' if its reply contains an affirmative token (whole word).

    Conservative: only explicit YES / APPROVE / AGREE / ACCEPT counts. We use
    word-boundary matching so the word "applied" (in the prompt) does NOT falsely
    read as a YES. Everything else (silence, errors, hedging, "NO") is a NO.
    Fail-closed.
    """
    import re
    t = (text or "").strip().upper()
    if not t:
        return False
    return bool(re.search(r"\b(YES|APPROVE|AGREE|ACCEPT)\b", t))


class ConsensusResult:
    """Outcome of the L2 consensus gate over a single proposal."""

    __slots__ = ("score", "threshold", "votes_yes", "votes_total",
                 "required", "confidence", "decision", "reasons")

    def __init__(self, score: float, threshold: float, votes_yes: int,
                 votes_total: int, required: int, confidence: float,
                 decision: bool, reasons: list[str]):
        self.score = score
        self.threshold = threshold
        self.votes_yes = votes_yes
        self.votes_total = votes_total
        self.required = required
        self.confidence = confidence
        self.decision = decision
        self.reasons = reasons

    def to_dict(self) -> dict:
        return {
            "score": self.score, "threshold": self.threshold,
            "votes_yes": self.votes_yes, "votes_total": self.votes_total,
            "required": self.required, "confidence": self.confidence,
            "decision": self.decision, "reasons": self.reasons,
        }


def _required_votes(score: float) -> tuple[int, str]:
    """Return (required yes-votes, human label) for a given score (gate logic)."""
    if score >= L2_UNANIMOUS_SCORE:
        return 3, "unanimous 3/3 (score>=75)"
    if score >= L2_MIN_SCORE:
        return 2, "2/3 majority (65<=score<75)"
    return 0, "L2 not invoked (score<65)"


def call_llm_consensus(
    proposal: dict,
    context: str = "",
    *,
    score: float | None = None,
    confidence: float | None = None,
    models: tuple[str, ...] = DEFAULT_MODELS,
    callers: dict[str, callable] | None = None,
) -> ConsensusResult:
    """Run the tiered three-model consensus gate over a proposal.

    `score` and `confidence` are normally taken from the L1 proposal; both are
    injectable so the gate logic is testable without producing a real proposal.
    `callers` lets tests inject fake model functions keyed by model name.

    Gate (fail-closed): below L2_MIN_SCORE the models are never consulted and the
    decision is REJECT (L1 must stand/rejected on its own). At/above the bar the
    required vote count (2/3 or 3/3) must be met AND confidence >= APPLY_CONF.
    """
    score = float(score if score is not None else proposal.get("confidence", 0.0) * 100)
    confidence = float(confidence if confidence is not None
                       else proposal.get("confidence", 0.0))
    required, label = _required_votes(score)

    if required == 0:
        return ConsensusResult(
            score, L2_MIN_SCORE, 0, 0, 0, confidence, False,
            [f"score {score:.0f} < {L2_MIN_SCORE}: L2 not invoked; L1 stands on its own"],
        )

    callers = callers or _MODEL_CALLERS
    prompt = (
        f"You are a senior trading-risk reviewer. Proposal: change "
        f"{proposal.get('variable')} from {proposal.get('old')} to "
        f"{proposal.get('new')} for {proposal.get('pair')}. Reason: "
        f"{proposal.get('reason')}. Context: {context}. Reply only YES or NO: "
        f"should this parameter change be applied?"
    )

    votes_yes = 0
    reached = 0
    call_errors: list[str] = []
    for name in models:
        if name not in callers:
            continue
        reached += 1
        try:
            reply = callers[name](prompt)
        except Exception as exc:  # noqa: BLE001 — fail-closed: a model error = NO
            call_errors.append(f"{name}:{type(exc).__name__}")
            continue
        if _parse_vote(reply):
            votes_yes += 1

    reasons = [f"score {score:.0f} -> {label}; votes {votes_yes}/{reached} "
               f"(required {required})"]
    if call_errors:
        reasons.append("model errors: " + ", ".join(call_errors))

    passed = votes_yes >= required
    conf_ok = confidence >= APPLY_CONFIDENCE
    decision = passed and conf_ok
    if not conf_ok:
        reasons.append(f"confidence {confidence:.2f} < {APPLY_CONFIDENCE}: apply blocked")
    if passed and conf_ok:
        reasons.append("CONSENSUS APPLY")
    else:
        reasons.append("CONSENSUS REJECT")
    return ConsensusResult(
        score, L2_MIN_SCORE if score < L2_UNANIMOUS_SCORE else L2_UNANIMOUS_SCORE,
        votes_yes, reached, required, confidence, decision, reasons,
    )
