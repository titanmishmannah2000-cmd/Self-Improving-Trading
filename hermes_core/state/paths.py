"""Runtime state path layout (discipline D2).

All mutable bot state lives under ``{HERMES_STATE_ROOT}/{bot}/state/``.
When ``HERMES_STATE_ROOT`` is unset, ``state_root()`` falls back to the repo
root so local dev/tests work without extra setup.
"""

from __future__ import annotations

import os
from pathlib import Path

from hermes_core.config.loader import _discover_bot_for_pair, state_root


def current_bot() -> str:
    """Active bot for this process (Railway sets HERMES_BOT_NAME per service)."""
    return os.getenv("HERMES_BOT_NAME", "forex")


def bot_for_pair(pair: str) -> str:
    """Resolve which bot owns a tradeable pair."""
    return _discover_bot_for_pair(pair) or current_bot()


def bot_state_dir(bot: str | None = None) -> Path:
    """Per-bot runtime state directory on the persistent volume."""
    b = bot or current_bot()
    d = state_root() / b / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def hypotheses_path(bot: str | None = None) -> Path:
    return bot_state_dir(bot) / "hypotheses.jsonl"


def flatline_log_path(bot: str | None = None, *, pair: str | None = None) -> Path:
    b = bot or (bot_for_pair(pair) if pair else current_bot())
    return bot_state_dir(b) / "flatline_log.jsonl"


def crisis_db_path(bot: str | None = None, *, pair: str | None = None) -> Path:
    b = bot or (bot_for_pair(pair) if pair else current_bot())
    return bot_state_dir(b) / "crisis_embeddings.json"


def discovered_dir(bot: str | None = None, *, pair: str | None = None) -> Path:
    b = bot or (bot_for_pair(pair) if pair else current_bot())
    d = bot_state_dir(b) / "discovered"
    d.mkdir(parents=True, exist_ok=True)
    return d


def discovered_path(pair: str) -> Path:
    safe = pair.replace("/", "_")
    return discovered_dir(pair=pair) / f"{safe}.json"


def policy_path(bot: str | None = None) -> Path:
    return bot_state_dir(bot) / "policy.json"


def gp_state_path(bot: str | None = None, *, pair: str | None = None) -> Path:
    b = bot or (bot_for_pair(pair) if pair else current_bot())
    return bot_state_dir(b) / "gp_intelligence.json"


def chart_cache_dir(bot: str | None = None) -> Path:
    d = bot_state_dir(bot) / "chart_cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def hypotheses_kb_path(bot: str | None = None) -> Path:
    return bot_state_dir(bot) / "hypotheses_kb.jsonl"


def cortex_dir(bot: str | None = None) -> Path:
    d = bot_state_dir(bot) / "cortex"
    d.mkdir(parents=True, exist_ok=True)
    return d
