"""Config + per-pair strategy loader (Session 1 / Phase 1).

Reads a bot's ``bots/<bot>/config.yaml`` (image/config) and each pair's
strategy file. Live strategies live on the runtime volume:

    {HERMES_STATE_ROOT}/{bot}/state/strategies/<PAIR>.yaml

Image seeds under ``bots/<bot>/state/strategies/`` are copied onto the volume
on first load (never overwriting an existing volume file). That keeps
reflection deploys on persistent storage (D2) while still shipping defaults
in the Docker image.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import yaml

from .schema import ValidationError
from .validator import validate_strategy_params

# Repo root = directory that contains the ``hermes_core`` package.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def repo_root() -> Path:
    """Absolute path to the project root (contains hermes_core/ and bots/).

    Code/config (config.yaml, strategy YAML *seeds*) is always read from here,
    which in the Docker image is ``/app``.
    """
    return _REPO_ROOT


def state_root() -> Path:
    """Root under which RUNTIME state is written.

    Discipline 3.1/2: engine state must live on the persistent
    ``/data`` volume, not inside the read-only image (``/app``), or it is
    wiped on every redeploy. Honour HERMES_STATE_ROOT (set to ``/data`` on
    Railway) and fall back to repo_root() for local/dev.

    This is SEPARATE from repo_root() on purpose: config is read from the
    image, but state is written to the volume.
    """
    env = os.getenv("HERMES_STATE_ROOT")
    if env:
        return Path(env)
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


def _strategy_fname(pair: str) -> str:
    return pair.replace("/", "_").replace("-", "_") + ".yaml"


def seed_strategy_path(pair: str, bot: str) -> Path:
    """Image seed: ``bots/{bot}/state/strategies/{PAIR}.yaml``."""
    return repo_root() / "bots" / bot / "state" / "strategies" / _strategy_fname(pair)


def strategy_yaml_path(pair: str, bot: str) -> Path:
    """Live strategy on the runtime volume (canonical write/read target)."""
    return state_root() / bot / "state" / "strategies" / _strategy_fname(pair)


def ensure_strategy_seeded(pair: str, bot: str) -> Path:
    """Copy the image seed onto the volume if the live file is missing.

    Never overwrites an existing volume strategy (reflection deploys must stick).
    Ensures a ``version`` field is present on first seed (baseline ``00``).
    Returns the live path (whether just created or already present).
    """
    live = strategy_yaml_path(pair, bot)
    if live.exists():
        return live
    seed = seed_strategy_path(pair, bot)
    if not seed.exists():
        raise ValidationError(f"strategy seed not found: {seed}")
    live.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = _read_yaml(seed)
    except ValidationError:
        shutil.copy2(seed, live)
        return live
    if "version" not in data:
        data["version"] = "00"
    if "pair" not in data:
        data["pair"] = pair
    tmp = live.with_suffix(live.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    tmp.replace(live)
    return live


def ensure_bot_strategies_seeded(bot: str) -> list[Path]:
    """Seed all configured pairs for ``bot`` onto the volume. Fail-soft per pair."""
    try:
        cfg = load_config(bot)
    except ValidationError:
        return []
    out: list[Path] = []
    for pair in cfg.get("pairs") or []:
        try:
            out.append(ensure_strategy_seeded(pair, bot))
        except ValidationError:
            continue
    return out


def load_strategy_for_pair(pair: str, bot: str | None = None) -> dict:
    """Load and validate the strategy file for ``pair``.

    Prefers the volume copy at ``{state_root}/{bot}/state/strategies/``.
    If missing, seeds from ``bots/{bot}/state/strategies/`` then loads.
    Falls back to reading the seed directly if the volume is not writable.

    Validates before returning so an out-of-range or wrong-strategy_type file can
    never reach the engine.
    """
    if bot is None:
        bot = _discover_bot_for_pair(pair)
    if bot is None:
        raise ValidationError(f"no bot config declares pair {pair!r}")
    try:
        path = ensure_strategy_seeded(pair, bot)
    except OSError:
        path = seed_strategy_path(pair, bot)
    strategy = _read_yaml(path)
    if "pair" not in strategy:
        strategy = {**strategy, "pair": pair}
    if "version" not in strategy:
        strategy = {**strategy, "version": "00"}
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
    """Provision empty per-pair state dirs + strategy seeds on first boot.

    Discipline 3.1: re-seeding is reserved for *empty* volumes — it must never
    silently overwrite existing production state. Creates directories if absent
    and copies missing strategy YAMLs from the image seeds.
    """
    for bot in ("forex", "gold", "crypto"):
        cfg_path = repo_root() / "bots" / bot / "config.yaml"
        if not cfg_path.exists():
            continue
        try:
            cfg = _read_yaml(cfg_path)
        except ValidationError:
            continue
        # Legacy local scaffold under repo/data/ (unchanged).
        data_root = repo_root() / "data"
        for pair in cfg.get("pairs") or []:
            pair_dir = data_root / bot / pair.replace("/", "_")
            pair_dir.mkdir(parents=True, exist_ok=True)
        # Volume strategies (HERMES_STATE_ROOT or repo/{bot}/state/strategies).
        ensure_bot_strategies_seeded(bot)
