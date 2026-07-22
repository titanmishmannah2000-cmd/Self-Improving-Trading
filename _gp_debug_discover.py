"""Debug: time GP evolve + admission gates for one pair."""
from __future__ import annotations

import os
import time

pair = os.environ.get("PAIR", "XAU/USD")
os.environ.setdefault("HERMES_STATE_ROOT", "/data")

from hermes_core.adapters.price import seed_history_interval_sync
from hermes_core.engines import genetic as g

h = seed_history_interval_sync(pair, interval="1d", period="2y", max_candles=500)
s = [c["price"] for c in (h or [])]
print("candles", len(s), "pair", pair)
t0 = time.time()
inds = g.discover(pair, s, horizon=60, generations=20, pop_size=30)
print("admitted", len(inds), "secs", round(time.time() - t0, 1))
for i in inds[:5]:
    print(" ", i.get("expr"), "approved=", i.get("backtest_approved"), "oos=", i.get("oos_corr"))
