"""Quarantine seed cortex stubs + rebuild cortex/policy from clean trades.

Soak SOP (#5, #12, #13, #19):
  1. Quarantine bots/*/state/cortex/{indicator_exile,indicator_tracker,policy}.json
     fixtures (stale_macd, dashboard fake policy) so they never land on the volume.
  2. Rebuild {HERMES_STATE_ROOT}/{bot}/state/cortex/cortex_memory.json from
     canonical trades.jsonl (skip fixtures / ±1.0 stub-only rows).
  3. Clear indicator_exile.json (re-earn exile from live evidence).
  4. Recompute {bot}/state/policy.json via PolicyEngine.

Usage:
  uv run python tools/rebuild_cortex.py              # all bots
  uv run python tools/rebuild_cortex.py forex gold
  uv run python tools/rebuild_cortex.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

BOTS = ("forex", "gold", "crypto", "btc")
SEED_CORTEX_FILES = (
    "indicator_exile.json",
    "indicator_tracker.json",
    "policy.json",
)

# Same fixture IDs as tools/clean_trades.py
FIXTURE_IDS = {
    "t_forex_1", "t_forex_2", "t_gold_1", "t_gold_2",
    "t_crypto_1", "t_crypto_2", "x", "forex:EUR/USD:1",
}


def _state_root() -> Path:
    import os
    env = os.getenv("HERMES_STATE_ROOT")
    if env:
        return Path(env)
    return ROOT


def quarantine_seed_stubs(*, dry_run: bool = False) -> list[str]:
    """Move image-seed cortex fixtures under bots/*/state/archive/."""
    moved: list[str] = []
    ts = time.strftime("%Y%m%d_%H%M%S")
    for bot in BOTS:
        seed_dir = ROOT / "bots" / bot / "state" / "cortex"
        if not seed_dir.is_dir():
            continue
        archive = ROOT / "bots" / bot / "state" / "archive" / f"cortex_seed_{ts}"
        for name in SEED_CORTEX_FILES:
            src = seed_dir / name
            if not src.exists():
                continue
            dest = archive / name
            moved.append(f"{bot}:{name}")
            if dry_run:
                continue
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        # Leave a clean empty exile so accidental copies are harmless
        if not dry_run:
            seed_dir.mkdir(parents=True, exist_ok=True)
            (seed_dir / "indicator_exile.json").write_text("{}\n", encoding="utf-8")
            # Drop obsolete tracker/policy stubs — live policy is state/policy.json
            for gone in ("indicator_tracker.json", "policy.json"):
                p = seed_dir / gone
                if p.exists():
                    p.unlink()
    return moved


def _is_stub_trade(rec: dict) -> bool:
    tid = str(rec.get("id") or "")
    if tid in FIXTURE_IDS or tid.startswith("t_"):
        return True
    try:
        pnl = float(rec.get("pnl_pct", rec.get("pnl", 0)) or 0)
    except (TypeError, ValueError):
        return True
    # Exact ±1.0 with no real hold / price often means unit-test dumps
    if pnl in (1.0, -1.0) and not rec.get("exit_ts"):
        return True
    return False


def _entry_type(rec: dict) -> str:
    et = str(rec.get("entry_type") or rec.get("strategy_version") or "mean_reversion")
    if et in ("shadow", "gp_ensemble"):
        return "gp_ensemble"
    if et in ("mean_reversion", "rsi_momentum"):
        return et if et == "mean_reversion" else "mean_reversion"
    return "mean_reversion"


def rebuild_bot(bot: str, *, dry_run: bool = False) -> dict:
    """Rebuild cortex memory + clear exile + recompute policy for one bot."""
    import os
    os.environ["HERMES_BOT_NAME"] = bot

    from hermes_core.engines.decision_cortex import Cortex
    from hermes_core.engines.policy_engine import PolicyEngine
    from hermes_core.state.atomic_json import atomic_write_json
    from hermes_core.state.paths import bot_state_dir, cortex_dir, policy_path

    sdir = bot_state_dir(bot)
    trades_path = sdir / "trades.jsonl"
    cdir = cortex_dir(bot)
    mem_path = cdir / "cortex_memory.json"
    exile_path = cdir / "indicator_exile.json"

    kept = 0
    skipped = 0
    entries: list[dict] = []
    ind_stats: dict[str, dict] = {}

    if trades_path.exists():
        for line in trades_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(rec, dict) or _is_stub_trade(rec):
                skipped += 1
                continue
            if rec.get("partial"):
                skipped += 1
                continue
            pair = rec.get("pair")
            if not pair:
                skipped += 1
                continue
            try:
                pnl = float(rec.get("pnl_pct", rec.get("pnl", 0)) or 0)
            except (TypeError, ValueError):
                skipped += 1
                continue
            et = _entry_type(rec)
            row = {
                "pair": pair,
                "type": et,
                "outcome": 1 if pnl > 0 else 0,
                "pnl": pnl,
            }
            for k in ("mfe_pct", "mae_pct", "giveback_pct", "giveback_frac", "mfe_capture"):
                if rec.get(k) is not None:
                    try:
                        row[k] = float(rec[k])
                    except (TypeError, ValueError):
                        pass
            entries.append(row)
            kept += 1
            # Credit GP indicators if present on the trade record
            if et == "gp_ensemble":
                for ind_id in rec.get("gp_indicators") or []:
                    if not isinstance(ind_id, str):
                        continue
                    st = ind_stats.setdefault(
                        ind_id,
                        {"attempts": 0, "wins": 0, "pnl": 0.0, "exiled": False,
                         "gp": {"attempts": 0, "wins": 0, "pnl": 0.0}},
                    )
                    st["attempts"] += 1
                    st["pnl"] = float(st["pnl"]) + pnl
                    if pnl > 0:
                        st["wins"] += 1
                    gp = st["gp"]
                    gp["attempts"] += 1
                    gp["pnl"] = float(gp["pnl"]) + pnl
                    if pnl > 0:
                        gp["wins"] += 1

    summary = {
        "bot": bot,
        "kept": kept,
        "skipped": skipped,
        "memory": str(mem_path),
        "policy": str(policy_path(bot)),
    }
    if dry_run:
        summary["dry_run"] = True
        return summary

    cdir.mkdir(parents=True, exist_ok=True)
    # Archive prior memory if present
    if mem_path.exists():
        arch = cdir / f"cortex_memory.json.bak-{int(time.time())}"
        shutil.copy2(mem_path, arch)
    atomic_write_json(mem_path, {"entries": entries, "indicator_stats": ind_stats})
    atomic_write_json(exile_path, {}, indent=2)

    # Drop runtime stub tracker / cortex policy if present
    for stub in ("indicator_tracker.json", "policy.json"):
        p = cdir / stub
        if p.exists():
            dest = cdir / f"{stub}.quarantine-{int(time.time())}"
            p.replace(dest)

    # Recompute live policy from rebuilt cortex
    pairs: list[str] = []
    try:
        from hermes_core.config import load_config
        pairs = list(load_config(bot).get("pairs") or [])
    except Exception:
        pairs = sorted({e["pair"] for e in entries})
    cx = Cortex(bot=bot)
    if pairs:
        PolicyEngine().evaluate(0, pairs, cortex=cx)

    summary["policy_written"] = policy_path(bot).exists()
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("bots", nargs="*", default=list(BOTS))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-seed-quarantine", action="store_true")
    args = ap.parse_args()

    if not args.skip_seed_quarantine:
        moved = quarantine_seed_stubs(dry_run=args.dry_run)
        print(f"[rebuild_cortex] seed stubs quarantined: {moved or 'none'}")

    for bot in args.bots:
        if bot not in BOTS:
            print(f"[rebuild_cortex] skip unknown bot {bot!r}")
            continue
        info = rebuild_bot(bot, dry_run=args.dry_run)
        print(f"[rebuild_cortex] {json.dumps(info)}")


if __name__ == "__main__":
    main()
