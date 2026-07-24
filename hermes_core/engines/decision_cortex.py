"""Decision cortex (Session 15 / Phase 15).

Unified memory across reflection / GP / dashboard: per-type & per-indicator
win-rate, indicator exile, and condition routing. Everything persists to disk
(D2) so a restart never silently rebuilds from scratch.

Governance (blueprint ENGINE 8 / Phase 15):
  * auto-exile: an indicator with <30% WR as a GP entry after >=5 attempts is
    exiled (L36 exile filter — removed from GP candidacy).
  * exile decay: reconsider after 100 entries; reinstate if WR >= 40%.
  * wall-clock escape: reinstate after EXILE_WALL_CLOCK_S (default 7d) so an
    early soak exile streak cannot empty the ensemble for the whole run.
  * best_entry_type() always returns a known, valid type.

Persistence:
  {HERMES_STATE_ROOT}/{bot}/state/cortex/indicator_exile.json
  {HERMES_STATE_ROOT}/{bot}/state/cortex/cortex_memory.json
  Live policy (not under cortex/): {bot}/state/policy.json
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path

from hermes_core.state.atomic_json import atomic_write_json, load_json, quarantine_corrupt
from hermes_core.state.paths import cortex_dir, current_bot

# ── gates ──────────────────────────────────────────────────────────────────
EXILE_WR = 0.30  # [GUARD L36] WR below this after enough attempts -> exile
EXILE_MIN_ATTEMPTS = 5  # need >=5 GP attempts before exile can trigger
REINSTATE_WR = 0.40  # WR at/above this reinstates an exiled indicator
EXILE_DECAY_ENTRIES = 100  # reconsider exiled indicators every 100 entries
# Wall-clock escape so an early-soak exile streak cannot empty GP forever.
EXILE_WALL_CLOCK_S = int(os.getenv("EXILE_DECAY_S", str(7 * 86400)))
VALID_ENTRY_TYPES = ("mean_reversion", "gp_ensemble")
# Cap closed+open rows kept on disk (30-day soak hygiene). Opens always kept.
MEMORY_MAX_ENTRIES = 5000

# Optional test overrides (tests monkeypatch these module attributes).
CORTEX_DIR: Path | None = None
EXILE_PATH: Path | None = None
MEMORY_PATH: Path | None = None


def _cortex_paths(bot: str | None = None) -> tuple[Path, Path, Path]:
    base = CORTEX_DIR if CORTEX_DIR is not None else cortex_dir(bot or current_bot())
    exile = EXILE_PATH or (base / "indicator_exile.json")
    memory = MEMORY_PATH or (base / "cortex_memory.json")
    return base, exile, memory


def _load_exiles(bot: str | None = None) -> dict:
    _, exile_path, _ = _cortex_paths(bot)
    raw = load_json(exile_path, default={})
    return raw if isinstance(raw, dict) else {}


def _save_exiles(data: dict, bot: str | None = None) -> None:
    base, exile_path, _ = _cortex_paths(bot)
    base.mkdir(parents=True, exist_ok=True)
    atomic_write_json(exile_path, data, indent=2)


def _load_memory(bot: str | None = None) -> dict:
    """Persisted entry/outcome history (D2): survives restart + per-cycle reset.

    Corrupt files are quarantined (``*.corrupt-<ts>``) — never silently treated
    as empty then overwritten without an audit trail. Valid JSON that is not a
    dict is also quarantined (wrong-shape pollution).
    """
    _, _, memory_path = _cortex_paths(bot)
    raw = load_json(memory_path, default=None)
    if raw is None:
        return {"entries": [], "indicator_stats": {}}
    if not isinstance(raw, dict):
        quarantine_corrupt(memory_path, reason="not_dict")
        return {"entries": [], "indicator_stats": {}}
    entries = raw.get("entries")
    stats = raw.get("indicator_stats")
    return {
        "entries": entries if isinstance(entries, list) else [],
        "indicator_stats": stats if isinstance(stats, dict) else {},
    }


def _save_memory(data: dict, bot: str | None = None) -> None:
    base, _, memory_path = _cortex_paths(bot)
    base.mkdir(parents=True, exist_ok=True)
    atomic_write_json(memory_path, data)


def _trim_entries(entries: list[dict], *, max_n: int = MEMORY_MAX_ENTRIES) -> list[dict]:
    """Keep all open rows + newest closed rows within ``max_n`` total."""
    if len(entries) <= max_n:
        return entries
    opens = [e for e in entries if e.get("outcome") is None
             and e.get("type") not in ("hypothesis", "discovery")]
    closed = [e for e in entries if e.get("outcome") is not None]
    other = [e for e in entries
             if e.get("type") in ("hypothesis", "discovery")]
    budget = max(0, max_n - len(opens))
    kept_closed = closed[-budget:] if budget else []
    # Preserve relative order: trimmed closed, then opens, then other tail.
    return kept_closed + opens + other[-min(100, len(other)):]


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
        # File is source of truth for L36; sync memory flags after load.
        self._sync_exile_flags_from_file()

    def _sync_exile_flags_from_file(self) -> None:
        exiles = _load_exiles(self._bot)
        exile_ids = set(exiles.keys())
        for ind_id, st in self._indicator_stats.items():
            st["exiled"] = ind_id in exile_ids
        for ind_id in exile_ids:
            st = self._indicator_stats.setdefault(
                ind_id,
                {"attempts": 0, "wins": 0, "pnl": 0.0, "exiled": True,
                 "gp": {"attempts": 0, "wins": 0, "pnl": 0.0}},
            )
            st["exiled"] = True

    def _flush(self) -> None:
        self._entries = _trim_entries(self._entries)
        _save_memory(
            {"entries": self._entries, "indicator_stats": self._indicator_stats},
            self._bot,
        )

    def closed_outcome_count(self) -> int:
        """Closed non-partial outcomes (policy probe_interval / evidence)."""
        return sum(
            1 for e in self._entries
            if e.get("outcome") is not None
            and not e.get("partial")
            and e.get("type") not in ("hypothesis", "discovery")
        )

    # ── recording ──────────────────────────────────────────────────────────
    def record_entry(self, pair: str, entry_type: str) -> None:
        self._entries.append({"pair": pair, "type": entry_type, "outcome": None})
        self._flush()

    def record_outcome(
        self,
        pair: str,
        entry_type: str,
        pnl: float,
        *,
        mfe_pct: float | None = None,
        mae_pct: float | None = None,
        giveback_pct: float | None = None,
        giveback_frac: float | None = None,
        mfe_capture: float | None = None,
        partial: bool = False,
    ) -> None:
        try:
            _pnl = float(pnl)
        except (TypeError, ValueError):
            _pnl = 0.0
        # Flat PnL is neither win nor loss for WR / policy math.
        if abs(_pnl) < 1e-6:
            _outcome: int | str = "flat"
        else:
            _outcome = 1 if _pnl > 0 else 0
        row = {
            "pair": pair,
            "type": entry_type,
            "outcome": _outcome,
            "pnl": _pnl,
        }
        if partial:
            row["partial"] = True
        if mfe_pct is not None:
            row["mfe_pct"] = float(mfe_pct)
        if mae_pct is not None:
            row["mae_pct"] = float(mae_pct)
        if giveback_pct is not None:
            row["giveback_pct"] = float(giveback_pct)
        if giveback_frac is not None:
            row["giveback_frac"] = float(giveback_frac)
        if mfe_capture is not None:
            row["mfe_capture"] = float(mfe_capture)
        elif mfe_pct is not None and float(mfe_pct) > 1e-9:
            with contextlib.suppress(TypeError, ValueError, ZeroDivisionError):
                row["mfe_capture"] = round(float(pnl) / float(mfe_pct), 4)
        # Fill matching open row (newest first) so entries_open does not grow
        # forever. Partials never close an open row — full close does.
        # Shadow opens are credited as gp_ensemble on close (same GP evidence).
        def _open_matches(open_type: str | None, close_type: str) -> bool:
            if open_type == close_type:
                return True
            gpish = {"gp_ensemble", "shadow"}
            return (open_type in gpish) and (close_type in gpish)

        filled = False
        if not partial:
            for i in range(len(self._entries) - 1, -1, -1):
                e = self._entries[i]
                if (
                    e.get("outcome") is None
                    and e.get("pair") == pair
                    and _open_matches(e.get("type"), entry_type)
                    and e.get("type") not in ("hypothesis", "discovery")
                ):
                    e.update(row)
                    filled = True
                    break
        if not filled:
            self._entries.append(row)
        self._flush()

    def record_hypothesis(self, pair: str, text: str) -> None:
        self._entries.append({"pair": pair, "type": "hypothesis", "text": text})
        self._flush()

    def record_discovery(self, pair: str, ind_id: str) -> None:
        self._entries.append({"pair": pair, "type": "discovery", "ind": ind_id})
        self._flush()

    # ── per-type win-rate ───────────────────────────────────────────────────
    def entry_type_wr(self, entry_type: str, pair: str | None = None) -> float | None:
        """Win-rate for ``entry_type``, optionally scoped to one ``pair``.

        Per-pair WRs stop a bad pair from benching GP (or MR) fleet-wide.
        Partial closes are excluded (full close is the policy/sizing truth).
        """
        outcomes = [
            e
            for e in self._entries
            if e.get("type") == entry_type
            and e.get("outcome") is not None
            and e.get("outcome") != "flat"
            and not e.get("partial")
            and (pair is None or e.get("pair") == pair)
        ]
        if not outcomes:
            return None
        wins = sum(1 for e in outcomes if e.get("outcome") in (1, True))
        return wins / len(outcomes)

    def evidence_n(self, pair: str, entry_type: str) -> int:
        """Closed-outcome count for (pair, entry_type) — HIF Phase-1 probe gate.

        Only full (non-partial) rows with a recorded outcome count.
        """
        return sum(
            1
            for e in self._entries
            if e.get("pair") == pair
            and e.get("type") == entry_type
            and e.get("outcome") is not None
            and not e.get("partial")
        )

    def edge_stats(self, pair: str, entry_type: str) -> dict:
        """Wins/losses + avg win/loss PnL for Kelly (HIF Phase-5).

        Missing pnl on an outcome is ignored for averages but still counts W/L.
        Partials excluded.
        """
        outcomes = [
            e
            for e in self._entries
            if e.get("pair") == pair
            and e.get("type") == entry_type
            and e.get("outcome") is not None
            and e.get("outcome") != "flat"
            and not e.get("partial")
        ]
        wins = sum(1 for e in outcomes if e.get("outcome") in (1, True))
        losses = len(outcomes) - wins
        win_pnls: list[float] = []
        loss_pnls: list[float] = []
        for e in outcomes:
            if "pnl" not in e:
                continue
            try:
                pnl = float(e["pnl"])
            except (TypeError, ValueError):
                continue
            if e.get("outcome") in (1, True):
                win_pnls.append(pnl)
            else:
                loss_pnls.append(abs(pnl))
        avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else None
        avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else None
        return {
            "wins": wins,
            "losses": losses,
            "n": len(outcomes),
            "avg_win": avg_win,
            "avg_loss": avg_loss,
        }

    def excursion_stats(self, pair: str, entry_type: str) -> dict:
        """Average MFE/MAE/giveback/capture for closed outcomes with excursions.

        Prefer this over exit-PnL WR when many closes are time_exit — capture and
        giveback measure whether the entry had real edge, not whether price
        drifted for N hours.
        """
        rows = [
            e
            for e in self._entries
            if e.get("pair") == pair
            and e.get("type") == entry_type
            and e.get("outcome") is not None
            and e.get("mfe_pct") is not None
        ]
        empty = {
            "n": 0,
            "avg_mfe": None,
            "avg_mae": None,
            "avg_giveback": None,
            "avg_giveback_frac": None,
            "avg_mfe_capture": None,
        }
        if not rows:
            return empty
        mfes, maes, gbs, gfs, caps = [], [], [], [], []
        for e in rows:
            try:
                mfe = float(e["mfe_pct"])
                mfes.append(mfe)
            except (TypeError, ValueError, KeyError):
                mfe = None
            try:
                if e.get("mae_pct") is not None:
                    maes.append(float(e["mae_pct"]))
            except (TypeError, ValueError):
                pass
            try:
                if e.get("giveback_pct") is not None:
                    gbs.append(float(e["giveback_pct"]))
            except (TypeError, ValueError):
                pass
            try:
                if e.get("giveback_frac") is not None:
                    gfs.append(float(e["giveback_frac"]))
            except (TypeError, ValueError):
                pass
            # Capture = pnl / mfe (stored or derived).
            try:
                if e.get("mfe_capture") is not None:
                    caps.append(float(e["mfe_capture"]))
                elif mfe is not None and mfe > 1e-9 and e.get("pnl") is not None:
                    caps.append(float(e["pnl"]) / mfe)
            except (TypeError, ValueError):
                pass
        return {
            "n": len(rows),
            "avg_mfe": round(sum(mfes) / len(mfes), 4) if mfes else None,
            "avg_mae": round(sum(maes) / len(maes), 4) if maes else None,
            "avg_giveback": round(sum(gbs) / len(gbs), 4) if gbs else None,
            "avg_giveback_frac": (round(sum(gfs) / len(gfs), 4) if gfs else None),
            "avg_mfe_capture": (round(sum(caps) / len(caps), 4) if caps else None),
        }

    def excursion_scoreboard(self) -> dict:
        """Fleet-level excursion scoreboard (edge quality, not time-exit PnL)."""
        rows = [
            e
            for e in self._entries
            if e.get("outcome") is not None and e.get("mfe_pct") is not None
        ]
        if not rows:
            return {
                "n": 0,
                "avg_mfe": None,
                "avg_mae": None,
                "avg_giveback_frac": None,
                "avg_mfe_capture": None,
                "by_entry_type": {},
                "by_pair": {},
            }
        # Aggregate via existing per-key helper.
        by_type: dict[str, dict] = {}
        by_pair: dict[str, dict] = {}
        seen_types: set[str] = set()
        seen_pairs: set[str] = set()
        for e in rows:
            t, p = e.get("type"), e.get("pair")
            if t:
                seen_types.add(t)
            if p:
                seen_pairs.add(p)
        for t in seen_types:
            # Use any pair that has this type — recompute from rows for type-wide.
            type_rows = [e for e in rows if e.get("type") == t]
            by_type[t] = self._excursion_from_rows(type_rows)
        for p in seen_pairs:
            pair_rows = [e for e in rows if e.get("pair") == p]
            by_pair[p] = self._excursion_from_rows(pair_rows)
        fleet = self._excursion_from_rows(rows)
        return {
            **fleet,
            "by_entry_type": by_type,
            "by_pair": by_pair,
        }

    @staticmethod
    def _excursion_from_rows(rows: list[dict]) -> dict:
        if not rows:
            return {
                "n": 0,
                "avg_mfe": None,
                "avg_mae": None,
                "avg_giveback_frac": None,
                "avg_mfe_capture": None,
            }
        mfes, maes, gfs, caps = [], [], [], []
        for e in rows:
            try:
                mfe = float(e["mfe_pct"])
                mfes.append(mfe)
            except (TypeError, ValueError, KeyError):
                mfe = None
            try:
                if e.get("mae_pct") is not None:
                    maes.append(float(e["mae_pct"]))
            except (TypeError, ValueError):
                pass
            try:
                if e.get("giveback_frac") is not None:
                    gfs.append(float(e["giveback_frac"]))
            except (TypeError, ValueError):
                pass
            try:
                if e.get("mfe_capture") is not None:
                    caps.append(float(e["mfe_capture"]))
                elif mfe is not None and mfe > 1e-9 and e.get("pnl") is not None:
                    caps.append(float(e["pnl"]) / mfe)
            except (TypeError, ValueError):
                pass
        return {
            "n": len(rows),
            "avg_mfe": round(sum(mfes) / len(mfes), 4) if mfes else None,
            "avg_mae": round(sum(maes) / len(maes), 4) if maes else None,
            "avg_giveback_frac": (round(sum(gfs) / len(gfs), 4) if gfs else None),
            "avg_mfe_capture": (round(sum(caps) / len(caps), 4) if caps else None),
        }

    # ── best entry type (router) ────────────────────────────────────────────
    def best_entry_type(self, pair: str | None = None) -> str:
        """Return the entry type with the higher known win-rate, falling back to
        a valid default. Never returns an unknown type.

        When ``pair`` is set, WRs are scoped to that pair (a bleeding pair must
        not set the fleet-wide "best" style).
        """
        wrs = {t: self.entry_type_wr(t, pair=pair) for t in VALID_ENTRY_TYPES}
        known = {t: w for t, w in wrs.items() if w is not None}
        if not known:
            return "mean_reversion"  # safe default when no data yet
        return max(known, key=known.get)

    # ── per-indicator exile system ──────────────────────────────────────────
    def record_indicator_outcome(
        self, ind_id: str, pnl: float, entry_type: str | None = None
    ) -> None:
        """Track a GP indicator's outcome; auto-exile / reinstate per gates.

        Exile/reinstate use the GP-entry sub-block (B9), not blended overall WR.
        `entry_type` (optional) lets us separate GP-ensemble credit from any
        other credit so the dashboard can show per-indicator GP-entry WR (B9).
        """
        st = self._indicator_stats.setdefault(
            ind_id,
            {
                "attempts": 0,
                "wins": 0,
                "pnl": 0.0,
                "exiled": False,
                "gp": {"attempts": 0, "wins": 0, "pnl": 0.0},
            },
        )
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
        # L36 gates on GP attempts only (doc contract); fall back to overall
        # only when no GP sub-block exists yet.
        gp = st.get("gp") or {}
        gate_attempts = int(gp.get("attempts") or 0) or int(st.get("attempts") or 0)
        gate_wins = int(gp.get("wins") or 0) if gp.get("attempts") else int(st.get("wins") or 0)
        wr = (gate_wins / gate_attempts) if gate_attempts else 0.0
        # File is source of truth for whether currently exiled.
        exiles = _load_exiles(self._bot)
        currently = ind_id in exiles or bool(st.get("exiled"))
        st["exiled"] = currently
        if currently:
            # decay reconsider: GP cadence OR wall-clock escape
            aged = False
            rec = exiles.get(ind_id) or {}
            ts = rec.get("exiled_at")
            if ts is not None and (time.time() - float(ts)) >= max(1, int(EXILE_WALL_CLOCK_S)):
                aged = True
            if (
                gate_attempts > 0
                and gate_attempts % EXILE_DECAY_ENTRIES == 0
                and wr >= REINSTATE_WR
            ) or aged:
                st["exiled"] = False
                exiles.pop(ind_id, None)
        elif gate_attempts >= EXILE_MIN_ATTEMPTS and wr < EXILE_WR:
            st["exiled"] = True
            exiles[ind_id] = {
                "exiled_at_attempts": gate_attempts,
                "wr": round(wr, 3),
                "gp": True,
                "exiled_at": time.time(),
            }
        _save_exiles(exiles, self._bot)
        self._flush()

    def is_indicator_exiled(self, ind_id: str) -> bool:
        exiles = _load_exiles(self._bot)
        rec = exiles.get(ind_id)
        if not rec:
            return False
        ts = rec.get("exiled_at")
        if ts is not None and (time.time() - float(ts)) >= max(1, int(EXILE_WALL_CLOCK_S)):
            # Wall-clock reinstate without waiting for another outcome tick.
            exiles.pop(ind_id, None)
            _save_exiles(exiles, self._bot)
            st = self._indicator_stats.get(ind_id)
            if st is not None:
                st["exiled"] = False
                self._flush()
            return False
        return True

    def exile_indicator(self, ind_id: str) -> None:
        exiles = _load_exiles(self._bot)
        prev = exiles.get(ind_id) or {}
        if not isinstance(prev, dict):
            prev = {}
        prev.setdefault("manual", True)
        prev.setdefault("exiled_at", time.time())
        exiles[ind_id] = prev
        _save_exiles(exiles, self._bot)
        st = self._indicator_stats.setdefault(
            ind_id, {"attempts": 0, "wins": 0, "pnl": 0.0, "exiled": True,
                     "gp": {"attempts": 0, "wins": 0, "pnl": 0.0}},
        )
        st["exiled"] = True
        self._flush()

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
            if outcome is None or outcome == "flat" or e.get("partial"):
                continue
            t = e.get("type")
            p = e.get("pair")
            win = 1 if outcome in (1, True) else 0
            if t:
                d = by_type.setdefault(t, {"n": 0, "wins": 0, "pnl": 0.0})
                d["n"] += 1
                d["wins"] += win
                d["pnl"] += e.get("pnl", 0.0)
            if p:
                d = by_pair.setdefault(p, {"n": 0, "wins": 0, "pnl": 0.0})
                d["n"] += 1
                d["wins"] += win
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
        # HIF Phase-1: per (pair, entry_type) closed evidence for probe/full UI.
        from hermes_core.engines.risk import PROBE_EVIDENCE_MIN, evidence_state_for

        probe_by_key: dict[str, dict] = {}
        for e in self._entries:
            if e.get("outcome") is None or e.get("partial"):
                continue
            p, t = e.get("pair"), e.get("type")
            if not p or not t or t in ("hypothesis", "discovery"):
                continue
            key = f"{p}|{t}"
            bucket = probe_by_key.setdefault(key, {"pair": p, "entry_type": t, "n": 0})
            bucket["n"] += 1
        for bucket in probe_by_key.values():
            n = int(bucket["n"])
            bucket["evidence_n"] = n
            # enabled=True here: state is about evidence thickness only; the Live
            # badge still respects PROBE_SIZING via open-trade size_mode.
            bucket["evidence_state"] = evidence_state_for(
                n,
                enabled=True,
                evidence_min=PROBE_EVIDENCE_MIN,
            )
            bucket["size_mode_if_enabled"] = (
                "probe" if bucket["evidence_state"] == "thin" else "full"
            )

        return {
            "summary": {
                # Closed outcomes only — UI labels this "completed".
                "entries_total": sum(
                    1 for e in self._entries
                    if e.get("outcome") is not None and not e.get("partial")
                ),
                "entries_open": sum(
                    1 for e in self._entries
                    if e.get("outcome") is None
                    and e.get("type") not in ("hypothesis", "discovery")
                ),
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
            # Excursion scoreboard — prefer over exit-PnL WR while time_exit dominates.
            "excursion": self.excursion_scoreboard(),
            "probe_evidence": {
                "threshold": PROBE_EVIDENCE_MIN,
                "by_key": probe_by_key,
            },
            "gates": {
                "exile": f"GP indicator WR < {EXILE_WR:.0%} after ≥{EXILE_MIN_ATTEMPTS} GP attempts → exiled",
                "reinstate": f"After {EXILE_DECAY_ENTRIES} GP entries, reinstate if WR ≥ {REINSTATE_WR:.0%}",
                "best_entry": "Router picks the entry type with the higher known per-pair win-rate",
                "probe": (
                    f"HIF Phase-1: when PROBE_SIZING=1 and closed evidence "
                    f"< {PROBE_EVIDENCE_MIN} for (pair, entry_type) → 25% probe size. "
                    f"Missing cortex evidence fails open to full size."
                ),
                "priority_discovery": (
                    "Dashboard/ops signal only: ≥2 exiled indicators — invent "
                    "scheduling is not auto-accelerated from this flag yet"
                ),
                "excursion": (
                    "Edge quality: avg MFE / giveback_frac / mfe_capture "
                    "(pnl÷peak MFE). Prefer over time_exit PnL WR."
                ),
            },
        }
