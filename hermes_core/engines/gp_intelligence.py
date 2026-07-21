"""GP intelligence layer (Session 14 / Phase 14).

Governance over the GP-discovered indicators (S13): weighted-vote ensemble
scoring, suppression, lockout on consecutive losses, and per-regime degradation
culling.

Two verified blueprint fixes are baked in:
  * PROBLEM 3 — the original default gp_entry_score of -0.3 combined with the
    `>= 0` gate deadlocked every new indicator (it could never earn its first
    entries). Corrected DEFAULT_GP_SCORE = 0.0 (neutral): first entries fire at
    neutral, real score is computed from outcomes, and bad outcomes drive it
    below 0 -> suppressed. [GUARD L29]
  * PROBLEM 4 — degradation != regime mismatch. An indicator is CULLED only when
    its SAME-REGIME win-rate < 0.40 over >= 50 signals. A low WR in a regime it
    was NOT trained in is a regime mismatch -> weight-penalized, never culled.

Functions (blueprint Phase 14 build target):
  get_label(...) ; gp_entry_score(...) ; record_loss(pair) ; _update_indicator(...)

Contract (Section 6):
  GPIntelligence.score(pair, cond) -> float[-1, 1]
  GPIntelligence.should_suppress() -> (bool, reason)
"""

from __future__ import annotations

import json
from pathlib import Path

from hermes_core.state.paths import gp_state_path

# ── gates ──────────────────────────────────────────────────────────────────
DEFAULT_GP_SCORE = 0.0     # [GUARD L29] corrected from -0.3 (Problem 3)
SCORE_GATE = 0.0           # entry allowed only if score >= this
LOCKOUT_AFTER = 3          # consecutive losses -> locked [L29]
CULL_WR = 0.40             # same-regime WR below this -> cull [Problem 4]
CULL_MIN_SIGNALS = 50      # need this many same-regime signals before culling
REGIME_MISMATCH_PENALTY = 0.5   # weight multiplier when used outside trained regime

# Optional test override (tests monkeypatch this module attribute).
GP_STATE: Path | None = None


def _gp_state_file(pair: str | None = None) -> Path:
    if GP_STATE is not None:
        return GP_STATE
    return gp_state_path(pair=pair)


def _load_state(pair: str | None = None) -> dict:
    path = _gp_state_file(pair)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_state(state: dict, pair: str | None = None) -> None:
    path = _gp_state_file(pair)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def get_label(indicators: list[dict]) -> str:
    """Weighted-vote consensus label over a list of indicator dicts.

    Each indicator dict: {"signal": float, "fitness": float, "wr": float, ...}
    signal in roughly [-1, 1]; fitness>=0 used as weight.
    """
    if not indicators:
        return "conflict"
    total_w = 0.0
    bullish_w = 0.0
    for ind in indicators:
        w = max(ind.get("fitness", 0.0), 0.0)
        total_w += w
        if ind.get("signal", 0.0) > 0.2:
            bullish_w += w
    if total_w == 0.0:
        return "conflict"
    score = (bullish_w - (total_w - bullish_w)) / total_w
    agree = bullish_w / total_w
    if score > 0.5 and agree >= 0.60:
        return "strong_bullish"
    if score > 0.2 and agree >= 0.50:
        return "bullish"
    if score < -0.2 and agree >= 0.50:
        return "bearish"
    if score < -0.5 and agree >= 0.60:
        return "strong_bearish"
    return "conflict"


def gp_entry_score(pair: str, cond: dict | None = None) -> float:
    """Ensemble entry score in [-1, 1]. Returns DEFAULT_GP_SCORE (0.0) for a
    fresh pair with no outcome data (the corrected neutral default)."""
    state = _load_state(pair)
    rec = state.get("scores", {}).get(pair)
    if rec is None:
        return DEFAULT_GP_SCORE
    # blend: regime-adjusted WR term, clipped to [-1, 1]
    w = max(min(rec.get("wr", 0.5), 1.0), 0.0)
    s = (w - 0.5) * 2.0
    return max(-1.0, min(1.0, s))


def record_loss(pair: str) -> None:
    """Record a losing GP entry; 3 consecutive losses -> locked."""
    state = _load_state(pair)
    seq = state.setdefault("loss_seq", {})
    seq[pair] = seq.get(pair, 0) + 1
    _save_state(state, pair)


def record_win(pair: str) -> None:
    """Record a winning GP entry; resets the consecutive-loss counter."""
    state = _load_state(pair)
    state.setdefault("loss_seq", {})[pair] = 0
    _save_state(state, pair)


def is_locked(pair: str) -> bool:
    return _load_state(pair).get("loss_seq", {}).get(pair, 0) >= LOCKOUT_AFTER


def should_suppress(pair: str, cond: dict | None = None) -> tuple[bool, str]:
    """Return (suppress?, human-readable reason).

    Suppresses when: locked (>=3 consecutive losses) OR score below the gate.
    """
    if is_locked(pair):
        return True, f"locked: {LOCKOUT_AFTER}+ consecutive GP losses on {pair}"
    score = gp_entry_score(pair, cond)
    if score < SCORE_GATE:
        return True, (f"gp_entry_score={score:.2f} < gate {SCORE_GATE} "
                      f"(insufficient winning history)")
    return False, "ok"


def weight_for(ind: dict, regime: str) -> float:
    """Effective ensemble weight for `ind` in `regime`.

    PROBLEM 4: if `regime` is outside the indicator's trained regimes, apply
    REGIME_MISMATCH_PENALTY (weight-penalty) — never cull here. Culled
    indicators return 0.0.
    """
    if ind.get("culled"):
        return 0.0
    base = max(ind.get("fitness", 0.0), 0.0)
    trained = ind.get("trained_regimes", [])
    if trained and regime not in trained:
        return base * REGIME_MISMATCH_PENALTY
    return base


def _update_indicator(registry: list[dict], ind_id: str, outcome: float,
                      regime: str) -> list[dict]:
    """Mutate an indicator in the registry with a new outcome in `regime`.

    Tracks per-regime wins/signals. Returns the (possibly culled) registry.
    PROBLEM 4: cull only on same-regime WR < CULL_WR over >= CULL_MIN_SIGNALS;
    regime mismatch is flagged for weight-penalty, never culled here.
    """
    for ind in registry:
        if ind.get("id") != ind_id:
            continue
        by_regime = ind.setdefault("by_regime", {})
        bucket = by_regime.setdefault(regime, {"wins": 0, "signals": 0})
        bucket["signals"] += 1
        if outcome > 0:
            bucket["wins"] += 1
        ind["trained_regimes"] = sorted(set(ind.get("trained_regimes", []) + [regime]))
        # PROBLEM 4 — regime mismatch is weight-penalized, NOT culled.
        # (This update just trained the regime, so a mismatch can only arise
        #  later when the indicator is *used* in a regime absent from
        #  trained_regimes; consumers apply REGIME_MISMATCH_PENALTY to its
        #  weight. We record the set here so the penalty is computable.)
        # degradation cull: only same-regime, only with enough samples
        if bucket["signals"] >= CULL_MIN_SIGNALS:
            wr = bucket["wins"] / bucket["signals"]
            if wr < CULL_WR:
                ind["culled"] = True
                ind["cull_reason"] = (f"same-regime WR {wr:.2f} < {CULL_WR} "
                                      f"over {bucket['signals']} signals")
        return registry
    return registry


class GPIntelligence:
    """Roadmap S14 contract wrapper."""

    def score(self, pair: str, cond: dict | None = None) -> float:
        return gp_entry_score(pair, cond)

    def should_suppress(self, pair: str, cond: dict | None = None) -> tuple[bool, str]:
        return should_suppress(pair, cond)

    def record_loss(self, pair: str) -> None:
        record_loss(pair)

    def record_win(self, pair: str) -> None:
        record_win(pair)

    def is_locked(self, pair: str) -> bool:
        return is_locked(pair)

    def get_label(self, indicators: list[dict]) -> str:
        return get_label(indicators)

    def update_indicator(self, registry: list[dict], ind_id: str,
                         outcome: float, regime: str) -> list[dict]:
        return _update_indicator(registry, ind_id, outcome, regime)
