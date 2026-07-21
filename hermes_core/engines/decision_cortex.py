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
from pathlib import Path

from hermes_core.state.paths import cortex_dir, current_bot

# ── gates ──────────────────────────────────────────────────────────────────
EXILE_WR = 0.30          # [GUARD L36] WR below this after enough attempts -> exile
EXILE_MIN_ATTEMPTS = 5   # need >=5 GP attempts before exile can trigger
REINSTATE_WR = 0.40      # WR at/above this reinstates an exiled indicator
EXILE_DECAY_ENTRIES = 100  # reconsider exiled indicators every 100 entries
VALID_ENTRY_TYPES = ("mean_reversion", "gp_ensemble")

# Optional test overrides (tests monkeypatch these module attributes).
CORTEX_DIR: Path | None = None
EXILE_PATH: Path | None = None
MEMORY_PATH: Path | None = None


def _cortex_paths(bot: str | None = None) -> tuple[Path, Path, Path]:
    if CORTEX_DIR is not None:
        base = CORTEX_DIR
    else:
        base = cortex_dir(bot or current_bot())
    exile = EXILE_PATH or (base / "indicator_exile.json")
    memory = MEMORY_PATH or (base / "cortex_memory.json")
    return base, exile, memory


def _load_exiles(bot: str | None = None) -> dict:
    _, exile_path, _ = _cortex_paths(bot)
    if exile_path.exists():
        try:
            return json.loads(exile_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_exiles(data: dict, bot: str | None = None) -> None:
    base, exile_path, _ = _cortex_paths(bot)
    base.mkdir(parents=True, exist_ok=True)
    exile_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _load_memory(bot: str | None = None) -> dict:
    """Persisted entry/outcome history (D2): survives restart + per-cycle reset."""
    _, _, memory_path = _cortex_paths(bot)
    if memory_path.exists():
        try:
            return json.loads(memory_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {"entries": [], "indicator_stats": {}}


def _save_memory(data: dict, bot: str | None = None) -> None:
    base, _, memory_path = _cortex_paths(bot)
    base.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(json.dumps(data), encoding="utf-8")


class Cortex:
    """Per-pair, per-type, per-indicator memory + exile system.

    Memory persists to disk (D2) so a restart, or the per-cycle re-creation
    in the bot loop, never silently rebuilds from scratch.
    """

    def __init__(self, bot: str | None = None) -> None:
        self._bot = bot or current_bot()
        mem = _load_memory(self._bot)
        self._entries: list[dict] = mem.get("entries", [])
        self._indicator_stats: dict[str, dict] = mem.get("indicator_stats", {})

    def _flush(self) -> None:
        _save_memory({"entries": self._entries,
                      "indicator_stats": self._indicator_stats}, self._bot)

    # ── recording ──────────────────────────────────────────────────────────
    def record_entry(self, pair: str, entry_type: str) -> None:
        self._entries.append({"pair": pair, "type": entry_type, "outcome": None})
        self._flush()

    def record_outcome(self, pair: str, entry_type: str, pnl: float) -> None:
        self._entries.append({"pair": pair, "type": entry_type,
                              "outcome": 1 if pnl > 0 else 0,
                              "pnl": float(pnl)})
        self._flush()

    def record_hypothesis(self, pair: str, text: str) -> None:
        self._entries.append({"pair": pair, "type": "hypothesis", "text": text})
        self._flush()

    def record_discovery(self, pair: str, ind_id: str) -> None:
        self._entries.append({"pair": pair, "type": "discovery", "ind": ind_id})
        self._flush()

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
    def record_indicator_outcome(self, ind_id: str, pnl: float,
                                 entry_type: str | None = None) -> None:
        """Track a GP indicator's outcome; auto-exile / reinstate per gates.

        `entry_type` (optional) lets us separate GP-ensemble credit from any
        other credit so the dashboard can show per-indicator GP-entry WR (B9).
        """
        st = self._indicator_stats.setdefault(
            ind_id, {"attempts": 0, "wins": 0, "pnl": 0.0, "exiled": False,
                     "gp": {"attempts": 0, "wins": 0, "pnl": 0.0}})
        st["attempts"] += 1
        st["pnl"] = float(st.get("pnl", 0.0)) + float(pnl)
        if pnl > 0:
            st["wins"] += 1
        if entry_type == "gp_ensemble":
            gp = st.setdefault("gp", {"attempts": 0, "wins": 0, "pnl": 0.0})
            gp["attempts"] += 1
            gp["pnl"] = float(gp.get("pnl", 0.0)) + float(pnl)
            if pnl > 0:
                gp["wins"] += 1
        wr = st["wins"] / st["attempts"]
        exiles = _load_exiles(self._bot)
        if st["exiled"]:
            # decay reconsider: only act near the decay cadence, reinstate >=40%
            if st["attempts"] % EXILE_DECAY_ENTRIES == 0 and wr >= REINSTATE_WR:
                st["exiled"] = False
                exiles.pop(ind_id, None)
        elif (st["attempts"] >= EXILE_MIN_ATTEMPTS
              and wr < EXILE_WR):
            st["exiled"] = True
            exiles[ind_id] = {"exiled_at_attempts": st["attempts"], "wr": round(wr, 3)}
        _save_exiles(exiles, self._bot)
        self._flush()

    def is_indicator_exiled(self, ind_id: str) -> bool:
        return bool(_load_exiles(self._bot).get(ind_id))

    def exile_indicator(self, ind_id: str) -> None:
        exiles = _load_exiles(self._bot)
        exiles[ind_id] = exiles.get(ind_id, {"manual": True})
        _save_exiles(exiles, self._bot)
        if ind_id in self._indicator_stats:
            self._indicator_stats[ind_id]["exiled"] = True

    def get_exiled_indicators(self) -> list[str]:
        return sorted(_load_exiles(self._bot).keys())

    def indicator_live_stats(self, ind_id: str) -> dict:
        """Return the GP-entry live stats for an indicator (B9 `gp` sub-block).

        Used by B10 live feedback to bend discovered-indicator fitness toward
        realized paper PnL. Returns {} when the indicator has no GP record yet.
        """
        st = self._indicator_stats.get(ind_id)
        if not st:
            return {}
        gp = st.get("gp", {}) or {}
        return {
            "attempts": gp.get("attempts", 0),
            "wins": gp.get("wins", 0),
            "pnl": float(gp.get("pnl", 0.0)),
        }

    def summary(self) -> dict:
        by_type: dict[str, dict] = {}
        by_pair: dict[str, dict] = {}
        for e in self._entries:
            outcome = e.get("outcome")
            if outcome is None:
                continue
            t = e.get("type")
            p = e.get("pair")
            if t:
                d = by_type.setdefault(t, {"n": 0, "wins": 0, "pnl": 0.0})
                d["n"] += 1
                d["wins"] += outcome
                d["pnl"] += e.get("pnl", 0.0)
            if p:
                d = by_pair.setdefault(p, {"n": 0, "wins": 0, "pnl": 0.0})
                d["n"] += 1
                d["wins"] += outcome
                d["pnl"] += e.get("pnl", 0.0)
        indicators = {}
        for ind_id, st in self._indicator_stats.items():
            attempts = st.get("attempts", 0)
            wins = st.get("wins", 0)
            gp = st.get("gp", {}) or {}
            # Per-indicator GP-entry WR (what the dashboard's GP-Entry column
            # shows). Only populated when the indicator fired as a GP entry.
            gp_block = {}
            if gp.get("attempts"):
                gp_block = {
                    "entries": gp["attempts"],
                    "wins": gp["wins"],
                    "pnl": round(gp.get("pnl", 0.0), 2),
                }
            indicators[ind_id] = {
                "entries": attempts,
                "wins": wins,
                "pnl": round(float(st.get("pnl", 0.0)), 2),
                "exiled": st.get("exiled", False),
                "by_type": {"gp_ensemble": gp_block} if gp_block else {},
            }
        return {
            "summary": {
                "entries_total": len(self._entries),
                "exiled_indicators": len(self.get_exiled_indicators()),
                "indicators_tracked": len(indicators),
                "best_entry_type": self.best_entry_type(),
            },
            "exiled": self.get_exiled_indicators(),
            "indicators": indicators,
            "policy": {"version": 1},
            "by_entry_type": by_type,
            "by_pair": by_pair,
            "type_wr": {t: self.entry_type_wr(t) for t in VALID_ENTRY_TYPES},
        }
