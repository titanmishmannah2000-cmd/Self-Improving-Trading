"""audit_runner.py — Autonomous Hermes Self-Audit Engine.

Orchestrates the full audit pipeline for one or more domains:
  1. Creates an audit run record in SQLite
  2. For each domain, collects code/log/deploy data
  3. Loads the prior audit JSON for improvement-loop tracking
  4. Calls DeepSeek V4 Flash with the structured audit prompt
  5. Parses and stores findings
  6. Returns a summary dict

Usage:
  python audit_runner.py                    # Run all 5 domains
  python audit_runner.py --domain static-code  # Single domain
  python audit_runner.py --dry-run          # Collect data but don't call LLM

Environment:
  DEEPSEEK_API_KEY  — required for LLM calls
  DASHBOARD_API_URL — optional, defaults to Railway production URL
"""

import argparse
import json
import os
import sys
from pathlib import Path

# ── Auto-load .env from same directory ──
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and "=" in _line and not _line.startswith("#"):
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k, _v)

from data_collector import (
    ALL_DOMAINS,
    collect_domain,
)
from findings_store import (
    create_run,
    finish_run,
    get_latest_run,
    get_summary_stats,
    init_db,
    insert_findings_batch,
    insert_maturity,
)

# ── LLM Caller ─────────────────────────────────────────────────────────────

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
MODEL = "deepseek-v4-flash"
MAX_TOKENS_PER_DOMAIN = 8192
TOTAL_MAX_TOKENS = 32768  # fallback for very large domains

# The system prompt loaded from the skill reference
SYSTEM_PROMPT = """You are Hermes-Audit, the autonomous self-audit engine for an algorithmic trading system called Hermes. You review Hermes' own code, logs, configuration, and deployment state on Railway.

You have two jobs, weighted equally:
1. Find real problems. Be skeptical by default — code that runs without errors is not the same as code that is correct. Look for bugs, logic that has drifted from stated intent, missing edge-case handling, data/logging gaps, and risk-boundary weaknesses.
2. Give an honest capability assessment, including what's genuinely solid. Do not manufacture findings to appear thorough. Do not withhold credit from code that is well-built. A report that claims everything is broken is exactly as useless as one that misses real problems — both destroy trust in your findings.

CRITICAL CONTEXT: Nothing you report is applied automatically. Every finding is a proposal that a human reviews and explicitly approves before any code changes, and explicitly applies before deployment. Your job is detection and diagnosis, never autonomous modification. Write every "suggested_fix" as a recommendation for a human reviewer, not as an instruction you expect to be auto-executed.

RULES:
1. Every finding must cite a specific file, function, or line. No vague "this could be an issue somewhere" findings.
2. Classify every finding as exactly one of: BUG | GAP | RISK | UPGRADE | STRENGTH
   - BUG: incorrect behavior, provably wrong.
   - GAP: missing handling/logic that should exist.
   - RISK: correct today, dangerous under some condition.
   - UPGRADE: works fine, but a materially better approach exists.
   - STRENGTH: genuinely well-built, cite specific evidence why.
3. Severity applies to BUG/GAP/RISK only: CRITICAL | HIGH | MEDIUM | LOW. CRITICAL = could lose money, corrupt state, or crash live trading. Use "N/A" for UPGRADE/STRENGTH.
4. For each finding include: location, description, why it matters IN A TRADING CONTEXT specifically, a concrete suggested fix, and confidence 0-100. If confidence is below 60, say explicitly what would need to be checked to confirm it.
5. Score current capability maturity for this domain, 1-5:
   1 Fragile — only works in the happy path
   2 Functional — works, no guardrails or monitoring around it
   3 Solid — has error handling and some test coverage
   4 Robust — tested, monitored, degrades gracefully on failure
   5 Hardened — robust, with evidence of surviving real edge cases
6. Compare against the prior audit JSON provided (if any): mark which prior findings are resolved, which are still open, and whether any new finding is a regression of something previously fixed.
7. Be terse. No hedging filler, no restating code back to the user, no praise language outside STRENGTH entries.
8. Output ONLY valid JSON matching the schema below. No text before or after the JSON object.

OUTPUT SCHEMA:
{
  "audit_domain": string,
  "files_reviewed": [string],
  "maturity": {
    "score": 1-5,
    "justification": string,
    "compared_to_prior_audit": "improved" | "same" | "regressed" | "first_audit"
  },
  "findings": [
    {
      "id": string,
      "type": "BUG" | "GAP" | "RISK" | "UPGRADE" | "STRENGTH",
      "severity": "CRITICAL" | "HIGH" | "MEDIUM" | "LOW" | "N/A",
      "location": string,
      "description": string,
      "trading_impact": string,
      "suggested_fix": string,
      "confidence": number
    }
  ],
  "summary": {
    "total_findings": number,
    "critical_count": number,
    "strength_count": number,
    "resolved_since_prior_audit": [string],
    "unresolved_from_prior_audit": [string],
    "regressions": [string]
  }
}"""


def _get_api_key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError("DEEPSEEK_API_KEY not set. Run: export DEEPSEEK_API_KEY='sk-...'")
    return key


def _call_llm(domain_data: dict, prior_audit_json: str | None = None) -> dict:
    """Call DeepSeek V4 Flash with the collected domain data. Returns parsed JSON."""
    import httpx

    api_key = _get_api_key()

    # Build user prompt
    domain = domain_data["domain"]
    files_section = ""
    for path, content in sorted(domain_data.get("files", {}).items()):
        files_section += f"\n--- FILE: {path} ---\n{content}\n"

    # If change context is available, add a summary of what changed
    change_context = domain_data.get("change_context", "")
    if change_context:
        files_section = (
            f"[CHANGE CONTEXT — only these files changed since last audit]\n{change_context}\n\n"
            + files_section
        )

    logs_section = ""
    for source, content in sorted(domain_data.get("logs", {}).items()):
        logs_section += f"\n--- LOG: {source} ---\n{content}\n"

    deploy_section = ""
    for bot, state in sorted(domain_data.get("deploy_state", {}).items()):
        deploy_section += f"\n--- DEPLOY STATE: {bot} ---\n{state}\n"

    strategy_spec = domain_data.get("strategy_spec", "N/A")
    baselines = domain_data.get("baselines", {})

    user_prompt = f"Audit domain: {domain}\n\n"
    if strategy_spec and strategy_spec != "N/A":
        user_prompt += f"Intended strategy spec:\n{strategy_spec}\n\n"
    else:
        user_prompt += "Intended strategy spec: N/A\n\n"

    if prior_audit_json:
        user_prompt += f"Prior audit result for this domain:\n{prior_audit_json}\n\n"
    else:
        user_prompt += "Prior audit result: N/A (first audit)\n\n"

    if baselines:
        user_prompt += f"Current known-good baselines:\n{json.dumps(baselines, indent=2)}\n\n"

    # Inject user feedback context (Layer 6)
    try:
        from feedback_learner import get_prompt_context

        fb_context = get_prompt_context(domain)
        if fb_context:
            user_prompt += f"{fb_context}\n\n"
    except ImportError:
        pass

    user_prompt += f"Files and logs for this domain:\n{files_section}\n\n{logs_section}"
    if deploy_section:
        user_prompt += f"\n\nDeployment state:\n{deploy_section}"

    # Truncate if too long (384K token limit, estimate ~4 chars/token)
    max_chars = 384000 * 3  # conservative ~3 chars per token
    if len(user_prompt) > max_chars:
        print(f"  ⚠  User prompt too long ({len(user_prompt)} chars), truncating to ~{max_chars}")
        user_prompt = user_prompt[:max_chars] + "\n... [TRUNCATED]"

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": MAX_TOKENS_PER_DOMAIN,
        "temperature": 0.1,
    }

    print(f"  Calling DeepSeek V4 Flash for domain '{domain}'...")
    print(f"  Prompt size: ~{len(user_prompt)} chars")

    response = httpx.post(
        DEEPSEEK_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
    )

    if response.status_code != 200:
        raise RuntimeError(f"DeepSeek API error {response.status_code}: {response.text[:500]}")

    result = response.json()
    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("DeepSeek returned empty content")

    # Parse JSON from response
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        print(f"  ⚠  LLM returned invalid JSON: {e}")
        print(f"  Raw response (first 500 chars): {content[:500]}")
        # Try to salvage - wrap in retry
        raise RuntimeError(f"Invalid JSON from LLM: {e}") from e

    return parsed


# ── Prior Audit Tracking ──────────────────────────────────────────────────


def _get_prior_audit_json(domain: str) -> str | None:
    """Get the most recent audit JSON for this domain from the latest run that included it."""
    latest = get_latest_run()
    if not latest:
        return None

    # Check if the latest run included this domain
    domains_run = json.loads(latest.get("domains_run", "[]"))
    if domain not in domains_run:
        return None

    # Get the summary_json from the latest run
    summary_raw = latest.get("summary_json")
    if not summary_raw:
        return None

    # summary_json stores the full LLM response per domain as a JSON object
    try:
        all_summaries = json.loads(summary_raw)
        domain_summary = all_summaries.get(domain)
        if domain_summary:
            return json.dumps(domain_summary, indent=2)
    except (json.JSONDecodeError, AttributeError):
        pass

    return None


# ── Main Runner ───────────────────────────────────────────────────────────


def run_audit(
    domains: list[str] | None = None,
    dry_run: bool = False,
    verbose: bool = False,
    progress_id: str | None = None,
) -> dict:
    """Run the audit pipeline. Returns a summary dict.

    Args:
        domains: List of domains to audit. Defaults to ALL_DOMAINS.
        dry_run: If True, collect data but skip LLM calls.
        verbose: Print progress during collection.
        progress_id: If set, updates audit_progress table as domains complete.

    Returns:
        dict with run results summary.
    """
    if domains is None:
        domains = ALL_DOMAINS

    # Validate
    for d in domains:
        if d not in ALL_DOMAINS:
            raise ValueError(f"Unknown domain: {d}. Valid: {ALL_DOMAINS}")

    # Bootstrap DB
    init_db()

    # Create run record
    run = create_run(domains)
    run_id = run["id"]
    print(f"\n{'=' * 60}")
    print(f"AUDIT RUN {run_id}")
    print(f"Domains: {', '.join(domains)}")
    print(f"{'=' * 60}\n")

    all_findings = []
    total_critical = 0
    maturity_scores = {}
    intelligence_maturity = {}
    resolved_prior_all = []
    regressions_all = []
    domain_summaries = {}

    for i, domain in enumerate(domains):
        print(f"\n{'─' * 40}")
        print(f"Domain: {domain}")
        print(f"{'─' * 40}")

        # Update progress
        if progress_id:
            pct = int((i / len(domains)) * 85) + 5  # 5-90% range
            try:
                from findings_store import update_audit_progress

                update_audit_progress(
                    progress_id,
                    progress_pct=pct,
                    current_domain=domain,
                    domains_done=i,
                    message=f"Auditing {domain}...",
                )
            except Exception:
                pass

        # 1. Collect data
        print("  Collecting data...")
        domain_data = collect_domain(domain)
        if verbose:
            n_files = len(domain_data.get("files", {}))
            n_logs = len(domain_data.get("logs", {}))
            print(f"  {n_files} files, {n_logs} log sources collected")

        if dry_run:
            print("  [DRY RUN] Skipping LLM call")
            continue

        # 2. Get prior audit
        prior_json = _get_prior_audit_json(domain)
        if prior_json:
            print("  Prior audit found for this domain")
        else:
            print("  No prior audit — first audit for this domain")

        # 3. Call LLM
        try:
            llm_result = _call_llm(domain_data, prior_json)
        except Exception as e:
            print(f"  ❌ LLM call failed: {e}")
            continue

        if not isinstance(llm_result, dict):
            print(f"  ❌ LLM returned non-dict result: {type(llm_result)}")
            continue

        # 4. Extract findings
        findings_raw = llm_result.get("findings", [])
        maturity = llm_result.get("maturity", {})
        summary = llm_result.get("summary", {})

        # Add domain to each finding
        for f in findings_raw:
            f["domain"] = domain

        # 5. Store findings
        count = insert_findings_batch(findings_raw, run_id)
        all_findings.extend(findings_raw)

        # 6. Track critical count
        for f in findings_raw:
            if f.get("severity") == "CRITICAL":
                total_critical += 1

        # 7. Store maturity
        score = maturity.get("score", 0)
        justification = maturity.get("justification", "")
        compared = maturity.get("compared_to_prior_audit", "first_audit")
        # INTELLIGENCE score: for now infer from UPGRADE/INTELLIGENCE findings proportion
        upgrade_count = sum(1 for f in findings_raw if f.get("type") in ("UPGRADE", "INTELLIGENCE"))
        intel_score = min(5, max(0, upgrade_count)) if upgrade_count > 0 else 0

        insert_maturity(domain, score, intel_score, justification, compared, run_id)
        maturity_scores[domain] = score
        intelligence_maturity[domain] = intel_score

        # 8. Track resolved/regressed
        resolved = summary.get("resolved_since_prior_audit", [])
        regressed = summary.get("regressions", [])
        resolved_prior_all.extend(resolved)
        regressions_all.extend(regressed)

        # 9. Store full LLM result for future prior tracking
        domain_summaries[domain] = llm_result

        print(f"  ✅ {count} findings stored")
        if resolved:
            print(f"  ✅ {len(resolved)} prior findings resolved")
        if regressed:
            print(f"  ⚠  {len(regressed)} regressions detected")
        if score:
            print(f"  Maturity: {score}/5 — {compared}")

    # 10. Finalize run
    finish_run(
        run_id=run_id,
        findings_count=len(all_findings),
        critical_count=total_critical,
        resolved_prior=resolved_prior_all,
        regressions=regressions_all,
        maturity_scores=maturity_scores,
        intelligence_maturity=intelligence_maturity,
        summary_json=json.dumps(domain_summaries) if domain_summaries else None,
    )

    # 11. Print summary
    print(f"\n{'=' * 60}")
    print(f"AUDIT COMPLETE — Run {run_id}")
    print(f"{'=' * 60}")
    print(f"  Domains audited: {len(domains)}")
    print(f"  Total findings:  {len(all_findings)}")
    print(f"  CRITICAL:        {total_critical}")
    print(f"  Resolved prior:  {len(resolved_prior_all)}")
    print(f"  Regressions:     {len(regressions_all)}")
    print(f"  Maturity scores: {json.dumps(maturity_scores)}")
    print("\n  To review findings: view dashboard or check audit_state/audit.db")
    print(f"{'=' * 60}\n")

    return {
        "run_id": run_id,
        "domains": domains,
        "total_findings": len(all_findings),
        "critical_count": total_critical,
        "resolved_prior": resolved_prior_all,
        "regressions": regressions_all,
        "maturity_scores": maturity_scores,
        "intelligence_maturity": intelligence_maturity,
    }


# ── CLI Entry Point ───────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(description="Hermes Self-Audit Engine")
    parser.add_argument(
        "--domain",
        "-d",
        action="append",
        choices=ALL_DOMAINS,
        help="Audit a specific domain (repeatable). Default: all domains.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect data but skip LLM calls (for testing collectors).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed progress during data collection.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass sentinel and run all domains.",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Show current audit summary stats and exit.",
    )

    args = parser.parse_args()

    if args.summary:
        stats = get_summary_stats()
        print(json.dumps(stats, indent=2))
        return

    # Use sentinel to decide domains unless explicit or --force
    if args.domain:
        domains = args.domain
    elif args.force:
        domains = ALL_DOMAINS
        print("[SENTINEL] --force: running all domains")
    else:
        try:
            from sentinel import decide_domains

            domains = decide_domains(verbose=True)
            if not domains:
                print("\n✅ Sentinel: no audit needed. Use --force to run anyway.\n")
                return
        except ImportError:
            print("[SENTINEL] Module not found, falling back to all domains")
            domains = ALL_DOMAINS

    if not os.environ.get("DEEPSEEK_API_KEY") and not args.dry_run:
        print("ERROR: DEEPSEEK_API_KEY environment variable is not set.")
        print("Set it with: export DEEPSEEK_API_KEY='sk-...'")
        print("Or use --dry-run to test data collection without LLM calls.")
        sys.exit(1)

    result = run_audit(domains, dry_run=args.dry_run, verbose=args.verbose)

    # Print compact JSON result
    print("\n--- RESULT JSON ---")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
