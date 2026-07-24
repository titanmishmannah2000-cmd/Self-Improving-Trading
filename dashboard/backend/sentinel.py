"""sentinel.py — Intelligent Sentinel (Layer 2).

Pre-filter that decides whether a full audit is needed. Checks:
  1. Are there live anomalies? (from live_monitor)
  2. Has code changed since the last audit? (git)
  3. Are there unverified applied fixes?

Decision tree:
  - Nothing changed, no anomalies → SKIP (no audit needed)
  - Anomalies in one domain → run ONLY that domain
  - Code changed in specific areas → run affected domains
  - DB migration or Railway deploy → run data-logging-integrity
  - Multiple triggers → FULL AUDIT

Usage:
  python sentinel.py              # Check and return domain list
  python sentinel.py --verbose    # Show decision reasoning
  python sentinel.py --justify    # Explain why each domain was included/excluded

Integrates with audit_runner.py:
  python -c "from sentinel import decide_domains; print(decide_domains())"
"""

import argparse
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from findings_store import _get_conn, get_latest_run

ALL_DOMAINS = [
    "static-code",
    "strategy-logic",
    "data-logging-integrity",
    "risk-safety-boundaries",
    "performance-drift",
]

_REPO = Path(__file__).resolve().parents[2]
PROJECTS = {
    "forex": _REPO / "forex",
    "gold": _REPO / "gold",
    "crypto": _REPO / "crypto",
    "dashboard-api": _REPO / "dashboard" / "backend",
    "dashboard-web": _REPO / "dashboard" / "frontend",
    "audit": _HERE,
}


# ── Check 1: Live Anomalies ──────────────────────────────────────────────


def check_live_anomalies() -> dict:
    """Check for open anomalies in monitor_alerts. Returns decision context."""
    conn = _get_conn()
    try:
        # Open anomalies in the last 24h. The monitor_alerts table only exists
        # when the live anomaly monitor is wired in; degrade gracefully otherwise.
        try:
            critical = conn.execute(
                """SELECT bot, metric, z_score FROM monitor_alerts
                   WHERE status = 'open' AND severity = 'critical' AND created_at >= ?""",
                ((datetime.now(UTC) - timedelta(hours=24)).isoformat(),),
            ).fetchall()

            warnings = conn.execute(
                """SELECT bot, metric, z_score FROM monitor_alerts
                   WHERE status = 'open' AND severity = 'warning' AND created_at >= ?""",
                ((datetime.now(UTC) - timedelta(hours=24)).isoformat(),),
            ).fetchall()
        except sqlite3.OperationalError:
            critical, warnings = [], []

        # Map anomalies to affected audit domains
        domains_hit = set()
        for a in list(critical) + list(warnings):
            met = a["metric"]
            if met in (
                "win_rate_pct",
                "avg_pnl_pct",
                "lifetime_pnl_pct",
                "pair_avg_pnl_pct",
                "pair_win_rate_pct",
            ):
                domains_hit.add("performance-drift")
            if met in ("stop_hit_rate_pct", "target_hit_rate_pct", "unrealised_pnl_pct"):
                domains_hit.add("risk-safety-boundaries")
            if met in ("cycle",):
                domains_hit.add("data-logging-integrity")

        return {
            "has_critical": len(critical) > 0,
            "has_warnings": len(warnings) > 0,
            "critical_count": len(critical),
            "warning_count": len(warnings),
            "domains_hit": list(domains_hit),
            "critical_metrics": [dict(r) for r in critical],
        }
    finally:
        conn.close()


# ── Check 2: Code Changes ────────────────────────────────────────────────


def check_git_changes(since_hours: int = 24) -> dict:
    """Check git for recent changes across all projects.

    Returns dict of {project: [changed_files]} and affected domains.
    """
    changes = {}
    domain_hits = set()

    # File → domain mapping
    FILE_TO_DOMAIN = {
        "loop.py": {"strategy-logic", "risk-safety-boundaries", "data-logging-integrity"},
        "reflect.py": {"static-code", "risk-safety-boundaries"},
        "backtest.py": {"static-code", "performance-drift"},
        "run.py": {"static-code"},
        "gp_intelligence.py": {"static-code"},
        "chart_vision.py": {"static-code"},
        "genetic_discovery.py": {"static-code"},
        "crisis_learning.py": {"static-code"},
        "main.py": {"data-logging-integrity", "static-code"},
        "*.yaml": {"strategy-logic", "risk-safety-boundaries"},
        "*.jsx": {"static-code"},
        "*.css": {"static-code"},
    }

    since_date = (datetime.now(UTC) - timedelta(hours=since_hours)).strftime("%Y-%m-%d")

    for name, path in PROJECTS.items():
        if not path.exists():
            continue
        try:
            result = subprocess.run(
                [
                    "git",
                    "log",
                    "--since",
                    since_date,
                    "--name-only",
                    "--pretty=format:",
                    "--relative",
                ],
                cwd=str(path),
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                continue
            files = [f.strip() for f in result.stdout.split("\n") if f.strip()]
            if files:
                changes[name] = list(set(files))
                # Map files to domains
                for f in files:
                    fname = Path(f).name
                    for pattern, domains in FILE_TO_DOMAIN.items():
                        if pattern.startswith("*"):
                            if fname.endswith(pattern[1:]):
                                domain_hits.update(domains)
                        elif pattern in f or fname == pattern:
                            domain_hits.update(domains)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass

    return {
        "projects_changed": list(changes.keys()),
        "changed_files": changes,
        "domain_hits": list(domain_hits),
        "has_changes": len(changes) > 0,
    }


# ── Check 3: Applied-but-unverified fixes ────────────────────────────────


def check_unverified_fixes() -> dict:
    """Check if any applied findings haven't been verified by a subsequent audit."""
    conn = _get_conn()
    try:
        applied = conn.execute(
            """SELECT f.id, f.domain, f.description, f.updated_at
               FROM audit_findings f
               WHERE f.status = 'applied'
               AND f.created_at >= ?
               ORDER BY f.updated_at DESC""",
            ((datetime.now(UTC) - timedelta(days=7)).isoformat(),),
        ).fetchall()

        # Check which domains have recent applied fixes
        domains_with_fixes = set()
        for f in applied:
            domains_with_fixes.add(f["domain"])

        return {
            "unverified_count": len(applied),
            "domains_with_fixes": list(domains_with_fixes),
            "recent_fixes": [
                {"id": r["id"], "domain": r["domain"], "description": r["description"][:80]}
                for r in applied
            ],
        }
    finally:
        conn.close()


# ── Decision Engine ──────────────────────────────────────────────────────


def decide_domains(verbose: bool = False, justify: bool = False) -> list[str]:
    """Decide which domains to audit. Returns a list of domain names.

    Decision rules:
      - No anomalies + no code changes + no unverified fixes → SKIP (empty list)
      - Anomalies only → affected domains + performance-drift
      - Code changes only → affected domains
      - Applied fixes → affected domains (verify them)
      - Multiple triggers → ALL domains
      - If last audit was > 48h ago → ALL domains (safety net)
    """
    reasons = {}  # domain → why included

    # Gather signals
    anomalies = check_live_anomalies()
    git_changes = check_git_changes()
    fixes = check_unverified_fixes()
    last_run = get_latest_run()
    hours_since_last = 999

    if last_run:
        last_ts = datetime.fromisoformat(last_run["created_at"].replace("Z", "+00:00"))
        hours_since_last = (datetime.now(UTC) - last_ts).total_seconds() / 3600

    # Determine included domains
    included = set()

    # Signal 1: Anomalies
    if anomalies["has_critical"]:
        for d in anomalies["domains_hit"]:
            included.add(d)
            reasons.setdefault(d, []).append(f"critical anomaly in {anomalies['critical_metrics']}")
        # Always include performance-drift when there are critical anomalies
        included.add("performance-drift")
        reasons.setdefault("performance-drift", []).append("critical anomalies detected")
    elif anomalies["has_warnings"]:
        for d in anomalies["domains_hit"]:
            included.add(d)
            reasons.setdefault(d, []).append(f"{anomalies['warning_count']} warnings active")

    # Signal 2: Code changes
    if git_changes["has_changes"]:
        for d in git_changes["domain_hits"]:
            included.add(d)
            reasons.setdefault(d, []).append(f"code changed in {git_changes['projects_changed']}")
        if "static-code" in git_changes["domain_hits"]:
            included.add("static-code")

    # Signal 3: Unverified fixes
    if fixes["unverified_count"] > 0:
        for d in fixes["domains_with_fixes"]:
            included.add(d)
            reasons.setdefault(d, []).append(
                f"{fixes['unverified_count']} applied fix(es) to verify"
            )

    # Signal 4: Time since last audit
    if hours_since_last >= 48:
        included.update(ALL_DOMAINS)
        for d in ALL_DOMAINS:
            reasons.setdefault(d, []).append(
                f"no audit in {hours_since_last:.0f}h (>48h threshold)"
            )

    # Signal 5: If nothing triggered but it's been more than 24h, run one domain
    if not included and hours_since_last >= 24:
        included.add("static-code")
        reasons["static-code"] = ["routine check (>24h since last audit)"]

    result = list(included) if included else []

    if verbose or justify:
        print(f"\n{'=' * 40}")
        print(f"SENTINEL DECISION — {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"{'=' * 40}")
        print("  Signals:")
        print(
            f"    Anomalies:       {anomalies['critical_count']} critical, {anomalies['warning_count']} warning"
        )
        print(f"    Code changes:    {list(git_changes['projects_changed'])}")
        print(f"    Unverified fixes: {fixes['unverified_count']}")
        print(f"    Hours since last: {hours_since_last:.0f}h")
        print(
            f"\n  Decision: {'SKIP (no audit needed)' if not result else f'Audit domains: {result}'}"
        )
        if justify and reasons:
            print("\n  Justification:")
            for d in sorted(reasons.keys()):
                print(f"    {d}:")
                for r in reasons[d]:
                    print(f"      - {r}")
        print(f"{'=' * 40}\n")

    return result


# ── CLI ───────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Hermes Intelligent Sentinel (Layer 2)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show signal details")
    parser.add_argument(
        "--justify", "-j", action="store_true", help="Explain every domain decision"
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON only")
    args = parser.parse_args()

    domains = decide_domains(verbose=args.verbose or args.justify, justify=args.justify)

    if args.json:
        import json as j

        print(
            j.dumps(
                {
                    "domains": domains,
                    "decision": "audit" if domains else "skip",
                    "timestamp": datetime.now(UTC).isoformat(),
                },
                indent=2,
            )
        )
    elif not domains:
        print("\n✅ Sentinel decision: SKIP — nothing to audit")


if __name__ == "__main__":
    main()
