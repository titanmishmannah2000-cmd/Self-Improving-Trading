"""Self-audit engine (Session 15+ / blueprint Appendix J).

Report-only: checks runtime state layout, heartbeat freshness, soak
go/no-go criteria, and config integrity. Never mutates live trading state
(roadmap 8.5) except via explicit ops tools.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from hermes_core.config import load_config, repo_root, strategy_yaml_path
from hermes_core.engines.soak_controls import (
    entries_halted,
    feed_error_rate,
    idle_skip_slo,
    price_sanity_book,
)
from hermes_core.state.paths import bot_state_dir, current_bot

HEARTBEAT_MAX_AGE_S = 10 * 60  # soak go/no-go: 10 minutes
VALID_BOTS = ("forex", "gold", "crypto")


@dataclass
class Report:
    bot: str
    ok: bool
    go_nogo: bool
    checks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "bot": self.bot,
            "ok": self.ok,
            "go_nogo": self.go_nogo,
            "checks": self.checks,
        }


def _check(name: str, passed: bool, detail: str = "", *, critical: bool = True) -> dict:
    return {
        "name": name,
        "passed": passed,
        "detail": detail,
        "critical": critical,
    }


def _archive_pollution_refs(state_dir: Path) -> list[str]:
    """Flag configs that point at polluted archive files."""
    bad: list[str] = []
    for p in state_dir.rglob("*"):
        if not p.is_file():
            continue
        name = p.name
        if "polluted" in name and "archive" in p.parts:
            # Presence of archives is OK; only fail if a live pointer exists.
            continue
    # Live files must not be polluted archives themselves.
    for rel in ("trades.jsonl", "hypotheses.jsonl", "skips.jsonl"):
        p = state_dir / rel
        if p.exists() and "polluted" in p.name:
            bad.append(str(p))
    # goldbot orphan is a hard fail when auditing any bot (shared deploy smell).
    orphan = repo_root() / "goldbot" / "state" / "gp_promote_gate.json"
    if orphan.exists():
        bad.append(str(orphan))
    return bad


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
        cfg = {}

    state_dir = bot_state_dir(b)
    checks.append(_check("state_dir", state_dir.exists(), str(state_dir)))

    # Heartbeat freshness (soak: 10 min)
    hb = state_dir / "heartbeat.json"
    hb_data: dict = {}
    if hb.exists():
        try:
            hb_data = json.loads(hb.read_text(encoding="utf-8"))
            age = time.time() - float(hb_data.get("ts", 0))
            # Reject obvious stub heartbeats (fixed ISO from seed fixtures).
            ts_raw = hb_data.get("ts")
            stub = False
            if isinstance(ts_raw, str) and ts_raw.startswith("2026-07-17"):
                stub = True
            checks.append(
                _check(
                    "heartbeat_fresh",
                    (not stub) and age < HEARTBEAT_MAX_AGE_S,
                    f"age={age:.0f}s stub={stub} cycle={hb_data.get('cycle')}",
                )
            )
            if b == "gold":
                cycle = int(hb_data.get("cycle") or 0)
                checks.append(
                    _check(
                        "gold_cycle_advancing",
                        cycle >= 1,
                        f"cycle={cycle}",
                        critical=False,
                    )
                )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            checks.append(_check("heartbeat_fresh", False, str(exc)))
    else:
        checks.append(_check("heartbeat_fresh", False, "missing heartbeat.json"))

    # Price sanity from heartbeat — empty prices with configured pairs is a fail
    # (bots must publish real quotes each cycle).
    hb_prices = hb_data.get("prices") if isinstance(hb_data, dict) else None
    ok_px, px_reason = price_sanity_book(
        hb_prices,
        hb_data.get("price_history") if isinstance(hb_data, dict) else None,
    )
    if pairs and (not hb_prices):
        ok_px, px_reason = False, "price_sanity:empty_prices"
    checks.append(_check("price_sanity", ok_px, px_reason or "ok"))

    # Canonical trade book present
    trades = state_dir / "trades.jsonl"
    checks.append(_check("trades_file", trades.exists(), str(trades)))

    # Feed skip mix
    feed = feed_error_rate(state_dir / "skips.jsonl")
    checks.append(
        _check(
            "feed_skip_mix",
            bool(feed.get("ok")),
            f"rate={feed.get('rate')} feed_n={feed.get('feed_n')}/{feed.get('n')}",
            critical=False,
        )
    )

    idle = idle_skip_slo(state_dir / "skips.jsonl")
    checks.append(
        _check(
            "not_effectively_paused",
            not bool(idle.get("effectively_paused")),
            str(idle.get("detail")),
            critical=False,
        )
    )

    # Discovery pulse admitted (soft)
    disc = state_dir / "discovered"
    admitted_any = 0
    best_oos = None
    if disc.exists():
        for p in disc.glob("_pulse_*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                admitted_any = max(admitted_any, int(data.get("admitted") or 0))
                if data.get("best_oos") is not None:
                    best_oos = data.get("best_oos")
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                continue
        pulse = disc / "_discovery_pulse.json"
        if pulse.exists():
            try:
                data = json.loads(pulse.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for v in data.values():
                        if isinstance(v, dict):
                            admitted_any = max(
                                admitted_any,
                                int(v.get("admitted") or 0),
                            )
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
    checks.append(
        _check(
            "gp_admitted",
            admitted_any > 0,
            f"admitted_any={admitted_any} best_oos={best_oos}",
            critical=False,
        )
    )

    # Shadow num_active
    shadow_n = 0
    shadow_active = 0
    sp = state_dir / "gp_shadow.jsonl"
    if sp.exists():
        try:
            for line in sp.read_text(encoding="utf-8").splitlines()[-50:]:
                if not line.strip():
                    continue
                rec = json.loads(line)
                shadow_n += 1
                if int(rec.get("num_active") or 0) > 0:
                    shadow_active += 1
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    checks.append(
        _check(
            "gp_shadow_active",
            shadow_active > 0,
            f"active_rows={shadow_active}/{shadow_n}",
            critical=False,
        )
    )

    # Promote gate under bot_state_dir
    gate = state_dir / "gp_promote_gate.json"
    checks.append(
        _check(
            "promote_gate_path",
            True,
            str(gate) + (" exists" if gate.exists() else " missing_ok"),
            critical=False,
        )
    )

    # Halt status (informational — halted is not a soak fail by itself)
    halted, halt_reason = entries_halted(b)
    checks.append(
        _check(
            "halt_switch_readable",
            True,
            f"halted={halted} {halt_reason}",
            critical=False,
        )
    )

    # Strategy files
    for pair in pairs:
        live = strategy_yaml_path(pair, b)
        seed = (
            repo_root()
            / "bots"
            / b
            / "state"
            / "strategies"
            / (pair.replace("/", "_").replace("-", "_") + ".yaml")
        )
        present = live.exists() or seed.exists()
        checks.append(
            _check(
                f"strategy_{pair}",
                present,
                str(live if live.exists() else seed),
            )
        )

    for rel in ("hypotheses.jsonl", "flatline_log.jsonl", "policy.json"):
        p = state_dir / rel
        checks.append(
            _check(
                f"artifact_{rel}",
                True,
                "optional" if not p.exists() else "ok",
                critical=False,
            )
        )

    # Hypotheses pollution
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
                if (
                    "max_dd 2.00%" in reason
                    or "max_dd 2.0%" in reason
                    or rec.get("variable") == "rsi_period"
                    and rec.get("reasoning") == "improve WR"
                ):
                    polluted += 1
        except OSError as exc:
            checks.append(_check("hypotheses_clean", False, str(exc)))
        else:
            checks.append(
                _check(
                    "hypotheses_clean",
                    polluted == 0,
                    f"polluted={polluted}/{total}" if total else "empty",
                )
            )

    # Archive / orphan isolation
    bad_refs = _archive_pollution_refs(state_dir)
    checks.append(
        _check(
            "archive_isolated",
            len(bad_refs) == 0,
            "ok" if not bad_refs else ",".join(bad_refs[:5]),
        )
    )

    # Live prices stubs under bots/*/state
    stub_live = repo_root() / "bots" / b / "state" / f"live_prices_{b}.json"
    stub_bad = False
    if stub_live.exists():
        try:
            data = json.loads(stub_live.read_text(encoding="utf-8"))
            for k, v in (data or {}).items():
                if str(k).endswith("-x") or (
                    isinstance(v, dict) and float(v.get("price") or 0) == 1.0
                ):
                    stub_bad = True
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            stub_bad = True
    checks.append(
        _check(
            "no_live_price_stubs",
            not stub_bad,
            str(stub_live) if stub_bad else "ok",
            critical=False,
        )
    )

    critical_ok = all(c["passed"] for c in checks if c.get("critical", True))
    # Go/no-go: criticals + heartbeat + price sanity + trades file + archive
    must = {
        "config_load",
        "state_dir",
        "heartbeat_fresh",
        "price_sanity",
        "trades_file",
        "archive_isolated",
    }
    go = all(c["passed"] for c in checks if c["name"] in must)
    return Report(bot=b, ok=critical_ok, go_nogo=go, checks=checks)


def run_all(bots: tuple[str, ...] = VALID_BOTS) -> dict:
    """Audit every bot; return combined go/no-go."""
    reports = {b: run(b).to_dict() for b in bots}
    return {
        "go_nogo": all(r.get("go_nogo") for r in reports.values()),
        "bots": reports,
        "ts": time.time(),
    }


class SelfAudit:
    """Blueprint Section 6 contract wrapper."""

    def run(self, bot: str | None = None) -> dict:
        return run(bot).to_dict()


def main() -> None:
    import pprint

    pprint.pp(run_all())


if __name__ == "__main__":
    main()
