"""data_collector.py — gathers file content, logs, and deploy state per audit domain.

Each `collect_*` function returns a dict with:
  - domain: str
  - files: dict[str, str]  — {relative_path: file_content}
  - logs: dict[str, str]   — {source: log_content}
  - deploy_state: dict     — Railway status info
  - strategy_spec: str     — intended strategy spec (if applicable)
  - baselines: dict        — known-good baselines (if applicable)

Paths are resolved from PROJECTS_ROOT.
"""

import csv
import json
import os
import subprocess
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Optional

# On Railway, there are no D:/projects/ paths. Fall back to bundled copies or current dir.
PROJECTS_ROOT = Path("D:/projects")
RAILWAY_BUNDLE = Path(__file__).parent.parent / "bot_code"

if not PROJECTS_ROOT.exists():
    # Running on Railway — source code not available
    PROJECTS_ROOT = RAILWAY_BUNDLE

BOT_PATHS = {}
for bot_name in ["forex", "gold", "dashboard_api"]:
    p = PROJECTS_ROOT / f"hermes-{bot_name}"
    if p.exists():
        BOT_PATHS[bot_name] = p

# Core files to inspect per bot
CORE_FILES = [
    "loop.py",
    "reflect.py",
    "run.py",
    "backtest.py",
    "gp_intelligence.py",
    "chart_vision.py",
    "genetic_discovery.py",
    "crisis_learning.py",
]

STATE_DIRS = {
    "forex": PROJECTS_ROOT / "hermes-forex" / "state",
    "gold": PROJECTS_ROOT / "hermes-gold" / "state",
}

DASHBOARD_API_URL = os.environ.get(
    "DASHBOARD_API_URL",
    "https://hermes-dashboard-api-production.up.railway.app",
)


def _read_file(path: Path, max_chars: int = 50000) -> Optional[str]:
    """Read a file up to max_chars. Returns None if not found or error."""
    try:
        if not path.exists():
            return None
        content = path.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n... [TRUNCATED at {max_chars} chars]"
        return content
    except Exception as e:
        return f"[ERROR reading {path.name}: {e}]"


def _read_state_files(bot_name: str) -> dict[str, str]:
    """Read all YAML state files for a bot."""
    state_dir = STATE_DIRS.get(bot_name)
    if not state_dir or not state_dir.exists():
        return {"error": f"No state dir for {bot_name}"}
    result = {}
    if (state_dir / "strategies").exists():
        for yaml_file in sorted((state_dir / "strategies").glob("*.yaml")):
            result[f"strategies/{yaml_file.name}"] = _read_file(yaml_file) or ""
    # Goal files
    for goal_file in sorted(state_dir.glob("goal*")) if state_dir.exists() else []:
        result[f"goal/{goal_file.name}"] = _read_file(goal_file) or ""
    # Trades log (last 100 lines)
    trades_file = state_dir / "trades.jsonl"
    if trades_file.exists():
        try:
            lines = trades_file.read_text(encoding="utf-8").splitlines()
            last_100 = lines[-100:]
            result["trades.jsonl (last 100)"] = "\n".join(last_100)
        except Exception as e:
            result["trades.jsonl"] = f"[ERROR: {e}]"
    return result


def _run_cmd(cmd: list[str], timeout: int = 15, cwd: Optional[str] = None) -> str:
    """Run a shell command and return stdout + stderr."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        out = r.stdout or ""
        if r.stderr:
            out += f"\n[STDERR]\n{r.stderr[:2000]}"
        return out[:3000]
    except subprocess.TimeoutExpired:
        return "[TIMEOUT]"
    except FileNotFoundError:
        return "[COMMAND NOT FOUND]"
    except Exception as e:
        return f"[ERROR: {e}]"


def collect_static_code(include_all_core: bool = True) -> dict:
    """Collect all .py files for static-code analysis."""
    files = {}
    for bot_name, bot_path in BOT_PATHS.items():
        if bot_name == "dashboard_api":
            continue  # handled separately to avoid huge context
        pkg = "hermes_trading" if bot_name == "gold" else "hermes_forex"
        src_dir = bot_path / pkg
        for fname in (CORE_FILES if include_all_core else ["loop.py", "reflect.py"]):
            content = _read_file(src_dir / fname)
            if content:
                files[f"{bot_name}/{pkg}/{fname}"] = content

    # Dashboard API main.py
    api_main = _read_file(BOT_PATHS["dashboard_api"] / "main.py")
    if api_main:
        files["dashboard-api/main.py"] = api_main

    return {
        "domain": "static-code",
        "files": files,
        "logs": {},
        "deploy_state": _get_deploy_state(),
        "strategy_spec": "N/A",
        "baselines": {},
    }


def collect_strategy_logic() -> dict:
    """Collect loop.py + state/strategies/*.yaml for strategy-logic analysis."""
    files = {}
    for bot_name in ["forex", "gold"]:
        pkg = "hermes_trading" if bot_name == "gold" else "hermes_forex"
        src_dir = BOT_PATHS[bot_name] / pkg
        content = _read_file(src_dir / "loop.py")
        if content:
            files[f"{bot_name}/{pkg}/loop.py"] = content
        # State files
        state_files = _read_state_files(bot_name)
        for k, v in state_files.items():
            files[f"{bot_name}/{k}"] = v

    return {
        "domain": "strategy-logic",
        "files": files,
        "logs": {},
        "deploy_state": _get_deploy_state(),
        "strategy_spec": _get_strategy_spec(),
        "baselines": {},
    }


def collect_data_logging_integrity() -> dict:
    """Collect push_to_dashboard code, trade logs, dashboard overview."""
    files = {}
    logs = {}

    for bot_name in ["forex", "gold"]:
        pkg = "hermes_trading" if bot_name == "gold" else "hermes_forex"
        src_dir = BOT_PATHS[bot_name] / pkg
        content = _read_file(src_dir / "loop.py")
        if content:
            files[f"{bot_name}/{pkg}/loop.py"] = content
        # Trades log
        state_files = _read_state_files(bot_name)
        for k, v in state_files.items():
            if "trades" in k:
                logs[f"{bot_name}/{k}"] = v
        # Heartbeat
        hb = src_dir.parent / "state" / "heartbeat"
        if hb.exists():
            logs[f"{bot_name}/heartbeat"] = _read_file(hb) or ""

    # Dashboard API overview
    try:
        import httpx
        r = httpx.get(f"{DASHBOARD_API_URL}/api/overview", timeout=10)
        if r.status_code == 200:
            logs["dashboard/overview"] = json.dumps(r.json(), indent=2)
    except Exception as e:
        logs["dashboard/overview"] = f"[ERROR fetching overview: {e}]"

    return {
        "domain": "data-logging-integrity",
        "files": files,
        "logs": logs,
        "deploy_state": _get_deploy_state(),
        "strategy_spec": "N/A",
        "baselines": {},
    }


def collect_risk_safety() -> dict:
    """Collect stop loss, profit target, breakeven, correlation, volatility code."""
    files = {}
    for bot_name in ["forex", "gold"]:
        pkg = "hermes_trading" if bot_name == "gold" else "hermes_forex"
        src_dir = BOT_PATHS[bot_name] / pkg
        for fname in ["loop.py", "reflect.py"]:
            content = _read_file(src_dir / fname)
            if content:
                files[f"{bot_name}/{pkg}/{fname}"] = content
        # YAML state (current stop/target values)
        state_files = _read_state_files(bot_name)
        for k, v in state_files.items():
            if "strategies" in k or "trades" in k:
                files[f"{bot_name}/{k}"] = v

    return {
        "domain": "risk-safety-boundaries",
        "files": files,
        "logs": {},
        "deploy_state": _get_deploy_state(),
        "strategy_spec": "N/A",
        "baselines": _get_risk_baselines(),
    }


def collect_performance_drift() -> dict:
    """Collect recent trades, lifetime-summary, backtest results."""
    files = {}
    logs = {}

    # Lifetime summary from dashboard
    try:
        import httpx
        r = httpx.get(f"{DASHBOARD_API_URL}/api/lifetime-summary", timeout=10)
        if r.status_code == 200:
            logs["dashboard/lifetime-summary"] = json.dumps(r.json(), indent=2)
        r2 = httpx.get(f"{DASHBOARD_API_URL}/api/overview", timeout=10)
        if r2.status_code == 200:
            logs["dashboard/overview"] = json.dumps(r2.json(), indent=2)
    except Exception as e:
        logs["dashboard"] = f"[ERROR fetching dashboard data: {e}]"

    # Backtest results
    for bot_name in ["forex", "gold"]:
        pkg = "hermes_trading" if bot_name == "gold" else "hermes_forex"
        bt_path = BOT_PATHS[bot_name] / pkg / "backtest.py"
        bt_content = _read_file(bt_path)
        if bt_content:
            files[f"{bot_name}/{pkg}/backtest.py"] = bt_content
        # Any backtest output files
        for f in (BOT_PATHS[bot_name] / "state").glob("*backtest*"):
            logs[f"{bot_name}/{f.name}"] = _read_file(f) or ""
        for f in (BOT_PATHS[bot_name] / "state").glob("*crisis*"):
            logs[f"{bot_name}/{f.name}"] = _read_file(f) or ""

    return {
        "domain": "performance-drift",
        "files": files,
        "logs": logs,
        "deploy_state": _get_deploy_state(),
        "strategy_spec": "N/A",
        "baselines": _get_performance_baselines(),
    }


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_deploy_state() -> dict:
    """Check Railway deploy status for both projects."""
    state = {}
    for bot_name, path in [("forex", BOT_PATHS["forex"]), ("gold", BOT_PATHS["gold"])]:
        try:
            out = _run_cmd(["railway", "status"], cwd=path, timeout=10)
            state[bot_name] = out
        except Exception as e:
            state[bot_name] = f"[ERROR: {e}]"
    return state


def _get_strategy_spec() -> str:
    """Read any strategy spec / goal files."""
    parts = []
    for bot_name in ["forex", "gold"]:
        state_dir = STATE_DIRS.get(bot_name)
        if not state_dir:
            continue
        for f in sorted(state_dir.glob("goal*")):
            content = _read_file(f)
            if content:
                parts.append(f"--- {bot_name}/{f.name} ---\n{content}")
    return "\n\n".join(parts) if parts else "N/A (no strategy spec files found)"


def _get_risk_baselines() -> dict:
    """Read current stop_loss_pct and profit_target_pct from strategy YAMLs."""
    baselines = {}
    for bot_name in ["forex", "gold"]:
        strategies_dir = STATE_DIRS.get(bot_name, "") / "strategies"
        if not strategies_dir or not strategies_dir.exists():
            continue
        for yaml_file in sorted(strategies_dir.glob("*.yaml")):
            try:
                content = yaml_file.read_text(encoding="utf-8")
                # Simple parse for key fields
                for line in content.splitlines():
                    line = line.strip()
                    for key in ["stop_loss_pct", "profit_target_pct", "max_risk_pct",
                                "min_trades", "max_drawdown_pct"]:
                        if line.startswith(key + ":") or line.startswith(key + ":"):
                            val = line.split(":", 1)[1].strip()
                            pair = yaml_file.stem.replace("_", "/")
                            baselines.setdefault(bot_name, {})
                            baselines[bot_name].setdefault(pair, {})
                            baselines[bot_name][pair][key] = val
            except Exception:
                pass
    return baselines


def _get_performance_baselines() -> dict:
    """Return known-good baselines for performance comparison."""
    return {
        "expected_win_rate_pct": "55-65",
        "expected_monthly_pnl_pct": "3-8",
        "expected_avg_trade_duration_hours": "2-48",
        "expected_max_drawdown_pct": "<15",
        "expected_sharpe_ratio": ">1.5",
        "note": "These are approximate baselines. The LLM should judge based on actual data.",
    }


# ── Domain Dispatch ────────────────────────────────────────────────────────

COLLECTORS = {
    "static-code": collect_static_code,
    "strategy-logic": collect_strategy_logic,
    "data-logging-integrity": collect_data_logging_integrity,
    "risk-safety-boundaries": collect_risk_safety,
    "performance-drift": collect_performance_drift,
}

ALL_DOMAINS = list(COLLECTORS.keys())


def collect_domain(domain: str) -> dict:
    """Collect data for a single domain. Raises ValueError if unknown."""
    fn = COLLECTORS.get(domain)
    if not fn:
        raise ValueError(f"Unknown domain: {domain}. Valid: {ALL_DOMAINS}")
    return fn()
