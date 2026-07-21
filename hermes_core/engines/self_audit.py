"""Self-audit engine (Session 15+ / blueprint Appendix J).

Report-only: checks runtime state layout, heartbeat freshness, and config
integrity. Never mutates live trading state (roadmap 8.5).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from hermes_core.config import load_config, repo_root, strategy_yaml_path
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

    # Strategy files present for each pair (volume first, seed as fallback)
    for pair in pairs:
        live = strategy_yaml_path(pair, b)
        seed = repo_root() / "bots" / b / "state" / "strategies" / (
            pair.replace("/", "_").replace("-", "_") + ".yaml"
        )
        present = live.exists() or seed.exists()
        checks.append(_check(
            f"strategy_{pair}",
            present,
            str(live if live.exists() else seed),
        ))

    # Optional state artifacts (warn if absent, not fatal)
    for rel in ("hypotheses.jsonl", "flatline_log.jsonl", "policy.json"):
        p: Path = state_dir / rel
        checks.append(_check(f"artifact_{rel}", p.exists(), "optional" if not p.exists() else "ok"))

    # Item 12: flag known fixture pollution still sitting in hypotheses.jsonl
    hyp = state_dir / "hypotheses.jsonl"
    if hyp.exists():
        polluted = 0
        total = 0
        try:
            for line in hyp.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                total += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    polluted += 1
                    continue
                reason = str(rec.get("reason") or "")
                if "max_dd 2.00%" in reason or "max_dd 2.0%" in reason:
                    polluted += 1
                elif rec.get("variable") == "rsi_period" and rec.get("reasoning") == "improve WR":
                    polluted += 1
        except OSError as exc:
            checks.append(_check("hypotheses_clean", False, str(exc)))
        else:
            checks.append(_check(
                "hypotheses_clean",
                polluted == 0,
                f"polluted={polluted}/{total}" if total else "empty",
            ))

    ok = all(c["passed"] for c in checks if not c["name"].startswith("artifact_"))
    return Report(bot=b, ok=ok, checks=checks)


class SelfAudit:
    """Blueprint Section 6 contract wrapper."""

    def run(self, bot: str | None = None) -> dict:
        return run(bot).to_dict()
