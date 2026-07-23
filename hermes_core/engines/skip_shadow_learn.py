"""HIF Phase-4 — skip + GP-shadow fuel for reflection (Layer D lite).

When ``SKIP_SHADOW_REFLECT=1``, dry pairs still produce **shadow hypotheses**
from recent ``skips.jsonl`` + ``gp_shadow.jsonl``. These never auto-deploy
strategy YAML — they only append to ``hypotheses.jsonl`` so the dashboard /
operator can see learning pressure while books are quiet.

Also builds a compact skip/shadow summary string that trade-based reflection
can attach as context (``skipped_json``).
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

from hermes_core.env import get_env
from hermes_core.state.paths import bot_state_dir, current_bot

# Cadence / gates
SKIP_WINDOW = 200           # recent skips scanned per pair
SHADOW_WINDOW = 80          # recent gp_shadow rows scanned
SKIP_FIRE_EVERY = 50        # fire when pair skip count crosses multiples of this
SKIP_MIN_FOR_RULE = 20      # need at least this many in-window skips to propose
DOMINANCE = 0.55            # top reason share to treat as dominant

LATCH_NAME = ".skip_shadow_latches.json"


def skip_shadow_reflect_enabled() -> bool:
    return get_env("SKIP_SHADOW_REFLECT", "0") == "1"


def skip_shadow_promote_enabled() -> bool:
    return get_env("SKIP_SHADOW_PROMOTE", "0") == "1"


PROMOTE_LATCH_NAME = ".skip_shadow_promote_latches.json"
def _read_jsonl(path: Path, limit: int) -> list[dict]:
    if not path.exists() or limit <= 0:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_pair_skips(bot: str, pair: str, *, limit: int = SKIP_WINDOW) -> list[dict]:
    rows = _read_jsonl(bot_state_dir(bot) / "skips.jsonl", limit * 3)
    return [r for r in rows if r.get("pair") == pair][-limit:]


def load_pair_shadow(bot: str, pair: str, *, limit: int = SHADOW_WINDOW) -> list[dict]:
    rows = _read_jsonl(bot_state_dir(bot) / "gp_shadow.jsonl", limit * 3)
    return [r for r in rows if r.get("pair") == pair][-limit:]


def analyze_skip_shadow(
    skips: list[dict],
    shadows: list[dict],
) -> dict:
    """Pure summary of skip reasons + GP shadow lean."""
    reasons = Counter()
    for s in skips:
        reasons[str(s.get("reason_skipped") or s.get("reason") or "unknown")] += 1
    total = sum(reasons.values())
    top = reasons.most_common(5)
    top_share = (top[0][1] / total) if total and top else 0.0

    cons = Counter()
    strengths: list[float] = []
    signals = 0
    for sh in shadows:
        c = sh.get("consensus")
        if c:
            cons[str(c)] += 1
        if sh.get("signal"):
            signals += 1
        try:
            if sh.get("gp_strength") is not None:
                strengths.append(float(sh["gp_strength"]))
        except (TypeError, ValueError):
            pass

    return {
        "skip_count": total,
        "top_reasons": [{"reason": r, "n": n} for r, n in top],
        "top_reason": top[0][0] if top else None,
        "top_share": round(top_share, 4),
        "shadow_count": len(shadows),
        "shadow_signals": signals,
        "shadow_consensus": dict(cons),
        "avg_gp_strength": round(sum(strengths) / len(strengths), 4) if strengths else None,
    }


def format_skip_shadow_context(analysis: dict) -> str:
    """Compact string for combined_reflect skipped_json / dashboard."""
    if not analysis or not analysis.get("skip_count"):
        return ""
    parts = [f"skips={analysis['skip_count']}"]
    if analysis.get("top_reason"):
        parts.append(
            f"top={analysis['top_reason']}({analysis.get('top_share', 0):.0%})"
        )
    if analysis.get("shadow_count"):
        parts.append(f"shadow_n={analysis['shadow_count']}")
        if analysis.get("shadow_consensus"):
            lean = max(analysis["shadow_consensus"], key=analysis["shadow_consensus"].get)
            parts.append(f"shadow_lean={lean}")
        if analysis.get("avg_gp_strength") is not None:
            parts.append(f"gp_str={analysis['avg_gp_strength']}")
    return "; ".join(parts)


def propose_skip_shadow_notes(
    pair: str,
    bot: str,
    analysis: dict,
    strategy: dict | None = None,
) -> list[dict]:
    """Build shadow-only hypothesis records (never applied automatically)."""
    notes: list[dict] = []
    n = int(analysis.get("skip_count") or 0)
    if n < SKIP_MIN_FOR_RULE:
        return notes

    top = analysis.get("top_reason")
    share = float(analysis.get("top_share") or 0.0)
    ctx = format_skip_shadow_context(analysis)
    base = {
        "pair": pair,
        "bot": bot,
        "ts": time.time(),
        "status": "skip_shadow_note",
        "source": "skip_shadow_learn",
        "stats": analysis,
        "confidence": min(0.55, 0.25 + share * 0.3),
        "skip_context": ctx,
        "deployable": False,
    }

    if top and share >= DOMINANCE:
        notes.append({
            **base,
            "variable": "observation",
            "old": None,
            "new": None,
            "reason": (
                f"HIF Phase-4: dominant skip '{top}' "
                f"({share:.0%} of {n} recent). Context: {ctx}"
            ),
        })

    # Actionable shadow proposal: RR guard dominance with TP < SL.
    if top == "rr_guard" and share >= DOMINANCE and strategy:
        try:
            sl = float(strategy.get("stop_loss_pct") or 0)
            tp = float(strategy.get("profit_target_pct") or 0)
        except (TypeError, ValueError):
            sl, tp = 0.0, 0.0
        if sl > 0 and tp > 0 and tp < sl:
            notes.append({
                **base,
                "status": "skip_shadow_proposed",
                "variable": "profit_target_pct",
                "old": tp,
                "new": round(sl, 4),  # restore RR >= 1.0
                "reason": (
                    f"HIF Phase-4: rr_guard dominates skips ({share:.0%}); "
                    f"TP {tp} < SL {sl} — propose TP=SL for RR=1.0 (shadow only)"
                ),
                "confidence": 0.5,
                "deployable": False,
            })

    # Shadow lean note when GP often votes but live stays quiet.
    cons = analysis.get("shadow_consensus") or {}
    if (
        analysis.get("shadow_signals", 0) >= 5
        and top == "no_signal"
        and share >= 0.5
        and cons
    ):
        lean = max(cons, key=cons.get)
        notes.append({
            **base,
            "variable": "observation",
            "old": None,
            "new": None,
            "reason": (
                f"HIF Phase-4: live no_signal dominant while GP shadow leans "
                f"'{lean}' ({analysis.get('shadow_signals')} shadow signals). {ctx}"
            ),
        })

    return notes


def _latch_path(bot: str) -> Path:
    return bot_state_dir(bot) / LATCH_NAME


def _load_latches(bot: str) -> dict:
    path = _latch_path(bot)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_latches(bot: str, data: dict) -> None:
    path = _latch_path(bot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def _log_hypotheses(recs: list[dict]) -> None:
    if not recs:
        return
    from hermes_core.engines.reflect import _log_hypothesis
    for rec in recs:
        try:
            _log_hypothesis(rec)
        except Exception:  # noqa: BLE001
            pass


def maybe_skip_shadow_learn(
    bot: str | None = None,
    pairs: list[str] | None = None,
    *,
    strategies: dict | None = None,
) -> dict:
    """Scan pairs; when skip cadence hits, log shadow hypotheses. Fail-soft.

    Returns a dashboard-friendly summary dict.
    """
    bot = bot or current_bot()
    summary: dict = {
        "enabled": skip_shadow_reflect_enabled(),
        "pairs": {},
        "fired": [],
    }
    if not skip_shadow_reflect_enabled():
        return summary
    if not pairs:
        return summary

    latches = _load_latches(bot)
    strategies = strategies or {}

    for pair in pairs:
        try:
            skips = load_pair_skips(bot, pair)
            shadows = load_pair_shadow(bot, pair)
            analysis = analyze_skip_shadow(skips, shadows)
            summary["pairs"][pair] = {
                **analysis,
                "context": format_skip_shadow_context(analysis),
            }
            total_skips = analysis["skip_count"]
            # Use absolute skip file count for this pair via latch key on window size
            # Fire when in-window count crosses EVERY and latch advances.
            bucket = (total_skips // SKIP_FIRE_EVERY) * SKIP_FIRE_EVERY
            if bucket < SKIP_FIRE_EVERY or total_skips < SKIP_MIN_FOR_RULE:
                continue
            prev = int(latches.get(pair, 0) or 0)
            if bucket <= prev:
                continue
            notes = propose_skip_shadow_notes(
                pair, bot, analysis, strategy=strategies.get(pair),
            )
            if notes:
                _log_hypotheses(notes)
                summary["fired"].append({
                    "pair": pair,
                    "bucket": bucket,
                    "n_notes": len(notes),
                    "top_reason": analysis.get("top_reason"),
                })
            latches[pair] = bucket
        except Exception:  # noqa: BLE001 — never break the cycle
            continue

    _save_latches(bot, latches)
    return summary


def _promote_latch_path(bot: str) -> Path:
    return bot_state_dir(bot) / PROMOTE_LATCH_NAME


def _load_promote_latches(bot: str) -> dict:
    path = _promote_latch_path(bot)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_promote_latches(bot: str, data: dict) -> None:
    path = _promote_latch_path(bot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def _proposal_key(rec: dict) -> str:
    return (
        f"{rec.get('pair')}|{rec.get('variable')}|{rec.get('old')}|"
        f"{rec.get('new')}|{rec.get('ts')}"
    )


def _load_recent_proposed(bot: str, *, limit: int = 80) -> list[dict]:
    path = bot_state_dir(bot) / "hypotheses.jsonl"
    rows = _read_jsonl(path, limit)
    out = []
    for r in rows:
        if r.get("status") != "skip_shadow_proposed":
            continue
        if r.get("variable") in (None, "observation"):
            continue
        if r.get("old") is None or r.get("new") is None:
            continue
        out.append(r)
    return out


def promote_skip_shadow_proposal(
    rec: dict,
    *,
    bot: str | None = None,
    strategy: dict | None = None,
    prices: list[float] | None = None,
    auto_deploy: bool | None = None,
    backtest_fn=None,
) -> dict:
    """Backtest one skip_shadow_proposed record; optionally deploy YAML.

    Never blind: always runs backtest first. ``auto_deploy`` defaults to
    REFLECT_AUTO_DEPLOY (same lever as trade reflection).
    """
    from hermes_core.engines.backtest import backtest_with_history
    from hermes_core.engines.reflect import apply_strategy_change, _log_hypothesis
    from hermes_core.config import load_strategy_for_pair

    bot = bot or rec.get("bot") or current_bot()
    pair = rec.get("pair")
    variable = rec.get("variable")
    old_val = rec.get("old")
    new_val = rec.get("new")
    if not pair or not variable or old_val is None or new_val is None:
        return {"status": "skip", "reason": "incomplete_proposal", "deployed": False}

    if auto_deploy is None:
        auto_deploy = get_env("REFLECT_AUTO_DEPLOY", "1") != "0"

    if strategy is None:
        try:
            strategy = load_strategy_for_pair(pair, bot)
        except Exception:  # noqa: BLE001
            strategy = {}

    bt = backtest_fn or backtest_with_history
    try:
        verdict = bt(
            pair, variable, old_val, new_val,
            strategy=strategy, prices=prices, bot=bot,
        )
    except Exception as exc:  # noqa: BLE001 — fail closed (no deploy)
        _log_hypothesis({
            "pair": pair, "bot": bot, "variable": variable,
            "old": old_val, "new": new_val,
            "status": "backtest_rejected",
            "source": "skip_shadow_promote",
            "backtest": {"approved": False, "reason": f"error:{exc}"},
            "ts": time.time(),
            "deployable": False,
        })
        return {"status": "backtest_reject", "deployed": False, "error": str(exc)}

    approved = bool(verdict.get("approved"))
    _log_hypothesis({
        "pair": pair, "bot": bot, "variable": variable,
        "old": old_val, "new": new_val,
        "status": "backtest_approved" if approved else "backtest_rejected",
        "source": "skip_shadow_promote",
        "backtest": {
            "approved": approved,
            "reason": verdict.get("reason"),
            "version_bumped": (verdict.get("phases") or {}).get(
                "phase6_deploy", {},
            ).get("version_bumped"),
        },
        "ts": time.time(),
        "deployable": False,
    })
    if not approved:
        return {
            "status": "backtest_reject",
            "pair": pair,
            "deployed": False,
            "verdict": verdict,
        }

    bumped = (verdict.get("phases") or {}).get("phase6_deploy", {}).get(
        "version_bumped",
    )
    if not auto_deploy:
        _log_hypothesis({
            "pair": pair, "bot": bot, "variable": variable,
            "old": old_val, "new": new_val,
            "status": "approved_pending_deploy",
            "source": "skip_shadow_promote",
            "version": bumped,
            "ts": time.time(),
            "deployable": True,
        })
        return {
            "status": "approved_pending_deploy",
            "pair": pair,
            "deployed": False,
            "verdict": verdict,
            "version": bumped,
        }

    written = apply_strategy_change(
        pair, variable, new_val,
        bot=bot, version=bumped, strategy=strategy,
    )
    _log_hypothesis({
        "pair": pair, "bot": bot, "variable": variable,
        "old": old_val, "new": new_val,
        "status": "deployed",
        "source": "skip_shadow_promote",
        "version": written.get("version"),
        "ts": time.time(),
        "deployable": True,
    })
    return {
        "status": "deployed",
        "pair": pair,
        "deployed": True,
        "verdict": verdict,
        "version": written.get("version"),
    }


def maybe_promote_skip_shadow(
    bot: str | None = None,
    *,
    strategies: dict | None = None,
    max_per_cycle: int = 2,
) -> dict:
    """Scan recent skip_shadow_proposed; gate each once via backtest.

    Flag off → no-op. Observational notes ignored. Fail-soft.
    """
    bot = bot or current_bot()
    summary: dict = {
        "enabled": skip_shadow_promote_enabled(),
        "attempted": [],
        "results": [],
    }
    if not skip_shadow_promote_enabled():
        return summary

    latches = _load_promote_latches(bot)
    strategies = strategies or {}
    tried = 0
    for rec in reversed(_load_recent_proposed(bot)):
        if tried >= max_per_cycle:
            break
        key = _proposal_key(rec)
        if latches.get(key):
            continue
        pair = rec.get("pair")
        try:
            result = promote_skip_shadow_proposal(
                rec,
                bot=bot,
                strategy=strategies.get(pair) if pair else None,
            )
            summary["attempted"].append({"pair": pair, "variable": rec.get("variable")})
            summary["results"].append(result)
            latches[key] = {
                "status": result.get("status"),
                "ts": time.time(),
            }
            tried += 1
        except Exception:  # noqa: BLE001
            latches[key] = {"status": "error", "ts": time.time()}
            continue

    _save_promote_latches(bot, latches)
    return summary
