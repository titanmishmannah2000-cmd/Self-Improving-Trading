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
  get_label(...) ; gp_entry_score(...) ; record_loss(pair) ; update_indicator(...)

Contract (Section 6):
  GPIntelligence.score(pair, cond) -> float[-1, 1]
  GPIntelligence.should_suppress() -> (bool, reason)

Score gate:
  SCORE_GATE = 0.0 — fresh pairs stay at DEFAULT_GP_SCORE (0.0) and are NOT
  suppressed (gate is strict ``score < SCORE_GATE``). After decisive win/loss
  samples, ``state["scores"][pair].wr`` moves and the blended score follows;
  sustained losses push score below the gate.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from hermes_core.state.atomic_json import atomic_write_json, load_json
from hermes_core.state.paths import gp_state_path

# ── gates ──────────────────────────────────────────────────────────────────
DEFAULT_GP_SCORE = 0.0  # [GUARD L29] corrected from -0.3 (Problem 3)
SCORE_GATE = 0.0  # entry allowed only if score >= this (strict < suppresses)
LOCKOUT_AFTER = 3  # consecutive losses -> locked [L29]
# Wall-clock unlock so a 3-loss streak cannot empty the ensemble for the whole soak.
LOCKOUT_DECAY_S = int(os.getenv("GP_LOCKOUT_DECAY_S", str(6 * 3600)))
CULL_WR = 0.40  # same-regime WR below this -> cull [Problem 4]
CULL_MIN_SIGNALS = 50  # need this many same-regime signals before culling
REGIME_MISMATCH_PENALTY = 0.5  # weight multiplier when used outside trained regime
FLAT_PNL_EPS = 1e-6  # |pnl| below this is flat / neutral (not a loss)

_STATE_LOCK = threading.RLock()

# Optional test override (tests monkeypatch this module attribute).
GP_STATE: Path | None = None


def is_flat_pnl(pnl: float) -> bool:
    """True when PnL is effectively flat (no win/loss for lockout or WR)."""
    try:
        return abs(float(pnl)) < FLAT_PNL_EPS
    except (TypeError, ValueError):
        return True


def is_win_pnl(pnl: float) -> bool:
    try:
        return float(pnl) > FLAT_PNL_EPS
    except (TypeError, ValueError):
        return False


def is_loss_pnl(pnl: float) -> bool:
    try:
        return float(pnl) < -FLAT_PNL_EPS
    except (TypeError, ValueError):
        return False


def _gp_state_file(pair: str | None = None) -> Path:
    if GP_STATE is not None:
        return GP_STATE
    return gp_state_path(pair=pair)


def _load_state(pair: str | None = None) -> dict:
    path = _gp_state_file(pair)
    raw = load_json(path, default={}, quarantine=True)
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict, pair: str | None = None) -> None:
    path = _gp_state_file(pair)
    try:
        atomic_write_json(path, state, indent=2)
    except OSError:
        pass


def _update_pair_score(state: dict, pair: str, *, win: bool) -> None:
    """Bump rolling WR for ``pair`` so ``gp_entry_score`` reflects outcomes."""
    scores = state.setdefault("scores", {})
    rec = scores.setdefault(pair, {"wins": 0, "attempts": 0, "wr": 0.5})
    rec["attempts"] = int(rec.get("attempts") or 0) + 1
    if win:
        rec["wins"] = int(rec.get("wins") or 0) + 1
    attempts = max(1, int(rec["attempts"]))
    rec["wr"] = float(rec.get("wins") or 0) / attempts


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
    fresh pair with no outcome data (the corrected neutral default).

    After ``record_win`` / ``record_loss`` / ``record_outcome``, ``scores[pair].wr``
    is updated and this blends to ``(wr - 0.5) * 2`` so the score gate can fire.
    """
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
    with _STATE_LOCK:
        state = _load_state(pair)
        seq = state.setdefault("loss_seq", {})
        seq[pair] = seq.get(pair, 0) + 1
        if seq[pair] >= LOCKOUT_AFTER:
            state.setdefault("lockout_ts", {})[pair] = time.time()
        _update_pair_score(state, pair, win=False)
        _save_state(state, pair)


def record_win(pair: str) -> None:
    """Record a winning GP entry; resets the consecutive-loss counter."""
    with _STATE_LOCK:
        state = _load_state(pair)
        state.setdefault("loss_seq", {})[pair] = 0
        state.setdefault("lockout_ts", {}).pop(pair, None)
        _update_pair_score(state, pair, win=True)
        _save_state(state, pair)


def record_outcome(pair: str, pnl: float) -> str:
    """Route a closed PnL to win / loss / flat (flat-PnL neutrality).

    Returns ``\"win\"``, ``\"loss\"``, or ``\"flat\"``. Flat (|pnl| < 1e-6) does
    not touch ``loss_seq`` or rolling WR.
    """
    if is_flat_pnl(pnl):
        return "flat"
    if is_win_pnl(pnl):
        record_win(pair)
        return "win"
    record_loss(pair)
    return "loss"


def is_locked(pair: str) -> bool:
    """True while consecutive losses >= LOCKOUT_AFTER and decay window not elapsed."""
    with _STATE_LOCK:
        state = _load_state(pair)
        if state.get("loss_seq", {}).get(pair, 0) < LOCKOUT_AFTER:
            return False
        locked_at = state.get("lockout_ts", {}).get(pair)
        if locked_at is None:
            # Legacy lock without timestamp — start the decay clock now.
            state.setdefault("lockout_ts", {})[pair] = time.time()
            _save_state(state, pair)
            return True
        if (time.time() - float(locked_at)) >= max(1, int(LOCKOUT_DECAY_S)):
            state.setdefault("loss_seq", {})[pair] = 0
            state.setdefault("lockout_ts", {}).pop(pair, None)
            _save_state(state, pair)
            return False
        return True


def should_suppress(pair: str, cond: dict | None = None) -> tuple[bool, str]:
    """Return (suppress?, human-readable reason).

    Suppresses when: locked (>=3 consecutive losses) OR score below the gate.
    Fresh pairs score DEFAULT_GP_SCORE (0.0) which is not below SCORE_GATE.
    """
    if is_locked(pair):
        return True, f"locked: {LOCKOUT_AFTER}+ consecutive GP losses on {pair}"
    score = gp_entry_score(pair, cond)
    if score < SCORE_GATE:
        return True, (
            f"gp_entry_score={score:.2f} < gate {SCORE_GATE} (insufficient winning history)"
        )
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


def _update_indicator(registry: list[dict], ind_id: str, outcome: float, regime: str) -> list[dict]:
    """Mutate an indicator in the registry with a new outcome in `regime`.

    Matches ``id`` or ``name`` (entry credits fire by name). Flat outcomes
    (|pnl| < FLAT_PNL_EPS) are ignored so they do not dilute same-regime WR.

    Tracks per-regime wins/signals. Returns the (possibly culled) registry.
    PROBLEM 4: cull only on same-regime WR < CULL_WR over >= CULL_MIN_SIGNALS;
    regime mismatch is flagged for weight-penalty, never culled here.
    """
    if is_flat_pnl(outcome):
        return registry
    for ind in registry:
        if ind.get("id") != ind_id and ind.get("name") != ind_id:
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
                ind["cull_reason"] = (
                    f"same-regime WR {wr:.2f} < {CULL_WR} over {bucket['signals']} signals"
                )
        return registry
    return registry


def update_indicator(
    registry: list[dict], ind_id: str, outcome: float, regime: str
) -> list[dict]:
    """Public wrapper for regime-cull updates (see ``_update_indicator``)."""
    return _update_indicator(registry, ind_id, outcome, regime)


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

    def record_outcome(self, pair: str, pnl: float) -> str:
        return record_outcome(pair, pnl)

    def is_locked(self, pair: str) -> bool:
        return is_locked(pair)

    def get_label(self, indicators: list[dict]) -> str:
        return get_label(indicators)

    def update_indicator(
        self, registry: list[dict], ind_id: str, outcome: float, regime: str
    ) -> list[dict]:
        return update_indicator(registry, ind_id, outcome, regime)
