"""outcome_correlator.py — Layer 5: Outcome Correlation.

Links audit findings to actual PnL impact. Queries the dashboard DB
to find trades related to each finding and computes the financial cost
of unresolved bugs.

Usage:
  python outcome_correlator.py                                # Correlate all open findings
  python outcome_correlator.py --finding abc123               # One finding
  python outcome_correlator.py --domain risk-safety           # By domain
"""

import json
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from findings_store import _get_conn, list_findings, get_finding

DASHBOARD_DB_PATH = Path("D:/projects/hermes-dashboard-api/data/trading.db")


# ── Keyword → Finding mapping ─────────────────────────────────────────────

# Map finding patterns to trade-related keywords
# Each entry: keyword → (domain, finding_type, description)
PATTERN_MAP = {
    "spread": ("static-code", "GAP", "Spread check silently passes errors"),
    "stop_loss": ("risk-safety-boundaries", "RISK", "Stop loss related issues"),
    "profit_target": ("risk-safety-boundaries", "GAP", "Profit target handling"),
    "hermes_forex": ("static-code", "BUG", "Cross-contamination between bots"),
    "FOREX_PAIRS": ("strategy-logic", "BUG", "Gold bot reads FOREX_PAIRS"),
    "railway down": ("static-code", "RISK", "railway down kills container"),
    "yaml": ("risk-safety-boundaries", "RISK", "YAML loading without validation"),
    "snapshot": ("static-code", "BUG", "Snapshot scoping bug"),
    "latch": ("strategy-logic", "GAP", "Reflection latch not version-keyed"),
    "ate_limit": ("static-code", "GAP", "No rate limit between LLM calls"),
    "consecutive": ("risk-safety-boundaries", "GAP", "Flatline only counts stop_loss exits"),
    "auth": ("static-code", "GAP", "No authentication on API endpoints"),
    "crisis_embeddings": ("static-code", "LOW", "Both bots write to same crisis file"),
}


def get_dashboard_conn() -> Optional[sqlite3.Connection]:
    if not DASHBOARD_DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(DASHBOARD_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def correlate_finding(finding: dict) -> dict:
    """Find trades related to a finding and compute PnL impact.

    Uses location keywords and description patterns to match against
    trade data in the dashboard DB.
    """
    finding_id = finding["id"]
    domain = finding["domain"]
    location = finding.get("location", "") or ""
    description = finding.get("description", "") or ""
    status = finding["status"]

    result = {
        "finding_id": finding_id,
        "severity": finding["severity"],
        "domain": domain,
        "description": description[:120],
        "status": status,
        "potential_trades_found": 0,
        "total_pnl_impact": 0.0,
        "win_rate_impact": None,
        "related_trades": [],
        "confidence": 0,
        "note": "",
    }

    # Extract keywords from location and description
    keywords = set()
    loc_lower = location.lower()
    desc_lower = description.lower()
    for kw in PATTERN_MAP:
        if kw in loc_lower or kw in desc_lower:
            keywords.add(kw)

    if not keywords:
        result["note"] = "No correlatable keywords found in finding"
        return result

    # Query dashboard DB for trades matching keywords
    conn = get_dashboard_conn()
    if not conn:
        result["note"] = f"Dashboard DB not found at {DASHBOARD_DB_PATH}"
        return result

    try:
        # Search trade exit_reasons and other fields for keywords
        conditions = []
        params = []
        for kw in keywords:
            conditions.append("LOWER(exit_reason) LIKE ?")
            params.append(f"%{kw}%")

        if conditions:
            where = " OR ".join(conditions)
            rows = conn.execute(
                f"""SELECT bot, pair, pnl_pct, exit_reason, entry_ts, exit_ts
                    FROM trades WHERE ({where})
                    ORDER BY exit_ts DESC LIMIT 50""",
                params,
            ).fetchall()

            if rows:
                pnls = [r["pnl_pct"] or 0 for r in rows]
                total_pnl = sum(pnls)
                win_rate = (sum(1 for p in pnls if p > 0) / len(pnls) * 100) if pnls else 0

                result["potential_trades_found"] = len(rows)
                result["total_pnl_impact"] = round(total_pnl, 2)
                result["win_rate_impact"] = round(win_rate, 1)
                result["related_trades"] = [
                    {
                        "bot": r["bot"],
                        "pair": r["pair"],
                        "pnl_pct": r["pnl_pct"],
                        "exit_reason": r["exit_reason"],
                        "exit_ts": r["exit_ts"],
                    }
                    for r in rows[:10]
                ]
                result["confidence"] = min(90, 50 + len(rows) * 5)
                if status in ("pending", "approved"):
                    result["note"] = f"Unresolved finding — {len(rows)} related trades found, {total_pnl:+.2f}% total PnL impact"
                elif status == "applied":
                    result["note"] = f"Fix applied — {len(rows)} historical trades linked to this pattern"
            else:
                result["note"] = f"No trades found matching keywords: {keywords}"
                result["confidence"] = 30
    except Exception as e:
        result["note"] = f"DB query error: {e}"
    finally:
        conn.close()

    return result


def correlate_all(domain: Optional[str] = None, verbose: bool = True) -> list[dict]:
    """Correlate all open findings (or by domain) with trade impact."""
    findings = list_findings(
        status=None,  # all statuses
        domain=domain,
        limit=100,
    )

    # Filter to non-test findings
    real_findings = [f for f in findings if f.get("domain") != "test-domain"]

    if not real_findings:
        if verbose:
            print("[CORRELATE] No findings to correlate")
        return []

    results = []
    for f in real_findings:
        r = correlate_finding(f)
        results.append(r)
        if verbose and r["potential_trades_found"] > 0:
            sev_icon = "🔴" if r["severity"] == "CRITICAL" else "🟡" if r["severity"] == "HIGH" else "ℹ️"
            print(f"  {sev_icon} [{r['domain']:>22}] {r['description'][:70]}")
            print(f"      → {r['potential_trades_found']} trades, {r['total_pnl_impact']:+.2f}% PnL, {r['win_rate_impact']}% WR")

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Outcome Correlator (Layer 5)")
    parser.add_argument("--finding", type=str, help="Correlate a specific finding")
    parser.add_argument("--domain", type=str, help="Filter by domain")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.finding:
        f = get_finding(args.finding)
        if f:
            result = correlate_finding(f)
            print(json.dumps(result, indent=2, default=str))
        else:
            print(f"Finding {args.finding} not found")
    else:
        results = correlate_all(domain=args.domain, verbose=not args.json)
        if args.json:
            print(json.dumps(results, indent=2, default=str))
