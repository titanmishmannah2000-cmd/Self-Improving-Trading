"""Per-setup playbooks in bot state (L3)."""

from __future__ import annotations

import json
from pathlib import Path


def _path(bot: str | None) -> Path:
    from hermes_core.state.paths import bot_state_dir

    return bot_state_dir(bot) / "playbooks.json"


def setup_key(pair: str, entry_type: str, d1: str, session: str = "", quality_bin: str = "") -> str:
    return "|".join(
        [
            str(pair or ""),
            str(entry_type or ""),
            str(d1 or "unknown"),
            str(session or "any"),
            str(quality_bin or "mid"),
        ]
    )


def load_playbooks(bot: str | None = None) -> dict:
    p = _path(bot)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    return {}


def save_playbooks(data: dict, bot: str | None = None) -> None:
    p = _path(bot)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def update_playbook_on_close(
    *,
    bot: str | None,
    pair: str,
    entry_type: str,
    d1: str,
    pnl: float,
    mfe: float | None,
    capture: float | None,
    hold_cycles: int | None,
    fees_pct: float | None = None,
) -> dict:
    books = load_playbooks(bot)
    key = setup_key(pair, entry_type, d1)
    st = books.get(key) or {
        "n": 0,
        "wins": 0,
        "fee_wins": 0,
        "sum_mfe": 0.0,
        "sum_capture": 0.0,
        "sum_hold": 0.0,
        "die_in_chop": 0,
    }
    st["n"] = int(st.get("n") or 0) + 1
    if pnl > 0:
        st["wins"] = int(st.get("wins") or 0) + 1
    try:
        fee = float(fees_pct) if fees_pct is not None else 0.0
    except (TypeError, ValueError):
        fee = 0.0
    # Fee-aware win: net clears round-trip (or residual haircut proxy).
    if float(pnl) > max(0.0, fee):
        st["fee_wins"] = int(st.get("fee_wins") or 0) + 1
    if mfe is not None:
        st["sum_mfe"] = float(st.get("sum_mfe") or 0) + float(mfe)
    if capture is not None:
        st["sum_capture"] = float(st.get("sum_capture") or 0) + float(capture)
    if hold_cycles is not None:
        st["sum_hold"] = float(st.get("sum_hold") or 0) + float(hold_cycles)
    if "chop" in str(d1).lower() and (mfe or 0) > 0.2 and pnl <= 0:
        st["die_in_chop"] = int(st.get("die_in_chop") or 0) + 1
    n = max(1, int(st["n"]))
    st["wr"] = st["wins"] / n
    st["fee_wr"] = int(st.get("fee_wins") or 0) / n
    st["avg_mfe"] = st["sum_mfe"] / n
    st["avg_capture"] = st["sum_capture"] / n
    st["median_hold"] = st["sum_hold"] / n
    st["die_in_chop_rate"] = st["die_in_chop"] / n
    books[key] = st
    save_playbooks(books, bot)
    return st


def playbook_patience(
    *, pair: str, entry_type: str, d1: str, bot: str | None = None
) -> float | None:
    books = load_playbooks(bot)
    key = setup_key(pair, entry_type, d1)
    st = books.get(key)
    if not st or int(st.get("n") or 0) < 8:
        return None
    rate = float(st.get("die_in_chop_rate") or 0)
    if rate >= 0.5:
        return 0.6
    if float(st.get("wr") or 0) >= 0.55:
        return 1.2
    return 1.0
