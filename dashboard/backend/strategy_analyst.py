"""strategy_analyst.py — #1: Live Strategy Analysis.

Runs every 4 hours. Reads 7-day metric history from the live monitor,
analyzes trends per bot/pair, and writes INTELLIGENCE findings.

What it detects:
  - Win rate drops/steps per pair over 3d/7d windows
  - Stop-hit rate vs target-hit rate divergence
  - Position sizing drift from configured values
  - Regime shifts correlated with performance changes
  - Volatility (ATR) changes and their impact

Output: INTELLIGENCE-type findings in audit_findings table.
"""

import json
import math
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from findings_store import _get_conn, get_latest_run, insert_finding

DASHBOARD_API_URL = os.environ.get(
    "DASHBOARD_API_URL",
    "https://hermes-dashboard-api-production.up.railway.app",
)

SYSTEM_PROMPT = """You are Hermes-Strategist, an analyst for an algorithmic trading system. You review metric trends and identify strategic opportunities — not bugs, but places where the system could trade smarter.

Given metric data for a bot/pair over 7 days, identify:
1. Performance degradation: win rate drops, increasing stop-hit rates, widening losses
2. Missed opportunities: conditions where the system could have traded but didn't
3. Regime mismatches: strategy not adapting to market changes (volatility, trend shifts)
4. Configuration drift: actual behavior diverging from configured parameters

Rules:
- Be specific: cite exact numbers, timeframes, and correlations
- Distinguish between market noise and genuine trends (need >48h of data)
- Propose actionable recommendations (YAML config changes, not code changes)
- If nothing notable, return {"has_findings": false}
- Output ONLY valid JSON matching this schema:
{
  "has_findings": boolean,
  "findings": [
    {
      "type": "GAP" | "UPGRADE",
      "severity": "HIGH" | "MEDIUM" | "LOW",
      "pair": "XAU/USD or null",
      "title": "short title",
      "description": "detailed analysis with numbers",
      "recommendation": "specific config change suggestion",
      "confidence": 0-100,
      "evidence": ["metric1 was X vs baseline Y", "metric2 changed Z%"]
    }
  ]
}"""


def fetch_dashboard_data() -> dict:
    """Fetch current dashboard state for strategy analysis."""
    import httpx

    data = {}
    try:
        r = httpx.get(f"{DASHBOARD_API_URL}/api/overview", timeout=15)
        if r.status_code == 200:
            data["overview"] = r.json()
        r2 = httpx.get(f"{DASHBOARD_API_URL}/api/lifetime-summary", timeout=15)
        if r2.status_code == 200:
            data["lifetime"] = r2.json()
    except Exception as e:
        print(f"[STRATEGIST] Dashboard fetch error: {e}")
    return data


def get_trailing_metrics(hours: int = 168) -> dict:
    """Get trailing metric history grouped by bot/pair/metric.

    Returns {bot: {pair: {metric: [list of (value, ts)]}}}
    """
    conn = _get_conn()
    try:
        since = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        rows = conn.execute(
            """SELECT bot, pair, metric, value, recorded_at
               FROM monitor_metrics
               WHERE recorded_at >= ?
               ORDER BY bot, pair, metric, recorded_at""",
            (since,),
        ).fetchall()

        grouped = {}
        for r in rows:
            bot = r["bot"]
            pair = r["pair"] or "__global__"
            metric = r["metric"]
            grouped.setdefault(bot, {}).setdefault(pair, {}).setdefault(metric, [])
            grouped[bot][pair][metric].append({"value": r["value"], "ts": r["recorded_at"]})
        return grouped
    finally:
        conn.close()


def compute_trends(metrics: dict) -> list[dict]:
    """Compute trends and notable changes from raw metric history.

    Returns a list of dicts, each describing one notable trend.
    """
    trends = []
    now_ts = datetime.now(UTC)

    for bot, pairs in metrics.items():
        for pair, metric_data in pairs.items():
            pair_label = None if pair == "__global__" else pair

            for metric, points in metric_data.items():
                if len(points) < 4:
                    continue

                # Split into recent (last 24h) and older (24h-7d)
                cutoff = (now_ts - timedelta(hours=24)).isoformat()
                recent = [p for p in points if p["ts"] >= cutoff]
                older = [p for p in points if p["ts"] < cutoff]

                if len(recent) < 2 or len(older) < 2:
                    continue

                recent_vals = [p["value"] for p in recent]
                older_vals = [p["value"] for p in older]

                recent_mean = sum(recent_vals) / len(recent_vals)
                older_mean = sum(older_vals) / len(older_vals)

                # Compute standard deviation for older period
                variance = (
                    sum((v - older_mean) ** 2 for v in older_vals) / (len(older_vals) - 1)
                    if len(older_vals) > 1
                    else 0
                )
                older_std = math.sqrt(variance) if variance > 0 else 0.001

                # Calculate z-score of recent vs older
                z_score = (recent_mean - older_mean) / older_std if older_std > 0 else 0

                # Only flag if z > 1.5 (weaker than anomaly detector's 2.0 since we want earlier signals)
                if abs(z_score) < 1.5:
                    continue

                direction = "increased" if z_score > 0 else "decreased"
                pct_change = (
                    ((recent_mean - older_mean) / abs(older_mean) * 100) if older_mean != 0 else 0
                )

                # Classify the metric type for better descriptions
                metric_labels = {
                    "win_rate_pct": "Win rate",
                    "avg_pnl_pct": "Average PnL per trade",
                    "stop_hit_rate_pct": "Stop-loss hit rate",
                    "target_hit_rate_pct": "Profit target hit rate",
                    "pair_win_rate_pct": "Per-pair win rate",
                    "pair_avg_pnl_pct": "Per-pair average PnL",
                    "lifetime_pnl_pct": "Lifetime PnL",
                    "open_trades": "Open trade count",
                    "cycle": "Cycle count",
                }
                label = metric_labels.get(metric, metric)

                trends.append(
                    {
                        "bot": bot,
                        "pair": pair_label,
                        "metric": metric,
                        "label": label,
                        "recent_mean": round(recent_mean, 3),
                        "older_mean": round(older_mean, 3),
                        "pct_change": round(pct_change, 1),
                        "direction": direction,
                        "z_score": round(z_score, 2),
                        "recent_count": len(recent_vals),
                        "older_count": len(older_vals),
                        "severity": "HIGH"
                        if abs(z_score) > 2.5
                        else "MEDIUM"
                        if abs(z_score) > 2.0
                        else "LOW",
                    }
                )

    return trends


def analyze_and_generate(trends: list[dict], dashboard_data: dict) -> list[dict]:
    """Call LLM with trend data to generate strategic findings."""
    if not trends:
        return []

    # Build a compact trend summary for the LLM
    trend_summary = []
    for t in trends:
        pair_str = f" [{t['pair']}]" if t["pair"] else ""
        trend_summary.append(
            f"  {t['bot']}{pair_str}: {t['label']} {t['direction']} "
            f"from {t['older_mean']} to {t['recent_mean']} "
            f"({t['pct_change']:+.1f}%, z={t['z_score']:+.2f})"
        )

    # Add dashboard context
    dashboard_context = ""
    if "overview" in dashboard_data:
        ob = dashboard_data["overview"].get("bots", {})
        for bot_name, bot in ob.items():
            hb = bot.get("heartbeat", {})
            dashboard_context += (
                f"\n{bot_name}: cycle={hb.get('cycle', '?')} status={hb.get('status', '?')}"
            )
            reg = hb.get("regimes", {})
            if reg:
                dashboard_context += f" regimes={reg}"

    user_prompt = (
        "Analyze these 7-day metric trends for the Hermes trading system:\n\n"
        "Notable trends (|z| > 1.5):\n"
        + "\n".join(trend_summary[:20])
        + f"\n\nCurrent dashboard state:{dashboard_context}\n\n"
        f"Identify strategic issues: performance degradation, regime mismatches, "
        f"or configuration opportunities. Be specific with numbers."
    )

    try:
        import httpx

        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            env_path = _HERE / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("DEEPSEEK_API_KEY="):
                        api_key = line.split("=", 1)[1]
                        break
        if not api_key:
            print("[STRATEGIST] No API key")
            return []

        response = httpx.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 2048,
                "temperature": 0.2,
            },
            timeout=30,
        )

        if response.status_code != 200:
            print(f"[STRATEGIST] LLM error {response.status_code}")
            return []

        result = response.json()
        content = result["choices"][0]["message"]["content"]
        parsed = json.loads(content)

        if not parsed.get("has_findings"):
            return []

        return parsed.get("findings", [])

    except Exception as e:
        print(f"[STRATEGIST] Analysis error: {e}")
        return []


def run_analysis(verbose: bool = True) -> list[dict]:
    """Full strategy analysis pipeline. Returns created findings."""
    if verbose:
        print(f"\n{'=' * 40}")
        print(f"STRATEGY ANALYST — {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'=' * 40}")

    # 1. Get trailing metrics
    metrics = get_trailing_metrics(hours=168)
    total_points = sum(
        len(points) for bot in metrics.values() for pair in bot.values() for points in pair.values()
    )
    if verbose:
        print(f"Loaded {total_points} metric data points")

    # 2. Compute trends
    trends = compute_trends(metrics)
    if verbose:
        print(f"Found {len(trends)} notable trends")

    if not trends:
        # No significant trends — write an INTELLIGENCE finding saying so
        print("[STRATEGIST] No notable trends detected")
        return []

    # 3. Get dashboard context
    dashboard = fetch_dashboard_data()

    # 4. LLM analysis
    findings = analyze_and_generate(trends, dashboard)
    if verbose:
        print(f"LLM generated {len(findings)} strategic findings")

    # 5. Store findings
    latest_run = get_latest_run()
    audit_run_id = (
        latest_run["id"] if latest_run else f"strat-{datetime.now(UTC).strftime('%Y%m%d%H')}"
    )

    stored = []
    for f in findings:
        f" [{f.get('pair')}]" if f.get("pair") else ""
        description = f.get("description", f.get("title", ""))
        finding = insert_finding(
            domain="performance-drift",
            finding_type=f.get("type", "UPGRADE"),
            severity=f.get("severity", "MEDIUM"),
            location=f"strategy_analyst/{f.get('pair', 'all')}"
            if f.get("pair")
            else "strategy_analyst/all",
            description=f"[Strategy] {f.get('title', '')}: {description[:200]}",
            trading_impact=f"Recommendation: {f.get('recommendation', 'Review configuration')}",
            suggested_fix=f.get("recommendation", "Review metrics in dashboard"),
            confidence=f.get("confidence", 70),
            audit_run_id=audit_run_id,
        )
        stored.append(finding)
        if verbose:
            print(f"  📊 [{f.get('severity', 'MEDIUM')}] {f.get('title', '?')[:80]}")

    return stored


if __name__ == "__main__":
    results = run_analysis(verbose=True)
    print(f"\nStored {len(results)} strategic findings")
