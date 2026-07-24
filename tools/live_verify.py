"""Live verification (items 18-20) — read-only health report."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)

from hermes_core.config.loader import state_root  # noqa: E402
from hermes_core.state.paths import (  # noqa: E402
    bot_state_dir,
    hypotheses_kb_path,
    hypotheses_path,
    reflection_latch_path,
)


def main() -> None:
    print("state_root=", state_root())
    print()
    for bot in ("forex", "gold", "crypto"):
        sd = bot_state_dir(bot)
        print(f"=== {bot} state={sd} ===")
        hb = sd / "heartbeat.json"
        if hb.exists():
            h = json.loads(hb.read_text(encoding="utf-8"))
            age = time.time() - float(h.get("ts", 0))
            print(f"  heartbeat status={h.get('status')} cycle={h.get('cycle')} age_s={age:.0f}")
        else:
            print("  heartbeat MISSING")

        strat_dir = sd / "strategies"
        seeds = list((ROOT / "bots" / bot / "state" / "strategies").glob("*.yaml"))
        lives = list(strat_dir.glob("*.yaml")) if strat_dir.exists() else []
        print(f"  strategies live={len(lives)} seed={len(seeds)}")
        for p in sorted(lives)[:8]:
            d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            print(f"    LIVE {p.name} version={d.get('version')} sl={d.get('stop_loss_pct')}")
        if not lives:
            for p in sorted(seeds)[:2]:
                d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                print(f"    SEED-only {p.name} version={d.get('version')}")

        disc = sd / "discovered"
        files = sorted(disc.glob("*.json")) if disc.exists() else []
        print(f"  discovered files={len(files)}")
        for p in files[:8]:
            rows = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(rows, dict):
                rows = rows.get("indicators") or []
            if not isinstance(rows, list):
                rows = []
            approved = sum(
                1 for r in rows if isinstance(r, dict) and r.get("backtest_approved") is True
            )
            print(f"    {p.name}: n={len(rows)} approved={approved}")
            if rows:
                r0 = rows[0]
                print(
                    f"      sample expr={str(r0.get('expr') or r0.get('expr_str'))[:60]} "
                    f"src={r0.get('source')} bt={r0.get('backtest_approved')}"
                )

        shadow = sd / "gp_shadow.jsonl"
        if shadow.exists():
            lines = [
                json.loads(line)
                for line in shadow.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            act = Counter(row.get("num_active") for row in lines[-50:])
            print(f"  gp_shadow lines={len(lines)} recent_num_active={dict(act)}")
            if lines:
                print(f"    last={lines[-1]}")
        else:
            print("  gp_shadow MISSING")

        hyp = hypotheses_path(bot)
        if hyp.exists():
            hl = [
                json.loads(line)
                for line in hyp.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            print(
                f"  hypotheses lines={len(hl)} status={dict(Counter(x.get('status') for x in hl))}"
            )
        else:
            print("  hypotheses empty/missing")

        latch = reflection_latch_path(bot)
        if latch.exists():
            print(f"  latch={latch.read_text(encoding='utf-8')[:240]}")
        else:
            print("  latch MISSING")

        kb = hypotheses_kb_path(bot)
        if kb.exists():
            kl = [
                json.loads(line)
                for line in kb.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            print(
                f"  kb lines={len(kl)} approved={sum(1 for x in kl if x.get('approved'))} "
                f"gp_expr={sum(1 for x in kl if x.get('param') == 'gp_expr')}"
            )
        else:
            print("  kb MISSING")

        tr = sd / "trades.jsonl"
        if tr.exists():
            tl = [
                json.loads(line)
                for line in tr.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            by = Counter(t.get("pair") for t in tl)
            vers = Counter(str(t.get("strategy_version")) for t in tl[-30:])
            print(f"  trades={len(tl)} by_pair={dict(by)} recent_versions={dict(vers)}")
        print()


if __name__ == "__main__":
    main()
