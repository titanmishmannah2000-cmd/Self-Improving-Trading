"""Dashboard backend API (Session 16 / Phase 16).

Stdlib-only HTTP server + SQLite. No third-party web framework (D1: keep the
dependency surface minimal). Testable in-process via make_app()/test_client().

Fixes the documented original-system bug: the PRIMARY KEY is composite
(bot, id) — NOT id alone. Two bots (forex, gold) may both report trade "X"
and they stay distinct; the old single-id key caused silent cross-bot
overwrite/contamination.

Guards:
  * INGEST_TOKEN auth on POST /api/ingest/{bot} (never logged, per 4.6) [L?]
  * unknown bot name -> 404, never persisted.
  * every state file has a read endpoint; (bot, id) PK prevents collisions.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import threading
from contextlib import suppress
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from hermes_core.env import get_env, load_env

# ── live-price real-time fan-out (SSE) ──────────────────────────────────────
# Thread-safe registry of SSE subscriber queues. Each connected browser tab
# (or other HTTP client) registers a queue.Queue here; _broadcast_price fans
# new price snapshots out to every subscriber. [GUARD L64]
_sse_subscribers: set[queue.Queue] = set()
_sse_lock = threading.Lock()
SSE_HEARTBEAT_S = 15.0  # keep-alive comment interval

# ── config ──────────────────────────────────────────────────────────────────
# Load .env so local + Railway read the same single source of truth (the bots
# call load_env() too; the dashboard must do the same or its INGEST_TOKEN /
# DASHBOARD_DB never get populated from .env locally). [GUARD L62]
load_env()
VALID_BOTS = {"forex", "gold", "crypto"}
INGEST_TOKEN = get_env("INGEST_TOKEN", "")   # empty -> rejects all ingests
DB_PATH = get_env("DASHBOARD_DB", "state/dashboard.db")
STATE_DIR = get_env("HERMES_STATE", "state")
# Built frontend (vite build -> dashboard/frontend/dist/). Served by the Python
# backend so Railway needs no separate web server / nginx. [GUARD L62]
_DIST_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
DIST_DIR = get_env("DASHBOARD_DIST", os.path.join(_DIST_MODULE_DIR, "..", "frontend", "dist"))

DDL = """
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
CREATE TABLE IF NOT EXISTS live_prices (
    bot TEXT NOT NULL, pair TEXT NOT NULL, price REAL NOT NULL, ts TEXT NOT NULL,
    PRIMARY KEY (bot, pair)
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
"""


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    # WAL lets the 3 bot POSTs + dashboard GET/SSE readers share the single
    # file without "database is locked" (default rollback journal serializes
    # writers). busy_timeout makes a contended writer WAIT instead of failing
    # immediately — essential because every bot cycle POSTs a price snapshot
    # concurrently. [GUARD L62]
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass  # read-only / in-memory client still works
    return conn


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
    return _configure(conn)


def init_db() -> None:
    conn = get_conn()
    conn.executescript(DDL)
    conn.commit()
    conn.close()


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


class DashboardHandler(BaseHTTPRequestHandler):
    # quiet the default stderr request logging
    def log_message(self, *args: Any) -> None:  # noqa: D102
        pass

    def handle(self) -> None:
        # A client that disconnects mid-request (browser tab closed, curl
        # --max-time, SSE drop) raises ConnectionResetError/BrokenPipeError at
        # the socket layer. That is benign — never log it as a server fault.
        # [GUARD L62]
        with suppress(ConnectionResetError, BrokenPipeError, OSError):
            super().handle()

    # ── helpers ────────────────────────────────────────────────────────────
    def _send(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        # write the raw HTTP response directly (works for both the real server
        # and the in-process test client, which never runs __init__)
        head = (
            f"HTTP/1.1 {code} OK\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
        self.wfile.write(head + body)

    def _serve_static(self, rel: str) -> None:
        """Serve a built frontend asset from DIST_DIR (fail-soft). [GUARD L62]."""
        # prevent path traversal: resolve and confirm within DIST_DIR
        base = os.path.abspath(DIST_DIR)
        target = os.path.abspath(os.path.join(base, rel))
        if not target.startswith(base + os.sep) and target != base:
            return self._send(404, {"error": "not found"})
        if os.path.isdir(target):
            target = os.path.join(target, "index.html")
        if not os.path.exists(target):
            # SPA fallback -> serve index.html so client routing works
            target = os.path.join(base, "index.html")
        if not os.path.exists(target):
            return self._send(404, {"error": "not found"})
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(os.path.splitext(target)[1], "application/octet-stream")
        try:
            with open(target, "rb") as fh:
                body = fh.read()
        except OSError:
            return self._send(404, {"error": "not found"})
        head = (
            f"HTTP/1.1 200 OK\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode()
        self.wfile.write(head + body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def _bot_from_path(self, prefix: str) -> str | None:
        # /api/ingest/{bot}  -> strip prefix, take first path segment
        rest = self.path[len(prefix):]
        seg = rest.split("/")[0].split("?")[0]
        return seg or None

    def _auth_ok(self) -> bool:
        if not INGEST_TOKEN:
            return False
        return self.headers.get("X-Ingest-Token") == INGEST_TOKEN

    # ── POST /api/ingest/{bot} ──────────────────────────────────────────────
    def do_POST(self) -> None:
        if not self.path.startswith("/api/ingest/") and not self.path.startswith("/api/price/"):
            self._send(404, {"error": "not found"})
            return
        if self.path.startswith("/api/ingest/"):
            self._post_ingest()
        else:
            self._post_price()

    def _post_ingest(self) -> None:
        bot = self._bot_from_path("/api/ingest/")
        if bot not in VALID_BOTS:
            self._send(404, {"error": f"unknown bot: {bot}"})   # [GUARD] reject unknown
            return
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"})          # INGEST_TOKEN required
            return
        data = self._read_json()
        self._ingest(bot, data)
        self._send(200, {"ok": True, "bot": bot, "id": data.get("id")})

    def _post_price(self) -> None:
        """POST /api/price/{bot}  { prices: {PAIR: float, ...} }  [L64]."""
        bot = self._bot_from_path("/api/price/")
        if bot not in VALID_BOTS:
            self._send(404, {"error": f"unknown bot: {bot}"})   # [GUARD] reject unknown
            return
        if not self._auth_ok():
            self._send(401, {"error": "unauthorized"})          # INGEST_TOKEN required
            return
        data = self._read_json()
        prices = data.get("prices") if isinstance(data, dict) else None
        if not isinstance(prices, dict):
            self._send(400, {"error": "expected {'prices': {PAIR: float}}"})
            return
        now = _now_iso()
        rows = []
        for pair, price in prices.items():
            try:
                rows.append((bot, pair, float(price), now))
            except (TypeError, ValueError):
                continue
        if rows:
            conn = get_conn()
            conn.executemany(
                "INSERT INTO live_prices(bot, pair, price, ts) VALUES(?,?,?,?) "
                "ON CONFLICT(bot, pair) DO UPDATE SET price=excluded.price, ts=excluded.ts",
                rows,
            )
            conn.commit()
            conn.close()
            valid = {p: pr for p, pr in prices.items() if isinstance(pr, (int, float))}
            _broadcast_price(bot, valid)
        self._send(200, {"ok": True, "bot": bot, "n": len(rows)})

    def _ingest(self, bot: str, data: dict) -> None:
        conn = get_conn()
        # trades (composite PK bot,id — no cross-bot collision)
        if "id" in data:
            raw = data.get("raw_json", json.dumps(data))
            conn.execute(
                """INSERT OR REPLACE INTO trades
                   (id,bot,pair,entry_price,exit_price,entry_ts,exit_ts,pnl_pct,
                    exit_reason,hold_cycles,entry_rsi,entry_regime,entry_quality_score,
                    strategy_type,chart_context,raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (data.get("id"), bot, data.get("pair"), data.get("entry_price"),
                 data.get("exit_price"), data.get("entry_ts"), data.get("exit_ts"),
                 data.get("pnl_pct"), data.get("exit_reason"), data.get("hold_cycles"),
                 data.get("entry_rsi"), data.get("entry_regime"),
                 data.get("entry_quality_score"), data.get("strategy_type"),
                 data.get("chart_context"), raw),
            )
        if "hypotheses" in data:
            for h in data["hypotheses"]:
                conn.execute(
                    """INSERT OR REPLACE INTO hypotheses
                       (bot,ts,pair,version_from,version_to,variable,old_value,
                        new_value,reasoning,mode,raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (bot, h.get("ts"), h.get("pair"), h.get("version_from"),
                     h.get("version_to"), h.get("variable"), h.get("old_value"),
                     h.get("new_value"), h.get("reasoning"), h.get("mode"),
                     json.dumps(h)),
                )
        if "skips" in data:
            for s in data["skips"]:
                conn.execute(
                    """INSERT OR REPLACE INTO skips
                       (bot,ts,pair,reason_skipped,rsi_at_skip,price_at_skip,
                        missed_pnl,raw_json)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (bot, s.get("ts"), s.get("pair"), s.get("reason_skipped"),
                     s.get("rsi_at_skip"), s.get("price_at_skip"), s.get("missed_pnl"),
                     json.dumps(s)),
                )
        conn.commit()
        conn.close()

    # ── GET read endpoints ───────────────────────────────────────────────────
    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        if path == "/api/overview" or path == "/api/overview/":
            return self._send(200, self._overview())
        if path.startswith("/api/trades/"):
            return self._send(200, self._query("trades", self._bot_from_path("/api/trades/")))
        if path.startswith("/api/hypotheses/"):
            bot = self._bot_from_path("/api/hypotheses/")
            return self._send(200, self._query("hypotheses", bot))
        if path.startswith("/api/skips/"):
            return self._send(200, self._query("skips", self._bot_from_path("/api/skips/")))
        if path.startswith("/api/discovered/"):
            return self._send(200, self._discovered(self._bot_from_path("/api/discovered/")))
        if path.startswith("/api/cortex/"):
            return self._send(200, self._cortex(self._bot_from_path("/api/cortex/")))
        if path.startswith("/api/flatline/"):
            return self._send(200, self._flatline(self._bot_from_path("/api/flatline/")))
        if path.startswith("/api/heartbeat/"):
            return self._send(200, self._heartbeat(self._bot_from_path("/api/heartbeat/")))
        if path.startswith("/api/price/") and path.endswith("/stream"):
            return self._sse_stream(self._bot_from_path("/api/price/").rstrip("/"))
        if path.startswith("/api/price/"):
            return self._send(200, self._get_prices(self._bot_from_path("/api/price/")))
        # ── static frontend (built vite app) ──────────────────────────────────
        # Serves index.html for "/" and assets from dist/. Keeps Railway free of
        # a separate nginx. [GUARD L62]
        if path == "/" or path == "":
            return self._serve_static("index.html")
        if not path.startswith("/api/"):
            return self._serve_static(path.lstrip("/"))
        self._send(404, {"error": "not found"})

    def _query(self, table: str, bot: str | None) -> list[dict]:
        if bot is None or bot not in VALID_BOTS:
            return []                      # explicit empty, never a 500
        conn = get_conn()
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE bot=?", (bot,)
        ).fetchall()
        conn.close()
        return [_row_to_dict(r) for r in rows]

    def _overview(self) -> dict:
        out: dict[str, Any] = {}
        for bot in sorted(VALID_BOTS):
            out[bot] = {
                "trades": len(self._query("trades", bot)),
                "hypotheses": len(self._query("hypotheses", bot)),
                "skips": len(self._query("skips", bot)),
            }
        return out

    def _file_json(self, rel: str) -> Any:
        p = os.path.join(STATE_DIR, rel)
        if not os.path.exists(p):
            return None
        try:
            return json.loads(open(p, encoding="utf-8").read())
        except (json.JSONDecodeError, OSError):
            return None

    def _discovered(self, bot: str | None) -> list[dict]:
        if bot is None or bot not in VALID_BOTS:
            return []
        safe = (bot or "unknown").replace("/", "_")
        data = self._file_json(f"discovered/{safe}.json")
        return data if isinstance(data, list) else ([] if data is None else [data])

    def _cortex(self, bot: str | None) -> Any:
        if bot is None or bot not in VALID_BOTS:
            return {"exiled": []}
        data = self._file_json("cortex/indicator_exile.json")
        return {"exiled": sorted(data.keys()) if isinstance(data, dict) else []}

    def _flatline(self, bot: str | None) -> list[dict]:
        if bot is None or bot not in VALID_BOTS:
            return []
        data = self._file_json("flatline_log.jsonl")
        if isinstance(data, list):
            return data
        # jsonl fallback
        p = os.path.join(STATE_DIR, "flatline_log.jsonl")
        if os.path.exists(p):
            out = []
            with open(p, encoding="utf-8") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return out
        return []

    def _heartbeat(self, bot: str | None) -> Any:
        if bot is None or bot not in VALID_BOTS:
            return None
        return self._file_json(f"heartbeat_{bot}.json") or self._file_json("heartbeat.json")

    # ── live prices (real-time) ────────────────────────────────────────────
    def _get_prices(self, bot: str | None) -> dict:
        """GET /api/price/{bot} -> { PAIR: {price, ts}, ... } [L64]."""
        if bot is None or bot not in VALID_BOTS:
            return {}
        conn = get_conn()
        rows = conn.execute(
            "SELECT pair, price, ts FROM live_prices WHERE bot=?", (bot,)
        ).fetchall()
        conn.close()
        return {r["pair"]: {"price": r["price"], "ts": r["ts"]} for r in rows}

    def _sse_stream(self, bot: str | None) -> None:
        """GET /api/price/{bot}/stream -> Server-Sent Events (real-time) [L64].

        Pushes a ``data:`` event per price update for `bot` (or all bots if
        `bot` is empty). Kept dependency-free: a stdlib queue.Queue per
        subscriber, drained over the HTTP response body with keep-alive
        comments so proxies don't drop the connection.
        """
        q: queue.Queue = queue.Queue()
        with _sse_lock:
            _sse_subscribers.add(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")  # disable proxy buffering
        self.end_headers()
        try:
            while True:
                try:
                    payload = q.get(timeout=SSE_HEARTBEAT_S)
                except queue.Empty:
                    # keep-alive comment so the stream isn't GC'd by a proxy
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if bot and payload.get("bot") != bot:
                    continue  # subscriber only wants one bot
                line = f"data: {json.dumps(payload)}\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _sse_lock:
                _sse_subscribers.discard(q)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _broadcast_price(bot: str, prices: dict) -> None:
    """Fan a price update out to all SSE subscribers [L64]."""
    if not _sse_subscribers:
        return
    payload = {"bot": bot, "prices": prices, "ts": _now_iso()}
    with _sse_lock:
        for q in list(_sse_subscribers):
            with suppress(Exception):  # [GUARD L64] a dead sub never blocks
                q.put_nowait(payload)


def make_app() -> type[DashboardHandler]:
    init_db()
    return DashboardHandler


def run(host: str = "0.0.0.0", port: int | None = None) -> None:
    # Railway injects $PORT; honor it so the service is reachable in prod.
    # Locally port defaults to 8000. [GUARD L62]
    port = port if port is not None else int(get_env("PORT", "8000"))
    srv = ThreadingHTTPServer((host, port), make_app())
    print(f"[dashboard] serving on {host}:{port}", flush=True)
    srv.serve_forever()


# ── in-process test client (no socket) ──────────────────────────────────────
class test_client:
    """Drive the handler in-process: client.post(path, json=..., headers=...)."""

    def __init__(self) -> None:
        self._handler_cls = make_app()

    def _handle(self, method: str, path: str, json_body: Any = None,
                 headers: dict | None = None) -> response:
        import io

        class Headers:
            def __init__(self, d: dict) -> None:
                self._d = {k.lower(): str(v) for k, v in d.items()}

            def get(self, key: str, default: Any = None) -> Any:
                return self._d.get(key.lower(), default)

        h = self._handler_cls.__new__(self._handler_cls)
        h.command = method
        h.path = path
        body = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
        hdr_dict = {k.lower(): str(v) for k, v in (headers or {}).items()}
        if body:
            hdr_dict["content-length"] = str(len(body))
        h.headers = Headers(hdr_dict)
        h.wfile = io.BytesIO()

        class _Rfile(io.BytesIO):
            def read(self, n=-1):  # type: ignore[override]
                return super().read(n)

        h.rfile = _Rfile(body)
        if method == "POST":
            h.do_POST()
        else:
            h.do_GET()
        raw = h.wfile.getvalue()
        head, _, payload = raw.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n")[0].decode()
        code = int(status_line.split(" ", 2)[1])
        # parse response headers so tests can assert Content-Type etc.
        headers: dict[str, str] = {}
        for line in head.split(b"\r\n")[1:]:
            if b":" in line:
                k, _, v = line.partition(b":")
                headers[k.decode().strip().lower()] = v.decode().strip()
        return response(code, payload.decode("utf-8"), headers)

    def post(self, path: str, json: Any = None, headers: dict | None = None) -> response:
        return self._handle("POST", path, json, headers)

    def get(self, path: str, headers: dict | None = None) -> response:
        return self._handle("GET", path, None, headers)


class response:
    def __init__(self, status_code: int, text: str,
                 headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    @property
    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except json.JSONDecodeError:
            return None


if __name__ == "__main__":
    run()
