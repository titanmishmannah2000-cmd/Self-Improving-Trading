"""Chart vision engine (Session 8 / Phase 8) — CHART -> vision LLM -> context.

Chart PNG -> vision LLM -> structured text context the entry loop consumes.

Guards (tagged so tools/verify_guard_tags.py can find them):
  L14  hard block — context containing "avoid" or "downtrend" -> the asset is
       untradeable; the loop skips it entirely (no_signal).
  L16  soft filter — context containing "sell" AND a low quality (<5) -> skip
       (weaker than L14; a confident sell still passes through to the engines).

Behaviour (blueprint Section 7 / Engine 6):
  * PRIMARY  Gemini gemini-2.5-flash, FALLBACK Groq llama-4-scout.
  * 60-minute cache (in-memory + on-disk) so we don't re-call the LLM every
    60s cycle.
  * FAIL-OPEN: any pipeline error yields an empty context, never an exception —
    the loop treats a missing chart as "no extra signal", never as a crash.

The pipeline (fetch -> render -> analyze) is dispatched through module-level
functions so the unit tests can monkeypatch them without network or the heavy
yfinance/mplfinance/httpx deps. Those deps are imported lazily inside the real
functions so this module imports cleanly in any environment.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import time
from pathlib import Path

from hermes_core.config import repo_root

# ── config ────────────────────────────────────────────────────────────────
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
CACHE_INTERVAL_S = 3600  # 60 minutes
_CACHE_DIR = repo_root() / "state" / "chart_cache"
_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# bot symbol -> yfinance ticker (used by the real fetch path)
SYMBOL_MAP = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "AUD/USD": "AUDUSD=X",
    "GBP/JPY": "GBPJPY=X",
}

# in-memory cache: pair -> (context, ts)
_context_cache: dict[str, tuple[str, float]] = {}

CHART_PROMPT = (
    "You are a professional technical analyst. Look at this price chart "
    "and return ONLY a JSON object with NO extra text. Format exactly:\n"
    '{"trend": "uptrend"|"downtrend"|"sideways", '
    '"confidence": 0.0-1.0, '
    '"sr_level": "support at X, resistance at Y", '
    '"recommendation": "enter long"|"wait for pullback"|"avoid entirely"}\n'
    "The confidence value must reflect how certain you are about the trend "
    "direction. Brief and precise. NO markdown, NO explanation, ONLY the JSON object."
)


# ── guard predicates (pure, never raise) ──────────────────────────────────
def hard_block(context: str) -> bool:
    """[GUARD L14] Hard block: vision flagged this asset as untradeable."""
    c = (context or "").lower()
    return "avoid" in c or "downtrend" in c


def _quality_of(context: str) -> float:
    """Extract a 0..10 quality from the '(conf=0.50)' token; default 5."""
    m = re.search(r"conf\s*=\s*([0-9]*\.?[0-9]+)", context or "")
    if not m:
        return 5.0
    try:
        return round(float(m.group(1)) * 10.0, 2)
    except ValueError:
        return 5.0


def soft_block(context: str) -> bool:
    """[GUARD L16] Soft filter: a low-quality 'sell' recommendation -> skip."""
    c = (context or "").lower()
    return "sell" in c and _quality_of(context) < 5.0


# ── cache ────────────────────────────────────────────────────────────────
def _cache_file(symbol: str) -> Path:
    return _CACHE_DIR / f"chart_ctx_{symbol.replace('/', '_')}.json"


def _get_cached(symbol: str, now: float = time.time()) -> str | None:
    if symbol in _context_cache:
        context, ts = _context_cache[symbol]
        if now - ts < CACHE_INTERVAL_S:
            return context
    fp = _cache_file(symbol)
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            if now - data.get("ts", 0) < CACHE_INTERVAL_S:
                _context_cache[symbol] = (data["context"], data["ts"])
                return data["context"]
        except Exception:  # noqa: BLE001 — corrupt cache is not fatal
            pass
    return None


def _set_cached(symbol: str, context: str, now: float = time.time()) -> None:
    _context_cache[symbol] = (context, now)
    with contextlib.suppress(OSError):
        _cache_file(symbol).write_text(
            json.dumps({"context": context, "ts": now}), encoding="utf-8"
        )


# ── pipeline (module globals so tests can monkeypatch; heavy libs lazy) ────
def fetch_ohlcv(symbol: str):  # pragma: no cover - needs network + yfinance
    import pandas as pd
    import yfinance as yf

    ticker = SYMBOL_MAP.get(symbol, symbol)
    try:
        df = yf.download(ticker, period="5d", interval="1h", progress=False)
        if df is None or len(df) < 10:
            return None
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.tail(100)
        df.index = pd.to_datetime(df.index)
        return df[["Open", "High", "Low", "Close", "Volume"]].dropna()
    except Exception:  # noqa: BLE001
        return None


def generate_chart_png(df, symbol: str):  # pragma: no cover - needs mplfinance
    import matplotlib

    matplotlib.use("Agg")
    import mplfinance as mpf

    try:
        cache_path = _CACHE_DIR / f"chart_{symbol.replace('/', '_')}.png"
        style = mpf.make_mpf_style(
            base_mpf_style="nightclouds",
            marketcolors=mpf.make_marketcolors(
                up="lime", down="red", edge="inherit", wick="inherit", volume="in"
            ),
        )
        add_plots = [
            mpf.make_addplot(df["Close"].ewm(span=20).mean(), color="cyan", width=0.8),
            mpf.make_addplot(df["Close"].ewm(span=50).mean(), color="yellow", width=0.8),
        ]
        mpf.plot(
            df, type="candle", style=style, title=f"\n{symbol} — 1H Chart",
            ylabel="Price", volume=True, addplot=add_plots,
            savefig=dict(fname=str(cache_path), dpi=150, bbox_inches="tight"),
            returnfig=False,
        )
        return cache_path
    except Exception:  # noqa: BLE001
        return None


def _parse_chart_response(text: str) -> str:
    """Parse the LLM JSON into a structured summary line for loop consumers."""
    try:
        m = re.search(r'{[^}]*"trend"[^}]*}', text or "", re.DOTALL)
        if m:
            data = json.loads(m.group())
            trend = data.get("trend", "sideways")
            conf = data.get("confidence", 0.5)
            sr = data.get("sr_level", "")
            rec = data.get("recommendation", "wait for pullback")
            return f"trend: {trend} (conf={conf:.2f}). SR: {sr}. Rec: {rec}"
    except (json.JSONDecodeError, KeyError, ValueError):
        pass
    return (text or "").strip()


def analyze_chart_gemini(png_path, symbol: str) -> str | None:
    """PRIMARY vision call (Gemini). Returns structured context or None on failure."""
    if not GEMINI_API_KEY:
        return None
    import base64

    import httpx

    try:
        img_b64 = base64.b64encode(Path(png_path).read_bytes()).decode("utf-8")
    except OSError:
        return None
    payload = {
        "contents": [{
            "parts": [
                {"text": CHART_PROMPT},
                {"inline_data": {"mime_type": "image/png", "data": img_b64}},
            ]
        }]
    }
    try:
        resp = httpx.post(GEMINI_URL, json=payload,
                          params={"key": GEMINI_API_KEY}, timeout=30)
        if resp.status_code == 429:
            return "CHART: unavailable (rate limited)"
        resp.raise_for_status()
        text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        return _parse_chart_response(text)
    except Exception:  # noqa: BLE001 — primary failure falls through to fallback
        return None


def analyze_chart_groq(png_path, symbol: str) -> str | None:
    """FALLBACK vision call (Groq). Returns structured context or None on failure."""
    if not GROQ_API_KEY:
        return None
    import base64

    import httpx

    try:
        img_b64 = base64.b64encode(Path(png_path).read_bytes()).decode("utf-8")
    except OSError:
        return None
    payload = {
        "model": GROQ_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": CHART_PROMPT},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
            ],
        }],
        "max_tokens": 300,
        "temperature": 0.3,
    }
    try:
        resp = httpx.post(
            GROQ_URL, json=payload,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        return _parse_chart_response(text)
    except Exception:  # noqa: BLE001
        return None


def analyze_chart(png_path, symbol: str) -> str | None:
    """[GUARD L14/L16 source] Gemini PRIMARY -> Groq FALLBACK."""
    ctx = analyze_chart_gemini(png_path, symbol)
    if ctx is None or "unavailable" in ctx.lower() or "failed" in ctx.lower():
        ctx = analyze_chart_groq(png_path, symbol)
    return ctx


def get_chart_context(symbol: str, now: float = time.time()) -> str:
    """Return a structured chart context for ``symbol`` (cached 60 min).

    FAIL-OPEN: any failure yields a benign context string, never raises.
    """
    cached = _get_cached(symbol, now)
    if cached is not None:
        return cached
    df = fetch_ohlcv(symbol)
    if df is None or len(df) < 10:
        return "Chart data unavailable."
    png_path = generate_chart_png(df, symbol)
    if png_path is None:
        return "Chart generation failed."
    ctx = analyze_chart(png_path, symbol)
    if not ctx:
        return "CHART: unavailable"
    _set_cached(symbol, ctx, now)
    with contextlib.suppress(OSError):
        Path(png_path).unlink()
    return ctx


def get_all_chart_contexts(symbols: list[str]) -> dict[str, str]:
    return {sym: get_chart_context(sym) for sym in symbols}
