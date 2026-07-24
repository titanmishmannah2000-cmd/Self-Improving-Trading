"""Real-time live-price dashboard API tests (S19 real-time feature).

Network-free. Exercises the new endpoints added for the real-time dashboard:
  * POST /api/price/{bot}  (auth, validation, persist)
  * GET  /api/price/{bot}  (read back)
  * SSE fan-out            (_broadcast_price -> subscriber queue)

Blueprint guard tags honoured ([GUARD L64] real-time fan-out, [GUARD] unknown
bot / token rejection).
"""

from __future__ import annotations

import queue

import pytest

import dashboard.backend.main as m


@pytest.fixture(autouse=True)
def _tmp_backend(tmp_path, monkeypatch):
    """Isolate the dashboard DB + HERMES_STATE and set a known ingest token."""
    db = tmp_path / "dashboard.db"
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("DASHBOARD_DB", str(db))
    monkeypatch.setenv("HERMES_STATE", str(state))
    monkeypatch.setenv("INGEST_TOKEN", "secret-token")
    m.DB_PATH = str(db)
    m.STATE_DIR = str(state)
    m.INGEST_TOKEN = "secret-token"
    m.init_db()
    yield


TOKEN = {"X-Ingest-Token": "secret-token"}


def _price_post(c, bot, prices):
    return c.post(f"/api/price/{bot}", json={"prices": prices}, headers=TOKEN)


def test_price_ingest_requires_token():
    c = m.test_client()
    r = c.post("/api/price/forex", json={"prices": {"EUR/USD": 1.1}}, headers={})
    assert r.status_code == 401


def test_price_ingest_unknown_bot_404():
    c = m.test_client()
    r = _price_post(c, "martian", {"EUR/USD": 1.1})
    assert r.status_code == 404


def test_price_ingest_and_read_back():
    c = m.test_client()
    r = _price_post(c, "forex", {"EUR/USD": 1.1234, "GBP/USD": 1.25})
    assert r.status_code == 200
    assert r.json()["n"] == 2
    got = c.get("/api/price/forex").json()
    assert got["EUR/USD"]["price"] == 1.1234
    assert got["GBP/USD"]["price"] == 1.25


def test_price_upsert_latest_wins():
    c = m.test_client()
    _price_post(c, "forex", {"EUR/USD": 1.1})
    _price_post(c, "forex", {"EUR/USD": 1.2})
    got = c.get("/api/price/forex").json()
    assert got["EUR/USD"]["price"] == 1.2  # last write wins


def test_price_rejects_non_dict():
    c = m.test_client()
    r = c.post("/api/price/forex", json={"prices": "nope"}, headers=TOKEN)
    assert r.status_code == 400


def test_price_other_bot_not_leaked():
    c = m.test_client()
    _price_post(c, "forex", {"EUR/USD": 1.1})
    _price_post(c, "gold", {"XAU/USD": 2000.0})
    fx = c.get("/api/price/forex").json()
    gold = c.get("/api/price/gold").json()
    assert "XAU/USD" not in fx
    assert "EUR/USD" not in gold


def test_sse_broadcast_fans_out_to_subscriber():
    # Network-free proof of the real-time mechanism: a broadcast enqueues a
    # payload onto a subscriber queue, which is exactly what the SSE stream
    # drains and writes to the browser. [GUARD L64]
    q: queue.Queue = queue.Queue()
    with m._sse_lock:
        m._sse_subscribers.add(q)
    try:
        m._broadcast_price("crypto", {"BTC/USD": 63700.0})
        payload = q.get(timeout=1)
        assert payload["bot"] == "crypto"
        assert payload["prices"]["BTC/USD"] == 63700.0
        assert "ts" in payload
    finally:
        with m._sse_lock:
            m._sse_subscribers.discard(q)


def test_sse_broadcast_no_subscribers_is_safe():
    # [GUARD L64] with zero subscribers, broadcast must be a no-op, not crash.
    with m._sse_lock:
        m._sse_subscribers.clear()
    m._broadcast_price("forex", {"EUR/USD": 1.1})  # must not raise


def test_root_serves_built_frontend(tmp_path, monkeypatch):
    # [GUARD L62] the dashboard serves the built vite app from DIST_DIR so
    # Railway needs no separate nginx. / -> index.html; /assets/* -> asset.
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><html>OK</html>")
    (dist / "assets" / "app.js").write_text("console.log('hi')")
    monkeypatch.setattr(m, "DIST_DIR", str(dist))
    c = m.test_client()
    r = c.get("/")
    assert r.status_code == 200
    assert "OK" in r.text
    assert "text/html" in r.headers["content-type"]
    # deep asset path resolves too
    r2 = c.get("/assets/app.js")
    assert r2.status_code == 200
    assert "console.log" in r2.text


def test_unknown_path_without_api_falls_back_to_index(tmp_path, monkeypatch):
    # SPA fallback: a client-side route like /live-prices is served index.html
    # (not a 404) so the React router can take over in the browser. [GUARD L62]
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><html>SPA</html>")
    monkeypatch.setattr(m, "DIST_DIR", str(dist))
    c = m.test_client()
    r = c.get("/live-prices")
    assert r.status_code == 200
    assert "SPA" in r.text


def test_wal_mode_and_concurrent_writes_do_not_lock(tmp_path, monkeypatch):
    # [GUARD L62] under WAL + busy_timeout, concurrent writers (the 3 bots
    # POSTing price snapshots every cycle) must NOT raise "database is locked".
    db = tmp_path / "dashboard.db"
    monkeypatch.setenv("DASHBOARD_DB", str(db))
    monkeypatch.setenv("INGEST_TOKEN", "secret-token")
    m.DB_PATH = str(db)
    m.INGEST_TOKEN = "secret-token"
    m.init_db()
    # open a connection and confirm WAL was applied
    conn = m.get_conn()
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    conn.close()

    # simulate 3 bots hammering the price endpoint in parallel
    import threading

    c = m.test_client()
    errors: list[BaseException] = []

    def push(bot):
        try:
            for _ in range(20):
                r = c.post(
                    f"/api/price/{bot}",
                    json={"prices": {f"{bot}-x": 1.0}},
                    headers={"X-Ingest-Token": "secret-token"},
                )
                assert r.status_code == 200
        except BaseException as e:  # noqa: BLE001 — capture, don't crash thread
            errors.append(e)

    threads = [threading.Thread(target=push, args=(b,)) for b in ("forex", "gold", "crypto")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent writes raised: {errors}"


def test_healthz_ok_when_db_reachable():
    # [GUARD L62] Railway healthcheckPath=/healthz must return 200 + status ok
    # when the process is up and sqlite answers.
    c = m.test_client()
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_healthz_503_when_db_unreachable(monkeypatch):
    # Prove the probe is REAL: build the client first (valid DB), then point the
    # DB at an unopenable path so the probe's own SELECT fails -> 503, not a
    # false "healthy".
    c = m.test_client()
    monkeypatch.setattr(m, "DB_PATH", "/nonexistent-dir/definitely/not/here.db")
    r = c.get("/healthz")
    assert r.status_code == 503
    assert r.json()["status"] == "unhealthy"
