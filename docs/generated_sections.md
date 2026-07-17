# Section 3 — Guard list (auto-generated from [GUARD L##] tags)

- **L01** — referenced at hermes_core\adapters\price.py:24, hermes_core\adapters\price.py:28, hermes_core\adapters\price.py:69, hermes_core\adapters\price.py:94, hermes_core\adapters\price.py:108
- **L04** — referenced at hermes_core\engines\entry.py:51, hermes_core\engines\entry.py:97
- **L13** — referenced at hermes_core\engines\entry.py:116
- **L14** — referenced at hermes_core\engines\chart_vision.py:71, hermes_core\engines\entry.py:89
- **L15** — referenced at hermes_core\engines\entry.py:58, hermes_core\engines\entry.py:101
- **L16** — referenced at hermes_core\engines\chart_vision.py:88, hermes_core\engines\entry.py:93
- **L18** — referenced at hermes_core\engines\entry.py:129
- **L21** — referenced at hermes_core\engines\crisis_learning.py:35, hermes_core\engines\crisis_learning.py:38, hermes_core\engines\crisis_learning.py:39, hermes_core\engines\crisis_learning.py:40, hermes_core\engines\crisis_learning.py:201, hermes_core\engines\crisis_learning.py:259
- **L23** — referenced at hermes_core\engines\entry.py:58, hermes_core\engines\entry.py:101
- **L24** — referenced at hermes_core\engines\exit.py:19, hermes_core\engines\exit.py:28, hermes_core\engines\exit.py:52, hermes_core\engines\loop.py:40, hermes_core\engines\loop.py:300, hermes_core\engines\loop.py:307
- **L26** — referenced at hermes_core\engines\exit.py:16, hermes_core\engines\exit.py:94
- **L27** — referenced at hermes_core\engines\exit.py:14, hermes_core\engines\exit.py:86
- **L29** — referenced at hermes_core\engines\gp_intelligence.py:12, hermes_core\engines\gp_intelligence.py:32
- **L35** — referenced at hermes_core\engines\policy_engine.py:10, hermes_core\engines\policy_engine.py:25, hermes_core\engines\policy_engine.py:78
- **L36** — referenced at hermes_core\engines\decision_cortex.py:24
- **L40** — referenced at hermes_core\engines\risk.py:68
- **L45** — referenced at hermes_core\engines\reflect.py:34
- **L53** — referenced at hermes_core\engines\backtest.py:39, hermes_core\engines\backtest.py:42, hermes_core\engines\backtest.py:131, hermes_core\engines\genetic.py:30, hermes_core\engines\reflect.py:181, hermes_core\engines\reflect.py:183

# Appendix H — Dashboard SQLite DDL (generated from dashboard/backend/main.py)

```sql
""
CREATE TABLE IF NOT EXISTS trades (
    id TEXT NOT NULL, bot TEXT NOT NULL, pair TEXT, entry_price REAL,
    exit_price REAL, entry_ts TEXT, exit_ts TEXT, pnl_pct REAL, exit_reason TEXT,
    hold_cycles INTEGER, entry_rsi REAL, entry_regime TEXT, entry_quality_score REAL,
    strategy_type TEXT, chart_context TEXT, raw_json TEXT,
    PRIMARY KEY (bot, id)
);
CREATE TABLE IF NOT EXISTS hypotheses (
    bot TEXT NOT NULL, ts TEXT NOT NULL, pair TEXT, version_from TEXT, version_to TEXT,
    variable TEXT, old_value TEXT, new_value TEXT, reasoning TEXT, mode TEXT, raw_json TEXT,
    PRIMARY KEY (bot, ts, variable)
);
CREATE TABLE IF NOT EXISTS skips (
    bot TEXT NOT NULL, ts TEXT NOT NULL, pair TEXT, reason_skipped TEXT, rsi_at_skip REAL,
    price_at_skip REAL, missed_pnl REAL, raw_json TEXT, PRIMARY KEY (bot, ts, pair)
);
CREATE TABLE IF NOT EXISTS latest_state (
    bot TEXT PRIMARY KEY, strategy_json TEXT, goal_json TEXT, heartbeat_json TEXT,
    open_trades_json TEXT DEFAULT '[]', received_at TEXT
);
CREATE TABLE IF NOT EXISTS dismissed_alerts (
    alert_key TEXT PRIMARY KEY, dismissed_at TEXT
);
CREATE TABLE IF NOT EXISTS bot_status (
    bot TEXT PRIMARY KEY, desired_state TEXT NOT NULL DEFAULT 'running', updated_at TEXT
);
CREATE TABLE IF NOT EXISTS auth_tokens (
    token TEXT PRIMARY KEY, created_at TEXT, expires_at TEXT
);
CREATE TABLE IF NOT EXISTS app_config (
    key TEXT PRIMARY KEY, value TEXT
);
```