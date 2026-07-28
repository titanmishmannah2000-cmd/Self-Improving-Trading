"""Heartbeat / price health checks for Profitability Path Phase 0.

Stub prices near 1.1 for XAU/FX are a known failure mode (degraded feed).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from hermes_core.state.paths import bot_state_dir

# Plausible lower bounds for focus instruments (spot). Below → treat as stub/bad.
MIN_SANE_PRICE: dict[str, float] = {
    "XAU/USD": 500.0,  # gold never trades near 1.1
    "XAG/USD": 5.0,
    "EUR/USD": 0.5,
    "GBP/USD": 0.5,
    "AUD/USD": 0.3,
    "GBP/JPY": 50.0,
    "BTC/USD": 1000.0,
    "ETH/USD": 50.0,
}

# Upper sanity (catch inverted/garbage).
MAX_SANE_PRICE: dict[str, float] = {
    "XAU/USD": 20_000.0,
    "XAG/USD": 500.0,
    "EUR/USD": 3.0,
    "GBP/USD": 4.0,
    "AUD/USD": 3.0,
    "GBP/JPY": 400.0,
    "BTC/USD": 5_000_000.0,
    "ETH/USD": 500_000.0,
}


def load_heartbeat(bot: str, *, path: Path | None = None) -> dict[str, Any] | None:
    p = path or (bot_state_dir(bot) / "heartbeat.json")
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def price_sane(pair: str, price: float | None) -> bool:
    if price is None:
        return False
    try:
        px = float(price)
    except (TypeError, ValueError):
        return False
    lo = MIN_SANE_PRICE.get(pair.upper(), 0.0)
    hi = MAX_SANE_PRICE.get(pair.upper(), 1e12)
    return lo <= px <= hi


def check_heartbeat_health(
    bot: str,
    *,
    focus_pairs: list[str] | None = None,
    max_age_s: float = 900.0,
    heartbeat: dict | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Validate heartbeat age, status, and sane prices for focus pairs."""
    now = time.time() if now is None else float(now)
    hb = heartbeat if heartbeat is not None else load_heartbeat(bot)
    if hb is None:
        return {
            "ok": False,
            "bot": bot,
            "violations": ["missing_heartbeat"],
            "pairs": {},
        }

    violations: list[str] = []
    ts = float(hb.get("ts") or 0.0)
    age = now - ts if ts > 0 else 1e12
    if age > max_age_s:
        violations.append(f"stale_heartbeat_age_s={age:.0f}")

    status = str(hb.get("status") or "")
    if status in ("degraded", "error", "halted"):
        violations.append(f"status={status}")

    prices = hb.get("prices") if isinstance(hb.get("prices"), dict) else {}
    regimes = hb.get("regimes") if isinstance(hb.get("regimes"), dict) else {}
    focus = focus_pairs or list(prices.keys())
    pair_report: dict[str, Any] = {}
    for pair in focus:
        px = prices.get(pair)
        try:
            px_f = float(px) if px is not None else None
        except (TypeError, ValueError):
            px_f = None
        sane = price_sane(pair, px_f)
        reg = regimes.get(pair)
        if not sane:
            violations.append(f"insane_price:{pair}={px}")
        pair_report[pair] = {
            "price": px_f,
            "sane": sane,
            "regime": reg,
        }

    return {
        "ok": len(violations) == 0,
        "bot": bot,
        "age_s": round(age, 1) if ts > 0 else None,
        "status": status,
        "violations": violations,
        "pairs": pair_report,
        "hif_flags": hb.get("hif_flags"),
    }
