"""Discord/webhook alerting for trade events (S18 gap closure).

Stdlib-only (urllib) — no discord.py dependency. Fail-soft: a network or
serialisation error NEVER raises into the trade loop; it returns False and
logs to stderr so the cycle continues.

The webhook URL comes from config/env (DISCORD_ALERTS_WEBHOOK), never hardcoded,
so verify_no_secrets.py stays clean.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

DEFAULT_WEBHOOK_ENV = "DISCORD_ALERTS_WEBHOOK"


def _default_webhook() -> str | None:
    return os.environ.get(DEFAULT_WEBHOOK_ENV)


def send_alert(message: str, *, webhook_url: str | None = None,
               username: str = "Hermes", timeout: float = 10.0) -> bool:
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


def send_trade_alert(bot: str, pair: str, reason: str, pnl_pct: float,
                     *, entry_price: float | None = None,
                     exit_price: float | None = None,
                     webhook_url: str | None = None,
                     cycle: int | None = None) -> bool:
    """Compose and send a trade-close alert for ``bot``/``pair``.

    Fail-soft: never raises; returns False on any failure.
    """
    pnl = f"{pnl_pct:+.2f}%"
    bits: list[str] = [f"**{bot.upper()}** {pair} closed — {reason} ({pnl})"]
    if cycle is not None:
        bits.append(f"cycle {cycle}")
    if entry_price is not None and exit_price is not None:
        bits.append(f"entry {entry_price:.5f} → exit {exit_price:.5f}")
    return send_alert(" ".join(bits), webhook_url=webhook_url)


def send_text_alert(message: str, *, webhook_url: str | None = None) -> bool:
    """Generic text alert (heartbeat, error, flatline notice). Fail-soft."""
    return send_alert(message, webhook_url=webhook_url)
