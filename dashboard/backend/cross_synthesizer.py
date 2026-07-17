"""cross_synthesizer.py — #3: Cross-Finding Synthesis.

Weekly pass that groups related findings by topic and identifies
systemic issues spanning multiple files/domains. Writes UPGRADE findings.

Topics detected:
  - Spread/price validation logic
  - API/network error handling
  - File I/O patterns (read/write safety)
  - State management (stale data risks)
  - Configuration drift
  - Duplicate code between bots
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from findings_store import _get_conn, insert_finding, get_latest_run, list_findings

# Topic keywords → group definitions
TOPICS = {
    "spread": {
        "label": "Spread & Price Validation",
        "keywords": ["spread", "_check_spread", "price validation"],
    },
    "error_handling": {
        "label": "Error Handling & API Resilience",
        "keywords": ["except:", "except Exception", "try:", "bare except", "silent"],
    },
    "file_io": {
        "label": "File I/O Safety",
        "keywords": ["file", "write", "jsonl", "save_", "load_", "append"],
    },
    "state": {
        "label": "State Management & Staleness",
        "keywords": ["state", "latch", "cache", "stale", "version"],
    },
    "duplication": {
        "label": "Cross-Bot Code Duplication",
        "keywords": ["duplicate", "identical", "copy", "both bots", "forex and gold"],
    },
    "deployment": {
        "label": "Deployment & Infrastructure",
        "keywords": ["railway", "deploy", "container", "env", "docker"],
    },
    "config": {
        "label": "Configuration & Parameters",
        "keywords": ["config", "yaml", "parameter", "strategy", "threshold"],
    },
}


def synthesize(verbose: bool = True) -> list[dict]:
    """Group all findings by topic and identify systemic issues."""
    if verbose:
        print(f"\n{'='*40}")
        print(f"CROSS-FINDING SYNTHESIS — {datetime.now(timezone.utc).strftime('%Y-%m-%d UTC')}")
        print(f"{'='*40}")

    # Get all findings
    all_findings = list_findings(limit=200)
    if not all_findings:
        if verbose:
            print("No findings to synthesize")
        return []

    # Group by topic
    topic_groups = defaultdict(list)
    for f in all_findings:
        text = f"{f.get('description', '')} {f.get('location', '')} {f.get('suggested_fix', '')}".lower()
        for topic_id, topic in TOPICS.items():
            for kw in topic["keywords"]:
                if kw.lower() in text:
                    if f not in topic_groups[topic_id]:  # dedup
                        topic_groups[topic_id].append(f)
                    break

    # Filter to topics with 2+ findings (real sprawl)
    sprawls = {tid: fs for tid, fs in topic_groups.items() if len(fs) >= 2}

    if not sprawls:
        if verbose:
            print("No cross-cutting patterns found")
        return []

    if verbose:
        print(f"Found {len(sprawls)} topic groups with 2+ findings:")

    stored = []
    for topic_id, findings in sorted(sprawls.items(), key=lambda x: -len(x[1])):
        topic = TOPICS[topic_id]
        labels = list(set(f.get("type", "?") for f in findings))
        domains = list(set(f.get("domain", "?") for f in findings))

        # Build description
        locations = []
        for f in findings:
            loc = f.get("location", "?")
            desc = f.get("description", "")[:80]
            locations.append(f"  → {loc}: {desc}")

        description = (
            f"[Synthesis] {topic['label']} — {len(findings)} findings across "
            f"{len(domains)} domain(s): {', '.join(domains)}\n" +
            "\n".join(locations[:5])
        )

        if len(locations) > 5:
            description += f"\n  ... and {len(locations) - 5} more"

        recommendation = (
            f"Systemic issue identified: {topic['label']} problems are spread "
            f"across {len(findings)} findings in {len(domains)} domain(s). "
            f"Consider a consolidated approach rather than fixing individually."
        )

        latest_run = get_latest_run()
        audit_run_id = latest_run["id"] if latest_run else f"synth-{datetime.now(timezone.utc).strftime('%Y%m%d')}"

        finding = insert_finding(
            domain="static-code",
            finding_type="UPGRADE",
            severity="MEDIUM" if len(findings) >= 3 else "LOW",
            location=f"cross_synthesizer/{topic_id}",
            description=description[:500],
            trading_impact=f"{len(findings)} related issues — systemic {topic['label']} risk",
            suggested_fix=recommendation[:300],
            confidence=min(85, 50 + len(findings) * 5),
            audit_run_id=audit_run_id,
        )
        stored.append(finding)

        if verbose:
            print(f"  📎 [{topic['label']}] {len(findings)} findings — {', '.join(domains)}")

    return stored


if __name__ == "__main__":
    results = synthesize(verbose=True)
    print(f"\nStored {len(results)} synthesized findings")
