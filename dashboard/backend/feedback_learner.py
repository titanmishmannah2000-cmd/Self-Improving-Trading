"""feedback_learner.py — Layer 6: Feedback Learning.

When a finding is rejected, stores the reason why and injects it into
future audit prompts so the LLM learns user preferences.

Rejection reasons:
  - false_positive: stop showing similar patterns
  - won_t_fix: acceptable risk, don't escalate
  - needs_more_evidence: gather more data before flagging again
  - not_actionable: interesting but no clear fix

Usage:
  python feedback_learner.py                                      # Show all feedback
  python feedback_learner.py --reject abc123 false_positive       # Record rejection
  python feedback_learner.py --prompt-context domain=static-code  # Get feedback for prompt

Integrated into findings_store via update_finding_status.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from findings_store import _get_conn, get_finding

FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_id      TEXT NOT NULL,
    reason          TEXT NOT NULL,  -- false_positive | won_t_fix | needs_more_evidence | not_actionable
    comment         TEXT,
    pattern_keywords TEXT,          -- extracted keywords for pattern matching
    domain          TEXT NOT NULL,
    finding_type    TEXT NOT NULL,
    location_hint   TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_af_finding ON audit_feedback(finding_id);
CREATE INDEX IF NOT EXISTS idx_af_domain ON audit_feedback(domain);
"""


def extend_db():
    conn = _get_conn()
    try:
        conn.executescript(FEEDBACK_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def extract_keywords(finding: dict) -> list[str]:
    """Extract meaningful keywords from a finding for pattern matching."""
    words = set()
    text = f"{finding.get('location', '')} {finding.get('description', '')} {finding.get('suggested_fix', '')}"
    text = text.lower()
    # Simple extraction: take unique words > 4 chars
    for w in text.split():
        w = w.strip(".,:;!?()[]{}\"'")
        if len(w) > 4 and w not in (
            "this",
            "that",
            "with",
            "from",
            "have",
            "been",
            "would",
            "could",
            "should",
        ):
            words.add(w)
    return list(words)[:20]


def record_feedback(
    finding_id: str,
    reason: str,
    comment: str | None = None,
) -> dict:
    """Record user feedback for a rejected finding."""
    valid_reasons = {"false_positive", "won_t_fix", "needs_more_evidence", "not_actionable"}
    if reason not in valid_reasons:
        raise ValueError(f"Reason must be one of: {valid_reasons}")

    finding = get_finding(finding_id)
    if not finding:
        return {"status": "error", "message": "Finding not found"}

    extend_db()
    keywords = extract_keywords(finding)
    now = datetime.now(UTC).isoformat()

    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO audit_feedback
               (finding_id, reason, comment, pattern_keywords, domain, finding_type, location_hint, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                finding_id,
                reason,
                comment,
                json.dumps(keywords),
                finding["domain"],
                finding["type"],
                (finding.get("location") or "")[:100],
                now,
            ),
        )
        conn.commit()
        return {"status": "ok", "message": f"Feedback recorded: {reason}", "keywords": keywords}
    finally:
        conn.close()


def get_prompt_context(domain: str) -> str:
    """Get feedback context to inject into an audit prompt for a domain.

    Returns a string like:
      "[USER FEEDBACK] In previous audits, the user rejected these patterns:
       - 'Spread check silently passes' → false_positive (too noisy)
       - 'No auth on API' → won_t_fix (internal network only)
       Consider these as acceptable patterns unless context has changed."
    """
    extend_db()
    conn = _get_conn()
    try:
        rows = conn.execute(
            """SELECT reason, comment, pattern_keywords, finding_type, location_hint
               FROM audit_feedback WHERE domain = ?
               ORDER BY created_at DESC LIMIT 20""",
            (domain,),
        ).fetchall()

        if not rows:
            return ""

        sections = []
        for r in rows:
            reason_label = {
                "false_positive": "false positive — the user considered this not a real problem",
                "won_t_fix": "acceptable risk — the user chose not to fix this",
                "needs_more_evidence": "needs more evidence — don't flag unless stronger signal",
                "not_actionable": "not actionable — user acknowledges but no clear fix path",
            }.get(r["reason"], r["reason"])

            keywords = json.loads(r["pattern_keywords"]) if r["pattern_keywords"] else []
            kw_str = ", ".join(keywords[:5]) if keywords else "pattern-based"
            comment = f" — {r['comment']}" if r["comment"] else ""

            sections.append(f"  - [{r['finding_type']}] {kw_str} → {reason_label}{comment}")

        if sections:
            return (
                f"[USER FEEDBACK — {domain}]\n"
                f"In previous audits, the user rejected these patterns. "
                f"Treat them as acceptable unless significantly more evidence appears:\n"
                + "\n".join(sections)
            )
        return ""
    finally:
        conn.close()


def list_feedback(domain: str | None = None, limit: int = 20) -> list[dict]:
    """List all recorded feedback, optionally filtered by domain."""
    extend_db()
    conn = _get_conn()
    try:
        if domain:
            rows = conn.execute(
                "SELECT * FROM audit_feedback WHERE domain = ? ORDER BY created_at DESC LIMIT ?",
                (domain, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audit_feedback ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Feedback Learner (Layer 6)")
    parser.add_argument(
        "--reject",
        nargs="+",
        metavar=("FINDING_ID", "REASON"),
        help="Record rejection feedback: finding_id reason [comment]",
    )
    parser.add_argument(
        "--prompt-context",
        type=str,
        metavar="DOMAIN",
        help="Get feedback context for an audit prompt",
    )
    parser.add_argument("--list", action="store_true", help="List all feedback")
    parser.add_argument("--domain", type=str, help="Filter by domain")
    args = parser.parse_args()

    if args.reject:
        finding_id = args.reject[0]
        reason = args.reject[1] if len(args.reject) > 1 else "false_positive"
        comment = " ".join(args.reject[2:]) if len(args.reject) > 2 else None
        result = record_feedback(finding_id, reason, comment)
        print(json.dumps(result, indent=2))

    elif args.prompt_context:
        ctx = get_prompt_context(args.prompt_context)
        print(ctx if ctx else "(no feedback for this domain)")

    elif args.list:
        feedback = list_feedback(domain=args.domain)
        print(json.dumps(feedback, indent=2, default=str))

    else:
        # Show summary
        feedback = list_feedback(limit=50)
        print(f"Total feedback entries: {len(feedback)}")
        for fb in feedback:
            print(f"  [{fb['domain']:>22}] {fb['reason']:>20} — {fb.get('comment', '')[:60]}")
