"""Single source of truth for environment / secrets access (S19).

WHY: the user wants ONE place to set every key, and for local + Railway deploys
to read from that same file. This module is that place.

- `load_env()` calls python-dotenv's `load_dotenv()` (fail-soft: if the package
  or `.env` is missing, it silently no-ops — code falls back to real process
  env vars). Importing this module auto-loads once.
- `get_env(name, default="")` is the ONLY sanctioned way to read a secret/env
  var. New keys should be added here AND documented in `.env.example`.

Discipline: no raw `os.environ.get` for config/secrets elsewhere — funnel
through `get_env` so the contract is centralized and auditable. Secrets never
hardcoded; `.env` is git-ignored, `.env.example` (placeholders) is committed.
"""

from __future__ import annotations

import os

# Repo root is parent of hermes_core/ — explicit path so load_dotenv always
# finds THE .env (not a cwd-dependent search that can miss it).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_FILE = os.path.join(_REPO_ROOT, ".env")

_env_loaded = False


def load_env() -> None:
    """Load .env into os.environ (fail-soft). Idempotent. override=True so the
    file's values win over any stale process env (e.g. PRICE_BACKEND)."""
    global _env_loaded
    if _env_loaded:
        return
    _env_loaded = True
    try:
        from dotenv import load_dotenv  # lazy import so tests/CI don't require it

        load_dotenv(_ENV_FILE, override=True)  # explicit path + override
    except Exception:  # noqa: BLE001 — fail-soft; [GUARD L62]
        # No dotenv installed, or no .env file: fall back to process env.
        pass


def get_env(name: str, default: str = "") -> str:
    """Read an env var, loading .env first if not already done."""
    if not _env_loaded:
        load_env()
    return os.environ.get(name, default)


# Auto-load on import so any module importing this gets .env applied.
load_env()
