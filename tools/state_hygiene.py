"""State-root hygiene for 30-day paper soak readiness.

Quarantines legacy ``state/`` runtime files, removes live-price stubs and
stub heartbeats under ``bots/*/state``, deletes the ``goldbot/`` promote-gate
orphan, bootstraps canonical trade books, resets reflection latches, and
optionally rebuilds cortex/policy from post-scrub trades only.

Usage:
  python tools/state_hygiene.py
  python tools/state_hygiene.py --rebuild-learning
  python tools/state_hygiene.py --rotate-skips
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOTS = ("forex", "gold", "crypto")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def quarantine_legacy_state() -> list[str]:
    """Move legacy root ``state/`` runtime artifacts into archive."""
    actions: list[str] = []
    legacy = ROOT / "state"
    if not legacy.exists():
        return actions
    arch = legacy / "archive" / f"quarantine_{_stamp()}"
    arch.mkdir(parents=True, exist_ok=True)
    for name in (
        "trades.jsonl", "skips.jsonl", "heartbeat.json", "policy.json",
        "flatline_log.jsonl", "dashboard.db",
    ):
        src = legacy / name
        if src.exists():
            dst = arch / name
            shutil.move(str(src), str(dst))
            actions.append(f"quarantined {src} -> {dst}")
    return actions


def remove_stubs() -> list[str]:
    actions: list[str] = []
    for bot in BOTS:
        bdir = ROOT / "bots" / bot / "state"
        for p in bdir.glob("live_prices_*.json"):
            p.unlink(missing_ok=True)
            actions.append(f"removed stub {p}")
        hb = bdir / "heartbeat.json"
        if hb.exists():
            try:
                data = json.loads(hb.read_text(encoding="utf-8"))
                ts = str(data.get("ts") or "")
                if ts.startswith("2026-07-17") or data.get("last_cycle") == 42:
                    hb.unlink(missing_ok=True)
                    actions.append(f"removed stub heartbeat {hb}")
            except (OSError, json.JSONDecodeError):
                pass
    return actions


def remove_goldbot_orphan() -> list[str]:
    actions: list[str] = []
    orphan = ROOT / "goldbot"
    if orphan.exists():
        shutil.rmtree(orphan)
        actions.append(f"removed orphan {orphan}")
    return actions


def bootstrap_canonical() -> list[str]:
    from hermes_core.engines.soak_controls import (
        ensure_state_files,
        reset_reflection_latches,
    )
    actions: list[str] = []
    for bot in BOTS:
        d = ensure_state_files(bot)
        actions.append(f"ensured state files under {d}")
        reset_reflection_latches(bot)
        actions.append(f"reset reflection latches for {bot}")
    return actions


def purge_seed_discovered() -> list[str]:
    """Remove dashboard seed fixtures (ta.*/mom) from runtime discovered dirs."""
    actions: list[str] = []
    for bot in BOTS:
        d = ROOT / bot / "state" / "discovered"
        if not d.exists():
            continue
        for p in d.rglob("*.json"):
            if p.name.startswith("_"):
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            inds = data if isinstance(data, list) else data.get("indicators") or []
            if not isinstance(inds, list):
                continue
            dirty = False
            for ind in inds:
                expr = str(ind.get("expr") or ind.get("expr_str") or ind.get("name") or "")
                if expr.startswith("ta.") or "mom(close" in expr:
                    dirty = True
                    break
                if ind.get("horizon") is None or ind.get("interval") is None:
                    if ind.get("source") in ("seed", "dashboard", None):
                        dirty = True
                        break
            if dirty:
                arch = d / "archive"
                arch.mkdir(parents=True, exist_ok=True)
                dst = arch / f"{p.stem}_seed_{_stamp()}{p.suffix}"
                shutil.move(str(p), str(dst))
                actions.append(f"archived seed discovered {p} -> {dst}")
    return actions


def rebuild_learning(bot: str = "forex") -> list[str]:
    """Rebuild cortex + neutral policy from post-scrub trades only."""
    actions: list[str] = []
    state = ROOT / bot / "state"
    trades = state / "trades.jsonl"
    cortex_path = state / "cortex" / "cortex_memory.json"
    cortex_path.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    if trades.exists():
        for line in trades.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Skip stub ±1.0 toy PnLs and missing exits.
            pnl = rec.get("pnl_pct")
            if pnl is None:
                continue
            try:
                pnl_f = float(pnl)
            except (TypeError, ValueError):
                continue
            if abs(pnl_f) == 1.0 and rec.get("exit_reason") in (None, "tp", "sl"):
                # keep real ±1.0 if hold is long enough
                if int(rec.get("hold_cycles") or 0) < 2:
                    continue
            entries.append({
                "pair": rec.get("pair"),
                "type": rec.get("entry_type") or rec.get("type") or "mean_reversion",
                "outcome": 1 if pnl_f > 0 else 0,
                "pnl": pnl_f,
            })
    cortex_path.write_text(
        json.dumps({"entries": entries[-2000:], "indicator_stats": {}}, indent=2),
        encoding="utf-8",
    )
    actions.append(f"rebuilt cortex n={len(entries)} -> {cortex_path}")

    # Soft-neutral policy (no hard gp_ensemble suppression).
    policy = {
        "suppressions": {},
        "priority_discovery": False,
        "probe_interval": 50,
        "rollback": False,
        "soft_weights": True,
        "allocation": {},
        "rebuilt_from_scrub": True,
        "ts": time.time(),
    }
    (state / "policy.json").write_text(json.dumps(policy, indent=2), encoding="utf-8")
    actions.append(f"wrote neutral policy -> {state / 'policy.json'}")
    return actions


def rotate_skips(max_keep: int = 5000) -> list[str]:
    actions: list[str] = []
    for bot in BOTS:
        path = ROOT / bot / "state" / "skips.jsonl"
        if not path.exists():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) <= max_keep:
            continue
        arch = path.parent / "archive"
        arch.mkdir(parents=True, exist_ok=True)
        out = arch / f"skips_rotated_{_stamp()}.jsonl"
        # Aggregate counters for the rotated chunk.
        reasons: Counter[str] = Counter()
        for line in lines[:-max_keep]:
            try:
                rec = json.loads(line)
                reasons[str(rec.get("reason") or "?")] += 1
            except json.JSONDecodeError:
                reasons["?"] += 1
        meta = arch / f"skips_counts_{_stamp()}.json"
        meta.write_text(json.dumps(dict(reasons.most_common()), indent=2), encoding="utf-8")
        out.write_text("\n".join(lines[:-max_keep]) + "\n", encoding="utf-8")
        path.write_text("\n".join(lines[-max_keep:]) + "\n", encoding="utf-8")
        actions.append(f"rotated {path}: kept={max_keep} archived={out.name}")
    return actions


def set_soak_sessions_24h() -> list[str]:
    """Set runtime (+ seed) forex sessions to 24h for paper soak."""
    actions: list[str] = []
    import yaml  # type: ignore
    targets = []
    for base in (ROOT / "forex" / "state" / "strategies",
                 ROOT / "bots" / "forex" / "state" / "strategies"):
        if base.exists():
            targets.extend(base.glob("*.yaml"))
    for path in targets:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        entry = data.setdefault("entry", {})
        if not isinstance(entry, dict):
            continue
        old = entry.get("session_filter")
        if old == "24h":
            continue
        entry["session_filter"] = "24h"
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        actions.append(f"{path}: session_filter {old} -> 24h")
    return actions


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild-learning", action="store_true")
    ap.add_argument("--rotate-skips", action="store_true")
    ap.add_argument("--sessions-24h", action="store_true", default=True)
    ap.add_argument("--no-sessions-24h", action="store_true")
    args = ap.parse_args()

    actions: list[str] = []
    actions += quarantine_legacy_state()
    actions += remove_stubs()
    actions += remove_goldbot_orphan()
    actions += bootstrap_canonical()
    actions += purge_seed_discovered()
    if not args.no_sessions_24h and args.sessions_24h:
        actions += set_soak_sessions_24h()
    if args.rebuild_learning:
        for bot in BOTS:
            actions += rebuild_learning(bot)
    if args.rotate_skips:
        actions += rotate_skips()

    for a in actions:
        print(a)
    print(f"done actions={len(actions)}")


if __name__ == "__main__":
    main()
