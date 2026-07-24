"""Discord/webhook alerting for trade events (S18 gap closure).

Stdlib-only (urllib) — no discord.py dependency. Fail-soft: a network or
serialisation error NEVER raises into the trade loop; it returns False and
logs to stderr so the cycle continues.

The webhook URL comes from config/env (DISCORD_ALERTS_WEBHOOK), never hardcoded,
so verify_no_secrets.py stays clean.

D9 alert budget: max 1 Discord message per (bot, pair, guard) per 15-minute
window; excess firings are suppressed and summarized on the next allowed send.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

DEFAULT_WEBHOOK_ENV = "DISCORD_ALERTS_WEBHOOK"
BUDGET_WINDOW_S = 15 * 60  # D9: 15-minute alert budget window

# key -> {window_start, sent, suppressed}
_budget: dict[str, dict[str, float | int]] = {}


def _default_webhook() -> str | None:
    return os.environ.get(DEFAULT_WEBHOOK_ENV)


def _budget_key(bot: str, pair: str, guard: str) -> str:
    return f"{bot}:{pair}:{guard}"


def take_alert_budget(bot: str, pair: str, guard: str) -> tuple[bool, str]:
    """[GUARD D9] Enforce max 1 alert per (bot, pair, guard) per 15 minutes.

    Returns (allowed, suffix). When allowed after suppressions, suffix summarizes
    how many alerts were dropped in the prior window.
    """
    key = _budget_key(bot, pair, guard)
    now = time.monotonic()
    rec = _budget.get(key)
    if rec is None or now - float(rec["window_start"]) >= BUDGET_WINDOW_S:
        suffix = ""
        if rec and int(rec.get("suppressed", 0)) > 0:
            suffix = f" (+{int(rec['suppressed'])} suppressed last window)"
        _budget[key] = {"window_start": now, "sent": 1, "suppressed": 0}
        return True, suffix
    if int(rec.get("sent", 0)) < 1:
        rec["sent"] = int(rec["sent"]) + 1
        return True, ""
    rec["suppressed"] = int(rec.get("suppressed", 0)) + 1
    return False, ""


def reset_alert_budget() -> None:
    """Test helper — clear in-memory budget state."""
    _budget.clear()


def send_alert(
    message: str, *, webhook_url: str | None = None, username: str = "Hermes", timeout: float = 10.0
) -> bool:
    """POST ``message`` to a Discord webhook. Returns True on 2xx, else False.

    Fail-soft: any transport/HTTP error returns False (never raises).
    """
    url = webhook_url or _default_webhook()
    if not url:
        return False
    payload = {"content": message, "username": username}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return False


def send_trade_alert(
    bot: str,
    pair: str,
    reason: str,
    pnl_pct: float,
    *,
    entry_price: float | None = None,
    exit_price: float | None = None,
    webhook_url: str | None = None,
    cycle: int | None = None,
) -> bool:
    """Compose and send a trade-close alert for ``bot``/``pair``.

    Fail-soft: never raises; returns False on any failure or budget suppression.
    """
    allowed, suffix = take_alert_budget(bot, pair, "trade_close")
    if not allowed:
        return False
    pnl = f"{pnl_pct:+.2f}%"
    bits: list[str] = [f"**{bot.upper()}** {pair} closed — {reason} ({pnl})"]
    if cycle is not None:
        bits.append(f"cycle {cycle}")
    if entry_price is not None and exit_price is not None:
        bits.append(f"entry {entry_price:.5f} → exit {exit_price:.5f}")
    if suffix:
        bits.append(suffix.strip())
    return send_alert(" ".join(bits), webhook_url=webhook_url)


def send_text_alert(
    message: str,
    *,
    webhook_url: str | None = None,
    bot: str = "system",
    pair: str = "*",
    guard: str = "text",
) -> bool:
    """Generic text alert (heartbeat, error, flatline notice). Fail-soft."""
    allowed, suffix = take_alert_budget(bot, pair, guard)
    if not allowed:
        return False
    if suffix:
        message = f"{message} {suffix.strip()}"
    return send_alert(message, webhook_url=webhook_url)
