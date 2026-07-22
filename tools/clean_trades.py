"""One-shot cleaner: archive polluted trades.jsonl lines, keep real ones.

Pollution signatures (local audit 2026-07-22):
  * Fixture stubs (t_forex_1 / t_gold_1 / t_crypto_1, id=\"x\", entry_ts=\"t\")
  * Impossible entry prices for the pair (e.g. EUR/USD < 0.5, GBP/JPY < 100)
  * Instant closes (hold < 60s) — test/replay dumps
  * Known replay PnL fingerprints (-3.204598..., -4.153573..., +2.45339..., -5.267308...)
  * Legacy rows with no id + decaying synthetic prices
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Exact fixture / test IDs seen in bots/*/state/trades.jsonl and replay dumps.
FIXTURE_IDS = {
    "t_forex_1",
    "t_forex_2",
    "t_gold_1",
    "t_gold_2",
    "t_crypto_1",
    "t_crypto_2",
    "x",
    "forex:EUR/USD:1",  # duplicated gp_ensemble fixture @ entry 1.1
}

# Replay fingerprint PnLs that dominate the polluted forex log.
REPLAY_PNL = {
    round(-3.204598462749992, 6),
    round(-3.2045984627500066, 6),
    round(-3.2045984627500106, 6),
    round(-3.2045984627500164, 6),
    round(-3.2045984627499884, 6),
    round(-3.2045984627499946, 6),
    round(-3.2045984627499977, 6),
    round(-4.153572987625022, 6),
    round(-4.153572987625014, 6),
    round(2.45339, 6),
    round(2.4533899999999926, 6),
    round(2.4533900000000006, 6),
    round(-5.26730846950878, 6),
    round(-4.577725, 6),
}


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _hold_seconds(rec: dict) -> float | None:
    et, xt = rec.get("entry_ts"), rec.get("exit_ts")
    if not (isinstance(et, str) and isinstance(xt, str) and "T" in et and "T" in xt):
        return None
    try:
        a = datetime.fromisoformat(et.replace("Z", "+00:00"))
        b = datetime.fromisoformat(xt.replace("Z", "+00:00"))
        return (b - a).total_seconds()
    except Exception:
        return None


def _sane_price(pair: str, entry: float) -> bool:
    if pair in ("EUR/USD", "GBP/USD", "AUD/USD"):
        return 0.5 < entry < 2.5
    if pair == "GBP/JPY":
        return 100.0 < entry < 300.0
    if pair == "XAU/USD":
        return 1000.0 < entry < 6000.0
    if pair == "XAG/USD":
        return 10.0 < entry < 100.0
    if pair in ("BTC/USD", "ETH/USD"):
        return entry > 10.0
    return True


def is_polluted(rec: dict) -> bool:
    tid = str(rec.get("id") or "")
    if tid in FIXTURE_IDS:
        return True
    if rec.get("entry_ts") == "t":
        return True

    pair = str(rec.get("pair") or "")
    entry = _f(rec.get("entry_price") if rec.get("entry_price") is not None else rec.get("entry"))
    exit_p = _f(rec.get("exit_price") if rec.get("exit_price") is not None else rec.get("exit"))
    pnl = _f(rec.get("pnl_pct"))

    if pnl is not None and round(pnl, 6) in REPLAY_PNL:
        return True

    if entry is not None and pair and not _sane_price(pair, entry):
        return True
    if exit_p is not None and pair and not _sane_price(pair, exit_p):
        return True

    hold = _hold_seconds(rec)
    if hold is not None and hold < 60:
        return True

    # Legacy replay rows: no id, and either missing timestamps or synthetic decay prices.
    if not tid:
        if entry is not None and pair in ("EUR/USD", "GBP/USD", "AUD/USD") and entry < 0.8:
            return True
        if entry is not None and pair == "GBP/JPY" and entry < 150:
            return True
        # No id + exact fixture pnl pair (+1.2 / -0.4 stubs also appear without ids in some dumps)
        if pnl is not None and round(pnl, 2) in (1.2, -0.4) and entry in (1.08, 2300.0, 2300):
            return True

    return False


def clean_file(path: Path, stamp: str) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    kept: list[str] = []
    dumped: list[str] = []
    for ln in lines:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            dumped.append(ln)
            continue
        if not isinstance(rec, dict):
            dumped.append(ln)
            continue
        (dumped if is_polluted(rec) else kept).append(ln)
    if dumped:
        arch_dir = path.parent / "archive"
        arch_dir.mkdir(parents=True, exist_ok=True)
        arch = arch_dir / f"trades_polluted_{stamp}.jsonl"
        arch.write_text("\n".join(dumped) + "\n", encoding="utf-8")
    path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")
    return len(kept), len(dumped)


def main() -> None:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    targets = [
        ROOT / "forex" / "state" / "trades.jsonl",
        ROOT / "gold" / "state" / "trades.jsonl",
        ROOT / "crypto" / "state" / "trades.jsonl",
        ROOT / "bots" / "forex" / "state" / "trades.jsonl",
        ROOT / "bots" / "gold" / "state" / "trades.jsonl",
        ROOT / "bots" / "crypto" / "state" / "trades.jsonl",
    ]
    for path in targets:
        if not path.exists():
            print(f"skip missing {path.relative_to(ROOT)}")
            continue
        kept, archived = clean_file(path, stamp)
        print(f"{path.relative_to(ROOT)}: kept={kept} archived={archived}")


if __name__ == "__main__":
    main()
