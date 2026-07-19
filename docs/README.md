# Hermes Self-Improving Trading — Project Documentation

This directory holds the **living** documentation for the Hermes trading
system (forex + gold + crypto paper-trading bots and their live dashboard).

It is the single source of truth for *what every component is, how it works,
what every parameter means, how each piece interacts, and a complete
change history*. Every engine, every module, every parameter, and every
modification is captured here so the system is fully auditable end-to-end.

## Contents

| File | What it contains |
|------|------------------|
| `CHANGELOG.md` | Full chronological change history, grouped into eras. Every commit anchored to its SHA. |
| `COMPONENT_REGISTRY.md` | Every engine/service + module, with file path, intelligence level, what it does, how it works, key functions, and what it interacts with. Plus the cross-component data-flow diagram. |
| `engines/GP_ENGINE.md` | **Deep-dive spec of the Genetic-Programming "GP Brain"** — discovery engine, signal ensemble, promotion path, dashboard surfacing, every parameter, and how it connects to the rest of the system. This is the most detailed single document. |
| `engines/` | One file per major engine as the system grows (traditional strategy engine, cortex, price adapters, dashboard backend, frontend, etc.). |

## How to maintain these docs

- **After any change:** add a `### YYYY-MM-DD — Title` entry to `CHANGELOG.md`
  (template at the bottom of that file) covering scope, what changed, why, and
  verification.
- **After adding/modifying an engine or module:** update the relevant
  `COMPONENT_REGISTRY.md` section and, if it is the GP brain, `engines/GP_ENGINE.md`.
- **Keep parameter tables exact** — copy values from the source, not memory.
- Set the `Last updated` line at the top of each file.

## System at a glance

Three paper-trading bots (`forex`, `gold`, `crypto`) run as one Railway
single-image service dispatched by `HERMES_BOT_NAME`. Each one:
1. Fetches live + historical price data (free, keyed APIs only — no paid feeds).
2. Runs the **traditional strategy engine** (mean-reversion / RSI-momentum).
3. Runs the **GP discovery engine** (genetic programming) on a daily regime;
   discovered indicators feed the **GP ensemble** (`entry_type="gp_ensemble"`).
4. Pushes full state (prices, regimes, open trades, discoveries, cortex) to the
   **dashboard backend** (SQLite) every cycle.
5. The **dashboard frontend** renders it live — including the teal **"GP Brain"**
   badge on any open position the GP ensemble produced.

Paper trading only. No real orders. Fail-soft everywhere.
