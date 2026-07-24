"""post_mortem.py — #2: Post-Mortem Trade Analysis.

When a trade closes with >2x expected loss (or unusual exit), captures
the full decision trace and runs LLM analysis to find what the bot missed.

Triggered by: cron job polling the dashboard API for recent bad trades.
Output: GAP-type findings in audit_findings table.

What it catches:
  - Economic calendar events that bypassed the guard
  - Regime changes mid-trade
  - Data feed issues (stale prices)
  - Strategy configuration mismatches
"""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from findings_store import get_latest_run, insert_finding, list_findings

DASHBOARD_API_URL = os.environ.get(
    "DASHBOARD_API_URL",
    "https://hermes-dashboard-api-production.up.railway.app",
)

POST_MORTEM_PROMPT = """You are Hermes-Coroner, analysing why a trade lost money. Given the trade details and recent system state, determine:

1. Was this a normal loss (expected within strategy parameters)?
2. Was there a system failure (bug, data issue, configuration error)?
3. Was there a gap in the strategy (market condition not handled)?

Rules:
- If normal loss → return {"actionable": false}
- If system failure → return GAP finding with specific root cause
- If strategy gap → return UPGRADE finding with recommendation
- Be specific: cite exact values, timestamps, and conditions
- Output ONLY valid JSON matching this schema:
{
  "actionable": boolean,
  "finding": {
    "type": "GAP" | "UPGRADE",
    "severity": "HIGH" | "MEDIUM" | "LOW",
    "title": "Root cause summary",
    "description": "Detailed analysis of what went wrong",
    "recommendation": "Specific fix or config change",
    "confidence": 0-100,
    "contributing_factors": ["factor 1", "factor 2"]
  } | null
}"""


def get_recent_bad_trades(hours_back: int = 24) -> list[dict]:
    """Fetch trades closed in the last N hours with >2x expected loss."""
    import httpx

    bad_trades = []
    try:
        r = httpx.get(f"{DASHBOARD_API_URL}/api/overview", timeout=10)
        if r.status_code != 200:
            return bad_trades

        data = r.json()
        for bot_name, bot in data.get("bots", {}).items():
            for t in bot.get("recent_trades", []):
                if not t.get("exit_reason"):
                    continue
                pnl = t.get("pnl_pct", 0) or 0
                if pnl < -0.5:  # >0.5% loss threshold
                    bad_trades.append(
                        {
                            "bot": bot_name,
                            "id": t.get("id", "?"),
                            "pair": t.get("pair") or t.get("asset", "?"),
                            "pnl_pct": round(pnl, 3),
                            "exit_reason": t.get("exit_reason", "?"),
                            "entry_ts": t.get("entry_ts", "?"),
                            "exit_ts": t.get("exit_ts", "?"),
                            "entry_price": t.get("entry_price", "?"),
                            "hold_cycles": t.get("hold_cycles", 0),
                        }
                    )

        # Sort by loss size (largest first)
        bad_trades.sort(key=lambda x: x["pnl_pct"])
        return bad_trades[:5]  # Top 5 worst trades

    except Exception as e:
        print(f"[POSTMORTEM] Fetch error: {e}")
        return []


def check_already_analyzed(trade_id: str) -> bool:
    """Check if this trade was already post-mortem'd to avoid duplicates."""
    existing = list_findings(domain="performance-drift", limit=100)
    return any(trade_id in (f.get("location") or "") for f in existing)


def analyze_trade(trade: dict) -> dict:
    """Run LLM analysis on a single bad trade."""
    user_prompt = (
        f"Analyze this losing trade:\n\n"
        f"Bot: {trade['bot']}\n"
        f"Pair: {trade['pair']}\n"
        f"PnL: {trade['pnl_pct']}%\n"
        f"Exit reason: {trade['exit_reason']}\n"
        f"Held for: {trade['hold_cycles']} cycles\n"
        f"Entry price: {trade['entry_price']}\n"
        f"Entry: {trade['entry_ts']}\n"
        f"Exit: {trade['exit_ts']}\n\n"
        f"Is this a normal loss or a system issue? Be specific."
    )

    try:
        import httpx

        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            env_path = _HERE / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    if line.startswith("DEEPSEEK_API_KEY="):
                        api_key = line.split("=", 1)[1]
                        break
        if not api_key:
            return {"actionable": False}

        response = httpx.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "system", "content": POST_MORTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 1024,
                "temperature": 0.1,
            },
            timeout=30,
        )

        if response.status_code != 200:
            return {"actionable": False}

        result = response.json()
        content = result["choices"][0]["message"]["content"]
        return json.loads(content)

    except Exception as e:
        print(f"[POSTMORTEM] LLM error: {e}")
        return {"actionable": False}


def run_post_mortem(verbose: bool = True) -> list[dict]:
    """Check for recent bad trades and analyze them."""
    if verbose:
        print(f"\n{'=' * 40}")
        print(f"POST-MORTEM — {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'=' * 40}")

    bad_trades = get_recent_bad_trades()
    if not bad_trades:
        if verbose:
            print("No bad trades in last 24h")
        return []

    if verbose:
        print(f"Found {len(bad_trades)} bad trades to analyze")

    stored = []
    for trade in bad_trades:
        trade_id = trade.get("id", f"{trade['pair']}_{trade['exit_ts']}")

        if check_already_analyzed(trade_id):
            if verbose:
                print(f"  ⏭  {trade['pair']} — already analyzed")
            continue

        if verbose:
            print(
                f"  🔍 {trade['bot']} {trade['pair']}: {trade['pnl_pct']}% ({trade['exit_reason']})"
            )

        result = analyze_trade(trade)

        if not result.get("actionable"):
            if verbose:
                print("     → Normal loss, not actionable")
            continue

        finding_data = result.get("finding", {})
        if not finding_data:
            continue

        latest_run = get_latest_run()
        audit_run_id = (
            latest_run["id"] if latest_run else f"pm-{datetime.now(UTC).strftime('%Y%m%d%H')}"
        )

        finding = insert_finding(
            domain="performance-drift",
            finding_type=finding_data.get("type", "GAP"),
            severity=finding_data.get("severity", "MEDIUM"),
            location=f"post_mortem/{trade['bot']}/{trade_id}",
            description=f"[Post-Mortem] {finding_data.get('title', '')}: {finding_data.get('description', '')[:200]}",
            trading_impact=f"Trade lost {trade['pnl_pct']}% on {trade['pair']}. {finding_data.get('description', '')[:100]}",
            suggested_fix=finding_data.get("recommendation", "Review trade logic"),
            confidence=finding_data.get("confidence", 60),
            audit_run_id=audit_run_id,
        )
        stored.append(finding)
        if verbose:
            factors = finding_data.get("contributing_factors", [])
            print(f"     → {finding_data.get('title', '?')[:80]}")
            for f in factors:
                print(f"       • {f}")

    return stored


if __name__ == "__main__":
    results = run_post_mortem(verbose=True)
    print(f"\nStored {len(results)} post-mortem findings")
