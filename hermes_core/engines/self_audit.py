"""Self-audit engine (Session 15+ / blueprint Appendix J).

Report-only: checks runtime state layout, heartbeat freshness, and config
integrity. Never mutates live trading state (roadmap 8.5).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from hermes_core.config import load_config, repo_root
from hermes_core.state.paths import bot_state_dir, current_bot


@dataclass
class Report:
    bot: str
    ok: bool
    checks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"bot": self.bot, "ok": self.ok, "checks": self.checks}


def _check(name: str, passed: bool, detail: str = "") -> dict:
    return {"name": name, "passed": passed, "detail": detail}


def run(bot: str | None = None) -> Report:
    """Run on-demand self-audit for one bot. Returns a structured report."""
    b = bot or current_bot()
    checks: list[dict] = []

    # Config readable
    try:
        cfg = load_config(b)
        pairs = cfg.get("pairs") or []
        checks.append(_check("config_load", True, f"{len(pairs)} pairs"))
    except Exception as exc:  # noqa: BLE001
        checks.append(_check("config_load", False, str(exc)))
        pairs = []

    # State dir on volume
    state_dir = bot_state_dir(b)
    checks.append(_check(
        "state_dir",
        state_dir.exists(),
        str(state_dir),
    ))

    # Heartbeat freshness (informational — cron enforces alert)
    hb = state_dir / "heartbeat.json"
    if hb.exists():
        try:
            data = json.loads(hb.read_text(encoding="utf-8"))
            age = time.time() - float(data.get("ts", 0))
            checks.append(_check("heartbeat_fresh", age < 90 * 60, f"age={age:.0f}s"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            checks.append(_check("heartbeat_fresh", False, str(exc)))
    else:
        checks.append(_check("heartbeat_fresh", False, "missing heartbeat.json"))

    # Strategy files present for each pair
    for pair in pairs:
        fname = pair.replace("/", "_") + ".yaml"
        strat = repo_root() / "bots" / b / "state" / "strategies" / fname
        checks.append(_check(f"strategy_{pair}", strat.exists(), str(strat)))

    # Optional state artifacts (warn if absent, not fatal)
    for rel in ("hypotheses.jsonl", "flatline_log.jsonl", "policy.json"):
        p: Path = state_dir / rel
        checks.append(_check(f"artifact_{rel}", p.exists(), "optional" if not p.exists() else "ok"))

    ok = all(c["passed"] for c in checks if not c["name"].startswith("artifact_"))
    return Report(bot=b, ok=ok, checks=checks)


class SelfAudit:
    """Blueprint Section 6 contract wrapper."""

    def run(self, bot: str | None = None) -> dict:
        return run(bot).to_dict()
