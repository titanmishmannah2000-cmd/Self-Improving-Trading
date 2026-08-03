"""Event / calendar / narrative tilt (L7c)."""

from __future__ import annotations

import time
from datetime import datetime, timezone

from hermes_core.env import get_env


def event_pause_hard() -> bool:
    return get_env("BTC_EVENT_PAUSE", "0") == "1"


def fetch_event_context(*, bot: str | None = None) -> dict:
    """Structured event risk. Key-gated; fail-open to zero risk.

    Without API keys returns a mild weekend/session heuristic only.
    """
    risk = 0.0
    tilt = 0.0
    reasons: list[str] = []
    # Static high-impact windows can be supplied via env CSV of HH:MM UTC
    windows = (get_env("BTC_EVENT_WINDOWS", "") or "").strip()
    if windows:
        try:
            now = datetime.now(timezone.utc)
            hm = f"{now.hour:02d}:{now.minute:02d}"
            for part in windows.split(","):
                part = part.strip()
                if not part:
                    continue
                if part == hm or (len(part) == 5 and part[:2] == f"{now.hour:02d}"):
                    risk = max(risk, 0.8)
                    reasons.append(f"window:{part}")
        except Exception:  # noqa: BLE001
            pass

    key = (get_env("FMP_API_KEY", "") or get_env("EVENT_API_KEY", "") or "").strip()
    if key:
        # Optional: light stub — real HTTP left for deploy; mark available
        reasons.append("api_key_present")
        # Without network in unit tests we don't call out unless EVENT_FETCH=1
        if get_env("EVENT_FETCH", "0") == "1":
            try:
                import httpx  # noqa: F401

                # Placeholder — keep risk unchanged if fetch not wired
                reasons.append("event_fetch_skipped_stub")
            except Exception:  # noqa: BLE001
                pass

    # Weekend crypto still trades; slight narrative calm
    try:
        if datetime.now(timezone.utc).weekday() >= 5:
            tilt -= 0.05
            reasons.append("weekend")
    except Exception:  # noqa: BLE001
        pass

    return {
        "event_risk": risk,
        "narrative_tilt": tilt,
        "event_reasons": reasons,
        "ts": time.time(),
        "hard_pause": bool(event_pause_hard() and risk >= 0.7),
    }


def event_patience_mult(event: dict | None) -> float:
    if not event:
        return 1.0
    try:
        r = float(event.get("event_risk") or 0.0)
    except (TypeError, ValueError):
        r = 0.0
    if r >= 0.7:
        return 0.65
    if r >= 0.4:
        return 0.85
    return 1.0
