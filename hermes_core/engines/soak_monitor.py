"""Automated 30-day soak / discovery monitor with Discord notifications.

Runs inside each bot process (background thread) and as a standalone cron:

  python -m cron.soak_monitor
  python -m hermes_core.engines.soak_monitor

Watches:
  * heartbeat age
  * invent pulse status / admitted / near_misses / admit_zero_streak
  * self_audit go/no-go

Alerts Discord on regressions; sends a weekly digest even when healthy.
Fail-soft: never raises into the trade loop.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hermes_core.env import get_env
from hermes_core.engines.soak_controls import entries_halted
from hermes_core.state.paths import bot_state_dir, current_bot

VALID_BOTS = ("forex", "gold", "crypto")

# How often the embedded runner thread evaluates (default 5 min).
SOAK_MONITOR_INTERVAL_S = int(get_env("SOAK_MONITOR_INTERVAL_S", "300"))
# Weekly digest cadence (default 7d).
SOAK_WEEKLY_DIGEST_S = int(get_env("SOAK_WEEKLY_DIGEST_S", str(7 * 86400)))
# Alert thresholds.
SOAK_HB_ALERT_AGE_S = int(get_env("SOAK_HB_ALERT_AGE_S", str(15 * 60)))
SOAK_PULSE_STALE_S = int(get_env("SOAK_PULSE_STALE_S", str(36 * 3600)))
SOAK_ADMIT_ZERO_ALERT = int(get_env("SOAK_ADMIT_ZERO_ALERT", "8"))
# Set 0 to disable Discord (still prints + writes latch).
SOAK_MONITOR_NOTIFY = get_env("SOAK_MONITOR_NOTIFY", "1").strip() not in {"0", "false", "False"}


def _monitor_path(bot: str) -> Path:
    return bot_state_dir(bot) / "soak_monitor.json"


def _load_latch(bot: str) -> dict[str, Any]:
    path = _monitor_path(bot)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_latch(bot: str, data: dict[str, Any]) -> None:
    path = _monitor_path(bot)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def _pulse_age_s(pulse: dict) -> float | None:
    ts = pulse.get("ts")
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return max(0.0, time.time() - float(ts))
        # ISO-8601
        raw = str(ts).replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return max(0.0, time.time() - dt.timestamp())
    except (TypeError, ValueError):
        return None


def collect_bot_snapshot(bot: str) -> dict[str, Any]:
    """Collect heartbeat + invent pulse + go/no-go for one bot."""
    from hermes_core.config import load_config
    from hermes_core.engines.genetic import load_discovery_pulse
    from hermes_core.engines.self_audit import run as audit_run

    state = bot_state_dir(bot)
    hb_path = state / "heartbeat.json"
    hb: dict[str, Any] = {}
    hb_age: float | None = None
    if hb_path.exists():
        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            hb_age = max(0.0, time.time() - float(hb.get("ts") or 0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            hb = {}
            hb_age = None

    try:
        pairs = list(load_config(bot).get("pairs") or [])
    except Exception:  # noqa: BLE001
        pairs = []

    pulses: dict[str, dict] = {}
    for pair in pairs:
        try:
            p = load_discovery_pulse(pair)
        except Exception:  # noqa: BLE001
            p = None
        if isinstance(p, dict):
            pulses[pair] = p

    try:
        audit = audit_run(bot).to_dict()
    except Exception as exc:  # noqa: BLE001
        audit = {"go_nogo": False, "ok": False, "checks": [], "error": str(exc)}

    return {
        "bot": bot,
        "ts": time.time(),
        "heartbeat_age_s": hb_age,
        "last_discovery_run_ts": hb.get("last_discovery_run_ts"),
        "cycle": hb.get("cycle"),
        "pairs": pairs,
        "pulses": {
            pair: {
                "status": p.get("status"),
                "admitted": p.get("admitted"),
                "admit_zero_streak": p.get("admit_zero_streak"),
                "timeout_streak": p.get("timeout_streak"),
                "near_misses": len(p.get("near_misses") or [])
                if isinstance(p.get("near_misses"), list)
                else p.get("near_misses"),
                "best_oos": p.get("best_oos"),
                "reject_counts": p.get("reject_counts"),
                "age_s": _pulse_age_s(p),
                "reason": p.get("reason"),
            }
            for pair, p in pulses.items()
        },
        "go_nogo": bool(audit.get("go_nogo")),
        "audit_ok": bool(audit.get("ok")),
        "failed_checks": [
            c.get("name")
            for c in (audit.get("checks") or [])
            if isinstance(c, dict) and not c.get("passed")
        ],
    }


def evaluate_alerts(snap: dict[str, Any]) -> list[dict[str, str]]:
    """Return alert dicts ``{key, level, message}`` for regressions."""
    bot = str(snap.get("bot") or "?")
    alerts: list[dict[str, str]] = []

    try:
        halted, halt_reason = entries_halted(bot)
    except Exception:  # noqa: BLE001
        halted, halt_reason = False, ""
    if halted:
        alerts.append(
            {
                "key": "halt_active",
                "level": "critical",
                "message": f"[soak] {bot}: entries halted ({halt_reason or 'halt'})",
            }
        )

    hb_age = snap.get("heartbeat_age_s")
    if hb_age is None:
        alerts.append(
            {
                "key": "heartbeat_missing",
                "level": "critical",
                "message": f"[soak] {bot}: heartbeat missing",
            }
        )
    elif float(hb_age) > SOAK_HB_ALERT_AGE_S:
        alerts.append(
            {
                "key": "heartbeat_stale",
                "level": "critical",
                "message": (
                    f"[soak] {bot}: heartbeat age {float(hb_age) / 60:.0f}m "
                    f"(>{SOAK_HB_ALERT_AGE_S // 60}m)"
                ),
            }
        )

    if not snap.get("go_nogo"):
        failed = ",".join(snap.get("failed_checks") or []) or "unknown"
        alerts.append(
            {
                "key": "go_nogo_red",
                "level": "critical",
                "message": f"[soak] {bot}: go/no-go RED failed=[{failed}]",
            }
        )

    pulses = snap.get("pulses") or {}
    if not pulses and snap.get("pairs"):
        alerts.append(
            {
                "key": "invent_no_pulses",
                "level": "warn",
                "message": f"[soak] {bot}: no invent pulses yet for {snap.get('pairs')}",
            }
        )

    for pair, p in pulses.items():
        if not isinstance(p, dict):
            continue
        status = str(p.get("status") or "")
        age = p.get("age_s")
        if status == "chronic_timeout_backoff":
            alerts.append(
                {
                    "key": f"chronic_timeout:{pair}",
                    "level": "warn",
                    "message": f"[soak] {bot}/{pair}: invent chronic_timeout_backoff",
                }
            )
        if status == "timeout" and int(p.get("timeout_streak") or 0) >= 2:
            alerts.append(
                {
                    "key": f"timeout_streak:{pair}",
                    "level": "warn",
                    "message": (
                        f"[soak] {bot}/{pair}: invent timeout_streak={p.get('timeout_streak')}"
                    ),
                }
            )
        if age is not None and float(age) > SOAK_PULSE_STALE_S:
            alerts.append(
                {
                    "key": f"pulse_stale:{pair}",
                    "level": "warn",
                    "message": (
                        f"[soak] {bot}/{pair}: invent pulse stale "
                        f"{float(age) / 3600:.1f}h (status={status or '?'})"
                    ),
                }
            )
        az = int(p.get("admit_zero_streak") or 0)
        if SOAK_ADMIT_ZERO_ALERT > 0 and az >= SOAK_ADMIT_ZERO_ALERT:
            alerts.append(
                {
                    "key": f"admit_zero:{pair}",
                    "level": "info",
                    "message": (
                        f"[soak] {bot}/{pair}: admit_zero_streak={az} "
                        f"near_misses={p.get('near_misses')} "
                        f"(S10 strict — classical should still trade)"
                    ),
                }
            )
    return alerts


def format_weekly_digest(snap: dict[str, Any]) -> str:
    bot = snap.get("bot")
    hb = snap.get("heartbeat_age_s")
    hb_s = f"{float(hb) / 60:.1f}m" if hb is not None else "missing"
    lines = [
        f"[soak-weekly] {bot} go_nogo={'GREEN' if snap.get('go_nogo') else 'RED'} "
        f"hb_age={hb_s} cycle={snap.get('cycle')}",
    ]
    pulses = snap.get("pulses") or {}
    if not pulses:
        lines.append("  invent: (no pulses)")
    for pair, p in pulses.items():
        if not isinstance(p, dict):
            continue
        age = p.get("age_s")
        age_s = f"{float(age) / 3600:.1f}h" if age is not None else "?"
        lines.append(
            f"  {pair}: status={p.get('status')} admitted={p.get('admitted')} "
            f"near_misses={p.get('near_misses')} "
            f"admit_zero_streak={p.get('admit_zero_streak')} "
            f"pulse_age={age_s}"
        )
    failed = snap.get("failed_checks") or []
    if failed:
        lines.append(f"  failed_checks: {','.join(failed)}")
    return "\n".join(lines)


def _notify(message: str, *, bot: str, guard: str) -> bool:
    if not SOAK_MONITOR_NOTIFY:
        print(message, flush=True)
        return True  # print-only counts as sent
    try:
        from hermes_core.notify import send_text_alert

        ok = send_text_alert(message, bot=bot, pair="*", guard=guard)
        print(message, flush=True)
        return bool(ok)
    except Exception as exc:  # noqa: BLE001
        print(f"[soak_monitor] notify failed: {exc}", flush=True)
        print(message, flush=True)
        return False


def run_once(bot: str | None = None, *, force_weekly: bool = False) -> dict[str, Any]:
    """Evaluate one bot, notify on new alerts / weekly digest, persist latch."""
    b = bot or current_bot()
    snap = collect_bot_snapshot(b)
    alerts = evaluate_alerts(snap)
    latch = _load_latch(b)
    prev_keys = set(latch.get("active_alert_keys") or [])
    new_keys = {a["key"] for a in alerts}

    sent: list[str] = []
    for a in alerts:
        if a["key"] in prev_keys:
            continue  # already notified for this open condition
        if _notify(a["message"], bot=b, guard=f"soak_{a['key'].split(':')[0]}"):
            sent.append(a["key"])

    # Only latch keys we have successfully notified (or still open from a prior
    # successful notify). Failed Discord must not dedupe forever.
    latched_keys = (prev_keys & new_keys) | set(sent)

    # Cleared alerts — one recovery note for criticals only.
    cleared = prev_keys - new_keys
    critical_cleared = [
        k
        for k in cleared
        if k in {"heartbeat_missing", "heartbeat_stale", "go_nogo_red", "halt_active"}
    ]
    if critical_cleared:
        _notify(
            f"[soak] {b}: recovered cleared={','.join(sorted(critical_cleared))}",
            bot=b,
            guard="soak_recovered",
        )

    now = time.time()
    last_weekly = float(latch.get("last_weekly_digest_ts") or 0.0)
    weekly_due = force_weekly or (now - last_weekly) >= max(3600, SOAK_WEEKLY_DIGEST_S)
    weekly_sent = False
    if weekly_due:
        digest = format_weekly_digest(snap)
        if _notify(digest, bot=b, guard="soak_weekly"):
            weekly_sent = True
            latch["last_weekly_digest_ts"] = now

    latch.update(
        {
            "ts": now,
            "active_alert_keys": sorted(latched_keys),
            "last_snapshot": {
                "go_nogo": snap.get("go_nogo"),
                "heartbeat_age_s": snap.get("heartbeat_age_s"),
                "pulses": snap.get("pulses"),
                "failed_checks": snap.get("failed_checks"),
            },
            "last_alerts_sent": sent,
            "weekly_sent": weekly_sent,
        }
    )
    _save_latch(b, latch)
    return {
        "bot": b,
        "snapshot": snap,
        "alerts": alerts,
        "sent": sent,
        "weekly_sent": weekly_sent,
        "go_nogo": snap.get("go_nogo"),
    }


def run_all(bots: tuple[str, ...] = VALID_BOTS, *, force_weekly: bool = False) -> dict[str, Any]:
    reports = {b: run_once(b, force_weekly=force_weekly) for b in bots}
    return {
        "ts": time.time(),
        "go_nogo": all(r.get("go_nogo") for r in reports.values()),
        "bots": reports,
    }


def monitor_loop(bot: str, stop, *, interval_s: int | None = None) -> None:
    """Background loop for bots/_runner — fail-soft forever."""
    wait = max(60, int(interval_s or SOAK_MONITOR_INTERVAL_S))
    # First pass shortly after boot so go/no-go is visible early.
    if stop.wait(min(120, wait)):
        return
    while not stop.is_set():
        try:
            out = run_once(bot)
            print(
                f"[hermes][soak_monitor] {bot}: go_nogo={out.get('go_nogo')} "
                f"alerts={len(out.get('alerts') or [])} sent={out.get('sent')}",
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[hermes][soak_monitor] {bot}: ERROR {exc!r}", flush=True)
        if stop.wait(wait):
            return


def main() -> None:
    bot = os.getenv("HERMES_BOT_NAME")
    force = os.getenv("SOAK_MONITOR_FORCE_WEEKLY", "").strip() in {"1", "true", "True"}
    out = run_once(bot, force_weekly=force) if bot else run_all(force_weekly=force)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
