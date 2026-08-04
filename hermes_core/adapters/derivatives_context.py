"""Derivatives world context — funding / OI / basis (L4 + L7a multi-venue)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from hermes_core.env import get_env

_CACHE: dict[str, tuple[float, dict]] = {}
_TTL = 120.0


def limit_removal_enabled() -> bool:
    return get_env("LIMIT_REMOVAL", "0") == "1" or get_env("SENTIENT_HOLD", "0") == "1"


def _cache_path(bot: str | None = None) -> Path:
    try:
        from hermes_core.state.paths import bot_state_dir

        d = bot_state_dir(bot) / "world_cache"
        d.mkdir(parents=True, exist_ok=True)
        return d / "derivatives.json"
    except Exception:  # noqa: BLE001
        return Path("world_cache_derivatives.json")


def _perp_symbol(symbol: str) -> str:
    """Map spot pairs to USDT-m linear swap (BTC/USDT -> BTC/USDT:USDT)."""
    s = str(symbol or "").strip()
    if not s or ":" in s:
        return s
    if "/" not in s:
        return s
    base, quote = s.split("/", 1)
    quote = quote.split(":")[0].strip()
    base = base.strip()
    if not base or not quote:
        return s
    return f"{base}/{quote}:{quote}"


def _fetch_ccxt(symbol: str, exchange_id: str) -> dict | None:
    try:
        import ccxt  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    try:
        ex_cls = getattr(ccxt, exchange_id, None)
        if ex_cls is None:
            return None
        ex = ex_cls({"enableRateLimit": True})
        # Funding/OI are perp APIs — try swap first, then spot fallback for OI.
        markets = []
        perp = _perp_symbol(symbol)
        if perp:
            markets.append(perp)
        if symbol and symbol not in markets:
            markets.append(symbol)
        funding = None
        oi = None
        for market in markets:
            if funding is None:
                with contextlib_suppress():
                    if hasattr(ex, "fetch_funding_rate"):
                        fr = ex.fetch_funding_rate(market)
                        funding = float(fr.get("fundingRate") or fr.get("funding") or 0)
            if oi is None:
                with contextlib_suppress():
                    if hasattr(ex, "fetch_open_interest"):
                        o = ex.fetch_open_interest(market)
                        oi = float(o.get("openInterestAmount") or o.get("openInterest") or 0)
            if funding is not None and oi is not None:
                break
        if funding is None and oi is None:
            return None
        return {
            "funding": funding,
            "oi": oi,
            "source": exchange_id,
            "ts": time.time(),
        }
    except Exception:  # noqa: BLE001
        return None


class contextlib_suppress:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return True


def fetch_derivatives_context(pair: str, *, bot: str | None = None) -> dict:
    """Return world feats; degrade when stale (L7a). Never invent numbers."""
    now = time.time()
    key = f"{bot}:{pair}"
    if key in _CACHE and now - _CACHE[key][0] < _TTL:
        return dict(_CACHE[key][1])

    venues = ["binance", "bybit", "okx"]
    got = None
    for v in venues:
        got = _fetch_ccxt(pair, v)
        if got and (got.get("funding") is not None or got.get("oi") is not None):
            break
        got = None

    path = _cache_path(bot)
    degraded = False
    source = None
    if got is None:
        # disk cache
        try:
            if path.exists():
                disk = json.loads(path.read_text(encoding="utf-8"))
                age = now - float(disk.get("ts") or 0)
                if age < 3600:
                    got = disk
                    source = disk.get("source") or "disk"
                    if age > _TTL:
                        degraded = True
        except Exception:  # noqa: BLE001
            got = None
        if got is None:
            out = {
                "funding": None,
                "oi": None,
                "oi_z": None,
                "basis": None,
                "world_degraded": True,
                "world_freshness": 0.0,
                "world_source": None,
                "liq_stress": None,
            }
            _CACHE[key] = (now, out)
            return out
    else:
        source = got.get("source")
        try:
            path.write_text(json.dumps(got), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    funding = got.get("funding")
    oi = got.get("oi")
    # crude oi_z vs cached mean
    oi_z = None
    try:
        if oi is not None:
            prev = float((_CACHE.get(key) or (0, {}))[1].get("oi") or oi)
            oi_z = (float(oi) - prev) / max(abs(prev), 1.0)
    except (TypeError, ValueError):
        oi_z = None

    out = {
        "funding": funding,
        "oi": oi,
        "oi_z": oi_z,
        "basis": got.get("basis"),
        "world_degraded": degraded,
        "world_freshness": 1.0 if not degraded else 0.4,
        "world_source": source,
        "liq_stress": abs(float(funding)) * 10 if funding is not None else None,
        "ts": got.get("ts") or now,
    }
    _CACHE[key] = (now, out)
    return out


def world_patience_mult(world: dict | None, *, side: str = "long") -> float:
    if not world:
        return 1.0
    mult = 1.0
    if world.get("world_degraded"):
        mult *= 0.75
    try:
        f = world.get("funding")
        if f is not None:
            f = float(f)
            # positive funding = longs pay — bank earlier for longs
            if side.lower() in ("long", "buy") and f > 0.0003:
                mult *= 0.8
            if side.lower() in ("long", "buy") and f < -0.0003:
                mult *= 1.15
    except (TypeError, ValueError):
        pass
    try:
        oi_z = world.get("oi_z")
        if oi_z is not None and float(oi_z) < -0.05:
            mult *= 0.85
    except (TypeError, ValueError):
        pass
    return max(0.4, min(1.5, mult))
