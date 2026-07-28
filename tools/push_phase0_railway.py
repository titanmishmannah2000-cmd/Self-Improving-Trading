#!/usr/bin/env python3
"""Push Profitability Path Phase 0 HIF freeze vars to Railway services."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

SERVICES = ("forex", "gold", "crypto")

VARS = {
    "BOOK_RISK": "1",
    "SOFT_WEIGHTS": "0",
    "KELLY_SIZING": "0",
    "REGIME_SIZING": "0",
    "ENTRY_RANKING": "0",
    "EXIT_INTEL": "0",
    "PROBE_SIZING": "0",
    "SKIP_SHADOW_REFLECT": "0",
    "SKIP_SHADOW_PROMOTE": "0",
    "CRISIS_RECOMMEND": "0",
    "GP_PROMOTE": "0",
    "REFLECT_AUTO_DEPLOY": "0",
    "REFLECT_DEPLOY_STAGE": "prove",
    "MICRO_LIVE": "0",
    "MICRO_LIVE_SIZE_MULT": "0.25",
    "REGIME_DECAY": "0",
    "SCORECARD_COST_PCT": "0.05",
    "GP_PROMOTE_COST_PCT": "0.05",
}


def railway_bin() -> str:
    for name in ("railway.cmd", "railway.exe", "railway"):
        found = shutil.which(name)
        if found:
            return found
    npm = os.path.expandvars(r"%APPDATA%\npm\railway.cmd")
    if os.path.isfile(npm):
        return npm
    return "railway"


def main() -> int:
    bin_path = railway_bin()
    failed = 0
    for svc in SERVICES:
        print(f"=== service={svc} ({len(VARS)} flags, skip-deploys) ===")
        for k, v in VARS.items():
            cmd = [
                bin_path,
                "variable",
                "set",
                f"{k}={v}",
                "--service",
                svc,
                "--skip-deploys",
            ]
            proc = subprocess.run(cmd, check=False, capture_output=True, text=True)
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["unknown"]
                print(f"  FAIL {k}: {err[0]}", file=sys.stderr)
                failed += 1
            else:
                print(f"  ok {k}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
