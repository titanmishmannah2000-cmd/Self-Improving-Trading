import json
import urllib.request
from pathlib import Path

path = Path(r"C:\Users\titan\.cursor\projects\d-Self-Improving-Trading\agent-tools\5940eadc-a5f0-41f9-bf3b-02001c97f6c8.txt")
out_path = Path(r"D:\Self Improving Trading\_pulse_analysis_out.txt")
lines = []

def p(s=""):
    lines.append(str(s))
    print(s)

p(f"File size: {path.stat().st_size:,} bytes")
with path.open("r", encoding="utf-8") as f:
    data = json.load(f)

result = data.get("result", data) if isinstance(data, dict) else data
if isinstance(result, dict) and "result" in result and "bots" not in result:
    result = result["result"]

p("=== LOCAL FILE ===")
p(f"Top-level keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")

has_totals = isinstance(result, dict) and "totals" in result
p(f"Has totals key: {has_totals}")
if has_totals:
    p(f"totals value: {json.dumps(result['totals'], indent=2)[:2000]}")

bots = result.get("bots", {}) if isinstance(result, dict) else {}
p(f"Bots keys: {list(bots.keys()) if isinstance(bots, dict) else type(bots)}")

all_closed_ids = set()
old_pulse_sum = 0

if isinstance(bots, dict):
    for bot_name, bot in bots.items():
        if not isinstance(bot, dict):
            p(f"[{bot_name}] not a dict: {type(bot)}")
            continue
        closed = bot.get("closed_trades")
        open_count = bot.get("open_count")
        recent = bot.get("recent_trades") or []
        if not isinstance(recent, list):
            recent = []
        with_exit = [t for t in recent if isinstance(t, dict) and t.get("exit_reason")]
        ids = []
        for t in with_exit:
            tid = t.get("id") or t.get("trade_id") or t.get("ticket")
            if tid is not None:
                ids.append(tid)
                all_closed_ids.add(str(tid))
        unique_ids = len(set(str(i) for i in ids))
        old_pulse_sum += len(with_exit)
        p(f"[{bot_name}]")
        p(f"  closed_trades: {closed}")
        p(f"  open_count: {open_count}")
        p(f"  len(recent_trades): {len(recent)}")
        p(f"  recent_trades with exit_reason: {len(with_exit)}")
        p(f"  unique ids among those with exit_reason: {unique_ids}")

p("=== Top-level forex/gold/crypto trades ===")
for key in ("forex", "gold", "crypto"):
    src = None
    label = key
    if isinstance(result, dict) and key in result:
        src = result[key]
    elif isinstance(data, dict) and key in data:
        src = data[key]
        label = f"(data) {key}"
    if src is None:
        p(f"{key}: not present")
    elif isinstance(src, dict) and "trades" in src:
        p(f"{label}.trades: {src['trades']}")
    elif isinstance(src, dict):
        p(f"{label} keys: {list(src.keys())[:20]}; trades: {src.get('trades', '<missing>')}")
    else:
        p(f"{label}: {type(src).__name__}")

p("=== Aggregates (old pulse method) ===")
p(f"Sum of closed with exit_reason across bots (recent_trades): {old_pulse_sum}")
p(f"Deduped unique closed ids across all bots: {len(all_closed_ids)}")

p("=== LIVE API /api/lifetime-summary ===")
url = "https://hermes-dashboard-api-production.up.railway.app/api/lifetime-summary"
req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=60) as resp:
    api = json.loads(resp.read().decode("utf-8"))

api_result = api.get("result", api) if isinstance(api, dict) else api
api_bots = {}
if isinstance(api_result, dict):
    p(f"API result keys: {list(api_result.keys())[:30]}")
    if "totals" in api_result:
        p(f"API totals: {json.dumps(api_result['totals'])[:500]}")
    if "bots" in api_result and isinstance(api_result["bots"], dict):
        api_bots = api_result["bots"]
    else:
        api_bots = {k: v for k, v in api_result.items() if isinstance(v, dict) and "closed_trades" in v}

sum_closed = 0
for bot_name, bot in api_bots.items():
    if isinstance(bot, dict) and "closed_trades" in bot:
        ct = bot.get("closed_trades")
        p(f"  [{bot_name}] closed_trades: {ct}")
        try:
            sum_closed += int(ct or 0)
        except Exception:
            pass
p(f"Sum of closed_trades across bots: {sum_closed}")

out_path.write_text("\n".join(lines), encoding="utf-8")
print(f"Wrote {out_path}")
