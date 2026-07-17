"""fix_verifier.py — Layer 4: Fix Verification.

After a finding is marked `applied`, the next audit run that covers that
domain should confirm the specific bug pattern is gone.

Usage:
  python fix_verifier.py                          # Verify all applied-but-unverified
  python fix_verifier.py --finding abc123         # Verify a specific finding
"""

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from findings_store import _get_conn, list_findings, get_finding, get_latest_run, update_finding_status


def get_unverified_fixes(hours_back: int = 168) -> list[dict]:
    """Get all findings applied in the last N hours that haven't been verified.

    A fix is 'verified' if a subsequent audit run on the same domain
    didn't find the same pattern (same type + similar location).
    """
    conn = _get_conn()
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=hours_back)).isoformat()
        rows = conn.execute(
            """SELECT * FROM audit_findings
               WHERE status = 'applied' AND updated_at >= ?
               ORDER BY updated_at DESC""",
            (since,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def verify_finding(finding_id: str) -> dict:
    """Check if an applied finding's bug pattern has re-appeared.

    Uses the latest audit run's findings for the same domain.
    Returns {'status': 'verified'|'regressed'|'inconclusive', evidence: str}.
    """
    finding = get_finding(finding_id)
    if not finding:
        return {"status": "error", "evidence": "Finding not found"}
    if finding["status"] != "applied":
        return {"status": "inconclusive", "evidence": f"Finding is {finding['status']}, not applied"}

    domain = finding["domain"]
    finding_type = finding["type"]
    location_hint = (finding.get("location") or "")[:60]

    # Get the latest audit run findings for this domain
    latest = get_latest_run()
    if not latest:
        return {"status": "inconclusive", "evidence": "No subsequent audit run to compare against"}

    newer_findings = list_findings(
        domain=domain,
        limit=50,
    )

    # Filter to findings created AFTER this fix was applied
    fix_time = finding["updated_at"]
    regressions = []
    for nf in newer_findings:
        if nf.get("prior_finding_id") == finding_id:
            regressions.append(nf)
        elif nf["created_at"] > fix_time and nf["type"] == finding_type:
            # Same type in same domain — check if location suggests same bug
            nf_loc = (nf.get("location") or "")[:60]
            if location_hint and nf_loc and (
                location_hint[:30] in nf_loc or nf_loc[:30] in location_hint
            ):
                regressions.append(nf)

    if regressions:
        return {
            "status": "regressed",
            "evidence": f"Found {len(regressions)} similar finding(s) after fix was applied",
            "regressions": [r["id"] for r in regressions],
        }

    return {
        "status": "verified",
        "evidence": f"No regressions found for this finding in subsequent audit (domain: {domain})",
    }


def verify_all(verbose: bool = True) -> list[dict]:
    """Verify all applied-but-unverified fixes. Returns results list."""
    fixes = get_unverified_fixes()
    if not fixes:
        if verbose:
            print("[VERIFY] No applied fixes to verify")
        return []

    results = []
    for f in fixes:
        result = verify_finding(f["id"])
        result["finding_id"] = f["id"]
        result["domain"] = f["domain"]
        result["description"] = f["description"][:100]
        results.append(result)

        if verbose:
            status_icon = {"verified": "✅", "regressed": "🔴", "inconclusive": "🟡"}.get(result["status"], "❓")
            print(f"  {status_icon} [{f['domain']:>22}] {f['description'][:80]}")
            print(f"      → {result['status']}: {result['evidence'][:100]}")

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Fix Verification (Layer 4)")
    parser.add_argument("--finding", type=str, help="Verify a specific finding by ID")
    args = parser.parse_args()

    if args.finding:
        result = verify_finding(args.finding)
        print(json.dumps(result, indent=2))
    else:
        verify_all(verbose=True)
