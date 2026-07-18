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

All reads are fail-soft: missing files -> empty/[] , never a 500.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path


def _utcnow() -> str:
    """ISO-8601 UTC timestamp used as the default price-store ts.

    The live bots POST {"prices": {...}} WITHOUT a ts field, so the
    compat price route falls back to this. (Was referenced but never
    defined -> NameError -> HTTP 500 on every price push. FIXED.)
    """
    return datetime.now(timezone.utc).isoformat()

# ----------------------------------------------------------------------------
# Locate the bots' state directories.
# Priority (env override wins, then common layouts):
#   HERMES_STATE_ROOT/{bot}/state
#   /app/bots/{bot}/state          (Railway: bots + dashboard on same image/volume)
#   <repo>/bots/{bot}/state        (local dev)
#   /data/bots/{bot}/state
# ----------------------------------------------------------------------------

def _candidate_state_roots() -> list[Path]:
    roots = []
    env = os.getenv("HERMES_STATE_ROOT")
    if env:
        roots.append(Path(env))
    roots += [
        Path("/app/bots"),
        Path(__file__).resolve().parent.parent.parent / "bots",  # dashboard/backend -> repo/bots
        Path("/data/bots"),
        Path("/app/state"),
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

def _build_discovered(bot: str) -> dict:
    sdir = _bot_state_dir(bot)
    pairs: dict[str, list] = {}
    deg: dict = {}
    if sdir:
        disc_dir = sdir / "discovered"
        if disc_dir.exists():
            # rglob catches both flat (EUR_USD.json) and nested (EUR/USD.json)
            # naming the live bots use. Derive pair name from the relative path.
            for f in sorted(disc_dir.rglob("*.json")):
                rel = f.relative_to(disc_dir).with_suffix("")
                pair_name = str(rel).replace(os.sep, "/")
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                inds = data if isinstance(data, list) else data.get("indicators", [])
                if not isinstance(inds, list):
                    continue
                enriched = []
                for ind in inds:
                    if not isinstance(ind, dict):
                        continue
                    es = ind.get("expr_str", ind.get("expr", ""))
                    enriched.append({
                        "name": ind.get("name", "?"),
                        "expr": es[:80],
                        "fitness": round(float(ind.get("fitness", 0) or 0), 4),
                        "win_rate": float(ind.get("win_rate", 0) or 0),
                        "total_pnl": round(float(ind.get("total_pnl", 0) or 0), 4),
                        "uses": [k for k in ["volume", "dxy", "vix", "tnx", "spx", "oil", "gold", "btc", "fvx", "eem"] if k in es],
                        "discovered_at": ind.get("discovered_at", "unknown"),
                        "source": ind.get("source", "seed"),
                    })
                if enriched:
                    pairs[pair_name] = enriched
        deg_file = sdir / "discovered" / "_live_deg.json"
        if deg_file.exists():
            try:
                deg = json.loads(deg_file.read_text(encoding="utf-8"))
            except Exception:
                deg = {}
    ensemble = {}
    for pair, inds in pairs.items():
        if not inds:
            ensemble[pair] = {"status": "no_indicators", "signal": 0}
            continue
        tw = sum((i["fitness"] * i["win_rate"]) for i in inds if i["fitness"] > 0)
        bw = sum((i["fitness"] * i["win_rate"]) for i in inds if i.get("win_rate", 0.5) > 0.5)
        signal = ((bw - (tw - bw)) / max(tw, 0.001))
        ensemble[pair] = {
            "signal": round(signal, 3),
            "num_indicators": len(inds),
            "multi_dim": sum(1 for i in inds if i["uses"]),
            "best_fitness": max((i["fitness"] for i in inds), default=0),
            "best_wr": max((i["win_rate"] for i in inds), default=0),
        }
    return {
        "pairs": pairs,
        "ensemble": ensemble,
        "degradation": deg,
        "total_indicators": sum(len(v) for v in pairs.values()),
        "total_pairs": len(pairs),
    }


# ----------------------------------------------------------------------------
# /api/cortex  ->  {bot:{summary, exiled, indicators, policy}}
# ----------------------------------------------------------------------------

def _build_cortex(bot: str) -> dict:
    sdir = _bot_state_dir(bot)
    exile = {}
    tracker = {}
    policy = {}
    if sdir:
        cortex_dir = sdir / "cortex"
        if cortex_dir.exists():
            exile = _read_json(cortex_dir / "indicator_exile.json") or {}
            tracker = _read_json(cortex_dir / "indicator_tracker.json") or {}
            policy = _read_json(cortex_dir / "policy.json") or {}
            if not isinstance(exile, dict):
                exile = {}
            if not isinstance(tracker, dict):
                tracker = {}
            if not isinstance(policy, dict):
                policy = {}
    exiled_names = list(exile.keys())
    summary = {
        "entries_total": len(tracker),
        "entries_open": 0,
        "exiled_indicators": len(exiled_names),
        "indicators_tracked": len(tracker),
        "by_entry_type": {},
        "by_pair": {},
        "version": policy.get("version", "?"),
    }
    return {
        bot: {
            "summary": summary,
            "exiled": exiled_names,
            "indicators": tracker,
            "policy": policy,
            "exile_detail": exile,
        }
    }


# ----------------------------------------------------------------------------
# /api/heartbeat/{bot}  ->  raw heartbeat json (or {})
# ----------------------------------------------------------------------------

def _build_heartbeat(bot: str) -> dict:
    sdir = _bot_state_dir(bot)
    if sdir:
        hb = _read_json(sdir / f"heartbeat_{bot}.json") or _read_json(sdir / "heartbeat.json")
        if hb:
            return hb
    return {}


# ---------------------------------------------------------------------------
# /api/flatline/{bot}  ->  list of flatline events (from flatline_log.jsonl)
# Faithful to the original dashboard backend, which read these state files.
# ---------------------------------------------------------------------------

def _build_flatline(bot: str) -> list[dict]:
    sdir = _bot_state_dir(bot)
    if sdir:
        f = sdir / "flatline_log.jsonl"
        if f.exists():
            return _read_jsonl(f)
    return []


# ---------------------------------------------------------------------------
# Registration — must run BEFORE base routes are defined so these win.
# ---------------------------------------------------------------------------

def register(app, ingest_token_getter, valid_bots):
    """Register compat routes on the FastAPI app.

    ingest_token_getter: callable returning current INGEST_TOKEN (may be '').
    valid_bots: set of valid bot names.
    """
    from fastapi import Request, HTTPException

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
        prices = payload.get("prices") or {}
        if not isinstance(prices, dict):
            return {"status": "ignored", "reason": "no prices"}
        ts = payload.get("ts") or _utcnow()
        store = {}
        for pair, price in prices.items():
            try:
                store[pair] = {"price": float(price), "ts": ts}
            except (TypeError, ValueError):
                continue
        path = _price_store_path(bot)
        try:
            path.write_text(json.dumps(store), encoding="utf-8")
        except Exception:
            pass
        return {"status": "received", "bot": bot, "count": len(store)}

    def _get_price(bot: str) -> dict:
        path = _price_store_path(bot)
        data = _read_json(path)
        return data if isinstance(data, dict) else {}

    @app.get("/api/discovered")
    def compat_discovered():
        result = {"pairs": {}, "ensemble": {}, "degradation": {}, "total_indicators": 0, "total_pairs": 0}
        for bot in valid_bots:
            d = _build_discovered(bot)
            for p, inds in d["pairs"].items():
                result["pairs"].setdefault(p, [])
                result["pairs"][p].extend(inds)
            for p, e in d["ensemble"].items():
                result["ensemble"][p] = e
            result["degradation"].update(d["degradation"])
            result["total_indicators"] += d["total_indicators"]
            result["total_pairs"] = len(result["pairs"])
        return result

    @app.get("/api/cortex")
    def compat_cortex():
        result = {}
        any_data = False
        for bot in valid_bots:
            c = _build_cortex(bot)
            if c.get(bot, {}).get("exiled") or c.get(bot, {}).get("indicators"):
                any_data = True
            result.update(c)
        if not any_data:
            return {"status": "no_data"}
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

    @app.post("/api/price/{bot_name}")
    async def compat_price_post(bot_name: str, request: Request):
        if bot_name not in valid_bots:
            raise HTTPException(404, f"Unknown bot '{bot_name}'")
        _check_token(request)
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(400, "Body must be valid JSON")
        return _post_price(bot_name, payload)

    @app.get("/api/price/{bot_name}")
    def compat_price_get(bot_name: str):
        if bot_name not in valid_bots:
            return {}
        return _get_price(bot_name)
