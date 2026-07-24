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
  * layer1_rule_based NEVER mutates the live strategy; combined_reflect only
    LOGS a proposal to state/hypotheses.jsonl.
  * Live deploy is gated by run_reflection_pipeline: L2 (when score>=65) then
    backtest_with_history; on approve, apply_strategy_change writes YAML +
    version. Set REFLECT_AUTO_DEPLOY=0 to stop at approved_pending_deploy.
  * Every proposal is reconstructable: hypotheses.jsonl records
    pair, variable, old -> new, reason, confidence, and the trade stats that
    produced it.

Functions (blueprint Phase 9 build target + live latch):
  layer1_rule_based(pair, trades, goal, strategy) -> tuple | None
  combined_reflect(pair, trades, goal, chart_context="", ...) -> list[dict]
  maybe_reflect_pair / run_reflection_pipeline / apply_strategy_change
  _is_reflection_done / _mark_reflection_done
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_core.config import load_config, load_strategy_for_pair
from hermes_core.state.paths import hypotheses_path, reflection_latch_path

STOP_FLOOR = 0.5  # [GUARD L45] stop_loss_pct never goes below this
STOP_TIGHTEN = 0.3  # DD breach -> tighten by this much
CONFIDENCE = 0.40  # L1 fixed confidence gate


# ── pure helpers (unit-tested, no I/O) ─────────────────────────────────────
def aggregate_trades(trades: list[dict]) -> dict:
    """Compute win_rate, pnl stats, drawdown proxy from a batch of closed trades.

    A trade record carries at least: pnl_pct, exit_price, entry_price.
    'drawdown' here = worst single-trade loss (blueprint uses max_dd vs the
    goal's max_drawdown, expressed in %). Both are in percent units.
    """
    if not trades:
        return {"count": 0, "win_rate": 0.0, "ret": 0.0, "worst_loss": 0.0, "drawdown": 0.0}
    pnls = [float(t.get("pnl_pct", 0.0)) for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    worst = min(pnls) if pnls else 0.0
    return {
        "count": len(pnls),
        "win_rate": wins / len(pnls),
        "ret": sum(pnls),
        "worst_loss": worst,  # <= 0
        "drawdown": -worst,  # >= 0, percent
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
    path = hypotheses_path(rec.get("bot"))
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "a", encoding="utf-8") as fh:
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
    if skipped_json:
        reason = f"{reason} | skip_ctx: {skipped_json[:400]}"
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
    if skipped_json:
        rec["skip_context"] = skipped_json[:2000]
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
L2_MIN_SCORE = 65  # [GUARD L53] below this, L2 is never invoked
L2_UNANIMOUS_SCORE = 75  # at/above this, 3/3 unanimous required
APPLY_CONFIDENCE = 0.40  # [GUARD L53] min confidence to apply any change

# Ordered cascade: DeepSeek -> Gemini -> Groq. Each is a backup if the prior
# call fails (network/quota/empty). Tests inject fakes; prod lazy-imports.
DEFAULT_MODELS = ("deepseek", "gemini", "groq")


def call_deepseek(prompt: str, api_key: str | None = None) -> str:  # pragma: no cover
    """DeepSeek chat completion. Lazy import; network. Monkeypatch in tests."""
    from openai import OpenAI

    client = OpenAI(
        api_key=api_key or _env("DEEPSEEK_API_KEY"), base_url="https://api.deepseek.com"
    )
    resp = client.chat.completions.create(
        model="deepseek-chat", messages=[{"role": "user", "content": prompt}]
    )
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

    client = OpenAI(
        api_key=api_key or _env("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1"
    )
    resp = client.chat.completions.create(
        model="llama-3.1-8b-instant", messages=[{"role": "user", "content": prompt}]
    )
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

    __slots__ = (
        "score",
        "threshold",
        "votes_yes",
        "votes_total",
        "required",
        "confidence",
        "decision",
        "reasons",
    )

    def __init__(
        self,
        score: float,
        threshold: float,
        votes_yes: int,
        votes_total: int,
        required: int,
        confidence: float,
        decision: bool,
        reasons: list[str],
    ):
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
            "score": self.score,
            "threshold": self.threshold,
            "votes_yes": self.votes_yes,
            "votes_total": self.votes_total,
            "required": self.required,
            "confidence": self.confidence,
            "decision": self.decision,
            "reasons": self.reasons,
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
    confidence = float(confidence if confidence is not None else proposal.get("confidence", 0.0))
    required, label = _required_votes(score)

    if required == 0:
        return ConsensusResult(
            score,
            L2_MIN_SCORE,
            0,
            0,
            0,
            confidence,
            False,
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

    reasons = [f"score {score:.0f} -> {label}; votes {votes_yes}/{reached} (required {required})"]
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
        score,
        L2_MIN_SCORE if score < L2_UNANIMOUS_SCORE else L2_UNANIMOUS_SCORE,
        votes_yes,
        reached,
        required,
        confidence,
        decision,
        reasons,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Live latch + reflect → L2 → backtest → deploy (wired from the trade loop)
# ═══════════════════════════════════════════════════════════════════════════


def _load_reflection_latches(bot: str = "forex") -> dict:
    path = reflection_latch_path(bot)
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _save_reflection_latches(latches: dict, bot: str = "forex") -> None:
    path = reflection_latch_path(bot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(latches), encoding="utf-8")
    except OSError:
        pass


def _is_reflection_done(pair: str, closed_count: int, bot: str = "forex") -> bool:
    """True if we already reflected at this exact closed-trade count for `pair`."""
    entry = _load_reflection_latches(bot).get(pair)
    if entry is None:
        return False
    return entry.get("reflected_count") == closed_count


def _mark_reflection_done(pair: str, closed_count: int, bot: str = "forex") -> None:
    latches = _load_reflection_latches(bot)
    latches[pair] = {"reflected_count": closed_count}
    _save_reflection_latches(latches, bot)


def strategy_yaml_path(pair: str, bot: str = "forex") -> Path:
    """Canonical per-pair strategy file on the runtime volume."""
    from hermes_core.config.loader import strategy_yaml_path as _live

    return _live(pair, bot)


def _set_strategy_param(strategy: dict, variable: str, value) -> None:
    """Set a top-level or dotted param (e.g. entry.threshold) on a strategy dict."""
    if "." in variable:
        parts = variable.split(".")
        cur = strategy
        for part in parts[:-1]:
            nxt = cur.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                cur[part] = nxt
            cur = nxt
        cur[parts[-1]] = value
    else:
        strategy[variable] = value


def apply_strategy_change(
    pair: str,
    variable: str,
    new_val,
    *,
    bot: str = "forex",
    version: str | None = None,
    strategy: dict | None = None,
) -> dict:
    """Atomically write the approved param (+ version) to the pair strategy YAML.

    Returns the written strategy dict. Validates ranges before writing; raises
    on validation failure so callers can refuse a bad deploy.
    """
    import copy

    import yaml

    from hermes_core.config import validate_strategy_params

    strat = copy.deepcopy(strategy if strategy is not None else load_strategy_for_pair(pair, bot))
    _set_strategy_param(strat, variable, new_val)
    if version is not None:
        strat["version"] = str(version)
    elif "version" not in strat:
        strat["version"] = "01"
    validate_strategy_params(strat, raise_on_fail=True)

    path = strategy_yaml_path(pair, bot)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(strat, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    return strat


def run_reflection_pipeline(
    pair: str,
    trades: list[dict],
    *,
    bot: str = "forex",
    goal: dict | None = None,
    strategy: dict | None = None,
    chart_context: str = "",
    prices: list[float] | None = None,
    fetch_prices=None,
    llm_callers: dict | None = None,
    auto_deploy: bool = True,
    skipped_json: str = "",
) -> dict:
    """L1 → (optional L2) → backtest → deploy. Returns a status dict.

    Gate rules (roadmap S11):
      * score < 65  → L2 skipped; L1 proposal proceeds to backtest on its own.
      * score ≥ 65  → L2 consensus required before backtest.
      * backtest approve + auto_deploy → write strategy YAML + version bump.
    """
    from hermes_core.engines.backtest import backtest_with_history

    if goal is None:
        goal = (load_config(bot) or {}).get("goal", {})
    if strategy is None:
        strategy = load_strategy_for_pair(pair, bot)

    proposals = combined_reflect(
        pair,
        trades,
        goal=goal,
        chart_context=chart_context,
        skipped_json=skipped_json,
        strategy=strategy,
        bot=bot,
    )
    if not proposals:
        return {"status": "no_proposal", "pair": pair, "deployed": False}

    prop = proposals[0]
    score = float(prop.get("confidence", 0.0)) * 100.0
    # Allow an explicit numeric score on the proposal (tests / L2 escalation).
    if "score" in prop:
        score = float(prop["score"])

    if score >= L2_MIN_SCORE:
        cons = call_llm_consensus(
            prop,
            context=chart_context,
            score=score,
            confidence=float(prop.get("confidence", 0.0)),
            callers=llm_callers,
        )
        _log_hypothesis(
            {
                **{k: prop.get(k) for k in ("pair", "bot", "variable", "old", "new")},
                "status": "l2_approved" if cons.decision else "l2_rejected",
                "l2": cons.to_dict(),
                "ts": __import__("time").time(),
            }
        )
        if not cons.decision:
            return {
                "status": "l2_reject",
                "pair": pair,
                "deployed": False,
                "proposal": prop,
                "l2": cons.to_dict(),
            }

    kwargs = {
        "strategy": strategy,
        "prices": prices,
        "bot": bot,
    }
    if fetch_prices is not None:
        kwargs["fetch_prices"] = fetch_prices
    verdict = backtest_with_history(
        pair,
        prop["variable"],
        prop["old"],
        prop["new"],
        **kwargs,
    )
    _log_hypothesis(
        {
            **{k: prop.get(k) for k in ("pair", "bot", "variable", "old", "new")},
            "status": "backtest_approved" if verdict.get("approved") else "backtest_rejected",
            "backtest": {
                "approved": verdict.get("approved"),
                "reason": verdict.get("reason"),
                "version_bumped": (verdict.get("phases") or {})
                .get("phase6_deploy", {})
                .get("version_bumped"),
            },
            "ts": __import__("time").time(),
        }
    )
    if not verdict.get("approved"):
        return {
            "status": "backtest_reject",
            "pair": pair,
            "deployed": False,
            "proposal": prop,
            "verdict": verdict,
        }

    bumped = (verdict.get("phases") or {}).get("phase6_deploy", {}).get("version_bumped")
    if not auto_deploy:
        return {
            "status": "approved_pending_deploy",
            "pair": pair,
            "deployed": False,
            "proposal": prop,
            "verdict": verdict,
            "version": bumped,
        }

    written = apply_strategy_change(
        pair,
        prop["variable"],
        prop["new"],
        bot=bot,
        version=bumped,
        strategy=strategy,
    )
    _log_hypothesis(
        {
            **{k: prop.get(k) for k in ("pair", "bot", "variable", "old", "new")},
            "status": "deployed",
            "version": written.get("version"),
            "ts": __import__("time").time(),
        }
    )
    return {
        "status": "deployed",
        "pair": pair,
        "deployed": True,
        "proposal": prop,
        "verdict": verdict,
        "version": written.get("version"),
        "strategy": written,
    }


def maybe_reflect_pair(
    bot: str,
    pair: str,
    *,
    goal: dict | None = None,
    chart_context: str = "",
    prices: list[float] | None = None,
    fetch_prices=None,
    llm_callers: dict | None = None,
    auto_deploy: bool = True,
) -> dict | None:
    """Fire reflection when closed-count hits reflection_every and latch is clear.

    Returns the pipeline result dict, or None if cadence/latch skipped the run.
    Always fail-soft at the caller — this function may raise only on logic bugs;
    I/O errors inside the pipeline are converted to status dicts where possible.
    """
    from hermes_core.state.paths import bot_state_dir

    if goal is None:
        goal = (load_config(bot) or {}).get("goal", {})
    every = int(goal.get("reflection_every", 5) or 5)
    if every < 1:
        every = 5

    trades_path = bot_state_dir(bot) / "trades.jsonl"
    closed: list[dict] = []
    if trades_path.exists():
        try:
            for line in trades_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get("pair") != pair:
                    continue
                # Real closes carry exit_reason (or legacy reason) + pnl.
                if rec.get("exit_reason") or rec.get("reason") or "pnl_pct" in rec:
                    closed.append(rec)
        except (OSError, json.JSONDecodeError):
            closed = []

    total = len(closed)
    if total <= 0 or total % every != 0:
        return None
    if _is_reflection_done(pair, total, bot):
        return {"status": "latched", "pair": pair, "closed": total, "deployed": False}

    batch = closed[-every:]
    skipped_json = ""
    try:
        from hermes_core.engines.skip_shadow_learn import (
            analyze_skip_shadow,
            format_skip_shadow_context,
            load_pair_shadow,
            load_pair_skips,
            skip_shadow_reflect_enabled,
        )

        if skip_shadow_reflect_enabled():
            analysis = analyze_skip_shadow(
                load_pair_skips(bot, pair),
                load_pair_shadow(bot, pair),
            )
            skipped_json = format_skip_shadow_context(analysis)
    except Exception:  # noqa: BLE001
        skipped_json = ""

    try:
        result = run_reflection_pipeline(
            pair,
            batch,
            bot=bot,
            goal=goal,
            chart_context=chart_context,
            prices=prices,
            fetch_prices=fetch_prices,
            llm_callers=llm_callers,
            auto_deploy=auto_deploy,
            skipped_json=skipped_json,
        )
    except Exception as exc:  # noqa: BLE001 — never break the trade loop
        result = {
            "status": "error",
            "pair": pair,
            "deployed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    _mark_reflection_done(pair, total, bot)
    result["closed"] = total
    result["reflection_every"] = every
    if skipped_json:
        result["skip_context"] = skipped_json
    return result
