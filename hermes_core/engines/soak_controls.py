"""Soak-readiness controls: halt, price sanity, state bootstrap, feed SLOs.

Used by the live loop and self-audit. Fail-soft helpers never raise into the
trade cycle.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path
from typing import Any

from hermes_core.env import get_env
from hermes_core.state.paths import bot_state_dir

# FX stub ladder observed in polluted local heartbeats.
_FX_STUB_SET = frozenset({1.1, 1.11, 1.12, 1.13})
_FX_PAIRS = frozenset({"EUR/USD", "GBP/USD", "AUD/USD", "GBP/JPY"})
_FEED_SKIP_PREFIXES = ("fetch_error", "no_candle", "chart_error")
_STATE_TOUCH_FILES = (
    "trades.jsonl",
    "skips.jsonl",
    "gp_shadow.jsonl",
    "flatline_log.jsonl",
    "hypotheses.jsonl",
)


def halt_path(bot: str) -> Path:
    return bot_state_dir(bot) / "halt"


def entries_halted(bot: str) -> tuple[bool, str]:
    """True when env HALT_ENTRIES=1 or ``{bot}/state/halt`` exists."""
    if get_env("HALT_ENTRIES", "0").strip() in ("1", "true", "TRUE", "yes", "YES"):
        return True, "halt:env"
    p = halt_path(bot)
    if p.exists():
        return True, "halt:file"
    return False, ""


def write_halt(bot: str, reason: str) -> None:
    """Create halt file so new entries stop (exits still run)."""
    p = halt_path(bot)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {"ts": time.time(), "reason": reason}
        p.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    except OSError:
        pass


def clear_halt(bot: str) -> None:
    with contextlib.suppress(OSError):
        halt_path(bot).unlink(missing_ok=True)


def ensure_state_files(bot: str) -> Path:
    """Ensure canonical runtime files exist under ``{bot}/state/``."""
    d = bot_state_dir(bot)
    for name in _STATE_TOUCH_FILES:
        p = d / name
        if not p.exists():
            with contextlib.suppress(OSError):
                p.touch()
    (d / "discovered").mkdir(parents=True, exist_ok=True)
    (d / "cortex").mkdir(parents=True, exist_ok=True)
    (d / "strategies").mkdir(parents=True, exist_ok=True)
    return d


def reset_reflection_latches(bot: str) -> None:
    """Wipe reflection latches after a trade scrub so counts match the book."""
    p = bot_state_dir(bot) / ".reflection_latches.json"
    try:
        if p.exists():
            p.write_text("{}", encoding="utf-8")
    except OSError:
        pass


def _round_price(p: float) -> float:
    return round(float(p), 6)


def price_sanity_pair(pair: str, price: float | None) -> tuple[bool, str]:
    """Reject placeholder / impossible single quotes."""
    if price is None:
        return False, "price_sanity:none"
    try:
        px = float(price)
    except (TypeError, ValueError):
        return False, "price_sanity:nan"
    if px <= 0:
        return False, "price_sanity:non_positive"
    if abs(px - 1.0) < 1e-12:
        return False, "price_sanity:stub_1.0"
    if pair in _FX_PAIRS and _round_price(px) in _FX_STUB_SET:
        # Single tick in the stub ladder is suspicious but only fatal when the
        # whole book matches — handled by price_sanity_book.
        pass
    # Crude scale checks (also used by promote gate).
    if pair in {"EUR/USD", "GBP/USD", "AUD/USD"} and not (0.5 <= px <= 3.0):
        return False, f"price_sanity:fx_scale:{px}"
    if pair == "GBP/JPY" and not (50.0 <= px <= 400.0):
        return False, f"price_sanity:gbpjpy_scale:{px}"
    if pair.startswith("XAU") and not (500.0 <= px <= 10000.0):
        return False, f"price_sanity:xau_scale:{px}"
    if pair.startswith("XAG") and not (5.0 <= px <= 200.0):
        return False, f"price_sanity:xag_scale:{px}"
    if pair.startswith("BTC") and not (1000.0 <= px <= 500000.0):
        return False, f"price_sanity:btc_scale:{px}"
    if pair.startswith("ETH") and not (50.0 <= px <= 50000.0):
        return False, f"price_sanity:eth_scale:{px}"
    return True, ""


def price_sanity_book(
    prices: dict[str, float] | None, price_history: dict[str, list] | None = None
) -> tuple[bool, str]:
    """Detect synthetic FX ladders / cross-pair identical stubs."""
    prices = prices or {}
    if not prices:
        return True, ""
    for pair, px in prices.items():
        ok, reason = price_sanity_pair(pair, px)
        if not ok and "stub_1.0" in reason:
            return False, reason
        if not ok and "scale" in reason:
            return False, reason
    fx_vals = [_round_price(float(prices[p])) for p in prices if p in _FX_PAIRS]
    # Only fatal when the shared value is the known stub ladder / 1.0 (real
    # markets can briefly print similar majors; polluted local HB used 1.1x).
    if len(fx_vals) >= 3 and len(set(fx_vals)) == 1:
        shared = fx_vals[0]
        if shared in _FX_STUB_SET or abs(shared - 1.0) < 1e-12:
            return False, "price_sanity:fx_all_equal_stub"
    # History ladder: each FX pair only uses the known stub set.
    hist = price_history or {}
    stubish = 0
    checked = 0
    for pair in _FX_PAIRS:
        series = hist.get(pair) or []
        if len(series) < 8:
            continue
        checked += 1
        uniq = {_round_price(float(x)) for x in series}
        if uniq and uniq <= _FX_STUB_SET:
            stubish += 1
    if checked >= 2 and stubish == checked:
        return False, "price_sanity:fx_stub_ladder"
    return True, ""


def feed_error_rate(skips_path: Path, *, window: int = 200) -> dict[str, Any]:
    """Fraction of recent skips that are feed/chart failures."""
    if not skips_path.exists():
        return {"n": 0, "feed_n": 0, "rate": 0.0, "ok": True}
    rows: list[str] = []
    try:
        lines = skips_path.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[-window:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append(str(rec.get("reason") or rec.get("reason_skipped") or ""))
    except OSError:
        return {"n": 0, "feed_n": 0, "rate": 0.0, "ok": True}
    n = len(rows)
    feed_n = sum(1 for r in rows if r.startswith(_FEED_SKIP_PREFIXES))
    rate = (feed_n / n) if n else 0.0
    # Auto-halt threshold: >=40% of last 200 skips are feed failures, n>=40.
    ok = not (n >= 40 and rate >= 0.40)
    return {"n": n, "feed_n": feed_n, "rate": round(rate, 4), "ok": ok}


def idle_skip_slo(skips_path: Path, *, hours: float = 6.0, window: int = 500) -> dict[str, Any]:
    """Detect 'effectively paused': recent skips are all idle/feed with fresh activity."""
    now = time.time()
    cutoff = now - hours * 3600.0
    if not skips_path.exists():
        return {"effectively_paused": False, "detail": "no_skips"}
    reasons: list[str] = []
    recent_ts = 0.0
    try:
        for line in skips_path.read_text(encoding="utf-8", errors="replace").splitlines()[-window:]:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = float(rec.get("ts") or 0.0)
            if ts < cutoff:
                continue
            recent_ts = max(recent_ts, ts)
            reasons.append(str(rec.get("reason") or ""))
    except OSError as exc:
        return {"effectively_paused": False, "detail": str(exc)}
    if len(reasons) < 20:
        return {"effectively_paused": False, "detail": f"few_recent={len(reasons)}"}
    badish = sum(
        1
        for r in reasons
        if r == "no_signal"
        or r.startswith("no_signal:")
        or r.startswith(_FEED_SKIP_PREFIXES)
        or r.startswith("bb_bandwidth")
    )
    paused = badish == len(reasons)
    return {
        "effectively_paused": paused,
        "detail": f"recent={len(reasons)} badish={badish} last_age={now - recent_ts:.0f}s",
    }


def pair_price_scale_ok(pair: str, price: float) -> bool:
    ok, _ = price_sanity_pair(pair, price)
    return ok
