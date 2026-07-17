"""nli.py — Layer 8: Natural Language Interface.

Answers questions about system health by synthesizing data from all layers.
Integrates as a dashboard API endpoint: POST /api/ask

Supported questions:
  - "What's wrong right now?"
  - "Has anything changed since yesterday?"
  - "Why did gold lose money this week?"
  - "What should I fix first?"
  - "How are the bots doing?"
  - "Any regressions since last audit?"
  - "Summarize the last audit"
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))

from findings_store import _get_conn, get_summary_stats, get_latest_run, list_findings
from sentinel import check_live_anomalies, check_git_changes


SYSTEM_PROMPT = """You are Hermes-Steward, the natural language interface for an algorithmic trading system. You answer questions about system health by synthesizing data from multiple sources.

You have access to:
1. Audit findings (bugs, risks, gaps, upgrades)
2. Live anomaly alerts (real-time metric deviations)
3. System maturity scores
4. Recent code changes
5. Deployment state

Rules:
- Be direct and concise. No fluff.
- If something is critical, say so immediately.
- If nothing is wrong, say "Everything looks stable" and move on.
- Support your claims with specific data (finding IDs, metric values, timestamps).
- If you don't have enough data to answer, say so.
- Output ONLY valid JSON matching this schema:
{
  "answer": "the direct answer",
  "sources_used": ["list of data sources"],
  "critical_items": ["anything urgent the user should know"],
  "has_critical": boolean,
  "confidence": "high" | "medium" | "low"
}"""


def gather_context(question: str) -> dict:
    """Gather all available context for answering the question."""
    ctx = {
        "question": question,
        "summary": get_summary_stats(),
        "anomalies": check_live_anomalies(),
        "git_changes": check_git_changes(),
        "latest_run": get_latest_run(),
        "recent_audit_findings": [],
        "open_critical": [],
    }

    # Recent findings
    findings = list_findings(limit=30)
    ctx["recent_audit_findings"] = [
        {"id": f["id"], "severity": f["severity"], "type": f["type"],
         "domain": f["domain"], "description": f.get("description", "")[:150],
         "status": f["status"]}
        for f in findings
    ]

    # Open criticals
    ctx["open_critical"] = [
        f for f in ctx["recent_audit_findings"]
        if f["severity"] == "CRITICAL" and f["status"] in ("pending", "approved")
    ]

    return ctx


def answer(question: str) -> dict:
    """Answer a natural language question about system health."""
    ctx = gather_context(question)

    # Build user prompt with context
    context_json = json.dumps({
        "summary": ctx["summary"],
        "anomalies": {
            "critical_count": ctx["anomalies"]["critical_count"],
            "warning_count": ctx["anomalies"]["warning_count"],
            "critical_metrics": ctx["anomalies"]["critical_metrics"],
        },
        "git_changes": {
            "projects_changed": ctx["git_changes"]["projects_changed"],
            "changed_files": ctx["git_changes"]["changed_files"],
        },
        "last_audit": {
            "id": ctx["latest_run"]["id"] if ctx["latest_run"] else None,
            "findings_count": ctx["latest_run"]["findings_count"] if ctx["latest_run"] else 0,
            "critical_count": ctx["latest_run"]["critical_count"] if ctx["latest_run"] else 0,
            "maturity_scores": json.loads(ctx["latest_run"]["maturity_scores"]) if ctx["latest_run"] and ctx["latest_run"].get("maturity_scores") else {},
            "created_at": ctx["latest_run"]["created_at"] if ctx["latest_run"] else None,
        } if ctx["latest_run"] else "No audits yet",
        "open_critical_findings": ctx["open_critical"],
    }, default=str)

    user_prompt = f"Question: {question}\n\nCurrent system context:\n{context_json}"

    # Call LLM
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
            return {"answer": "System not configured: DEEPSEEK_API_KEY not set", "sources_used": [], "critical_items": [], "has_critical": False, "confidence": "low"}

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
                "max_tokens": 1024,
                "temperature": 0.2,
            },
            timeout=30,
        )

        if response.status_code != 200:
            return {"answer": f"LLM error: {response.status_code}", "sources_used": ["llm"], "critical_items": [], "has_critical": False, "confidence": "low"}

        result = response.json()
        content = result["choices"][0]["message"]["content"]
        return json.loads(content)

    except ImportError:
        return {"answer": "httpx not available for LLM calls", "sources_used": [], "critical_items": [], "has_critical": False, "confidence": "low"}
    except Exception as e:
        return {"answer": f"Error: {e}", "sources_used": [], "critical_items": [], "has_critical": False, "confidence": "low"}


# Simple rule-based fallback for common questions without LLM
def quick_answer(question: str) -> Optional[str]:
    """Quick rule-based answers for common questions (no LLM needed)."""
    q = question.lower()

    if "what's wrong" in q or "what is wrong" in q or "whats wrong" in q or "issues" in q:
        ctx = gather_context("")
        if ctx["open_critical"]:
            items = [f"• [{f['domain']}] {f['description'][:100]}" for f in ctx["open_critical"][:3]]
            return f"🔴 {len(ctx['open_critical'])} critical issue(s) open:\n" + "\n".join(items)
        if ctx["anomalies"]["has_critical"]:
            return f"🟡 {ctx['anomalies']['critical_count']} critical anomaly(ies) detected in live metrics"
        return "✅ No critical issues"

    if "how" in q and "bot" in q:
        ctx = gather_context("")
        total = ctx["summary"].get("total_findings", 0)
        applied = ctx["summary"].get("applied_findings", 0)
        run_id = ctx.get("latest_run", {}).get("id", "—")[:8] if ctx.get("latest_run") else "—"
        return (
            f"📊 System status:\n"
            f"  • {total} total findings ({applied} applied)\n"
            f"  • {ctx['anomalies']['critical_count']} active metric anomalies\n"
            f"  • Last audit: {run_id}\n"
            f"  • Code changed in: {ctx['git_changes']['projects_changed'] or 'none'}"
        )

    if "fix first" in q or "priority" in q:
        ctx = gather_context("")
        if ctx["open_critical"]:
            items = [f"• [{f['domain']}] {f['description'][:100]}" for f in ctx["open_critical"][:3]]
            return f"🔴 Fix these CRITICAL findings first:\n" + "\n".join(items)
        return "✅ No critical findings — all clear"

    if "regression" in q:
        latest = get_latest_run()
        if latest:
            regressions = json.loads(latest.get("regressions", "[]"))
            if regressions:
                return f"⚠️ {len(regressions)} regression(s) detected: {regressions}"
            return "✅ No regressions in latest audit"
        return "No audit data yet"

    return None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Natural Language Interface (Layer 8)")
    parser.add_argument("question", type=str, nargs="?", help="Question to answer")
    parser.add_argument("--quick", action="store_true", help="Use rule-based only (no LLM)")
    args = parser.parse_args()

    if not args.question:
        questions = [
            "What's wrong right now?",
            "How are the bots doing?",
            "What should I fix first?",
            "Any regressions since last audit?",
        ]
        print("Example questions:")
        for q in questions:
            print(f"\n  Q: {q}")
            if args.quick:
                ans = quick_answer(q)
                print(f"  A: {ans}")
            else:
                ans = answer(q)
                print(f"  A: {ans.get('answer', '—')}")
        print()
    else:
        if args.quick:
            ans = quick_answer(args.question)
            if ans:
                print(ans)
            else:
                print("No quick answer available. Try without --quick for LLM-powered response.")
        else:
            result = answer(args.question)
            print(json.dumps(result, indent=2))
