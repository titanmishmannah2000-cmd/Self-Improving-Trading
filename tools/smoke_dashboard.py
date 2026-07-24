"""Live socket smoke test for the dashboard API (real HTTP, not in-process).

Starts ThreadingHTTPServer on a real port, drives it with urllib, then
simulates a RESTART by relaunching on the SAME SQLite file and re-reading.
Run with: uv run python tools/smoke_dashboard.py
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import dashboard.backend.main as m

TMP = "/tmp/hermes_dash_smoke"
os.makedirs(TMP, exist_ok=True)
DB = os.path.join(TMP, "dash.db")
STATE = os.path.join(TMP, "state")
os.makedirs(STATE, exist_ok=True)
TOKEN = "live-token-123"

# redirect module-level config
m.DB_PATH = DB
m.STATE_DIR = STATE
m.INGEST_TOKEN = TOKEN

PORT = 8731
URL = f"http://127.0.0.1:{PORT}"


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), m.make_app())
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def req(method, path, body=None, token=None, raw=False):
    data = json.dumps(body).encode() if body is not None else None
    headers = {}
    if data:
        headers["Content-Length"] = str(len(data))
    if token is not None:
        headers["X-Ingest-Token"] = token
    r = urllib.request.Request(f"{URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r) as resp:
            txt = resp.read().decode()
            return resp.status, (txt if raw else json.loads(txt))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()) if not raw else e.read().decode()


def check(name, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {extra}")
    assert cond, f"{name} FAILED {extra}"


print("=== LIVE DASHBOARD SMOKE (real socket) ===")
srv = start()

# 1. ingest forex trade
st, _ = req("POST", "/api/ingest/forex", {"id": "X", "pair": "EUR/USD", "pnl_pct": 1.2}, TOKEN)
check("ingest forex 200", st == 200, f"({st})")

# 2. ingest gold SAME id X -> must NOT collide with forex X
st, _ = req("POST", "/api/ingest/gold", {"id": "X", "pair": "XAU/USD", "pnl_pct": -0.5}, TOKEN)
check("ingest gold 200", st == 200, f"({st})")

# 3. both X records coexist, distinct bot/pair
fx = req("GET", "/api/trades/forex")[1]
gd = req("GET", "/api/trades/gold")[1]
fxx = [t for t in fx if t["id"] == "X"]
gdx = [t for t in gd if t["id"] == "X"]
check("forex X present", len(fxx) == 1 and fxx[0]["pair"] == "EUR/USD")
check("gold X present & distinct", len(gdx) == 1 and gdx[0]["pair"] == "XAU/USD")

# 4. auth: missing token -> 401
st, _ = req("POST", "/api/ingest/forex", {"id": "Z"}, token=None)
check("no token -> 401", st == 401, f"({st})")

# 5. auth: wrong token -> 401
st, _ = req("POST", "/api/ingest/forex", {"id": "Z"}, token="wrong")
check("wrong token -> 401", st == 401, f"({st})")

# 6. unknown bot -> 404, never persisted
st, _ = req("POST", "/api/ingest/crypto", {"id": "Q"}, TOKEN)
check("unknown bot -> 404", st == 404, f"({st})")
st, body = req("GET", "/api/trades/crypto")
check("unknown bot read -> []", st == 200 and body == [], f"({st},{body})")

# 7. hypotheses + skips ingest + read
st, _ = req(
    "POST",
    "/api/ingest/forex",
    {
        "hypotheses": [
            {"ts": "t1", "variable": "stop_loss", "old_value": "0.5", "new_value": "0.6"}
        ],
        "skips": [{"ts": "s1", "pair": "GBP/USD", "reason_skipped": "rsi"}],
    },
    TOKEN,
)
check("forex hypotheses/skips ingest 200", st == 200, f"({st})")
hy = req("GET", "/api/hypotheses/forex")[1]
sk = req("GET", "/api/skips/forex")[1]
check("hypotheses read", any(h["variable"] == "stop_loss" for h in hy), f"({len(hy)})")
check("skips read", any(s["pair"] == "GBP/USD" for s in sk), f"({len(sk)})")

# 8. overview shows both bots
ov = req("GET", "/api/overview")[1]
check("overview has forex & gold", "forex" in ov and "gold" in ov)
check("overview forex trades>=1", ov["forex"]["trades"] >= 1, f"({ov['forex']})")
check("overview gold trades>=1", ov["gold"]["trades"] >= 1, f"({ov['gold']})")

# 9. empty tab -> explicit [] not 500
empty = req("GET", "/api/trades/gold_nope")
check("invalid bot read -> [] not 500", empty[0] == 200 and empty[1] == [], f"({empty})")

# 10. file-based tabs: drop a discovered file + heartbeat + cortex exile, read them
disc_dir = os.path.join(STATE, "discovered")
cortex_dir = os.path.join(STATE, "cortex")
os.makedirs(disc_dir, exist_ok=True)
os.makedirs(cortex_dir, exist_ok=True)
with open(os.path.join(disc_dir, "forex.json"), "w") as f:
    json.dump([{"name": "rsi_div", "complexity": 3}], f)
with open(os.path.join(cortex_dir, "indicator_exile.json"), "w") as f:
    json.dump({"slow_rsi": {"attempts": 9, "wr": 0.2}}, f)
with open(os.path.join(STATE, "heartbeat_forex.json"), "w") as f:
    json.dump({"ts": "now", "cycles": 42}, f)

disc = req("GET", "/api/discovered/forex")[1]
check("discovered tab reads file", any(d.get("name") == "rsi_div" for d in disc), f"({disc})")
ctx = req("GET", "/api/cortex/forex")[1]
check("cortex tab reads exile", "slow_rsi" in ctx["exiled"], f"({ctx})")
hb = req("GET", "/api/heartbeat/forex")[1]
check("heartbeat tab reads file", hb.get("cycles") == 42, f"({hb})")

# 11. PERSISTENCE ACROSS RESTART: stop server, relaunch on same DB, re-read
print("  -- simulating server restart on same DB --")
srv.shutdown()
srv.server_close()
srv2 = start()
fx2 = req("GET", "/api/trades/forex")[1]
gdx2 = [t for t in req("GET", "/api/trades/gold")[1] if t["id"] == "X"]
check("post-restart forex X still there", any(t["id"] == "X" for t in fx2))
check("post-restart gold X still there", len(gdx2) == 1 and gdx2[0]["pair"] == "XAU/USD")
srv2.shutdown()
srv2.server_close()

print("=== ALL LIVE SMOKE CHECKS PASSED ===")
