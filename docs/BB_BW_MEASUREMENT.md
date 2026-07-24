# BB bandwidth floor — measure then tune (#16)

Live guard: `BB_BW_MIN = 0.0003` in `hermes_core/engines/guards.py`.

## Procedure

1. Run bots on **real** prices for 24–48h with soak sessions `24h`.
2. Collect `bb_bandwidth:*` skip reasons and/or call `bb_bandwidth_samples()` from a debug shell after cycles.
3. Only lower/raise `BB_BW_MIN` if the distribution shows the floor is blocking healthy FX MR (typical live tick bw ~0.0004–0.0006).
4. Log the decision here:

| Date | Sample n | p10 | p50 | p90 | Decision |
|------|----------|-----|-----|-----|----------|
| _pending soak_ | | | | | keep 0.0003 until measured |

Do **not** blind-loosen the floor.
