"""daily_briefing.py — #6: Natural Language Daily Briefing.

Synthesizes the current state of everything into a push-style briefing
that gets delivered to you. Runs after the daily audit completes.

Reads from: audit_findings, monitor_metrics, audit_maturity, sentinel
Output: Structured text briefing (delivered via cron job)
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from findings_store import (
    _get_conn,
    get_latest_maturity_per_domain,
    get_latest_run,
    get_summary_stats,
    list_findings,
)
from sentinel import check_live_anomalies


def build_briefing(verbose: bool = True) -> str:
    """Build the daily briefing text."""
    stats = get_summary_stats()
    maturity = get_latest_maturity_per_domain()
    anomalies = check_live_anomalies()
    get_latest_run()

    now = datetime.now(UTC).strftime("%B %d, %Y")

    # ── System Health ──
    has_critical = stats.get("critical_open", 0) > 0 or anomalies.get("has_critical")
    health = "🔴 Issues detected" if has_critical else "✅ All clear"
    avg_maturity = 0
    if maturity:
        scores = [m["score"] for m in maturity.values()]
        avg_maturity = round(sum(scores) / len(scores), 1) if scores else 0

    # ── Live Metrics ──
    live_section = ""
    conn = _get_conn()
    try:
        # Get latest metric values
        latest_metrics = conn.execute(
            """SELECT bot, metric, value FROM (
                   SELECT bot, metric, value, ROW_NUMBER() OVER (PARTITION BY bot, metric ORDER BY recorded_at DESC) as rn
                   FROM monitor_metrics WHERE metric IN ('win_rate_pct','stop_hit_rate_pct','open_trades','lifetime_pnl_pct')
               ) WHERE rn = 1 ORDER BY bot, metric"""
        ).fetchall()

        metric_lines = []
        for r in latest_metrics:
            metric_lines.append(f"  • {r['bot']} {r['metric']}: {r['value']}")
        if metric_lines:
            live_section = "LIVE METRICS:\n" + "\n".join(metric_lines[:8])
    finally:
        conn.close()

    # ── Findings Summary ──
    recent_applied = list_findings(status="applied", limit=5)
    open_crit = list_findings(status=None, severity="CRITICAL", limit=5)

    findings_section = f"FINDINGS: {stats.get('total_findings', 0)} total"
    if recent_applied:
        findings_section += f"\n  ✅ Recently resolved ({len(recent_applied)}):"
        for f in recent_applied[:3]:
            findings_section += f"\n    • {f.get('description', '')[:80]}"
    if open_crit:
        findings_section += f"\n  🔴 Critical open ({len(open_crit)}):"
        for f in open_crit[:3]:
            findings_section += f"\n    • {f.get('description', '')[:80]}"

    # ── Recommendations ──
    recs = []
    if stats.get("critical_open", 0) > 0:
        recs.append(f"🔴 Review {stats['critical_open']} critical finding(s) in Audit tab")
    if anomalies.get("has_critical"):
        recs.append(f"🟡 {anomalies['critical_count']} live metric anomalies detected")
    if avg_maturity < 3:
        recs.append(f"📈 System maturity is {avg_maturity}/5 — focus on risk-safety domain")
    if not recs:
        recs.append("No urgent action needed")

    # ── Build Briefing ──
    briefing = (
        f"📋 Hermes Daily Brief — {now}\n"
        f"{'─' * 40}\n\n"
        f"SYSTEM HEALTH: {health} (maturity: {avg_maturity}/5)\n\n"
        f"{live_section}\n\n"
        f"{findings_section}\n\n"
        f"RECOMMENDATIONS:\n" + "\n".join(f"  {r}" for r in recs)
    )

    if verbose:
        print(briefing)

    return briefing


if __name__ == "__main__":
    build_briefing(verbose=True)
