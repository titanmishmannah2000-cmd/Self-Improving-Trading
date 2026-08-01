#!/usr/bin/env python3
"""Provision Railway project hermes-btc-usdt (crypto bot + BTC-scoped dashboard).

Requires Railway CLI 5.x, logged in. Does not print secret values.

Usage:
    uv run python tools/setup_railway_btc_usdt.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = "titanmishmannah2000-cmd/Self-Improving-Trading"
BRANCH = "BTC/USDT"
PROJECT_NAME = "hermes-btc-usdt"
LEGACY_PROJECT_ID = "026694c2-7d92-43a0-96fe-6d90f57bae77"

# Non-secret defaults applied to both / each service. Secrets are copied from
# the legacy crypto/dashboard services when available.
CRYPTO_DEFAULTS = {
    "HERMES_BOT_NAME": "crypto",
    "HERMES_STATE_ROOT": "/data",
    "HERMES_STATE": "/data",
    "PRICE_BACKEND": "aggregate",
    "BOOK_RISK": "1",
    "SOFT_WEIGHTS": "0",
    "KELLY_SIZING": "0",
    "REGIME_SIZING": "0",
    "ENTRY_RANKING": "0",
    "EXIT_INTEL": "0",
    "PROBE_SIZING": "0",
    "SKIP_SHADOW_REFLECT": "0",
    "SKIP_SHADOW_PROMOTE": "0",
    "CRISIS_RECOMMEND": "0",
    "GP_PROMOTE": "0",
    "REFLECT_AUTO_DEPLOY": "0",
    "REFLECT_DEPLOY_STAGE": "prove",
    "MICRO_LIVE": "0",
    "MICRO_LIVE_SIZE_MULT": "0.25",
    "REGIME_DECAY": "0",
    "SCORECARD_COST_PCT": "0.05",
    "GP_PROMOTE_COST_PCT": "0.05",
    "GP_EXCLUDE_PAIRS": "GBP/JPY",
    "COST_STRESS_MULT": "2.0",
}

DASHBOARD_DEFAULTS = {
    "HERMES_BOT_NAME": "dashboard",
    "HERMES_STATE_ROOT": "/data",
    "HERMES_STATE": "/data",
    "DASHBOARD_BOTS": "crypto",
    "DASHBOARD_TITLE": "Hermes BTC/USDT",
    "DASHBOARD_DB": "/data/dashboard.db",
}

# Keys to copy from legacy services (values never printed).
COPY_FROM_LEGACY_CRYPTO = (
    "INGEST_TOKEN",
    "GEMINI_API_KEY",
    "GROQ_API_KEY",
    "DEEPSEEK_API_KEY",
    "DISCORD_ALERTS_WEBHOOK",
    "DISCORD_REFLECTIONS_WEBHOOK",
    "DISCORD_DAILY_WEBHOOK",
    "ALPHA_VANTAGE_KEY",
    "METALS_API_KEY",
    "FMP_KEY",
    "BTC_MAKER_FEE_PCT",
    "BTC_TAKER_FEE_PCT",
    "BTC_SLIPPAGE_FLOOR_BPS",
    "BTC_SLIPPAGE_ATR_K",
)
COPY_FROM_LEGACY_DASHBOARD = ("INGEST_TOKEN",)


def railway_bin() -> str:
    for name in ("railway.cmd", "railway.exe", "railway"):
        found = shutil.which(name)
        if found:
            return found
    npm = Path.home() / "AppData" / "Roaming" / "npm" / "railway.cmd"
    if npm.is_file():
        return str(npm)
    return "railway"


def run(
    bin_path: str,
    args: list[str],
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = [bin_path, *args]
    proc = subprocess.run(cmd, check=False, capture_output=capture, text=True)
    if check and proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise SystemExit(f"FAIL: railway {' '.join(args)}\n{err}")
    return proc


def vars_json(
    bin_path: str,
    *,
    project: str,
    service: str,
    environment: str = "production",
) -> dict[str, str]:
    proc = run(
        bin_path,
        [
            "variable",
            "list",
            "--service",
            service,
            "--project",
            project,
            "--environment",
            environment,
            "--json",
        ],
        check=False,
    )
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items()}
    return {}


def set_vars(
    bin_path: str,
    *,
    project: str,
    service: str,
    mapping: dict[str, str],
    environment: str = "production",
) -> None:
    for key, value in mapping.items():
        if value is None or value == "":
            continue
        run(
            bin_path,
            [
                "variable",
                "set",
                f"{key}={value}",
                "--service",
                service,
                "--project",
                project,
                "--environment",
                environment,
                "--skip-deploys",
            ],
        )
        print(f"  set {service}.{key}")


def main() -> int:
    bin_path = railway_bin()
    print(f"railway={bin_path}")
    print(f"legacy_project={LEGACY_PROJECT_ID}")

    legacy_crypto = vars_json(bin_path, project=LEGACY_PROJECT_ID, service="crypto")
    legacy_dash = vars_json(bin_path, project=LEGACY_PROJECT_ID, service="dashboard")
    print(
        f"copied keys available: crypto={len(legacy_crypto)} dashboard={len(legacy_dash)}"
    )

    # Create + link new project (replaces local .railway link).
    init = run(bin_path, ["init", "--name", PROJECT_NAME, "--json"], check=False)
    if init.returncode != 0:
        # Maybe already exists — try link by listing.
        print("init failed; attempting to continue if project already linked…")
        print((init.stderr or init.stdout or "").strip())
    else:
        print((init.stdout or "").strip() or "project created")

    status = run(bin_path, ["status", "--json"], check=False)
    project_id = ""
    if status.returncode == 0:
        try:
            st = json.loads(status.stdout or "{}")
            project_id = str(st.get("id") or st.get("projectId") or "")
            # CLI shapes vary; also try nested.
            if not project_id and isinstance(st.get("project"), dict):
                project_id = str(st["project"].get("id") or "")
        except json.JSONDecodeError:
            pass
    if not project_id:
        # Fallback: parse `railway status` text is painful; use linked project via whoami-less status.
        text = run(bin_path, ["status"], capture=True, check=False)
        for line in (text.stdout or "").splitlines():
            if "Project ID:" in line:
                project_id = line.split(":", 1)[1].strip()
                break
    if not project_id:
        raise SystemExit("could not resolve new project id — run `railway status`")

    print(f"project_id={project_id}")

    for svc in ("crypto", "dashboard"):
        add = run(
            bin_path,
            ["add", "--service", svc, "--json"],
            check=False,
        )
        print(f"add {svc}: rc={add.returncode}")

    for svc in ("crypto", "dashboard"):
        run(
            bin_path,
            [
                "service",
                "source",
                "connect",
                "--repo",
                REPO,
                "--branch",
                BRANCH,
                "--service",
                svc,
                "--project",
                project_id,
            ],
            check=False,
        )
        print(f"source {svc} -> {REPO}@{BRANCH}")

    for svc in ("crypto", "dashboard"):
        run(bin_path, ["service", "link", svc], check=False)
        vol = run(
            bin_path,
            ["volume", "add", "--mount-path", "/data", "--json"],
            check=False,
        )
        print(f"volume {svc}: rc={vol.returncode}")

    # Resolve linked project environment for --project/--environment flags.
    env_name = "production"

    crypto_vars = dict(CRYPTO_DEFAULTS)
    for key in COPY_FROM_LEGACY_CRYPTO:
        if key in legacy_crypto and legacy_crypto[key]:
            crypto_vars[key] = legacy_crypto[key]
    if "INGEST_TOKEN" not in crypto_vars or not crypto_vars["INGEST_TOKEN"]:
        crypto_vars["INGEST_TOKEN"] = "change-me-btc-usdt-ingest"

    dash_vars = dict(DASHBOARD_DEFAULTS)
    for key in COPY_FROM_LEGACY_DASHBOARD:
        if key in legacy_dash and legacy_dash[key]:
            dash_vars[key] = legacy_dash[key]
    # Keep ingest token identical across the two new services.
    dash_vars["INGEST_TOKEN"] = crypto_vars["INGEST_TOKEN"]

    print("setting crypto vars…")
    set_vars(bin_path, project=project_id, service="crypto", mapping=crypto_vars)
    print("setting dashboard vars…")
    set_vars(bin_path, project=project_id, service="dashboard", mapping=dash_vars)

    domain = run(
        bin_path,
        ["domain", "--service", "dashboard", "--project", project_id, "--json"],
        check=False,
    )
    dash_url = ""
    if domain.returncode == 0:
        try:
            d = json.loads(domain.stdout or "{}")
            dash_url = str(d.get("domain") or d.get("url") or "")
        except json.JSONDecodeError:
            dash_url = (domain.stdout or "").strip()
    else:
        print((domain.stderr or domain.stdout or "").strip())

    if dash_url and not dash_url.startswith("http"):
        dash_url = f"https://{dash_url}"
    if dash_url:
        print(f"dashboard_url={dash_url}")
        set_vars(
            bin_path,
            project=project_id,
            service="crypto",
            mapping={"DASHBOARD_API_URL": dash_url},
        )
    else:
        print(
            "WARN: no dashboard domain yet — set DASHBOARD_API_URL on crypto after "
            "`railway domain --service dashboard`"
        )

    print("\nDone.")
    print(f"  Project: {PROJECT_NAME} ({project_id})")
    print("  Services: crypto, dashboard")
    print("  Open: railway open")
    print(
        f"  Re-link legacy multi-bot project anytime:\n"
        f"    railway link --project {LEGACY_PROJECT_ID}"
    )
    print(
        "  Trigger deploy from Railway UI or:\n"
        "    railway up --service crypto\n"
        "    railway up --service dashboard"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
