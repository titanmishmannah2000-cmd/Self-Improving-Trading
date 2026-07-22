"""One-shot: re-validate discovered inventory via S10; rediscover if empty.

Does NOT weaken S10 — only marks backtest_approved when backtest_gp_indicator
approves. Archives the previous file, then writes approved-only (or fresh GP).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gp_revalidate")

BOT = os.environ.get("HERMES_BOT_NAME", "").strip().lower()
PAIR_MAP = {
    "forex": ["EUR/USD", "GBP/USD", "GBP/JPY", "AUD/USD"],
    "gold": ["XAU/USD", "XAG/USD"],
    "crypto": ["BTC/USD", "ETH/USD"],
}


def main() -> int:
    if BOT not in PAIR_MAP:
        log.error("HERMES_BOT_NAME=%r not in %s", BOT, list(PAIR_MAP))
        return 2

    from hermes_core.adapters.price import seed_history_interval_sync
    from hermes_core.engines.backtest import backtest_gp_indicator
    from hermes_core.engines.genetic import (
        discover,
        indicator_expr,
        is_backtest_approved,
        load_discovered_indicators,
    )
    from hermes_core.state.paths import discovered_path

    pairs = PAIR_MAP[BOT]
    log.info("bot=%s pairs=%s", BOT, pairs)
    summary: dict[str, dict] = {}

    for pair in pairs:
        path = discovered_path(pair)
        own = load_discovered_indicators(pair, include_shared=False)
        already = [i for i in own if indicator_expr(i) and is_backtest_approved(i)]
        if already:
            log.info("%s: already has %d approved — skip", pair, len(already))
            summary[pair] = {"status": "already_approved", "n": len(already)}
            continue

        hist = seed_history_interval_sync(pair, interval="1d", period="2y", max_candles=500)
        series = [c["price"] for c in (hist or [])]
        log.info("%s: daily candles=%d path=%s own=%d", pair, len(series), path, len(own))
        if len(series) < 200:
            summary[pair] = {"status": "insufficient_history", "candles": len(series)}
            continue

        # Archive prior inventory (even if empty file).
        if path.exists():
            arch = path.with_suffix(
                f".pre_s10_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
            )
            shutil.copy2(path, arch)
            log.info("%s: archived -> %s", pair, arch.name)

        approved: list[dict] = []
        for ind in own:
            es = indicator_expr(ind)
            if not es:
                continue
            try:
                bt = backtest_gp_indicator(pair, es, prices=series)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s: S10 error on %s: %s", pair, es[:40], exc)
                continue
            if not bt.get("approved"):
                log.info("%s: S10 REJECT %s reason=%s", pair, es[:40], bt.get("reason"))
                continue
            row = dict(ind)
            row["backtest_approved"] = True
            row["backtest_reason"] = bt.get("reason")
            row["backtest_oos_corr"] = bt.get("oos_corr")
            row["revalidated_at"] = datetime.now(timezone.utc).isoformat()
            approved.append(row)
            log.info("%s: S10 APPROVE %s", pair, es[:40])

        if approved:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Persist via genetic saver so canonical shape matches live path.
            from hermes_core.engines.genetic import _save_discovered

            _save_discovered(pair, approved)
            summary[pair] = {"status": "revalidated", "n": len(approved)}
            continue

        log.info("%s: no S10 approvals — running full discover()", pair)
        # Remove unapproved zombie file so invent result is the sole inventory.
        if path.exists():
            path.unlink()
        t0 = time.time()
        inds = discover(pair, series, horizon=60, generations=40, pop_size=40)
        dt = round(time.time() - t0, 1)
        approved_n = sum(1 for i in inds if i.get("backtest_approved") is True)
        log.info("%s: discover admitted=%d approved=%d in %ss", pair, len(inds), approved_n, dt)
        summary[pair] = {
            "status": "rediscovered",
            "admitted": len(inds),
            "approved": approved_n,
            "seconds": dt,
        }

    out = {
        "bot": BOT,
        "ts": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
    }
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
