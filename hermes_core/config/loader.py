"""Config + per-pair strategy loader (Session 1 / Phase 1).

Reads a bot's ``bots/<bot>/config.yaml`` and each pair's strategy file
(``bots/<bot>/state/strategies/<PAIR>.yaml``) into plain dicts, then runs the
validator before returning. This is the single place config is read, satisfying
discipline D1 (bots are config instances; the engine never branches on bot name
except here in the loader).

Paths are resolved relative to the repo root so the loader works from any cwd
(tests, Railway runtime, cron). The repo root is discovered as the directory
containing ``hermes_core``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from .schema import ValidationError
from .validator import validate_strategy_params

# Repo root = directory that contains the ``hermes_core`` package.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def repo_root() -> Path:
    """Absolute path to the project root (contains hermes_core/ and bots/)."""
    return _REPO_ROOT


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        raise ValidationError(f"config file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValidationError(f"config root must be a mapping: {path}")
    return data


def load_config(bot: str) -> dict:
    """Load and validate ``bots/<bot>/config.yaml``.

    Returns the raw mapping (bot/pairs/goal/global). The `pairs` key is asserted
    to be a list, mirroring the blueprint test expectation:
        cfg["pairs"] == ["EUR/USD", "GBP/USD", "GBP/JPY", "AUD/USD"]
    """
    path = repo_root() / "bots" / bot / "config.yaml"
    cfg = _read_yaml(path)
    pairs = cfg.get("pairs")
    if not isinstance(pairs, list):
        raise ValidationError(f"{bot}/config.yaml: 'pairs' must be a list")
    # Surface an obvious bot-name mismatch early rather than letting it drift.
    if cfg.get("bot", {}).get("name") and cfg["bot"]["name"] != bot:
        raise ValidationError(
            f"{bot}/config.yaml: bot.name={cfg['bot']['name']!r} != folder {bot!r}"
        )
    return cfg


def load_strategy_for_pair(pair: str, bot: str | None = None) -> dict:
    """Load and validate the strategy file for ``pair``.

    The per-pair file lives at ``bots/<bot>/state/strategies/<PAIR>.yaml`` where
    PAIR uses underscores (EUR/USD -> EUR_USD.yaml), per blueprint
    strategy_filename(). If ``bot`` is None, discover which bot folder holds the
    pair by matching config.yaml pair lists (cheap, runs once per call).

    Validates before returning so an out-of-range or wrong-strategy_type file can
    never reach the engine.
    """
    if bot is None:
        bot = _discover_bot_for_pair(pair)
    if bot is None:
        raise ValidationError(f"no bot config declares pair {pair!r}")
    fname = pair.replace("/", "_").replace("-", "_") + ".yaml"
    path = repo_root() / "bots" / bot / "state" / "strategies" / fname
    strategy = _read_yaml(path)
    if "pair" not in strategy:
        strategy = {**strategy, "pair": pair}
    validate_strategy_params(strategy, raise_on_fail=True)
    return strategy


def _discover_bot_for_pair(pair: str) -> str | None:
    bots_dir = repo_root() / "bots"
    if not bots_dir.exists():
        return None
    for bot_dir in sorted(bots_dir.iterdir()):
        cfg_path = bot_dir / "config.yaml"
        if not cfg_path.exists():
            continue
        try:
            cfg = _read_yaml(cfg_path)
        except ValidationError:
            continue
        if any(declared == pair for declared in (cfg.get("pairs") or [])):
            return bot_dir.name
    return None


def _seed_default_state() -> None:
    """Provision empty per-pair state directories on a genuinely empty /data volume.

    Discipline 3.1: re-seeding is reserved for *empty* volumes on first boot — it
    must never silently overwrite existing production state. This function only
    creates the directory tree if absent; it does not touch or rewrite existing
    files. (The live /data volume is wired in Session 7; here we ensure the local
    data/ scaffold mirrors the expected layout so later sessions have a place to
    write.)
    """
    data_root = repo_root() / "data"
    for bot in ("forex", "gold"):
        cfg_path = repo_root() / "bots" / bot / "config.yaml"
        if not cfg_path.exists():
            continue
        try:
            cfg = _read_yaml(cfg_path)
        except ValidationError:
            continue
        for pair in cfg.get("pairs") or []:
            pair_dir = data_root / bot / pair.replace("/", "_")
            pair_dir.mkdir(parents=True, exist_ok=True)
