"""Phase 3 — Live experiment control for the reflection engine.

Every live deploy is treated as an EXPERIMENT (champion vN-1 → challenger vN).
After the challenger has traded for ``EXPERIMENT_EVAL_CLOSES`` real closes we
compare its live PnL against the champion's. If the challenger is not strictly
better we AUTO-REVERT: the prior strategy YAML is restored atomically, the exact
(pair, variable, old, new) change is quarantined in the hypothesis KB so it is
never re-proposed, the axis is put on a cooldown (so reflection is forced to try
a different lever), and the champion is flagged ``underperforming`` (Phase 4
hand-off signal to GP). When every axis is exhausted the pair enters SAFE MODE
(size-down, then pause).

Design constraints:
  * Deterministic + testable — cadence is measured in CLOSED TRADES (like the
    reflection latch), never wall-clock, so tests don't have to sleep.
  * Fail-soft — this module must NEVER raise into the trade loop. Every public
    entry point swallows I/O errors and degrades to a no-op.
  * No cross-contamination — reverts touch only the per-pair strategy YAML and
    the param-KB; they never touch indicator exile (that stays GP's domain,
    Phase 4.4).
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from hermes_core.state.atomic_json import atomic_write_json, load_json
from hermes_core.state.paths import bot_state_dir

# ── tunables ────────────────────────────────────────────────────────────────
# Closes on the challenger version before its verdict is judged.
EXPERIMENT_EVAL_CLOSES = int(os.environ.get("REFLECT_EXPERIMENT_CLOSES", "10") or 10)
# Challenger must beat the champion's avg pnl_pct by more than this (percent).
LIVE_IMPROVE_MARGIN = float(os.environ.get("REFLECT_LIVE_IMPROVE_MARGIN", "0.0") or 0.0)
# After a revert, the failed axis is blocked for this many additional closes.
AXIS_COOLDOWN_CLOSES = int(os.environ.get("REFLECT_AXIS_COOLDOWN_CLOSES", "30") or 30)
# Champion sample size used as the baseline comparison window.
CHAMPION_WINDOW = int(os.environ.get("REFLECT_CHAMPION_WINDOW", "20") or 20)

_EXPERIMENTS = "experiments.json"
_CHAMPIONS = "champions.json"
_AXIS_COOLDOWN = "axis_cooldown.json"
_SAFE_MODE = "safe_mode.json"
_GP_HANDOFF = "gp_handoff.json"
_RETUNE = "reflection_retune.json"
_DEPLOY_COOLDOWN = "deploy_cooldown.json"
_DEPLOY_STAGE = "deploy_stage.json"
_DIRECTION_COOLDOWN = "direction_cooldown.json"
_PIPELINE = "pipeline_outcomes.json"
_PLANS = "reflection_plans.json"
_L2_TRUST = "l2_trust.json"
_SHADOW = "shadow_challengers.json"
_EXPLORE = "explore_mode.json"

# Soft direction quarantine length (closes). Near-duplicate proposals in the
# same direction are blocked until this many additional closes land.
DIRECTION_COOLDOWN_CLOSES = int(
    os.environ.get("REFLECT_DIRECTION_COOLDOWN_CLOSES", str(AXIS_COOLDOWN_CLOSES))
    or AXIS_COOLDOWN_CLOSES
)
# Near-duplicate: new value within this fraction of the banned step size.
SOFT_NEAR_FRAC = float(os.environ.get("REFLECT_SOFT_NEAR_FRAC", "0.5") or 0.5)

# Phase 5.2 — max 1 deploy per pair per day + quiet period after a deploy.
DEPLOY_COOLDOWN_S = int(os.environ.get("REFLECT_DEPLOY_COOLDOWN_S", "86400") or 86400)
DEPLOY_QUIET_CLOSES = int(
    os.environ.get("REFLECT_DEPLOY_QUIET_CLOSES", str(EXPERIMENT_EVAL_CLOSES)) or EXPERIMENT_EVAL_CLOSES
)

# Phase 5.3 — staged unlock of live auto-deploy.
# prove → canary → full. REFLECT_AUTO_DEPLOY only actually writes YAML at
# canary/full; prove always stops at approved_pending_deploy.
VALID_DEPLOY_STAGES = ("prove", "canary", "full")
DEFAULT_DEPLOY_STAGE = os.environ.get("REFLECT_DEPLOY_STAGE", "prove") or "prove"


# ── generic state helpers ─────────────────────────────────────────────────---
def _path(bot: str | None, name: str) -> Path:
    return bot_state_dir(bot) / name


def _load(bot: str | None, name: str) -> dict:
    raw = load_json(_path(bot, name), default={})
    return raw if isinstance(raw, dict) else {}


def _save(bot: str | None, name: str, data: dict) -> None:
    with contextlib.suppress(Exception):
        atomic_write_json(_path(bot, name), data, indent=2)


# ── trade reads ─────────────────────────────────────────────────────────────
def _pair_closes(bot: str, pair: str) -> list[dict]:
    """Closed rows for ``pair`` in trade order (mirrors reflect._closed_trades_for_pair)."""
    with contextlib.suppress(Exception):
        from hermes_core.engines.trades_cache import closed_trades

        return closed_trades(bot, pair)
    # Fallback: direct scan (tests / cache import failure).
    path = bot_state_dir(bot) / "trades.jsonl"
    out: list[dict] = []
    if not path.exists():
        return out
    with contextlib.suppress(OSError):
        import json

        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("pair") != pair or rec.get("orphan"):
                continue
            if rec.get("exit_reason") or rec.get("reason") or "pnl_pct" in rec:
                out.append(rec)
    return out


def _avg_pnl(rows: list[dict]) -> float | None:
    vals = [float(r.get("pnl_pct", 0.0)) for r in rows if r.get("pnl_pct") is not None]
    return (sum(vals) / len(vals)) if vals else None


# ── deployment bookkeeping ────────────────────────────────────────────────---
def record_deployment(
    bot: str,
    pair: str,
    *,
    variable: str,
    old,
    new,
    version_from: str | None,
    version_to: str | None,
    prior_strategy: dict,
    closed_count: int,
    hypothesis_ts: float | None = None,
) -> None:
    """Open a live experiment and snapshot the champion (vN-1) for revert.

    ``prior_strategy`` is the FULL strategy dict that was live before the deploy
    — restoring it is how auto-revert undoes a bad change atomically. Fail-soft.
    """
    now = time.time()
    exps = _load(bot, _EXPERIMENTS)
    exps[pair] = {
        "status": "live",
        "variable": variable,
        "old": old,
        "new": new,
        "version_from": version_from,
        "version_to": version_to,
        "deployed_ts": now,
        "deployed_closed": int(closed_count),
        "hypothesis_ts": hypothesis_ts or now,
    }
    _save(bot, _EXPERIMENTS, exps)

    champs = _load(bot, _CHAMPIONS)
    # Preserve the ORIGINAL champion snapshot across a chain of deploys so a
    # revert always lands on a version that was actually validated, not on an
    # intermediate challenger.
    existing = champs.get(pair) or {}
    if existing.get("status") != "champion" or not existing.get("strategy"):
        champs[pair] = {
            "version": version_from,
            "strategy": prior_strategy,
            "status": "champion",
            "updated_ts": now,
        }
        _save(bot, _CHAMPIONS, champs)

    # Phase 5.2: start the per-pair deploy cooldown / quiet period clock.
    record_deploy_cooldown(bot, pair, closed_count=closed_count)


# ── experiment evaluation ─────────────────────────────────────────────────---
def evaluate_experiment(
    bot: str, pair: str, *, k: int = EXPERIMENT_EVAL_CLOSES, closes: list[dict] | None = None
) -> dict:
    """Judge the active experiment: pending / improved / worsened / none.

    Richer scorecard (mean + noise-adjusted edge + win-rate + drawdown) with
    sequential early-abort of clearly doomed challengers. Promotion still waits
    for the full adaptive window — only losers abort early.
    """
    exp = _load(bot, _EXPERIMENTS).get(pair)
    if not exp or exp.get("status") != "live":
        return {"status": "none", "pair": pair}

    v_to = str(exp.get("version_to"))
    v_from = str(exp.get("version_from"))
    if closes is None:
        closes = _pair_closes(bot, pair)

    # Phase 6: how much evidence a verdict needs is adaptive — a noisy pair must
    # show more closes before we trust "better"/"worse"; a quiet one less.
    need = max(1, k)
    with contextlib.suppress(Exception):
        from hermes_core.engines.adaptive import adaptive_eval_closes

        need = max(
            1,
            adaptive_eval_closes(
                k, [float(r.get("pnl_pct", 0.0)) for r in closes if r.get("pnl_pct") is not None]
            ),
        )

    challenger = [r for r in closes if str(r.get("strategy_version")) == v_to]
    champion = [r for r in closes if str(r.get("strategy_version")) == v_from]
    champ_window = CHAMPION_WINDOW
    with contextlib.suppress(Exception):
        from hermes_core.engines.adaptive import adaptive_champion_window

        champ_window = adaptive_champion_window(bot, pair, CHAMPION_WINDOW)
    champion = champion[-champ_window:] if champion else []

    from hermes_core.engines.live_verdict import judge_live

    verdict = judge_live(
        challenger,
        champion,
        need=need,
        margin=LIVE_IMPROVE_MARGIN,
    )
    return {
        **verdict,
        "pair": pair,
        "experiment": exp,
        "champion_window": champ_window,
    }


def _restore_champion(bot: str, pair: str, champ: dict) -> dict | None:
    """Atomically rewrite the pair YAML with the champion strategy. Returns it."""
    strategy = champ.get("strategy")
    if not isinstance(strategy, dict):
        return None
    with contextlib.suppress(Exception):
        from hermes_core.engines.reflect import apply_strategy_change

        # Rewrite via apply_strategy_change with a no-op field so validation +
        # atomic write are reused; the champion's own version is preserved.
        return apply_strategy_change(
            pair,
            "version",
            str(strategy.get("version", champ.get("version") or "00")),
            bot=bot,
            version=str(strategy.get("version", champ.get("version") or "00")),
            strategy=strategy,
        )
    return None


def maybe_auto_revert(bot: str, pair: str, *, k: int = EXPERIMENT_EVAL_CLOSES) -> dict:
    """Evaluate + act on the active experiment (Phase 3.1–3.3).

    * improved  → promote challenger to champion (snapshot refreshed), close exp.
    * worsened  → restore champion YAML, quarantine the change, cooldown the
                  axis, flag champion ``underperforming``, close exp as reverted.
    * pending/none → no action.

    Fail-soft: any error degrades to ``{"status": "error"}`` without raising.
    """
    try:
        ev = evaluate_experiment(bot, pair, k=k)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "pair": pair, "error": f"{type(exc).__name__}: {exc}"}

    if ev["status"] in ("none", "pending"):
        return ev

    exp = ev["experiment"]
    closes = _pair_closes(bot, pair)
    closed_now = len(closes)

    if ev["status"] == "improved":
        # Challenger wins: it becomes the new champion baseline.
        champs = _load(bot, _CHAMPIONS)
        with contextlib.suppress(Exception):
            from hermes_core.config import load_strategy_for_pair

            champs[pair] = {
                "version": exp.get("version_to"),
                "strategy": load_strategy_for_pair(pair, bot),
                "status": "champion",
                "updated_ts": time.time(),
            }
            _save(bot, _CHAMPIONS, champs)
        clear_gp_handoff(bot, pair)
        clear_explore(bot, pair)
        advance_plan(bot, pair, consumed_variable=exp.get("variable"))
        # Phase 5.3: a live improvement is the unlock signal for the next stage.
        with contextlib.suppress(Exception):
            advance_deploy_stage(bot, reason="experiment_improved")
        _close_experiment(bot, pair, exp, outcome="improved", detail=ev)
        _log(bot, pair, exp, status="experiment_improved", detail=ev)
        record_l2_outcome(bot, pair, exp, outcome="improved")
        return {"status": "improved", "pair": pair, "detail": ev}

    # worsened → REVERT
    champ = _load(bot, _CHAMPIONS).get(pair) or {}
    restored = _restore_champion(bot, pair, champ)

    # Quarantine the exact change so it is never re-proposed (KB live_worse ban).
    with contextlib.suppress(Exception):
        from hermes_core.engines.backtest import _kb_record

        _kb_record(
            pair,
            exp.get("variable"),
            exp.get("old"),
            exp.get("new"),
            False,
            f"live_worse: challenger avg {ev.get('challenger_avg')} <= "
            f"baseline {ev.get('baseline')} over {ev.get('n_challenger')} closes",
            bot=bot,
        )

    # Force a different axis. Phase 6: the sentence length is adaptive —
    # exponential backoff on a repeatedly-failing axis, shorter for one with a
    # real track record.
    cooldown_len = AXIS_COOLDOWN_CLOSES
    with contextlib.suppress(Exception):
        from hermes_core.engines.adaptive import adaptive_cooldown

        cooldown_len = adaptive_cooldown(
            bot, pair, str(exp.get("variable")), AXIS_COOLDOWN_CLOSES
        )
    set_axis_cooldown(bot, pair, exp.get("variable"), closed_now + cooldown_len)

    # Soft direction quarantine (#6): don't re-propose the same lever in the
    # same direction (1.5→1.8 then 1.5→1.7) until the cooldown clears.
    set_direction_cooldown(
        bot,
        pair,
        exp.get("variable"),
        exp.get("old"),
        exp.get("new"),
        until_closed=closed_now + max(cooldown_len, DIRECTION_COOLDOWN_CLOSES),
        reason="live_worse",
    )

    # Flag the reverted-to champion as underperforming (Phase 3.3 / GP hand-off).
    champs = _load(bot, _CHAMPIONS)
    entry = champs.get(pair) or {}
    entry["status"] = "underperforming"
    entry["reverted_ts"] = time.time()
    entry["reverted_from_version"] = exp.get("version_to")
    entry["revert_count"] = int(entry.get("revert_count", 0)) + 1
    if restored is not None:
        entry.setdefault("version", restored.get("version"))
    champs[pair] = entry
    _save(bot, _CHAMPIONS, champs)

    # Phase 4.1: underperforming + quarantined axis → explicit GP handoff.
    request_gp_handoff(
        bot,
        pair,
        reason=(
            f"live_worse on {exp.get('variable')}; "
            f"challenger {ev.get('challenger_avg')} <= baseline {ev.get('baseline')}"
        ),
        variable=exp.get("variable"),
    )

    # #11: sit on a known-bad champion at explore size, not full risk.
    enter_explore(bot, pair, reason="champion_underperforming_after_revert")
    clear_plan_step(bot, pair, exp.get("variable"))
    record_l2_outcome(bot, pair, exp, outcome="reverted")

    _close_experiment(bot, pair, exp, outcome="reverted", detail=ev)
    _log(bot, pair, exp, status="reverted", detail=ev, restored=bool(restored))
    return {
        "status": "reverted",
        "pair": pair,
        "restored": bool(restored),
        "restored_version": (restored or {}).get("version"),
        "detail": ev,
    }


def _close_experiment(bot: str, pair: str, exp: dict, *, outcome: str, detail: dict) -> None:
    exps = _load(bot, _EXPERIMENTS)
    rec = dict(exp)
    rec["status"] = outcome
    rec["closed_ts"] = time.time()
    # Richer scorecard + regime stamp for regime-conditioned credit (#3).
    keep = (
        "challenger_avg",
        "champion_avg",
        "baseline",
        "diff",
        "edge",
        "challenger_wr",
        "wr_delta",
        "challenger_max_dd",
        "dd_ok",
        "early_abort",
        "regime",
    )
    rec["verdict"] = {k: detail.get(k) for k in keep if k in detail}
    if detail.get("regime") and not rec.get("regime"):
        rec["regime"] = detail.get("regime")
    hist = exps.get("_history")
    if not isinstance(hist, list):
        hist = []
    hist.append({"pair": pair, **rec})
    exps["_history"] = hist[-100:]
    exps.pop(pair, None)
    _save(bot, _EXPERIMENTS, exps)


def _log(bot: str, pair: str, exp: dict, *, status: str, detail: dict, restored: bool = False) -> None:
    with contextlib.suppress(Exception):
        from hermes_core.engines.reflect import _log_hypothesis

        _log_hypothesis(
            {
                "pair": pair,
                "bot": bot,
                "variable": exp.get("variable"),
                "old": exp.get("old"),
                "new": exp.get("new"),
                "reason": (
                    f"experiment {status}: challenger avg {detail.get('challenger_avg')} vs "
                    f"baseline {detail.get('baseline')} ({detail.get('n_challenger')} closes)"
                    + (" — reverted to champion" if restored else "")
                ),
                "status": status,
                "version": exp.get("version_to"),
                "ts": time.time(),
            }
        )


# ── axis cooldown (Phase 3.4) ─────────────────────────────────────────────---
def set_axis_cooldown(bot: str, pair: str, variable: str | None, until_closed: int) -> None:
    if not variable:
        return
    data = _load(bot, _AXIS_COOLDOWN)
    per_pair = data.get(pair)
    if not isinstance(per_pair, dict):
        per_pair = {}
    per_pair[variable] = int(until_closed)
    data[pair] = per_pair
    _save(bot, _AXIS_COOLDOWN, data)


def blocked_axes(bot: str, pair: str, closed_count: int) -> set[str]:
    """Variables still under cooldown at ``closed_count`` (Phase 3.4)."""
    per_pair = _load(bot, _AXIS_COOLDOWN).get(pair)
    if not isinstance(per_pair, dict):
        return set()
    return {var for var, until in per_pair.items() if int(closed_count) < int(until)}


def axis_in_cooldown(bot: str, pair: str, variable: str, closed_count: int) -> bool:
    return variable in blocked_axes(bot, pair, closed_count)


# ── soft direction quarantine (#6) ──────────────────────────────────────────
def set_direction_cooldown(
    bot: str,
    pair: str,
    variable: str | None,
    old,
    new,
    *,
    until_closed: int,
    reason: str = "",
) -> None:
    """Block re-proposals of the same (pair, variable, direction) until ``until_closed``."""
    if not variable:
        return
    from hermes_core.engines.live_verdict import change_direction

    direction = change_direction(old, new)
    if not direction:
        return
    data = _load(bot, _DIRECTION_COOLDOWN)
    per_pair = data.get(pair)
    if not isinstance(per_pair, dict):
        per_pair = {}
    per_pair[variable] = {
        "direction": direction,
        "old": old,
        "new": new,
        "until_closed": int(until_closed),
        "reason": reason,
        "ts": time.time(),
    }
    data[pair] = per_pair
    _save(bot, _DIRECTION_COOLDOWN, data)


def direction_blocked(
    bot: str, pair: str, variable: str, old, new, closed_count: int
) -> dict | None:
    """Return a block reason if this proposal repeats a cooling direction."""
    from hermes_core.engines.live_verdict import change_direction

    direction = change_direction(old, new)
    if not direction:
        return None
    rec = (_load(bot, _DIRECTION_COOLDOWN).get(pair) or {}).get(variable)
    if not isinstance(rec, dict):
        return None
    if int(closed_count) >= int(rec.get("until_closed") or 0):
        return None
    if rec.get("direction") != direction:
        return None  # opposite direction is allowed (e.g. widen after tighten failed)
    return {
        "reason": "direction_cooldown",
        "direction": direction,
        "until_closed": rec.get("until_closed"),
        "banned": {"old": rec.get("old"), "new": rec.get("new")},
    }


def soft_quarantined(
    bot: str, pair: str, variable: str, old, new, closed_count: int
) -> dict | None:
    """Direction cooldown OR near-duplicate of a recent KB / pipeline rejection."""
    hit = direction_blocked(bot, pair, variable, old, new, closed_count)
    if hit:
        return hit
    # Soft KB / pipeline near-dupe: same pair+variable+direction, new close to a
    # recently rejected destination.
    from hermes_core.engines.live_verdict import change_direction

    direction = change_direction(old, new)
    if not direction:
        return None
    try:
        cand_new = float(new)
        cand_old = float(old)
    except (TypeError, ValueError):
        return None

    # Pipeline outcomes first (cheap, bounded).
    for rec in reversed(pipeline_outcomes(bot, pair)[-40:]):
        if rec.get("variable") != variable:
            continue
        if change_direction(rec.get("old"), rec.get("new")) != direction:
            continue
        try:
            banned_new = float(rec.get("new"))
            banned_old = float(rec.get("old"))
        except (TypeError, ValueError):
            continue
        step = abs(banned_new - banned_old) or abs(cand_new - cand_old) or 1.0
        if abs(cand_new - banned_new) <= SOFT_NEAR_FRAC * step + 1e-12:
            return {
                "reason": "pipeline_near_duplicate",
                "status": rec.get("status"),
                "banned": {"old": rec.get("old"), "new": rec.get("new")},
            }

    # Hypothesis KB near-dupe (rejected only).
    with contextlib.suppress(Exception):
        from hermes_core.engines.backtest import _kb_path
        import json

        path = _kb_path(bot)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines()[-200:]:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("pair") != pair or rec.get("param") != variable:
                    continue
                if rec.get("approved", True):
                    continue
                if change_direction(rec.get("old"), rec.get("new")) != direction:
                    continue
                try:
                    banned_new = float(rec.get("new"))
                    banned_old = float(rec.get("old"))
                except (TypeError, ValueError):
                    continue
                step = abs(banned_new - banned_old) or abs(cand_new - cand_old) or 1.0
                if abs(cand_new - banned_new) <= SOFT_NEAR_FRAC * step + 1e-12:
                    return {
                        "reason": "kb_near_duplicate",
                        "banned": {"old": rec.get("old"), "new": rec.get("new")},
                    }
    return None


# ── pipeline negative evidence (#4) ─────────────────────────────────────────
def record_pipeline_outcome(
    bot: str,
    pair: str,
    *,
    variable: str,
    status: str,
    old=None,
    new=None,
    regime: str | None = None,
    reason: str = "",
) -> None:
    """Append a non-live pipeline outcome (l2_reject / backtest_reject / …)."""
    data = _load(bot, _PIPELINE)
    hist = data.get("history")
    if not isinstance(hist, list):
        hist = []
    hist.append(
        {
            "pair": pair,
            "variable": variable,
            "status": status,
            "old": old,
            "new": new,
            "regime": regime,
            "reason": reason,
            "ts": time.time(),
        }
    )
    data["history"] = hist[-200:]
    _save(bot, _PIPELINE, data)

    # Soft-quarantine the direction on hard rejects so L1 doesn't immediately
    # re-emit a near-duplicate that will die in the same gate.
    if status in ("l2_reject", "backtest_reject", "backtest_rejected", "l2_rejected"):
        closed_now = 0
        with contextlib.suppress(Exception):
            closed_now = len(_pair_closes(bot, pair))
        set_direction_cooldown(
            bot,
            pair,
            variable,
            old,
            new,
            until_closed=closed_now + DIRECTION_COOLDOWN_CLOSES,
            reason=status,
        )


def pipeline_outcomes(bot: str, pair: str | None = None) -> list[dict]:
    hist = _load(bot, _PIPELINE).get("history")
    if not isinstance(hist, list):
        return []
    rows = [h for h in hist if isinstance(h, dict)]
    if pair is None:
        return rows
    return [h for h in rows if h.get("pair") == pair]


# ── safe mode (Phase 3.5) ─────────────────────────────────────────────────---
def pair_safe_mode(bot: str, pair: str) -> dict | None:
    """Current safe-mode record for the pair, or None if trading normally."""
    rec = _load(bot, _SAFE_MODE).get(pair)
    return rec if isinstance(rec, dict) and rec.get("mode") not in (None, "normal") else None


def set_safe_mode(bot: str, pair: str, mode: str, reason: str) -> dict:
    """Enter (or clear with ``mode='normal'``) safe mode for a pair.

    ``size_down`` = keep trading at reduced size; ``paused`` = stop entering.
    """
    data = _load(bot, _SAFE_MODE)
    if mode == "normal":
        data.pop(pair, None)
        _save(bot, _SAFE_MODE, data)
        return {"pair": pair, "mode": "normal"}
    rec = {"mode": mode, "reason": reason, "ts": time.time()}
    data[pair] = rec
    _save(bot, _SAFE_MODE, data)
    return {"pair": pair, **rec}


def escalate_safe_mode(bot: str, pair: str, reason: str) -> dict:
    """Progress the pair one rung deeper into safe mode.

    normal → size_down → paused. Reflection calls this when it has NO axis left
    to try (all quarantined / in cooldown / clamped to the stop floor).
    """
    cur = pair_safe_mode(bot, pair)
    if cur is None:
        return set_safe_mode(bot, pair, "size_down", reason)
    if cur.get("mode") == "size_down":
        return set_safe_mode(bot, pair, "paused", reason)
    return cur


# ── dashboard surface (Phase 3.6) ─────────────────────────────────────────---
def experiments_summary(bot: str, pairs: list[str] | None = None) -> dict:
    """Per-pair experiment + champion + safe-mode snapshot for the dashboard."""
    exps = _load(bot, _EXPERIMENTS)
    champs = _load(bot, _CHAMPIONS)
    safe = _load(bot, _SAFE_MODE)
    cooldowns = _load(bot, _AXIS_COOLDOWN)
    handoffs = _load(bot, _GP_HANDOFF)
    directions = _load(bot, _DIRECTION_COOLDOWN)
    plans = _load(bot, _PLANS)
    shadows = _load(bot, _SHADOW)
    explore = _load(bot, _EXPLORE)
    deploy_cd = _load(bot, _DEPLOY_COOLDOWN)

    keys = set(pairs or [])
    keys |= {k for k in exps.keys() if k != "_history"}
    keys |= set(champs.keys())
    keys |= set(safe.keys())
    keys |= set(handoffs.keys())
    keys |= set(plans.keys())
    keys |= set(shadows.keys())
    keys |= set(explore.keys())

    out: dict[str, dict] = {}
    for pair in sorted(keys):
        exp = exps.get(pair) if isinstance(exps.get(pair), dict) else None
        champ = champs.get(pair) if isinstance(champs.get(pair), dict) else None
        sm = safe.get(pair) if isinstance(safe.get(pair), dict) else None
        cd = cooldowns.get(pair) if isinstance(cooldowns.get(pair), dict) else None
        ho = handoffs.get(pair) if isinstance(handoffs.get(pair), dict) else None
        direc = directions.get(pair) if isinstance(directions.get(pair), dict) else None
        plan = plans.get(pair) if isinstance(plans.get(pair), dict) else None
        shadow = shadows.get(pair) if isinstance(shadows.get(pair), dict) else None
        exp_mode = explore.get(pair) if isinstance(explore.get(pair), dict) else None
        dcd = deploy_cd.get(pair) if isinstance(deploy_cd.get(pair), dict) else None
        out[pair] = {
            "experiment": (
                {
                    "status": exp.get("status"),
                    "variable": exp.get("variable"),
                    "old": exp.get("old"),
                    "new": exp.get("new"),
                    "version_from": exp.get("version_from"),
                    "version_to": exp.get("version_to"),
                    "deployed_ts": exp.get("deployed_ts"),
                    "deployed_closed": exp.get("deployed_closed"),
                }
                if exp
                else None
            ),
            "champion_status": (champ or {}).get("status"),
            "champion_version": (champ or {}).get("version"),
            "revert_count": (champ or {}).get("revert_count", 0),
            "safe_mode": (sm or {}).get("mode"),
            "safe_mode_reason": (sm or {}).get("reason"),
            "cooldown_axes": list(cd.keys()) if cd else [],
            "axis_cooldown": dict(cd) if cd else {},
            "direction_cooldown": dict(direc) if direc else {},
            "gp_handoff": bool(ho and ho.get("active")),
            "gp_handoff_reason": (ho or {}).get("reason"),
            "gp_handoff_variable": (ho or {}).get("variable"),
            "plan": (plan.get("steps") if plan else None),
            "plan_reason": (plan or {}).get("reason"),
            "shadow": shadow,
            "explore": bool(exp_mode and exp_mode.get("active")),
            "explore_reason": (exp_mode or {}).get("reason"),
            "deploy_cooldown": dcd,
        }

    history = exps.get("_history")
    return {
        "bot": bot,
        "pairs": out,
        "history": history[-40:] if isinstance(history, list) else [],
        "gp_handoff_pairs": gp_handoff_pairs(bot),
        "deploy_stage": get_deploy_stage(bot),
    }


# ── Phase 4: GP handoff + shared failure memory + post-admit retune ──────────
def request_gp_handoff(
    bot: str,
    pair: str,
    *,
    reason: str,
    variable: str | None = None,
) -> dict:
    """Mark ``pair`` as needing priority GP discovery (Phase 4.1).

    Fired when reflection's live experiment reverts (underperforming champion +
    quarantined axis). Does NOT touch indicator exile — that remains GP's domain
    (Phase 4.4 no-cross-contamination).
    """
    data = _load(bot, _GP_HANDOFF)
    rec = {
        "active": True,
        "reason": reason,
        "variable": variable,
        "ts": time.time(),
    }
    data[pair] = rec
    _save(bot, _GP_HANDOFF, data)
    return {"pair": pair, **rec}


def clear_gp_handoff(bot: str, pair: str) -> None:
    data = _load(bot, _GP_HANDOFF)
    if pair in data:
        data.pop(pair, None)
        _save(bot, _GP_HANDOFF, data)


def gp_handoff_pairs(bot: str) -> list[str]:
    """Pairs currently flagged for priority discovery."""
    data = _load(bot, _GP_HANDOFF)
    return sorted(p for p, rec in data.items() if isinstance(rec, dict) and rec.get("active"))


def needs_gp_handoff(bot: str, pair: str) -> bool:
    rec = _load(bot, _GP_HANDOFF).get(pair)
    return bool(isinstance(rec, dict) and rec.get("active"))


def param_quarantine(bot: str, *, pair: str | None = None, limit: int = 50) -> list[dict]:
    """Shared failure memory: rejected (pair,variable,old,new) from the hypothesis KB.

    Includes backtest rejects AND live_worse bans from auto-revert. This is the
    PARAM side of Cortex shared failure memory (Phase 4.3); indicator exile is
    the separate GP side — they never share a store (Phase 4.4).
    """
    out: list[dict] = []
    with contextlib.suppress(Exception):
        from hermes_core.engines.backtest import _kb_path

        path = _kb_path(bot)
        if not path.exists():
            return out
        import json

        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("approved") is not False:
                continue
            if pair is not None and rec.get("pair") != pair:
                continue
            out.append(
                {
                    "pair": rec.get("pair"),
                    "variable": rec.get("param") or rec.get("variable"),
                    "old": rec.get("old"),
                    "new": rec.get("new"),
                    "reason": rec.get("reason"),
                    "ts": rec.get("ts"),
                    "live_worse": "live_worse" in str(rec.get("reason") or ""),
                }
            )
    return out[-max(1, limit) :]


def schedule_reflection_retune(bot: str, pair: str, *, reason: str) -> dict:
    """After a GP admit on a handoff pair, unlock reflection to retune risk (4.5).

    Clears axis cooldowns (new entry behaviour needs a fresh axis search),
    downgrades pause→size_down so the pair can trade again, and latches a
    one-shot retune flag consumed by ``maybe_reflect_pair``.
    """
    # Clear cooldowns so L1 can propose stop/trail/size again for the new edge.
    data = _load(bot, _AXIS_COOLDOWN)
    if pair in data:
        data.pop(pair, None)
        _save(bot, _AXIS_COOLDOWN, data)

    sm = pair_safe_mode(bot, pair)
    if sm and sm.get("mode") == "paused":
        set_safe_mode(bot, pair, "size_down", f"retune after GP admit: {reason}")

    retunes = _load(bot, _RETUNE)
    rec = {"active": True, "reason": reason, "ts": time.time()}
    retunes[pair] = rec
    _save(bot, _RETUNE, retunes)
    return {"pair": pair, **rec}


def consume_reflection_retune(bot: str, pair: str) -> dict | None:
    """Return + clear the pending retune latch (or None)."""
    data = _load(bot, _RETUNE)
    rec = data.pop(pair, None)
    if rec is not None:
        _save(bot, _RETUNE, data)
    return rec if isinstance(rec, dict) else None


def pending_reflection_retune(bot: str, pair: str) -> bool:
    rec = _load(bot, _RETUNE).get(pair)
    return bool(isinstance(rec, dict) and rec.get("active"))


def on_gp_admit(bot: str, pair: str, *, admitted: int) -> dict | None:
    """Phase 4.5 hook: GP admitted new formulas for a handoff pair.

    Clears the handoff latch and schedules a reflection risk retune. Returns the
    retune record, or None if this pair was not on the handoff list. Never
    bumps a strategy version — GP remains a signal path only (Phase 4.2).
    """
    if admitted <= 0 or not needs_gp_handoff(bot, pair):
        return None
    clear_gp_handoff(bot, pair)
    return schedule_reflection_retune(
        bot, pair, reason=f"gp_admit n={admitted}; retune risk for new entry behaviour"
    )


# ── Phase 5: deploy cooldown + staged unlock ────────────────────────────────
def record_deploy_cooldown(bot: str, pair: str, *, closed_count: int) -> None:
    """Stamp the last live deploy for cooldown / quiet-period checks (5.2)."""
    data = _load(bot, _DEPLOY_COOLDOWN)
    data[pair] = {"ts": time.time(), "closed": int(closed_count)}
    _save(bot, _DEPLOY_COOLDOWN, data)


def deploy_blocked(
    bot: str,
    pair: str,
    *,
    closed_count: int,
    now: float | None = None,
) -> dict | None:
    """Return a block reason dict if deploy is still in cooldown/quiet, else None."""
    rec = _load(bot, _DEPLOY_COOLDOWN).get(pair)
    if not isinstance(rec, dict):
        return None
    now = time.time() if now is None else now
    age = now - float(rec.get("ts") or 0)
    need_s = DEPLOY_COOLDOWN_S
    with contextlib.suppress(Exception):
        from hermes_core.engines.adaptive import adaptive_deploy_cooldown_s

        need_s = adaptive_deploy_cooldown_s(bot, pair, DEPLOY_COOLDOWN_S)
    if age < need_s:
        return {
            "reason": "deploy_cooldown_day",
            "age_s": round(age, 1),
            "need_s": need_s,
        }
    quiet_until = int(rec.get("closed") or 0) + DEPLOY_QUIET_CLOSES
    if int(closed_count) < quiet_until:
        return {
            "reason": "deploy_quiet_period",
            "closed": int(closed_count),
            "quiet_until": quiet_until,
        }
    return None


def get_deploy_stage(bot: str) -> str:
    """Current staged-deploy rung for ``bot`` (prove|canary|full).

    Precedence: stage file → ``REFLECT_DEPLOY_STAGE`` env → ``full``.
    Defaulting to ``full`` preserves legacy ``auto_deploy=True`` callers (tests /
    explicit unlock). A soak that wants shadow-prove must set the env or call
    ``set_deploy_stage(bot, "prove")``.
    """
    data = _load(bot, _DEPLOY_STAGE)
    if isinstance(data.get("stage"), str) and data.get("stage"):
        stage = str(data["stage"]).lower()
    else:
        stage = str(os.environ.get("REFLECT_DEPLOY_STAGE", "full") or "full").lower()
    if stage not in VALID_DEPLOY_STAGES:
        stage = "prove"
    return stage


def set_deploy_stage(bot: str, stage: str, *, reason: str = "") -> str:
    stage = str(stage).lower()
    if stage not in VALID_DEPLOY_STAGES:
        raise ValueError(f"invalid deploy stage: {stage}")
    _save(
        bot,
        _DEPLOY_STAGE,
        {"stage": stage, "reason": reason, "ts": time.time()},
    )
    return stage


def advance_deploy_stage(bot: str, *, reason: str = "experiment_improved") -> str:
    """prove→canary→full. No-op at full. Returns the new stage."""
    cur = get_deploy_stage(bot)
    nxt = {"prove": "canary", "canary": "full"}.get(cur, cur)
    if nxt != cur:
        set_deploy_stage(bot, nxt, reason=reason)
    return nxt


def auto_deploy_allowed(bot: str, *, env_auto: bool) -> dict:
    """Phase 5.3 gate: REFLECT_AUTO_DEPLOY only writes YAML at canary/full.

    Returns ``{"allowed": bool, "stage": str, "reason": str}``.
    """
    stage = get_deploy_stage(bot)
    if not env_auto:
        return {
            "allowed": False,
            "stage": stage,
            "reason": "REFLECT_AUTO_DEPLOY=0",
        }
    if stage == "prove":
        return {
            "allowed": False,
            "stage": stage,
            "reason": "stage_prove_shadow_only",
        }
    return {"allowed": True, "stage": stage, "reason": "ok"}


# ── #9 planned one-at-a-time axis chains ────────────────────────────────────
def save_plan(bot: str, pair: str, steps: list[dict], *, reason: str = "") -> None:
    """Persist a short plan of follow-up axis changes (still one live change at a time)."""
    clean = []
    for s in steps or []:
        if not isinstance(s, dict) or not s.get("variable"):
            continue
        clean.append(
            {
                "variable": s["variable"],
                "old": s.get("old"),
                "new": s.get("new"),
                "why": s.get("why") or s.get("reason") or "",
                "priority": s.get("priority"),
            }
        )
        if len(clean) >= 3:
            break
    data = _load(bot, _PLANS)
    if not clean:
        data.pop(pair, None)
    else:
        data[pair] = {"steps": clean, "reason": reason, "ts": time.time()}
    _save(bot, _PLANS, data)


def peek_plan(bot: str, pair: str) -> dict | None:
    rec = _load(bot, _PLANS).get(pair)
    if not isinstance(rec, dict):
        return None
    steps = rec.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    return rec


def next_plan_step(bot: str, pair: str) -> dict | None:
    rec = peek_plan(bot, pair)
    if not rec:
        return None
    step = rec["steps"][0]
    return step if isinstance(step, dict) else None


def advance_plan(bot: str, pair: str, *, consumed_variable: str | None = None) -> None:
    """Drop the head step after a successful live experiment (optionally matched)."""
    rec = peek_plan(bot, pair)
    if not rec:
        return
    steps = list(rec.get("steps") or [])
    if not steps:
        save_plan(bot, pair, [])
        return
    if consumed_variable and steps[0].get("variable") != consumed_variable:
        return
    save_plan(bot, pair, steps[1:], reason="advanced_after_improve")


def clear_plan_step(bot: str, pair: str, variable: str | None) -> None:
    """Drop a planned step whose live attempt just reverted."""
    if not variable:
        return
    rec = peek_plan(bot, pair)
    if not rec:
        return
    steps = [s for s in (rec.get("steps") or []) if s.get("variable") != variable]
    save_plan(bot, pair, steps, reason="cleared_after_revert")


# ── #10 L2 critic trust calibration ─────────────────────────────────────────
def record_l2_votes(bot: str, pair: str, *, votes: dict[str, bool], decision: bool) -> None:
    """Stamp the latest L2 vote vector for later live outcome scoring."""
    data = _load(bot, _L2_TRUST)
    pending = data.get("_pending")
    if not isinstance(pending, dict):
        pending = {}
    pending[pair] = {
        "votes": dict(votes or {}),
        "decision": bool(decision),
        "ts": time.time(),
    }
    data["_pending"] = pending
    _save(bot, _L2_TRUST, data)


def record_l2_outcome(bot: str, pair: str, exp: dict | None, *, outcome: str) -> None:
    """Credit/blame each model for the live outcome of an L2-approved deploy."""
    data = _load(bot, _L2_TRUST)
    pending = data.get("_pending") if isinstance(data.get("_pending"), dict) else {}
    note = pending.pop(pair, None)
    data["_pending"] = pending
    models = data.get("models")
    if not isinstance(models, dict):
        models = {}
    if note and note.get("decision"):
        good = outcome == "improved"
        for name, voted_yes in (note.get("votes") or {}).items():
            st = models.setdefault(name, {"correct": 0, "wrong": 0, "votes": 0})
            st["votes"] = int(st.get("votes", 0)) + 1
            agreed = bool(voted_yes) == good
            if agreed:
                st["correct"] = int(st.get("correct", 0)) + 1
            else:
                st["wrong"] = int(st.get("wrong", 0)) + 1
            models[name] = st
    data["models"] = models
    if note and note.get("decision"):
        data["decisions"] = int(data.get("decisions", 0)) + 1
        if outcome == "improved":
            data["hits"] = int(data.get("hits", 0)) + 1
    _save(bot, _L2_TRUST, data)


def l2_model_weight(bot: str, model: str) -> float:
    """Vote weight in [0.25, 1.75]; 1.0 with no evidence."""
    st = (_load(bot, _L2_TRUST).get("models") or {}).get(model) or {}
    n = int(st.get("correct", 0)) + int(st.get("wrong", 0))
    if n <= 0:
        return 1.0
    hit = int(st.get("correct", 0)) / n
    return max(0.25, min(1.75, 0.25 + 1.5 * hit))


def l2_trust_summary(bot: str) -> dict:
    data = _load(bot, _L2_TRUST)
    decisions = int(data.get("decisions", 0) or 0)
    hits = int(data.get("hits", 0) or 0)
    models = data.get("models") if isinstance(data.get("models"), dict) else {}
    return {
        "decisions": decisions,
        "hits": hits,
        "hit_rate": (hits / decisions) if decisions else None,
        "models": {
            name: {**st, "weight": round(l2_model_weight(bot, name), 3)}
            for name, st in models.items()
        },
    }


# ── #11 explore size + shadow challenger ────────────────────────────────────
def enter_explore(bot: str, pair: str, *, reason: str = "") -> dict:
    """Cut size while the champion is known-underperforming (without full pause)."""
    cur = pair_safe_mode(bot, pair)
    if cur and cur.get("mode") == "paused":
        return cur
    data = _load(bot, _EXPLORE)
    rec = {"active": True, "reason": reason, "ts": time.time()}
    data[pair] = rec
    _save(bot, _EXPLORE, data)
    if cur is None or cur.get("mode") != "size_down":
        set_safe_mode(bot, pair, "size_down", reason or "explore_underperforming")
    return rec


def clear_explore(bot: str, pair: str) -> None:
    data = _load(bot, _EXPLORE)
    data.pop(pair, None)
    _save(bot, _EXPLORE, data)
    sm = pair_safe_mode(bot, pair)
    reason = str((sm or {}).get("reason") or "")
    if sm and sm.get("mode") == "size_down" and (
        "underperform" in reason or reason.startswith("explore")
    ):
        set_safe_mode(bot, pair, "normal", "explore_cleared")


def in_explore(bot: str, pair: str) -> bool:
    rec = _load(bot, _EXPLORE).get(pair)
    return bool(isinstance(rec, dict) and rec.get("active"))


def record_shadow_challenger(
    bot: str,
    pair: str,
    *,
    variable: str,
    old,
    new,
    reason: str = "",
    backtest: dict | None = None,
) -> None:
    """Paper-track an approved-but-not-deployed (or explore) challenger (#11)."""
    data = _load(bot, _SHADOW)
    data[pair] = {
        "variable": variable,
        "old": old,
        "new": new,
        "reason": reason,
        "backtest": {
            k: (backtest or {}).get(k)
            for k in (
                "approved",
                "old_pnl",
                "new_pnl",
                "improvement_full",
                "improvement_oos",
                "reason",
            )
        },
        "ts": time.time(),
        "status": "shadow",
    }
    _save(bot, _SHADOW, data)


def shadow_challenger(bot: str, pair: str) -> dict | None:
    rec = _load(bot, _SHADOW).get(pair)
    return rec if isinstance(rec, dict) else None


def clear_shadow_challenger(bot: str, pair: str) -> None:
    data = _load(bot, _SHADOW)
    data.pop(pair, None)
    _save(bot, _SHADOW, data)
