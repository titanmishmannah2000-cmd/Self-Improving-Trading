"""patch_generator.py — Layer 7: Code Patch Generator.

Takes a finding's `suggested_fix` text and generates an actual code diff
that you can review and apply. NOT auto-applied — always shown for approval.

Usage:
  python patch_generator.py --finding abc123              # Generate patch for one finding
  python patch_generator.py --finding abc123 --apply      # Generate + mark as applied
  python patch_generator.py --all-pending                 # Generate patches for all approved
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from findings_store import _get_conn, get_finding, list_findings, update_finding_status, _ts
from data_collector import BOT_PATHS

PATCH_DIR = _HERE / "generated_patches"
PATCH_DIR.mkdir(exist_ok=True)

SYSTEM_PROMPT = """You are a code patch generator. Given a finding description and suggested fix,
read the relevant source file and generate an exact unified diff (git format) that implements the fix.

Rules:
1. Read the actual file content from the provided path
2. Generate a minimal diff — only change what's necessary
3. Use standard unified diff format (git diff style)
4. Include proper error handling, logging, and backward compatibility
5. If the fix requires multiple files, include all diffs
6. If you cannot generate a safe patch, explain why
7. Output ONLY valid JSON matching this schema:
{
  "patchable": boolean,
  "diff": "unified diff text",
  "files_to_modify": ["path/to/file.py"],
  "explanation": "brief explanation of what the patch does",
  "risk": "low" | "medium" | "high",
  "requires_manual_review": boolean
}"""


def generate_patch(finding_id: str, apply: bool = False) -> dict:
    """Generate a code patch for a finding using the LLM.

    Args:
        finding_id: The finding ID to generate a patch for
        apply: If True, also mark the finding as applied after generation

    Returns:
        dict with patch info
    """
    finding = get_finding(finding_id)
    if not finding:
        return {"status": "error", "message": "Finding not found"}

    if finding["status"] not in ("approved", "pending"):
        return {"status": "error", "message": f"Finding is {finding['status']}, need approved or pending"}

    description = finding.get("description", "")
    suggested_fix = finding.get("suggested_fix", "")
    location = finding.get("location", "") or ""
    domain = finding.get("domain", "")

    if not suggested_fix or suggested_fix == "N/A":
        return {"status": "error", "message": "Finding has no suggested_fix"}

    # Extract file path from location hint
    target_file = None
    if location:
        # Try to extract file path from location (e.g. "forex/hermes_forex/loop.py, push_to_dashboard()")
        for bot_name, bot_path in BOT_PATHS.items():
            if bot_name == "dashboard_api":
                pkg = ""
            else:
                pkg = "hermes_trading" if bot_name == "gold" else "hermes_forex"
            if bot_name in location:
                # Parse out the specific file
                for part in location.replace(",", " ").split():
                    if part.endswith(".py"):
                        target_file = bot_path / pkg / part
                        break
                if not target_file:
                    # Try common filenames
                    for fname in ["loop.py", "reflect.py", "backtest.py", "main.py"]:
                        if fname in location:
                            target_file = bot_path / pkg / fname
                            break
                break

    if not target_file or not target_file.exists():
        return {"status": "error", "message": f"Could not resolve target file from location: {location}"}

    # Read current file content
    try:
        file_content = target_file.read_text(encoding="utf-8")
    except Exception as e:
        return {"status": "error", "message": f"Failed to read {target_file}: {e}"}

    # Build user prompt
    user_prompt = (
        f"Finding description: {description}\n\n"
        f"Suggested fix: {suggested_fix}\n\n"
        f"Target file: {target_file}\n\n"
        f"Current file content:\n```python\n{file_content}\n```\n\n"
        f"Generate a unified diff that implements this fix."
    )

    # Call LLM
    try:
        import httpx
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            # Try loading from .env
            env_path = _HERE / ".env"
            if env_path.exists():
                for line in env_path.read_text().splitlines():
                    line = line.strip()
                    if line.startswith("DEEPSEEK_API_KEY="):
                        api_key = line.split("=", 1)[1]
                        break

        if not api_key:
            return {"status": "error", "message": "DEEPSEEK_API_KEY not set"}

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
                "max_tokens": 4096,
                "temperature": 0.1,
            },
            timeout=60,
        )

        if response.status_code != 200:
            return {"status": "error", "message": f"LLM error {response.status_code}: {response.text[:300]}"}

        result = response.json()
        content = result["choices"][0]["message"]["content"]
        parsed = json.loads(content)

    except Exception as e:
        return {"status": "error", "message": f"Patch generation failed: {e}"}

    # Save the diff
    patch_file = PATCH_DIR / f"{finding_id}.patch"
    diff_text = parsed.get("diff", "")
    meta = {
        "finding_id": finding_id,
        "domain": domain,
        "description": description[:100],
        "target_file": str(target_file),
        "patchable": parsed.get("patchable", False),
        "risk": parsed.get("risk", "medium"),
        "requires_manual_review": parsed.get("requires_manual_review", True),
        "explanation": parsed.get("explanation", ""),
        "generated_at": _ts(),
        "applied": False,
    }

    with open(patch_file, "w") as f:
        f.write(f"# Patch for finding {finding_id}\n")
        f.write(f"# {description}\n")
        f.write(f"# Risk: {meta['risk']}\n")
        f.write(f"# Manual review: {meta['requires_manual_review']}\n")
        f.write(f"# Generated: {meta['generated_at']}\n")
        f.write("#" + "=" * 70 + "\n")
        f.write(diff_text)

    # Save metadata
    meta_file = PATCH_DIR / f"{finding_id}.meta.json"
    with open(meta_file, "w") as f:
        json.dump(meta, f, indent=2)

    print(f"[PATCH] Generated patch for finding {finding_id}")
    print(f"[PATCH]   File: {patch_file}")
    print(f"[PATCH]   Risk: {meta['risk']}")
    print(f"[PATCH]   Manual review: {meta['requires_manual_review']}")
    print(f"[PATCH]   Explanation: {meta['explanation'][:200]}")

    if apply and parsed.get("patchable", False):
        update_finding_status(finding_id, "applied")
        meta["applied"] = True
        with open(meta_file, "w") as f:
            json.dump(meta, f, indent=2)
        print(f"[PATCH] Finding {finding_id} marked as applied")

    return {"status": "ok", "finding_id": finding_id, "patch_file": str(patch_file), **meta}


def generate_all_approved(verbose: bool = True) -> list[dict]:
    """Generate patches for all approved findings without patches yet."""
    approved = list_findings(status="approved", limit=50)
    results = []

    for f in approved:
        if f.get("domain") == "test-domain":
            continue
        # Check if patch already exists
        patch_file = PATCH_DIR / f"{f['id']}.patch"
        if patch_file.exists():
            if verbose:
                print(f"  ⏭ [{f['domain']:>22}] {f['description'][:60]} — patch already exists")
            continue

        if verbose:
            print(f"  🔧 [{f['domain']:>22}] {f['description'][:60]}")
        result = generate_patch(f["id"])
        results.append(result)

    return results


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Patch Generator (Layer 7)")
    parser.add_argument("--finding", type=str, help="Generate patch for a finding")
    parser.add_argument("--apply", action="store_true", help="Also mark as applied")
    parser.add_argument("--all-approved", action="store_true", help="Generate patches for all approved findings")
    args = parser.parse_args()

    if args.all_approved:
        results = generate_all_approved()
        print(f"\nGenerated {len(results)} patches")
    elif args.finding:
        result = generate_patch(args.finding, apply=args.apply)
        print(json.dumps(result, indent=2))
    else:
        parser.print_help()
