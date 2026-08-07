"""Durable entry-taken log + historical trade truth backfill for reflection."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from hermes_core.engines.size_stamp import (
    ensure_mfe_path,
    infer_closed_size_fields,
)


def _state_dir(bot: str | None) -> Path:
    from hermes_core.state.paths import bot_state_dir

    return bot_state_dir(bot or "forex")


def entry_taken_path(bot: str | None) -> Path:
    return _state_dir(bot) / "entry_taken.jsonl"


def append_entry_taken(bot: str | None, row: dict) -> None:
    """Append one taken-entry row (open and/or close echo)."""
    path = entry_taken_path(bot)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = dict(row)
        rec.setdefault("ts", time.time())
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    except OSError:
        return []
    return out


def _f(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def join_probe_open_from_shadow(
    trade: dict,
    shadow_rows: list[dict],
    *,
    mark_tol_pct: float = 0.15,
    ts_tol_s: float = 7200.0,
) -> dict:
    """Attach entry_decision=probe when a probe_open shadow matches this close."""
    out = dict(trade)
    if out.get("entry_decision"):
        out.setdefault("decision_source", out.get("decision_source") or "stamped")
        return out
    pair = str(out.get("pair") or "")
    mid = _f(out.get("entry_mid") if out.get("entry_mid") is not None else out.get("entry_price"))
    # Prefer entry_ts epoch if ISO; else cycle match loosely via ts on trade
    entry_ts = out.get("entry_ts")
    entry_epoch = None
    if isinstance(entry_ts, (int, float)):
        entry_epoch = float(entry_ts)
    elif isinstance(entry_ts, str) and entry_ts:
        with __import__("contextlib").suppress(Exception):
            from datetime import datetime

            entry_epoch = datetime.fromisoformat(entry_ts.replace("Z", "+00:00")).timestamp()

    best = None
    best_score = 1e18
    for row in shadow_rows:
        if str(row.get("reason") or "") not in {"probe_open", "taken_open"}:
            continue
        if str(row.get("pair") or "") != pair:
            continue
        if str(row.get("entry_type") or "") and str(out.get("entry_type") or ""):
            if str(row.get("entry_type")) != str(out.get("entry_type")):
                continue
        mark = _f(row.get("mark"))
        if mid > 0 and mark > 0:
            dist = abs(mark - mid) / mid * 100.0
            if dist > mark_tol_pct:
                continue
        else:
            dist = 0.0
        score = dist
        if entry_epoch is not None and row.get("ts") is not None:
            dt = abs(float(row["ts"]) - entry_epoch)
            if dt > ts_tol_s:
                continue
            score += dt / 1000.0
        if score < best_score:
            best_score = score
            best = row
    if best is not None:
        out["entry_decision"] = "probe"
        out["decision_source"] = "shadow_join"
        if not out.get("size_reason") or out.get("size_stamp_inferred"):
            out["size_reason"] = "sentient_probe"
        if not out.get("size_mode"):
            out["size_mode"] = "probe"
    else:
        out.setdefault("decision_source", "unknown")
    return out


def enrich_closed_trade(
    trade: dict,
    *,
    strategy: dict | None = None,
    pair_max_size: float | None = None,
    shadow_rows: list[dict] | None = None,
) -> dict:
    """Shadow-join → size infer → ensure mfe_path. Never invents take."""
    out = dict(trade) if isinstance(trade, dict) else {}
    if shadow_rows:
        out = join_probe_open_from_shadow(out, shadow_rows)
    out = infer_closed_size_fields(
        out, strategy=strategy, pair_max_size=pair_max_size
    )
    out = ensure_mfe_path(out)
    return out


def enrich_closed_trades(
    trades: list[dict],
    *,
    bot: str | None = None,
    strategy: dict | None = None,
) -> list[dict]:
    """Enrich a batch in memory (reflection path)."""
    if not trades:
        return []
    shadow: list[dict] = []
    if bot:
        with __import__("contextlib").suppress(Exception):
            from hermes_core.engines.sentient_entry import _state_paths

            _, _, sp = _state_paths(bot)
            shadow = _load_jsonl(sp)
        with __import__("contextlib").suppress(Exception):
            shadow.extend(_load_jsonl(entry_taken_path(bot)))
    sizes = [_f(t.get("size")) for t in trades if isinstance(t, dict)]
    pair_max = max(sizes) if sizes else None
    return [
        enrich_closed_trade(
            t,
            strategy=strategy,
            pair_max_size=pair_max,
            shadow_rows=shadow,
        )
        for t in trades
        if isinstance(t, dict)
    ]


def backfill_trades_jsonl(bot: str, pair: str | None = None, *, strategy: dict | None = None) -> int:
    """Persist inferred stamps onto trades.jsonl. Returns n rows rewritten."""
    from hermes_core.engines.trades_cache import invalidate
    from hermes_core.state.paths import bot_state_dir

    path = bot_state_dir(bot) / "trades.jsonl"
    rows = _load_jsonl(path)
    if not rows:
        return 0
    shadow: list[dict] = []
    with __import__("contextlib").suppress(Exception):
        from hermes_core.engines.sentient_entry import _state_paths

        _, _, sp = _state_paths(bot)
        shadow = _load_jsonl(sp)
    with __import__("contextlib").suppress(Exception):
        shadow.extend(_load_jsonl(entry_taken_path(bot)))

    # pair → max size
    max_by_pair: dict[str, float] = {}
    for r in rows:
        p = str(r.get("pair") or "")
        max_by_pair[p] = max(max_by_pair.get(p, 0.0), _f(r.get("size")))

    changed = 0
    out_lines: list[str] = []
    for r in rows:
        p = str(r.get("pair") or "")
        if pair and p != pair:
            out_lines.append(json.dumps(r, default=str))
            continue
        need = (
            not r.get("size_mode")
            or not r.get("mfe_path")
            or (not r.get("entry_decision") and r.get("decision_source") != "unknown")
        )
        if not need and r.get("size_mode") and (r.get("mfe_path") or r.get("mfe_path_synthetic")):
            out_lines.append(json.dumps(r, default=str))
            continue
        enriched = enrich_closed_trade(
            r,
            strategy=strategy,
            pair_max_size=max_by_pair.get(p),
            shadow_rows=shadow,
        )
        if enriched != r:
            changed += 1
        out_lines.append(json.dumps(enriched, default=str))
    if changed:
        try:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
            tmp.replace(path)
            invalidate(bot)
        except OSError:
            return 0
    return changed
