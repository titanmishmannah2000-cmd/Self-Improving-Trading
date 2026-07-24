"""anomaly_diver.py — #5: Anomaly → Targeted Deep Dive.

When the live monitor detects a metric anomaly (>2σ), this module does
a lightweight LLM check to determine if it's a genuine system issue
or normal market variance. Prevents alarm fatigue.

Only confirmed anomalies become RISK findings. Dismissed anomalies
are logged but don't create dashboard noise.
"""

import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from findings_store import _get_conn, get_latest_run, insert_finding

DIVE_PROMPT = """You are Hermes-Sentinel. A statistical check flagged a possible anomaly in a live trading system. Your job is to judge whether this is a genuine problem or normal market variance.

Output ONLY JSON:
{
  "confirmed": boolean,
  "explanation": "short explanation",
  "recommended_severity": "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "NONE",
  "should_escalate": boolean,
  "likely_cause": "market_volatility" | "data_feed_issue" | "system_bug" | "configuration_change" | "normal_variance"
}"""


def check_open_anomalies(verbose: bool = True) -> list[dict]:
    """Check for open anomalies that haven't been deep-dived yet."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT ma.* FROM monitor_alerts ma
               LEFT JOIN audit_findings af ON ma.finding_id = af.id
               WHERE ma.status = 'open'
               AND (ma.finding_id IS NULL OR af.status IS NULL)
               AND ma.created_at >= ?
               ORDER BY ma.z_score DESC LIMIT 5""",
            ((datetime.now(UTC) - timedelta(hours=48)).isoformat(),),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def deep_dive(anomaly: dict) -> dict:
    """Run lightweight LLM check on an anomaly."""
    user_prompt = (
        f"Metric: {anomaly['metric']}\n"
        f"Bot: {anomaly['bot']}\n"
        f"Pair: {anomaly.get('pair') or 'all'}\n"
        f"Observed value: {anomaly['observed_value']}\n"
        f"Expected (mean): {anomaly['baseline_mean']}\n"
        f"Expected (std): {anomaly['baseline_std']}\n"
        f"Z-score: {anomaly['z_score']:.2f}\n"
        f"Direction: {anomaly['direction']}\n\n"
        f"Is this a genuine system problem or normal market variance?"
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
            return {"confirmed": False, "explanation": "No API key"}

        response = httpx.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "system", "content": DIVE_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 512,
                "temperature": 0.1,
            },
            timeout=15,
        )

        if response.status_code == 200:
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            return json.loads(content)
    except Exception as e:
        print(f"[DIVER] LLM error: {e}")

    return {
        "confirmed": True,
        "explanation": "Could not analyze — erring on side of alert",
        "recommended_severity": "MEDIUM",
        "should_escalate": False,
    }


def run_deep_dive(verbose: bool = True) -> list[dict]:
    """Check open anomalies and deep-dive each one."""
    if verbose:
        print(f"\n{'=' * 40}")
        print(f"ANOMALY DEEP DIVE — {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"{'=' * 40}")

    anomalies = check_open_anomalies()
    if not anomalies:
        if verbose:
            print("No open anomalies to analyze")
        return []

    if verbose:
        print(f"Analyzing {len(anomalies)} open anomalies")

    results = []
    conn = _get_conn()
    try:
        for anomaly in anomalies:
            if verbose:
                print(f"  🔍 {anomaly['bot']}/{anomaly['metric']}: z={anomaly['z_score']:+.1f}")

            verdict = deep_dive(anomaly)

            if verdict.get("confirmed"):
                severity = verdict.get("recommended_severity", "MEDIUM")
                if verbose:
                    print(
                        f"     → ✅ Confirmed ({severity}): {verdict.get('explanation', '')[:80]}"
                    )

                latest_run = get_latest_run()
                audit_run_id = (
                    latest_run["id"] if latest_run else f"dive-{int(datetime.now(UTC).timestamp())}"
                )

                finding = insert_finding(
                    domain="performance-drift",
                    finding_type="RISK",
                    severity=severity,
                    location=f"anomaly_diver/{anomaly['bot']}/{anomaly['metric']}",
                    description=f"[Live] {anomaly['bot']}/{anomaly.get('pair', 'all')} {anomaly['metric']} is {anomaly['direction']} baseline: observed={anomaly['observed_value']}, expected={anomaly['baseline_mean']}±{anomaly['baseline_std']} (z={anomaly['z_score']:+.1f}). Verdict: {verdict.get('explanation', '')[:150]}",
                    trading_impact=f"Metric deviated {abs(anomaly['z_score']):.1f}σ from baseline. {verdict.get('explanation', '')[:100]}",
                    suggested_fix=f"Cause: {verdict.get('likely_cause', 'unknown')}. Investigate {anomaly['bot']} {anomaly['metric']}.",
                    confidence=min(90, int(abs(anomaly["z_score"]) * 25)),
                    audit_run_id=audit_run_id,
                )
                results.append(finding)

                # Mark alert as processed
                conn.execute(
                    "UPDATE monitor_alerts SET finding_id = ? WHERE id = ?",
                    (finding["id"], anomaly["id"]),
                )
            else:
                if verbose:
                    print(f"     → ❌ Dismissed: {verdict.get('explanation', '')[:80]}")
                # Mark as dismissed (resolved)
                conn.execute(
                    "UPDATE monitor_alerts SET status = 'resolved' WHERE id = ?",
                    (anomaly["id"],),
                )
            conn.commit()
    finally:
        conn.close()

    return results


if __name__ == "__main__":
    results = run_deep_dive(verbose=True)
    print(f"\nConfirmed: {len(results)} findings created")
