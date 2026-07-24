# Cortex 30-day soak checklist

Track every inventory item from the Cortex audit. Status: done / ops-pending.

## P0 — must be green before soak clock

| # | Item | Status | Where |
|---|------|--------|-------|
| 1 | Atomic writes (temp + replace) for memory/exile/policy | **done** | `hermes_core/state/atomic_json.py`, cortex + policy |
| 2 | Corrupt JSON quarantined (`*.corrupt-*`), not silent wipe | **done** | `load_json` + cortex loaders |
| 3 | Policy saved under cortex bot, not `current_bot()` alone | **done** | `policy_engine.evaluate` |
| 4 | Live feedback reloads same bot as cortex | **done** | `genetic.apply_live_feedback` |
| 4b | `run_bot` sets `HERMES_BOT_NAME` after resolve | **done** | `bots/_runner.py` |
| 5 | Seed stubs quarantined (exile/tracker/cortex policy) | **done** | `tools/rebuild_cortex.py` + archive |
| 6 | Self-audit cortex go/no-go checks | **done** | `self_audit._cortex_soak_checks` |

## P1 — correctness before / early soak

| # | Item | Status | Where |
|---|------|--------|-------|
| 7 | `record_outcome` fills open row (opens don’t grow forever) | **done** | `decision_cortex.record_outcome` |
| 8 | Partials tagged + excluded from WR/evidence; GP remap | **done** | cortex + `loop.py` partial path |
| 9 | Exile/reinstate on GP sub-block WR | **done** | `record_indicator_outcome` |
| 10 | Exile file = source of truth; sync on load | **done** | `_sync_exile_flags_from_file` |
| 11 | `best_entry_type(pair)` per-pair | **done** | `decision_cortex` |
| 12 | Rebuild memory from clean trades (#18) | **done** | `tools/rebuild_cortex.py` + `state_hygiene --rebuild-learning` |
| 13 | Recompute policy from clean cortex (#17) | **done** | same tools |
| 14 | Dead flags documented (priority_discovery ops-only) | **done** | summary `gates` text |
| 19 | Scrub SOP (quarantine seeds + rebuild + clear exile) | **done** | `tools/rebuild_cortex.py` / `state_hygiene.py` |

## P2 — hygiene

| # | Item | Status | Where |
|---|------|--------|-------|
| 15 | Memory retention cap | **done** | `MEMORY_MAX_ENTRIES` / `_trim_entries` |
| 16 | Orphan tracker not used by engine | **done** | stubs removed; audit flags if present |
| 17 | Soak tests | **done** | `tests/test_cortex_soak.py` (46 cortex-related passed) |
| 18 | Path docs fixed | **done** | cortex docstring + `COMPONENT_REGISTRY.md` |
| 20 | Probe fail-open documented | **done** | `risk.apply_probe_sizing` docstring |

## Operator steps

- [x] Per-service `HERMES_BOT_NAME` + `HERMES_STATE_ROOT=/data` + `PRICE_BACKEND=aggregate` confirmed on Railway (forex/gold/crypto)
- [x] Local rebuild: `tools/rebuild_cortex.py` + `tools/state_hygiene.py --rebuild-learning`
- [x] Local self-audit cortex checks green (`cortex_no_corrupt`, `cortex_exile_no_seed_stub`, `cortex_stub_policy_absent`, `cortex_stub_tracker_absent`, `policy_json_valid`)
- [x] Redeploy forex / gold / crypto / dashboard from source (`railway redeploy --from-source`)
- [x] `Dockerfile` copies `tools/` so volume ops scripts exist in the image
- [x] Start 30-day paper clock markers via `tools/start_soak_clock.py` → `{bot}/state/soak_started.json`
- [x] After image with `tools/` is live: SSH each service and run `uv run python tools/start_soak_clock.py <bot>` against `/data` (volume truth)
  - forex rebuilt **65** closes; gold **29**; crypto **33**; `soak_started.json` written on each volume (2026-07-24)

## Tests to re-run after further edits

```bash
uv run pytest tests/test_cortex.py tests/test_cortex_soak.py tests/test_soft_weights.py tests/test_probe_sizing.py tests/test_kelly_sizing.py -q
```
