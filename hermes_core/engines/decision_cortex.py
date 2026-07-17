"""Decision cortex (Session 15 / Phase 15).

Unified memory across reflection / GP / dashboard: per-type & per-indicator
win-rate, indicator exile, and condition routing. Everything persists to disk
(D2) so a restart never silently rebuilds from scratch.

Governance (blueprint ENGINE 8 / Phase 15):
  * auto-exile: an indicator with <30% WR as a GP entry after >=5 attempts is
    exiled (L36 exile filter — removed from GP candidacy).
  * exile decay: reconsider after 100 entries; reinstate if WR >= 40%.
  * best_entry_type() always returns a known, valid type.

Persistence:
  state/cortex/indicator_exile.json  — exiled indicator set (survives restart)
"""

from __future__ import annotations

import json

from hermes_core.config import repo_root

# ── gates ──────────────────────────────────────────────────────────────────
EXILE_WR = 0.30          # [GUARD L36] WR below this after enough attempts -> exile
EXILE_MIN_ATTEMPTS = 5   # need >=5 GP attempts before exile can trigger
REINSTATE_WR = 0.40      # WR at/above this reinstates an exiled indicator
EXILE_DECAY_ENTRIES = 100  # reconsider exiled indicators every 100 entries
VALID_ENTRY_TYPES = ("mean_reversion", "gp_ensemble")

CORTEX_DIR = repo_root() / "state" / "cortex"
EXILE_PATH = CORTEX_DIR / "indicator_exile.json"


def _load_exiles() -> dict:
    if EXILE_PATH.exists():
        try:
            return json.loads(EXILE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_exiles(data: dict) -> None:
    CORTEX_DIR.mkdir(parents=True, exist_ok=True)
    EXILE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


class Cortex:
    """Per-pair, per-type, per-indicator memory + exile system."""

    def __init__(self) -> None:
        self._entries: list[dict] = []          # (pair, entry_type, outcome)
        self._indicator_stats: dict[str, dict] = {}   # ind_id -> attempts/wins/exiled

    # ── recording ──────────────────────────────────────────────────────────
    def record_entry(self, pair: str, entry_type: str) -> None:
        self._entries.append({"pair": pair, "type": entry_type, "outcome": None})

    def record_outcome(self, pair: str, entry_type: str, pnl: float) -> None:
        self._entries.append({"pair": pair, "type": entry_type,
                              "outcome": 1 if pnl > 0 else 0})

    def record_hypothesis(self, pair: str, text: str) -> None:
        self._entries.append({"pair": pair, "type": "hypothesis", "text": text})

    def record_discovery(self, pair: str, ind_id: str) -> None:
        self._entries.append({"pair": pair, "type": "discovery", "ind": ind_id})

    # ── per-type win-rate ───────────────────────────────────────────────────
    def entry_type_wr(self, entry_type: str) -> float | None:
        outcomes = [e for e in self._entries
                    if e.get("type") == entry_type and e.get("outcome") is not None]
        if not outcomes:
            return None
        wins = sum(e["outcome"] for e in outcomes)
        return wins / len(outcomes)

    # ── best entry type (router) ────────────────────────────────────────────
    def best_entry_type(self, pair: str | None = None) -> str:
        """Return the entry type with the higher known win-rate, falling back to
        a valid default. Never returns an unknown type."""
        wrs = {t: self.entry_type_wr(t) for t in VALID_ENTRY_TYPES}
        known = {t: w for t, w in wrs.items() if w is not None}
        if not known:
            return "mean_reversion"          # safe default when no data yet
        return max(known, key=known.get)

    # ── per-indicator exile system ──────────────────────────────────────────
    def record_indicator_outcome(self, ind_id: str, pnl: float) -> None:
        """Track a GP indicator's outcome; auto-exile / reinstate per gates."""
        st = self._indicator_stats.setdefault(
            ind_id, {"attempts": 0, "wins": 0, "exiled": False})
        st["attempts"] += 1
        if pnl > 0:
            st["wins"] += 1
        wr = st["wins"] / st["attempts"]
        exiles = _load_exiles()
        if st["exiled"]:
            # decay reconsider: only act near the decay cadence, reinstate >=40%
            if st["attempts"] % EXILE_DECAY_ENTRIES == 0 and wr >= REINSTATE_WR:
                st["exiled"] = False
                exiles.pop(ind_id, None)
        elif (st["attempts"] >= EXILE_MIN_ATTEMPTS
              and wr < EXILE_WR):
            st["exiled"] = True
            exiles[ind_id] = {"exiled_at_attempts": st["attempts"], "wr": round(wr, 3)}
        _save_exiles(exiles)

    def is_indicator_exiled(self, ind_id: str) -> bool:
        return bool(_load_exiles().get(ind_id))

    def exile_indicator(self, ind_id: str) -> None:
        exiles = _load_exiles()
        exiles[ind_id] = exiles.get(ind_id, {"manual": True})
        _save_exiles(exiles)
        if ind_id in self._indicator_stats:
            self._indicator_stats[ind_id]["exiled"] = True

    def get_exiled_indicators(self) -> list[str]:
        return sorted(_load_exiles().keys())

    def summary(self) -> dict:
        return {
            "entries": len(self._entries),
            "type_wr": {t: self.entry_type_wr(t) for t in VALID_ENTRY_TYPES},
            "best_entry_type": self.best_entry_type(),
            "exiled": self.get_exiled_indicators(),
        }
