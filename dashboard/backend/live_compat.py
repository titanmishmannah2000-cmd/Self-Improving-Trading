"""
live_compat.py — Bridge the LIVE Hermes bot pipeline into the redesigned dashboard.

The redesigned backend (hermes-dashboard-api/main.py) expects bots to POST a rich
full-state blob to /api/ingest/{bot} every cycle (strategies, discovered, cortex,
heartbeat, open trades). The ACTUAL live bots (bots/_runner.py + hermes_core loop)
instead:
  * POST prices to   /api/price/{bot}        body {prices:{pair:price}}
  * POST full state to /api/ingest/{bot}     (recent_trades/hypotheses/skips lists)
  * WRITE state files directly into each bot's state dir:
        bots/{bot}/state/discovered/{PAIR}.json   -> LIST[indicator]
        bots/{bot}/state/cortex/indicator_exile.json -> {name: {...}}
        bots/{bot}/state/cortex/indicator_tracker.json -> {name: {...}}
        bots/{bot}/state/cortex/policy.json          -> {...}
        bots/{bot}/state/heartbeat.json              -> {...}
        bots/{bot}/state/trades.jsonl / skips.jsonl / hypotheses.jsonl

This module registers routes that READ those real bot-state files and return the
exact JSON shapes the redesigned frontend expects, so nothing in the live pipeline
has to change. Registered BEFORE the base routes so it takes precedence.

Canonical runtime state (matches hermes_core.state_root):
    {HERMES_STATE_ROOT}/{bot}/state/...   e.g. /data/forex/state/
Image seeds (lower priority / defaults only):
    bots/{bot}/state/strategies/, bots/{bot}/state/discovered/

All reads are fail-soft: missing files -> empty/[] , never a 500.
"""

import contextlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path


def _utcnow() -> str:
    """ISO-8601 UTC timestamp used as the default price-store ts.

    The live bots POST {"prices": {...}} WITHOUT a ts field, so the
    compat price route falls back to this. (Was referenced but never
    defined -> NameError -> HTTP 500 on every price push. FIXED.)
    """
    return datetime.now(UTC).isoformat()


# ----------------------------------------------------------------------------
# Locate the bots' state directories.
# Priority (matches hermes_core.state_root / bot_state_dir):
#   HERMES_STATE_ROOT/{bot}/state     (Railway /data)
#   <repo>/{bot}/state                (local when env unset)
#   /data/{bot}/state
# Legacy seed trees (lower priority — not the live write target):
#   <repo>/bots/{bot}/state
#   /app/bots/{bot}/state
# ----------------------------------------------------------------------------


def _candidate_state_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.getenv("HERMES_STATE_ROOT") or os.getenv("HERMES_STATE")
    if env:
        roots.append(Path(env))
    repo = Path(__file__).resolve().parent.parent.parent  # dashboard/backend -> repo
    roots.append(repo)  # {repo}/{bot}/state — matches state_root() fallback
    roots.append(Path("/data"))  # Railway volume without env typo safety
    # Legacy / image seed trees — prefer only when canonical runtime is absent
    roots += [
        repo / "bots",
        Path("/app/bots"),
        Path("/data/bots"),
    ]
    return roots


def _bot_state_dir(bot: str) -> Path | None:
    """Return the first existing state dir for a bot, else a best-guess path."""
    for root in _candidate_state_roots():
        d = root / bot / "state"
        if d.exists():
            return d
    # Fallback guess (so writes/reads still land somewhere consistent)
    for root in _candidate_state_roots():
        d = root / bot / "state"
        if root.exists():
            return d
    return None


def _read_json(path: Path):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        pass
    return out


# ----------------------------------------------------------------------------
# /api/discovered  ->  {pairs:{pair:[ind]}, ensemble:{...}, total_indicators, total_pairs, degradation}
# ----------------------------------------------------------------------------


def _enrich_indicator(ind: dict, *, bot: str, pair: str) -> dict:
    """Normalize a discovered indicator for the dashboard tab.

    Matches fields the entry engine cares about (live_flag / live_fitness /
    shared penalty) and adds display helpers (uses, bot, pair). Phase A fields
    (engine_version, niche, pool_lift, island_id, admit_reason, max_dd, run_id)
    are passed through when present.
    """
    if not isinstance(ind, dict):
        return {"name": "?", "expr": "", "_bot": bot, "_pair": pair}
    out = dict(ind)
    expr = str(out.get("expr") or out.get("expr_str") or "")
    out["expr"] = expr
    out.setdefault("name", out.get("expr", "?")[:40] or "?")
    if not isinstance(out.get("uses"), list):
        out["uses"] = [
            k
            for k in ("volume", "dxy", "vix", "tnx", "spx", "oil", "gold", "btc", "fvx", "eem")
            if k in expr
        ]
    try:
        out["fitness"] = round(float(out.get("fitness") or 0), 4)
    except (TypeError, ValueError):
        out["fitness"] = 0.0
    try:
        out["win_rate"] = float(out.get("win_rate") or 0)
    except (TypeError, ValueError):
        out["win_rate"] = 0.0
    try:
        out["total_pnl"] = round(float(out.get("total_pnl") or 0), 4)
    except (TypeError, ValueError):
        out["total_pnl"] = 0.0
    # Phase A dashboard contract — coerce lightly, keep absent as missing
    if out.get("pool_lift") is not None:
        try:
            out["pool_lift"] = round(float(out["pool_lift"]), 4)
        except (TypeError, ValueError):
            out.pop("pool_lift", None)
    if out.get("max_dd") is not None:
        try:
            out["max_dd"] = round(float(out["max_dd"]), 4)
        except (TypeError, ValueError):
            out.pop("max_dd", None)
    if out.get("complexity") is not None:
        with contextlib.suppress(TypeError, ValueError):
            out["complexity"] = int(out["complexity"])
    if out.get("island_id") is not None:
        with contextlib.suppress(TypeError, ValueError):
            out["island_id"] = int(out["island_id"])
    if isinstance(out.get("niche"), dict):
        out["niche"] = {
            k: out["niche"].get(k)
            for k in ("horizon", "horizon_bin", "complexity_bin", "behavior", "niche_key")
            if k in out["niche"]
        }
    if out.get("niche_key") is None and isinstance(out.get("niche"), dict):
        out["niche_key"] = out["niche"].get("niche_key")
    out["_bot"] = bot
    out["_pair"] = pair
    return out


def _ensemble_for_pair(inds: list[dict]) -> dict:
    """Ensemble summary aligned with entry: suppressed indicators do not vote."""
    active = [i for i in inds if i.get("live_flag") != "suppress"]
    if not active:
        return {
            "status": "no_active_indicators",
            "signal": 0,
            "num_indicators": 0,
            "num_suppressed": len(inds),
            "multi_dim": 0,
            "best_fitness": 0,
            "best_wr": 0,
        }

    def _f(i, k):
        try:
            return float(i.get(k, 0) or 0)
        except Exception:
            return 0.0

    tw = sum((_f(i, "fitness") * _f(i, "win_rate")) for i in active if _f(i, "fitness") > 0)
    bw = sum((_f(i, "fitness") * _f(i, "win_rate")) for i in active if _f(i, "win_rate") > 0.5)
    signal = (bw - (tw - bw)) / max(tw, 0.001)
    return {
        "signal": round(signal, 3),
        "num_indicators": len(active),
        "num_suppressed": sum(1 for i in inds if i.get("live_flag") == "suppress"),
        "multi_dim": sum(1 for i in active if i.get("uses")),
        "best_fitness": max((_f(i, "fitness") for i in active), default=0),
        "best_wr": max((_f(i, "win_rate") for i in active), default=0),
        "status": "ok",
    }


def _degradation_for_pairs(pairs: dict[str, list]) -> dict:
    """Synthesize degradation/health stats from live_flag + WR (no separate file)."""
    out: dict[str, dict] = {}
    for pair, inds in pairs.items():
        if not inds:
            continue
        suppressed = sum(1 for i in inds if i.get("live_flag") == "suppress")
        promoted = sum(1 for i in inds if i.get("live_flag") == "promote")
        shared = sum(1 for i in inds if i.get("_shared_from"))
        weak = sum(1 for i in inds if 0 < float(i.get("win_rate") or 0) < 0.45)
        out[pair] = {
            "suppressed": suppressed,
            "promoted": promoted,
            "shared": shared,
            "weak_wr": weak,
            "active": len(inds) - suppressed,
            "total": len(inds),
        }
    return out


def _build_discovered(bot: str) -> dict:
    # Read from latest_state (SQLite) — the only cross-service channel. The bot
    # pushes discovered as {"EUR/USD":[...], ...}; reshape to the tab schema.
    # Phase B special keys: __gp_pulse__, __gp_niche_map__.
    pairs: dict[str, list] = {}
    pulse: dict = {}
    niche_map: dict = {}
    try:
        from dashboard.backend.main import get_conn

        conn = get_conn()
        row = conn.execute(
            "SELECT discovered_json FROM latest_state WHERE bot=?", (bot,)
        ).fetchone()
        conn.close()
        if row and row["discovered_json"]:
            raw = json.loads(row["discovered_json"])
            if isinstance(raw, dict):
                pulse_raw = raw.get("__gp_pulse__")
                if isinstance(pulse_raw, dict):
                    # Stamp owning bot on each pair pulse for tab filtering.
                    pulse = {}
                    for pk, pv in pulse_raw.items():
                        if isinstance(pv, dict):
                            row = dict(pv)
                            row.setdefault("_bot", bot)
                            row.setdefault("pair", pk)
                            pulse[pk] = row
                        else:
                            pulse[pk] = {"_bot": bot, "pair": pk, "raw": pv}
                niche_raw = raw.get("__gp_niche_map__")
                if isinstance(niche_raw, dict):
                    niche_map = niche_raw
                for pair, inds in raw.items():
                    if str(pair).startswith("__"):
                        continue
                    if isinstance(inds, list):
                        pairs[pair] = [_enrich_indicator(i, bot=bot, pair=pair) for i in inds]
    except Exception:
        pass
    # Synthesize niche map from indicators when push lacked it.
    if not niche_map:
        for pair, inds in pairs.items():
            niche_map[pair] = {
                "filled": 0,
                "total_cells": 0,
                "coverage": 0.0,
                "counts": {},
            }
            try:
                from hermes_core.engines.genetic import niche_map_from_indicators

                niche_map[pair] = niche_map_from_indicators(inds)
            except Exception:
                pass
    ensemble = {pair: _ensemble_for_pair(inds) for pair, inds in pairs.items()}
    return {
        "pairs": pairs,
        "ensemble": ensemble,
        "degradation": _degradation_for_pairs(pairs),
        "discovery_pulse": pulse,
        "niche_map": niche_map,
        "total_indicators": sum(len(v) for v in pairs.values()),
        "total_pairs": len(pairs),
        "bot": bot,
    }


# ----------------------------------------------------------------------------
# /api/cortex  ->  {bot:{summary, exiled, indicators, policy}}
# ----------------------------------------------------------------------------


def _build_cortex(bot: str) -> dict:
    # Read from latest_state (SQLite). The bot pushes cortex as
    # {bot:{summary,exiled,indicators,policy,exile_detail}} (the exile file).
    try:
        from dashboard.backend.main import get_conn

        conn = get_conn()
        row = conn.execute("SELECT cortex_json FROM latest_state WHERE bot=?", (bot,)).fetchone()
        conn.close()
        if row and row["cortex_json"]:
            raw = json.loads(row["cortex_json"])
            if isinstance(raw, dict) and raw.get(bot):
                return {bot: raw[bot]}
    except Exception:
        pass
    return {bot: {"summary": {}, "exiled": [], "indicators": {}, "policy": {}, "exile_detail": {}}}


# ----------------------------------------------------------------------------
# /api/heartbeat/{bot}  ->  raw heartbeat json (or {})
# ----------------------------------------------------------------------------


def _build_heartbeat(bot: str) -> dict:
    # Read from latest_state (SQLite) — the only cross-service channel (each
    # Railway service has its own /data volume, so file reads were stale).
    try:
        from dashboard.backend.main import get_conn

        conn = get_conn()
        row = conn.execute("SELECT heartbeat_json FROM latest_state WHERE bot=?", (bot,)).fetchone()
        conn.close()
        if row and row["heartbeat_json"]:
            return json.loads(row["heartbeat_json"])
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# /api/flatline/{bot}  ->  list of flatline events (from flatline_log.jsonl)
# Faithful to the original dashboard backend, which read these state files.
# ---------------------------------------------------------------------------


def _build_flatline(bot: str) -> list[dict]:
    """Flatline events for the Flatline tab.

    Prefer SQLite ``flatlined_json`` (pushed by the bot on ingest) so Railway
    works across service volumes. Fall back to local flatline_log.jsonl for
    single-process/dev.
    """
    try:
        from dashboard.backend.main import get_conn

        conn = get_conn()
        row = conn.execute("SELECT flatlined_json FROM latest_state WHERE bot=?", (bot,)).fetchone()
        conn.close()
        if row and row["flatlined_json"]:
            raw = json.loads(row["flatlined_json"])
            if isinstance(raw, list):
                return [x for x in raw if isinstance(x, dict)]
            if isinstance(raw, dict):
                # Legacy shape: {pair: reason} or {events: [...]}
                if isinstance(raw.get("events"), list):
                    return [x for x in raw["events"] if isinstance(x, dict)]
                return [
                    {"pair": k, **(v if isinstance(v, dict) else {"reason": v})}
                    for k, v in raw.items()
                    if k != "events"
                ]
    except Exception:
        pass
    sdir = _bot_state_dir(bot)
    if sdir:
        f = sdir / "flatline_log.jsonl"
        if f.exists():
            return _read_jsonl(f)
    return []


# ---------------------------------------------------------------------------
# Registration — must run BEFORE base routes are defined so these win.
# ---------------------------------------------------------------------------


def register(app, ingest_token_getter, valid_bots, on_price_broadcast=None):
    """Register compat routes on the FastAPI app.

    ingest_token_getter: callable returning current INGEST_TOKEN (may be '').
    valid_bots: set of valid bot names.
    """
    from fastapi import HTTPException, Request

    def _check_token(request: Request):
        tok = ingest_token_getter()
        if tok and request.headers.get("X-Ingest-Token", "") != tok:
            raise HTTPException(401, "Invalid or missing ingest token")

    _PRICE_LOCK = None  # unused placeholder for parity with prior layout

    def _price_store_path(bot: str) -> Path:
        sdir = _bot_state_dir(bot) or (Path("/tmp") / "hermes_prices")
        sdir.mkdir(parents=True, exist_ok=True)
        return sdir / f"live_prices_{bot}.json"

    def _post_price(bot: str, payload: dict) -> dict:
        prices = payload.get("prices")
        if prices is None:
            prices = {}
        if not isinstance(prices, dict):
            from fastapi import HTTPException

            raise HTTPException(400, "prices must be a dict")
        ts = payload.get("ts") or _utcnow()
        store = {}
        for pair, price in prices.items():
            try:
                store[pair] = {"price": float(price), "ts": ts}
            except (TypeError, ValueError):
                continue
        path = _price_store_path(bot)
        with contextlib.suppress(Exception):
            path.write_text(json.dumps(store), encoding="utf-8")
        if on_price_broadcast and store:
            on_price_broadcast(bot, {k: v["price"] for k, v in store.items()})
        return {"status": "received", "bot": bot, "count": len(store), "n": len(store)}

    def _get_price(bot: str) -> dict:
        path = _price_store_path(bot)
        data = _read_json(path)
        return data if isinstance(data, dict) else {}

    @app.get("/api/discovered")
    def compat_discovered():
        result = {
            "pairs": {},
            "ensemble": {},
            "degradation": {},
            "discovery_pulse": {},
            "niche_map": {},
            "bots": {},
            "total_indicators": 0,
            "total_pairs": 0,
        }
        seen: set[tuple] = set()  # (bot, pair, name, expr) dedupe
        for bot in valid_bots:
            d = _build_discovered(bot)
            result["bots"][bot] = {
                "total_indicators": d["total_indicators"],
                "total_pairs": d["total_pairs"],
            }
            for p, inds in d["pairs"].items():
                if str(p).startswith("__"):
                    continue
                result["pairs"].setdefault(p, [])
                for ind in inds:
                    key = (bot, p, ind.get("name"), ind.get("expr"))
                    if key in seen:
                        continue
                    seen.add(key)
                    result["pairs"][p].append(ind)
            for p, e in d["ensemble"].items():
                result["ensemble"][p] = e
            result["degradation"].update(d["degradation"])
            # Merge Phase B pulse / niche maps. Tag bot so the Discovered tab
            # can filter (BTC pulses must not appear under Forex).
            for p, pulse in (d.get("discovery_pulse") or {}).items():
                row = dict(pulse) if isinstance(pulse, dict) else {"raw": pulse}
                row.setdefault("_bot", bot)
                row.setdefault("pair", p)
                result["discovery_pulse"][p] = row
            for p, nm in (d.get("niche_map") or {}).items():
                result["niche_map"][p] = nm
        for p, inds in result["pairs"].items():
            result["ensemble"][p] = _ensemble_for_pair(inds)
        result["degradation"] = _degradation_for_pairs(result["pairs"])
        result["total_indicators"] = sum(len(v) for v in result["pairs"].values())
        result["total_pairs"] = len(result["pairs"])
        return result

    @app.get("/api/heartbeat/{bot_name}")
    def compat_heartbeat(bot_name: str):
        if bot_name not in valid_bots:
            return {}
        return _build_heartbeat(bot_name)

    @app.get("/api/flatline/{bot_name}")
    def compat_flatline(bot_name: str):
        if bot_name not in valid_bots:
            return []
        return _build_flatline(bot_name)

    @app.get("/api/discovered/{bot_name}")
    def compat_discovered_bot(bot_name: str):
        if bot_name not in valid_bots:
            return []
        d = _build_discovered(bot_name)
        pairs = d.get("pairs") or {}
        out: list = []
        for inds in pairs.values():
            if isinstance(inds, list):
                out.extend(inds)
        return out

    @app.post("/api/price/{bot_name}")
    async def compat_price_post(bot_name: str, request: Request):
        if bot_name not in valid_bots:
            raise HTTPException(404, f"Unknown bot '{bot_name}'")
        _check_token(request)
        try:
            payload = await request.json()
        except Exception as err:
            raise HTTPException(400, "Body must be valid JSON") from err
        return _post_price(bot_name, payload)

    @app.get("/api/price/{bot_name}")
    def compat_price_get(bot_name: str):
        if bot_name not in valid_bots:
            return {}
        return _get_price(bot_name)
