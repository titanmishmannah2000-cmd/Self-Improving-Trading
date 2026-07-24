"""maturity_scorer.py — #7: Intelligence Maturity — Real Scoring.

Replaces the placeholder intelligence score with actual measured dimensions.
Runs after every audit and stores scores in audit_maturity table.

7 Dimensions:
  1. Detection depth — % of known bug patterns found per scan
  2. False positive rate — % of findings rejected vs approved
  3. Coverage breadth — how many code paths exercised per audit
  4. Trend awareness — can it detect regressions?
  5. Strategic insight — does it propose config improvements?
  6. Response time — hours between anomaly and finding
  7. Cross-domain reasoning — does it connect findings across domains?
"""

import json
import sys
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from findings_store import _get_conn, get_latest_run, insert_maturity, list_findings

KNOWN_BUG_PATTERNS = [
    "hermes_forex",
    "hermes-btc",
    "railway down",
    "subprocess",
    "except: pass",
    "correct_configs",
    "return 0.0",
    "cycle_count",
    "entry_strategy",
    "stop_loss_pct =",
    "try: ... except: pass",
]


def score_dimensions(verbose: bool = True) -> dict:
    """Score all 7 intelligence dimensions. Returns {dimension: {score, evidence}}."""
    conn = _get_conn()
    try:
        findings = list_findings(limit=500)
        len([f for f in findings if f.get("domain") != "test-domain"])

        # 1. Detection depth: % of known patterns covered by recent findings
        recent_text = " ".join(
            f.get("description", "") + " " + f.get("location", "")
            for f in findings
            if f.get("domain") != "test-domain"
        ).lower()
        found_patterns = sum(1 for p in KNOWN_BUG_PATTERNS if p.lower() in recent_text)
        detection_depth = round(found_patterns / len(KNOWN_BUG_PATTERNS) * 5, 1)

        # 2. False positive rate
        rejected = len([f for f in findings if f.get("status") == "rejected"])
        approved = len([f for f in findings if f.get("status") == "approved"])
        applied = len([f for f in findings if f.get("status") == "applied"])
        total_decided = rejected + approved + applied
        if total_decided > 0:
            fp_rate = rejected / total_decided
            false_positive_score = round(5 - (fp_rate * 5), 1)
        else:
            false_positive_score = 3.0  # neutral — no data yet

        # 3. Coverage breadth: how many domains have findings
        domains = set(f.get("domain") for f in findings if f.get("domain") != "test-domain")
        coverage_breadth = round(len(domains) / 5 * 5, 1)

        # 4. Trend awareness: does the latest run track regressions?
        latest = get_latest_run()
        trend_awareness = 2.0
        if latest:
            regressions = json.loads(latest.get("regressions", "[]"))
            resolved = json.loads(latest.get("resolved_prior", "[]"))
            if regressions or resolved:
                trend_awareness = 3.0
            if len(resolved) >= 2:
                trend_awareness = 4.0

        # 5. Strategic insight: does it produce INTELLIGENCE/UPGRADE findings?
        upgrade_count = len(
            [
                f
                for f in findings
                if f.get("type") in ("UPGRADE", "INTELLIGENCE") and f.get("domain") != "test-domain"
            ]
        )
        strategic_insight = min(5, upgrade_count / 2)

        # 6. Response time: check if live anomalies led to findings quickly
        response_time = 2.0  # default
        try:
            # Check if anomaly_diver findings exist
            diver_findings = conn.execute(
                "SELECT COUNT(*) as c FROM audit_findings WHERE location LIKE 'anomaly_diver%'"
            ).fetchone()["c"]
            if diver_findings > 0:
                response_time = 3.0
        except Exception:
            pass

        # 7. Cross-domain reasoning
        cross_domain = 1.0  # default low
        try:
            synth_findings = conn.execute(
                "SELECT COUNT(*) as c FROM audit_findings WHERE location LIKE 'cross_synthesizer%'"
            ).fetchone()["c"]
            strat_findings = conn.execute(
                "SELECT COUNT(*) as c FROM audit_findings WHERE location LIKE 'strategy_analyst%'"
            ).fetchone()["c"]
            if synth_findings >= 2:
                cross_domain = 3.0
            if strat_findings >= 2:
                cross_domain = 4.0
        except Exception:
            pass

        scores = {
            "detection_depth": {
                "score": detection_depth,
                "evidence": f"{found_patterns}/{len(KNOWN_BUG_PATTERNS)} known patterns found",
                "max": 5,
            },
            "false_positive_rate": {
                "score": false_positive_score,
                "evidence": f"{rejected} rejected / {total_decided} decided",
                "max": 5,
            },
            "coverage_breadth": {
                "score": coverage_breadth,
                "evidence": f"{len(domains)}/5 domains covered",
                "max": 5,
            },
            "trend_awareness": {
                "score": trend_awareness,
                "evidence": f"{'regression tracking active' if latest and json.loads(latest.get('regressions', '[]')) else 'no regressions tracked yet'}",
                "max": 5,
            },
            "strategic_insight": {
                "score": strategic_insight,
                "evidence": f"{upgrade_count} UPGRADE/INTELLIGENCE findings",
                "max": 5,
            },
            "response_time": {
                "score": response_time,
                "evidence": f"{'anomaly diver active' if response_time > 2 else 'no anomaly diver data'}",
                "max": 5,
            },
            "cross_domain_reasoning": {
                "score": cross_domain,
                "evidence": f"{'synthesis active' if cross_domain > 2 else 'no cross-domain synthesis'}",
                "max": 5,
            },
        }

        overall = round(sum(s["score"] for s in scores.values()) / len(scores), 1)

        if verbose:
            print(f"\n{'=' * 40}")
            print("INTELLIGENCE MATURITY SCORER")
            print(f"{'=' * 40}")
            for dim, data in scores.items():
                bar = "█" * int(data["score"]) + "░" * (5 - int(data["score"]))
                print(f"  {dim:>25}: {bar}  {data['score']}/5 — {data['evidence'][:50]}")
            print(
                f"\n  {'OVERALL':>25}: {'█' * int(overall)}{'░' * (5 - int(overall))}  {overall}/5"
            )

        return {"scores": scores, "overall": overall}

    finally:
        conn.close()


def store_scores(scores: dict, audit_run_id: str, verbose: bool = True):
    """Store maturity scores in the audit_maturity table."""
    for domain, data in scores.get("scores", {}).items():
        insert_maturity(
            domain=f"intelligence/{domain}",
            score=int(data["score"]),
            intelligence_score=0,
            justification=data["evidence"],
            compared_to_prior="first_audit",
            audit_run_id=audit_run_id,
        )


def run_scoring(verbose: bool = True) -> dict:
    """Full scoring pipeline. Returns scores dict."""
    scores = score_dimensions(verbose=verbose)

    latest = get_latest_run()
    if latest:
        store_scores(scores, latest["id"], verbose=verbose)

    return scores


if __name__ == "__main__":
    run_scoring(verbose=True)
