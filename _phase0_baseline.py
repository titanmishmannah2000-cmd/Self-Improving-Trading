"""Phase 0 baseline: skip + trade counts before L18 relaxation."""
from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NOW = time.time()
WINDOWS = (("24h", 86400), ("72h", 259200), ("7d", 604800))


def _parse_ts(row: dict) -> float:
    for k in ("ts", "closed_ts", "exit_ts", "time", "timestamp"):
        v = row.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
        if isinstance(v, str) and "T" in v:
            try:
                import datetime as dt

                return dt.datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return 0.0


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def main() -> None:
    lines: list[str] = []

    def out(s: str = "") -> None:
        print(s)
        lines.append(s)

    out("=== PHASE 0 BASELINE ===")
    out(f"ts_now={int(NOW)}")
    out(f"iso={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(NOW))}")
    out("note: Phase 1 will set L18 min_oversold_pairs 2 -> 1")

    for bot in ("forex", "gold", "crypto"):
        out()
        out(f"## {bot}")
        for label, sd in (
            ("runtime", ROOT / bot / "state"),
            ("seed", ROOT / "bots" / bot / "state"),
        ):
            if not sd.exists():
                continue
            out(f"### {label}: {sd}")

            skips = _load_jsonl(sd / "skips.jsonl")
            out(f"  skips_total_rows={len(skips)}")
            for wname, secs in WINDOWS:
                cutoff = NOW - secs
                subset = [r for r in skips if _parse_ts(r) >= cutoff]
                by_reason = Counter(
                    str(r.get("reason") or r.get("reason_skipped") or "?") for r in subset
                )
                by_pair = Counter(str(r.get("pair") or "?") for r in subset)
                out(f"  skips_{wname}={len(subset)}")
                if subset:
                    out(f"    top_reasons={by_reason.most_common(8)}")
                    out(f"    by_pair={by_pair.most_common(10)}")

            trade_files: list[Path] = []
            for name in (
                "trades.jsonl",
                "closed_trades.jsonl",
                "history.jsonl",
                "fills.jsonl",
            ):
                p = sd / name
                if p.exists():
                    trade_files.append(p)
            trade_files.extend(
                p
                for p in sd.glob("*.jsonl")
                if "trade" in p.name.lower() or "closed" in p.name.lower()
            )
            seen: set[Path] = set()
            uniq: list[Path] = []
            for p in trade_files:
                rp = p.resolve()
                if rp not in seen:
                    seen.add(rp)
                    uniq.append(p)

            hb = sd / "heartbeat.json"
            if hb.exists():
                try:
                    h = json.loads(hb.read_text(encoding="utf-8"))
                    interesting = {
                        k: h[k]
                        for k in (
                            "trades",
                            "closed_trades",
                            "n_trades",
                            "trade_count",
                            "cycle",
                            "last_trade",
                            "open_positions",
                            "entries",
                            "closed",
                        )
                        if k in h
                    }
                    if interesting:
                        out(f"  heartbeat={interesting}")
                    else:
                        out(f"  heartbeat_top_keys={list(h)[:25]}")
                except (OSError, json.JSONDecodeError) as exc:
                    out(f"  heartbeat_error={exc}")

            if not uniq:
                out("  trade_jsonl: none found")
            for tf in uniq:
                rows = _load_jsonl(tf)
                out(f"  {tf.name}: rows={len(rows)}")
                for wname, secs in WINDOWS:
                    cutoff = NOW - secs
                    subset = [r for r in rows if _parse_ts(r) >= cutoff]
                    closed = [
                        r
                        for r in subset
                        if r.get("status") == "closed"
                        or r.get("exit_price") is not None
                        or r.get("pnl") is not None
                        or r.get("event") == "close"
                    ]
                    out(f"    {wname}: rows={len(subset)} closedish={len(closed)}")
                    pnls: list[float] = []
                    for r in closed or subset:
                        for k in ("pnl", "pnl_pct", "pnl_r", "realized_pnl"):
                            if k in r:
                                try:
                                    pnls.append(float(r[k]))
                                    break
                                except (TypeError, ValueError):
                                    pass
                    if pnls:
                        wins = sum(1 for x in pnls if x > 0)
                        losses = sum(1 for x in pnls if x < 0)
                        out(
                            f"      pnl_n={len(pnls)} wins={wins} "
                            f"losses={losses} sum={sum(pnls):.4f}"
                        )

    out_path = ROOT / "_phase0_baseline.txt"
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out(f"WROTE {out_path}")


if __name__ == "__main__":
    main()
