"""Heartbeat monitor — alerts on 90 minutes of silence (Session 7 / roadmap 4.2)."""

from __future__ import annotations

import json
import os
import time

SILENCE_SECONDS = 90 * 60


def main() -> None:
    from hermes_core.notify.discord import send_text_alert
    from hermes_core.state.paths import bot_state_dir, current_bot

    bot = os.getenv("HERMES_BOT_NAME", current_bot())
    hb_path = bot_state_dir(bot) / "heartbeat.json"
    if not hb_path.exists():
        msg = f"[heartbeat] {bot}: no heartbeat file at {hb_path}"
        print(msg, flush=True)
        send_text_alert(msg)
        return

    try:
        data = json.loads(hb_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        msg = f"[heartbeat] {bot}: unreadable heartbeat — {exc}"
        print(msg, flush=True)
        send_text_alert(msg)
        return

    ts = float(data.get("ts", 0))
    age = time.time() - ts
    if age > SILENCE_SECONDS:
        msg = (f"[heartbeat] ALERT {bot}: silent {age / 60:.0f} min "
               f"(threshold {SILENCE_SECONDS // 60} min)")
        print(msg, flush=True)
        send_text_alert(msg)
    else:
        print(f"[heartbeat] {bot}: ok ({age:.0f}s ago)", flush=True)


if __name__ == "__main__":
    main()
