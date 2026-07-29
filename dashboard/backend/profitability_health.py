"""Profitability Path health snapshot for the dashboard.

Builds a watcher-friendly OK / WARN / FAIL report from latest bot heartbeats
and closed trades in SQLite — so the UI can show what to trust without CLI.
"""

from __future__ import annotations

import json
import time
from typing import Any

# Keep in sync with hermes_core.engines.profitability_freeze / feed_health.
FOCUS_PAIRS: dict[str, list[str]] = {
    "forex": ["EUR/USD", "GBP/USD"],
    "gold": ["XAU/USD"],
    "crypto": ["BTC/USD", "ETH/USD"],
}

PHASE0_ON = ("BOOK_RISK",)
PHASE0_OFF = (
    "SOFT_WEIGHTS",
    "KELLY_SIZING",
    "REGIME_SIZING",
    "ENTRY_RANKING",
    "EXIT_INTEL",
    "PROBE_SIZING",
    "SKIP_SHADOW_REFLECT",
    "SKIP_SHADOW_PROMOTE",
    "CRISIS_RECOMMEND",
    "GP_PROMOTE",
)

MIN_SANE: dict[str, float] = {
    "XAU/USD": 500.0,
    "XAG/USD": 5.0,
    "EUR/USD": 0.5,
    "GBP/USD": 0.5,
    "AUD/USD": 0.3,
    "GBP/JPY": 50.0,
    "BTC/USD": 1000.0,
    "ETH/USD": 50.0,
}
MAX_SANE: dict[str, float] = {
    "XAU/USD": 20_000.0,
    "XAG/USD": 500.0,
    "EUR/USD": 3.0,
    "GBP/USD": 4.0,
    "AUD/USD": 3.0,
    "GBP/JPY": 400.0,
    "BTC/USD": 5_000_000.0,
    "ETH/USD": 500_000.0,
}

DEFAULT_COST_PCT = 0.05
DEFAULT_MAX_HB_AGE_S = 900.0
PHASE1_MIN_N = 20
# Profitability Path Phase 0 push ~ Jul 28 2026 UTC — score "since freeze" from here.
PHASE0_FREEZE_TS = 1753660800.0  # 2026-07-28T00:00:00Z


def _parse_trade_ts(row: Any) -> float:
    for key in ("exit_ts", "entry_ts"):
        raw = row[key] if hasattr(row, "keys") and key in row.keys() else None
        if raw is None and isinstance(row, dict):
            raw = row.get(key)
        if not raw:
            continue
        try:
            from datetime import datetime

            s = str(raw).replace("Z", "+00:00")
            return datetime.fromisoformat(s).timestamp()
        except (TypeError, ValueError):
            continue
    return 0.0


def _entry_type_of(row: Any) -> str:
    st = ""
    try:
        st = str(row["strategy_type"] or "")
    except Exception:
        st = ""
    raw = None
    try:
        raw = row["raw_json"]
    except Exception:
        raw = None
    if raw:
        try:
            blob = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(blob, dict):
                return str(blob.get("entry_type") or blob.get("strategy_type") or st or "")
        except (TypeError, json.JSONDecodeError):
            pass
    return st


def _price_sane(pair: str, price: Any) -> bool:
    try:
        px = float(price)
    except (TypeError, ValueError):
        return False
    lo = MIN_SANE.get(pair.upper(), 0.0)
    hi = MAX_SANE.get(pair.upper(), 1e12)
    return lo <= px <= hi


def _freeze_from_heartbeat(hb: dict) -> dict[str, Any]:
    hif = hb.get("hif_flags") if isinstance(hb.get("hif_flags"), dict) else {}
    flags = hif.get("flags") if isinstance(hif.get("flags"), dict) else {}
    enabled = sorted(k for k, v in flags.items() if v) if flags else list(hif.get("enabled") or [])
    violations: list[str] = []
    for key in PHASE0_ON:
        if flags and not flags.get(key):
            violations.append(f"{key} should be ON")
        elif not flags and key not in enabled:
            violations.append(f"{key} should be ON")
    for key in PHASE0_OFF:
        if flags.get(key) or key in enabled:
            violations.append(f"{key} should be OFF")
    if set(enabled) != set(PHASE0_ON):
        violations.append(f"enabled={enabled or ['(none)']} expected=['BOOK_RISK']")
    # Dedup
    seen: set[str] = set()
    uniq = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return {
        "ok": len(uniq) == 0 and bool(flags or enabled),
        "enabled": enabled,
        "violations": uniq,
        "missing_hif": not bool(flags or enabled),
    }


def _max_dd(pnls: list[float]) -> float:
    if not pnls:
        return 0.0
    eq = 0.0
    peak = 0.0
    dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    return round(dd, 4)


def _score_trades(rows: list[dict], *, cost: float) -> dict[str, Any]:
    pnls: list[float] = []
    for r in rows:
        try:
            pnls.append(float(r["pnl_pct"]) - cost)
        except (TypeError, ValueError, KeyError):
            continue
    n = len(pnls)
    if n == 0:
        return {
            "n": 0,
            "wr": 0.0,
            "expectancy": 0.0,
            "max_dd": 0.0,
            "verdict": "waiting",
            "sample_ok": False,
            "window": "empty",
        }
    wins = sum(1 for p in pnls if p > 0)
    exp = sum(pnls) / n
    if n < PHASE1_MIN_N:
        verdict = "waiting"
    elif exp > 0:
        verdict = "continue"
    else:
        verdict = "kill"
    return {
        "n": n,
        "wr": round(wins / n, 4),
        "expectancy": round(exp, 4),
        "max_dd": _max_dd(pnls),
        "verdict": verdict,
        "sample_ok": n >= PHASE1_MIN_N,
    }


def _bot_level(issues: list[dict]) -> str:
    if any(i.get("level") == "fail" for i in issues):
        return "fail"
    if any(i.get("level") == "warn" for i in issues):
        return "warn"
    return "ok"


def build_profitability_health(
    *,
    get_conn,
    bots: list[str] | tuple[str, ...] = ("forex", "gold", "crypto"),
    cost_pct: float | None = None,
    max_hb_age_s: float = DEFAULT_MAX_HB_AGE_S,
    now: float | None = None,
) -> dict[str, Any]:
    """Return fleet + per-bot health for the Live tab strip."""
    now = time.time() if now is None else float(now)
    try:
        import os

        cost = float(os.environ.get("SCORECARD_COST_PCT") or DEFAULT_COST_PCT)
    except ValueError:
        cost = DEFAULT_COST_PCT
    if cost_pct is not None:
        cost = float(cost_pct)

    conn = get_conn()
    try:
        bot_reports: dict[str, Any] = {}
        fleet_issues: list[dict] = []

        for bot in bots:
            issues: list[dict] = []
            focus = list(FOCUS_PAIRS.get(bot, []))
            state_row = conn.execute("SELECT * FROM latest_state WHERE bot=?", (bot,)).fetchone()
            hb: dict = {}
            if state_row and state_row["heartbeat_json"]:
                try:
                    hb = json.loads(state_row["heartbeat_json"]) or {}
                except (TypeError, json.JSONDecodeError):
                    hb = {}
            else:
                issues.append(
                    {
                        "level": "fail",
                        "code": "no_heartbeat",
                        "message": f"{bot}: no heartbeat — bot may be down or not pushing to the dashboard",
                        "what_to_tell": f"{bot} bot has no heartbeat on the dashboard",
                    }
                )

            ts = float(hb.get("ts") or 0.0) if hb else 0.0
            age = (now - ts) if ts > 0 else None
            if hb and (age is None or age > max_hb_age_s):
                issues.append(
                    {
                        "level": "fail",
                        "code": "stale_heartbeat",
                        "message": f"{bot}: heartbeat is stale ({int(age or 0)}s old)",
                        "what_to_tell": f"{bot} heartbeat is stale",
                    }
                )

            status = str(hb.get("status") or "")
            if status in ("degraded", "error", "halted"):
                issues.append(
                    {
                        "level": "fail" if status != "degraded" else "warn",
                        "code": "bad_status",
                        "message": f"{bot}: status is {status}",
                        "what_to_tell": f"{bot} status is {status}",
                    }
                )

            freeze = (
                _freeze_from_heartbeat(hb)
                if hb
                else {"ok": False, "enabled": [], "violations": ["no heartbeat"], "missing_hif": True}
            )
            if hb and freeze.get("missing_hif"):
                issues.append(
                    {
                        "level": "warn",
                        "code": "missing_hif",
                        "message": f"{bot}: freeze flags not in heartbeat yet (waiting for new deploy push)",
                        "what_to_tell": f"{bot} has not reported freeze flags yet",
                    }
                )
            elif hb and not freeze.get("ok"):
                issues.append(
                    {
                        "level": "fail",
                        "code": "freeze_broken",
                        "message": f"{bot}: freeze broken — {'; '.join(freeze.get('violations') or [])}",
                        "what_to_tell": f"{bot} freeze is broken: {', '.join(freeze.get('violations') or [])}",
                    }
                )

            prices = hb.get("prices") if isinstance(hb.get("prices"), dict) else {}
            pair_prices: dict[str, Any] = {}
            for pair in focus:
                px = prices.get(pair)
                sane = _price_sane(pair, px)
                pair_prices[pair] = {"price": px, "sane": sane}
                if px is None:
                    issues.append(
                        {
                            "level": "warn",
                            "code": "missing_price",
                            "message": f"{bot}: no live price for {pair}",
                            "what_to_tell": f"{bot} missing price for {pair}",
                        }
                    )
                elif not sane:
                    issues.append(
                        {
                            "level": "fail",
                            "code": "insane_price",
                            "message": f"{bot}: {pair} price looks fake/wrong ({px})",
                            "what_to_tell": f"{bot} {pair} price looks wrong ({px})",
                        }
                    )

            # Closed trades: focus pairs; prefer since-freeze non-GP for Phase 1 warn.
            rows = []
            for sql in (
                "SELECT pair, pnl_pct, exit_reason, exit_ts, entry_ts, strategy_type, raw_json "
                "FROM trades WHERE bot=? AND exit_reason IS NOT NULL AND exit_reason != '' "
                "AND pnl_pct IS NOT NULL ORDER BY COALESCE(exit_ts, entry_ts) DESC LIMIT 500",
                "SELECT pair, pnl_pct, exit_reason, exit_ts, entry_ts, raw_json "
                "FROM trades WHERE bot=? AND exit_reason IS NOT NULL AND exit_reason != '' "
                "AND pnl_pct IS NOT NULL ORDER BY COALESCE(exit_ts, entry_ts) DESC LIMIT 500",
                "SELECT pair, pnl_pct, exit_reason FROM trades "
                "WHERE bot=? AND exit_reason IS NOT NULL AND exit_reason != '' "
                "AND pnl_pct IS NOT NULL ORDER BY rowid DESC LIMIT 500",
            ):
                try:
                    rows = conn.execute(sql, (bot,)).fetchall()
                    break
                except Exception:
                    rows = []
                    continue
            lifetime: list[dict] = []
            since: list[dict] = []
            for r in rows:
                pair = r["pair"]
                if focus and pair not in focus:
                    continue
                rec = {"pair": pair, "pnl_pct": r["pnl_pct"]}
                lifetime.append(rec)
                ts = _parse_trade_ts(r)
                et = _entry_type_of(r).lower()
                if ts >= PHASE0_FREEZE_TS and et != "gp_ensemble":
                    since.append(rec)
            score_life = _score_trades(lifetime, cost=cost)
            score_life["window"] = "lifetime"
            score_since = _score_trades(since, cost=cost)
            score_since["window"] = "since_freeze"
            # Primary scorecard for UI = since-freeze when any samples; else lifetime.
            score = dict(score_since if score_since["n"] > 0 else score_life)
            score["lifetime"] = score_life
            score["since_freeze"] = score_since
            if score_since["verdict"] == "kill" and score_since["sample_ok"]:
                issues.append(
                    {
                        "level": "warn",
                        "code": "phase1_kill",
                        "message": (
                            f"{bot}: since freeze, after-cost expectancy is {score_since['expectancy']} "
                            f"over {score_since['n']} non-GP trades — Phase 1 kill"
                        ),
                        "what_to_tell": (
                            f"{bot} Phase 1 kill (since freeze): expectancy {score_since['expectancy']} "
                            f"after {score_since['n']} trades"
                        ),
                    }
                )
            elif (
                score_since["verdict"] == "waiting"
                and score_life["verdict"] == "kill"
                and score_life["sample_ok"]
            ):
                issues.append(
                    {
                        "level": "warn",
                        "code": "phase1_historical",
                        "message": (
                            f"{bot}: historical edge weak (exp {score_life['expectancy']} / "
                            f"{score_life['n']} trades) — watching new post-freeze trades "
                            f"({score_since['n']}/{PHASE1_MIN_N})"
                        ),
                        "what_to_tell": (
                            f"{bot} historical Phase 1 weak edge; only {score_since['n']} "
                            f"post-freeze non-GP trades so far"
                        ),
                    }
                )

            # GP live opens while promote should be off
            open_trades = []
            if state_row and state_row["open_trades_json"]:
                try:
                    open_trades = json.loads(state_row["open_trades_json"]) or []
                except (TypeError, json.JSONDecodeError):
                    open_trades = []
            gp_opens = [
                t
                for t in open_trades
                if isinstance(t, dict) and str(t.get("entry_type") or "") == "gp_ensemble"
            ]
            if gp_opens:
                issues.append(
                    {
                        "level": "warn",
                        "code": "gp_live_while_frozen",
                        "message": f"{bot}: {len(gp_opens)} GP Brain open trade(s) while GP promote should be OFF",
                        "what_to_tell": f"{bot} has GP Brain open trades but GP should be frozen",
                    }
                )

            rs = hb.get("regime_split") if isinstance(hb.get("regime_split"), dict) else {}
            if bot == "forex" and hb and rs and rs.get("enabled") is False:
                issues.append(
                    {
                        "level": "warn",
                        "code": "regime_split_off",
                        "message": "forex: regime split is OFF — set REGIME_SPLIT=1 or unset for default on",
                        "what_to_tell": "forex regime split is turned off",
                    }
                )

            level = _bot_level(issues)
            bot_reports[bot] = {
                "level": level,
                "focus_pairs": focus,
                "status": status or None,
                "heartbeat_age_s": round(age, 1) if age is not None else None,
                "freeze": freeze,
                "regime_split": rs
                if rs
                else {"enabled": None, "pending": bot == "forex"},
                "prices": pair_prices,
                "scorecard": score,
                "open_trades": len(open_trades) if isinstance(open_trades, list) else 0,
                "gp_open_trades": len(gp_opens),
                "issues": issues,
                "cycle": hb.get("cycle"),
            }
            fleet_issues.extend(issues)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    fleet_level = _bot_level(fleet_issues)
    headline = {
        "ok": "All clear — freeze on, feeds look sane. Keep watching.",
        "warn": "Something needs attention — read the yellow items below and tell Auto.",
        "fail": "Something is wrong — copy the red items and send them to Auto.",
    }[fleet_level]

    return {
        "ts": now,
        "level": fleet_level,
        "headline": headline,
        "cost_pct": cost,
        "phase1_min_n": PHASE1_MIN_N,
        "focus_pairs": FOCUS_PAIRS,
        "bots": bot_reports,
        "issues": fleet_issues,
        "what_to_report": [
            i.get("what_to_tell") or i.get("message")
            for i in fleet_issues
            if i.get("level") in ("fail", "warn")
        ],
    }
