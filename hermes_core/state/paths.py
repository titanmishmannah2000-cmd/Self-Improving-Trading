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


def strategies_dir(bot: str | None = None) -> Path:
    """Mutable per-bot strategies on the runtime volume (not the image)."""
    d = bot_state_dir(bot) / "strategies"
    d.mkdir(parents=True, exist_ok=True)
    return d


def strategy_path(pair: str, bot: str | None = None) -> Path:
    """Canonical live strategy YAML: ``{state_root}/{bot}/state/strategies/EUR_USD.yaml``."""
    b = bot or bot_for_pair(pair)
    fname = pair.replace("/", "_").replace("-", "_") + ".yaml"
    return strategies_dir(b) / fname


def seed_strategy_path(pair: str, bot: str | None = None) -> Path:
    """Image/seed strategy YAML under ``bots/{bot}/state/strategies/`` (read-only source)."""
    from hermes_core.config.loader import repo_root

    b = bot or bot_for_pair(pair)
    fname = pair.replace("/", "_").replace("-", "_") + ".yaml"
    return repo_root() / "bots" / b / "state" / "strategies" / fname


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


def reflection_latch_path(bot: str | None = None) -> Path:
    """Once-per-N-trades reflection latch (pair -> reflected_count)."""
    return bot_state_dir(bot) / ".reflection_latches.json"


def cortex_dir(bot: str | None = None) -> Path:
    d = bot_state_dir(bot) / "cortex"
    d.mkdir(parents=True, exist_ok=True)
    return d
