"""version_diff.py — #4: Version Diff Intelligence.

Tracks strategy YAML changes across versions. When a new version is deployed,
snapshots the old vs new config and analyzes the expected impact.

7 days later, checks if the prediction was correct and updates the finding.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from collections import defaultdict

_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE))
from findings_store import _get_conn, insert_finding, get_latest_run, list_findings, get_finding, update_finding_status, _ts

STRATEGY_DIRS = {
    "forex": Path("D:/projects/hermes-forex/state/strategies"),
    "gold": Path("D:/projects/hermes-gold/state/strategies"),
}

VERSION_HISTORY_DIR = _HERE / "version_snapshots"
VERSION_HISTORY_DIR.mkdir(exist_ok=True)


def snapshot_current(bot_name: str) -> dict:
    """Snapshot current strategy YAMLs for a bot. Returns {pair: content}."""
    strategies = {}
    strategy_dir = STRATEGY_DIRS.get(bot_name)
    if not strategy_dir or not strategy_dir.exists():
        return strategies
    for yaml_file in sorted(strategy_dir.glob("*.yaml")):
        try:
            strategies[yaml_file.stem] = yaml_file.read_text(encoding="utf-8")
        except Exception:
            pass
    return strategies


def get_version(bot_name: str, pair: str) -> int:
    """Get current strategy version from YAML."""
    strategy_dir = STRATEGY_DIRS.get(bot_name)
    if not strategy_dir:
        return 0
    yaml_file = strategy_dir / f"{pair}.yaml"
    if not yaml_file.exists():
        return 0
    try:
        content = yaml_file.read_text(encoding="utf-8")
        for line in content.splitlines():
            if line.strip().startswith("version:"):
                return int(line.split(":", 1)[1].strip())
    except Exception:
        pass
    return 0


def detect_changes(verbose: bool = True) -> list[dict]:
    """Detect strategy version changes by comparing snapshots.

    Returns list of change dicts: {bot, pair, old_version, new_version, old_yaml, new_yaml}
    """
    changes = []

    for bot_name in ["forex", "gold"]:
        # Load previous snapshot
        snapshot_file = VERSION_HISTORY_DIR / f"{bot_name}_strategies.json"
        previous = {}
        if snapshot_file.exists():
            try:
                previous = json.loads(snapshot_file.read_text())
            except Exception:
                pass

        # Get current
        current = snapshot_current(bot_name)

        # Compare
        all_pairs = set(list(previous.keys()) + list(current.keys()))
        for pair in all_pairs:
            old_yaml = previous.get(pair, "")
            new_yaml = current.get(pair, "")
            if old_yaml != new_yaml and old_yaml and new_yaml:
                # Extract version numbers
                old_ver = 0
                new_ver = 0
                for line in old_yaml.splitlines():
                    if line.strip().startswith("version:"):
                        try: old_ver = int(line.split(":", 1)[1].strip())
                        except: pass
                for line in new_yaml.splitlines():
                    if line.strip().startswith("version:"):
                        try: new_ver = int(line.split(":", 1)[1].strip())
                        except: pass

                if new_ver > old_ver:
                    changes.append({
                        "bot": bot_name,
                        "pair": pair,
                        "old_version": old_ver,
                        "new_version": new_ver,
                        "old_yaml": old_yaml,
                        "new_yaml": new_yaml,
                    })

        # Save current as snapshot for next run
        try:
            snapshot_file.write_text(json.dumps(current, indent=2))
        except Exception:
            pass

    return changes


def analyze_change(change: dict) -> dict:
    """Call LLM to analyze what changed and predict impact."""
    # Extract diff highlights
    old_lines = set(change["old_yaml"].splitlines())
    new_lines = set(change["new_yaml"].splitlines())
    added = [l for l in new_lines if l not in old_lines and l.strip()]
    removed = [l for l in old_lines if l not in new_lines and l.strip()]

    diff_text = "Added/Changed:\n" + "\n".join(added[:10])
    if removed:
        diff_text += "\nRemoved:\n" + "\n".join(removed[:5])

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
            return {"has_analysis": False}

        response = httpx.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-v4-flash",
                "messages": [
                    {"role": "system", "content": "You analyse strategy config changes. Given old vs new YAML for a trading pair, describe what changed and predict the likely impact on win rate, PnL, and risk. Output JSON: {has_analysis: bool, summary: str, predicted_impact: str, confidence: int}"},
                    {"role": "user", "content": f"Bot: {change['bot']}, Pair: {change['pair']}, v{change['old_version']} → v{change['new_version']}\n\nDiff:\n{diff_text}"},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": 1024,
                "temperature": 0.2,
            },
            timeout=30,
        )
        if response.status_code == 200:
            result = response.json()
            content = result["choices"][0]["message"]["content"]
            return json.loads(content)
    except Exception as e:
        print(f"[VERSION] Analysis error: {e}")

    return {"has_analysis": False}


def run_version_diff(verbose: bool = True) -> list[dict]:
    """Detect and analyze strategy version changes."""
    if verbose:
        print(f"\n{'='*40}")
        print(f"VERSION DIFF INTELLIGENCE")
        print(f"{'='*40}")

    changes = detect_changes(verbose)
    if not changes:
        if verbose:
            print("No version changes detected")
        return []

    if verbose:
        print(f"Found {len(changes)} version changes")

    stored = []
    for change in changes:
        if verbose:
            print(f"  📋 {change['bot']} {change['pair']}: v{change['old_version']} → v{change['new_version']}")

        analysis = analyze_change(change)
        if not analysis.get("has_analysis"):
            continue

        latest_run = get_latest_run()
        audit_run_id = latest_run["id"] if latest_run else f"ver-{datetime.now(timezone.utc).strftime('%Y%m%d')}"

        finding = insert_finding(
            domain="strategy-logic",
            finding_type="UPGRADE",
            severity="MEDIUM",
            location=f"version_diff/{change['bot']}/{change['pair']}",
            description=f"[Version] {change['bot']} {change['pair']} v{change['old_version']}→v{change['new_version']}: {analysis.get('summary', 'Config changed')[:200]}",
            trading_impact=f"Predicted: {analysis.get('predicted_impact', 'Unknown')[:200]}",
            suggested_fix="Monitor performance for 7 days to validate prediction",
            confidence=analysis.get("confidence", 60),
            audit_run_id=audit_run_id,
        )
        stored.append(finding)

    return stored


if __name__ == "__main__":
    results = run_version_diff(verbose=True)
    print(f"\nStored {len(results)} version diff findings")
