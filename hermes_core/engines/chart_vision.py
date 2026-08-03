"""Chart vision engine (Session 8 / Phase 8) — CHART -> vision LLM -> context.

Chart PNG -> vision LLM -> structured text context the entry loop consumes.

Guards (tagged so tools/verify_guard_tags.py can find them):
  L14  hard block — recommendation contains "avoid" (avoid entirely) -> capital
       veto; traditional entry AND GP promote both skip. Bare "downtrend" is
       NOT a hard block (that froze FX whenever vision labeled risk-off).
  L16  soft filter — legacy: "sell" AND low conf (<5) -> skip. Gray-zone chart
       (downtrend / wait for pullback) uses soft quality + size mults instead
       of skipping — feeds ENTRY_RANKING / probe-style sizing.

Behaviour (blueprint Section 7 / Engine 6):
  * PRIMARY  Gemini gemini-2.5-flash, FALLBACK Groq llama-4-scout (vision).
  * API keys read at call time via get_env (not frozen at import).
  * 60-minute cache (in-memory + on-disk) so we don't re-call the LLM every
    60s cycle.
  * FAIL-OPEN: any pipeline error yields a benign unavailable context, never an
    exception — the loop treats a missing chart as "no extra signal", never as
    a crash.

The pipeline (fetch -> render -> analyze) is dispatched through module-level
functions so the unit tests can monkeypatch them without network or the heavy
yfinance/mplfinance/httpx deps. Those deps are imported lazily inside the real
functions so this module imports cleanly in any environment.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
from pathlib import Path

from hermes_core.env import get_env
from hermes_core.state.paths import chart_cache_dir

# ── config ────────────────────────────────────────────────────────────────
# Keys MUST be read at call time via get_env — module-level os.environ capture
# freezes empty values when this file imports before load_env() (bots/_runner).
CACHE_INTERVAL_S = 3600  # 60 minutes
# Chart fallback needs a vision model. Do NOT reuse GROQ_MODEL (L2 text = 8B).
_DEFAULT_CHART_GROQ = "meta-llama/llama-4-scout-17b-16e-instruct"


def _gemini_key() -> str:
    return (get_env("GEMINI_API_KEY", "") or "").strip()


def _groq_key() -> str:
    return (get_env("GROQ_API_KEY", "") or "").strip()


def _gemini_url() -> str:
    model = get_env("GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash"
    return get_env(
        "GEMINI_URL",
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
    )


def _groq_model() -> str:
    """Vision-capable Groq model for chart fallback only."""
    return (
        get_env("CHART_GROQ_MODEL", "")
        or get_env("GROQ_VISION_MODEL", "")
        or _DEFAULT_CHART_GROQ
    )


def _groq_url() -> str:
    return get_env("GROQ_URL", "https://api.groq.com/openai/v1/chat/completions")


def _cache_dir() -> Path:
    return chart_cache_dir()


# bot symbol -> yfinance ticker (used by the real fetch path)
SYMBOL_MAP = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "AUD/USD": "AUDUSD=X",
    "GBP/JPY": "GBPJPY=X",
    "XAU/USD": "GC=F",
    "XAG/USD": "SI=F",
    "BTC/USD": "BTC-USD",
    "BTC/USDT": "BTC-USD",
    "ETH/USD": "ETH-USD",
    "ETH/USDT": "ETH-USD",
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


# Soft chart tilt (never zero — capital veto is hard_block only).
# Downtrend / wait-for-pullback haircut Signal.quality (ENTRY_RANKING) and size.
CHART_DOWNTREND_QUALITY_MULT = 0.70
CHART_PULLBACK_QUALITY_MULT = 0.85
CHART_DOWNTREND_SIZE_MULT = 0.50
CHART_PULLBACK_SIZE_MULT = 0.50
# Donchian (BTC Phase 3): vision "avoid" is a size/quality haircut, not a veto —
# D1 + channel are the capital gates; LLM avoid was freezing paper for days.
CHART_AVOID_QUALITY_MULT = 0.60
CHART_AVOID_SIZE_MULT = 0.40


# ── guard predicates (pure, never raise) ──────────────────────────────────
def hard_block(context: str) -> bool:
    """[GUARD L14] Hard block: vision recommendation is avoid (capital veto).

    Matches structured ``Rec: avoid entirely`` and bare ``avoid`` tokens.
    Does **not** hard-block on trend label ``downtrend`` alone — that is a soft
    quality/size tilt via ``chart_quality_mult`` / ``chart_size_mult``.
    """
    c = (context or "").lower()
    return "avoid" in c


def chart_hard_blocks_strategy(context: str, *, strategy_type: str | None = None) -> bool:
    """L14 capital veto, strategy-aware.

    ``donchian_breakout`` ignores vision avoid (soft tilt only) so BTC Phase 3
    is not frozen by a sideways ``Rec: avoid entirely`` cache.
    """
    st = (strategy_type or "").strip().lower()
    if st == "donchian_breakout":
        return False
    return hard_block(context)


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
    """[GUARD L16] Soft skip: low-confidence explicit sell (legacy schema).

    Vision prompt uses enter long / wait for pullback / avoid entirely — so this
    rarely fires live. Gray-zone downtrend/pullback must NOT soft-skip; they use
    ``chart_quality_mult`` / ``chart_size_mult`` instead.
    """
    c = (context or "").lower()
    if hard_block(c):
        return False  # L14 wins; don't double-count
    return "sell" in c and _quality_of(context) < 5.0


def chart_soft_reasons(
    context: str, *, strategy_type: str | None = None
) -> list[str]:
    """Human-readable soft-tilt tags for skips/position meta (never a hard veto)."""
    c = (context or "").lower()
    if not c:
        return []
    st = (strategy_type or "").strip().lower()
    treat_avoid_soft = st == "donchian_breakout"
    if hard_block(c) and not treat_avoid_soft:
        return []
    reasons: list[str] = []
    if treat_avoid_soft and "avoid" in c:
        reasons.append("avoid")
    if "downtrend" in c:
        reasons.append("downtrend")
    if "wait for pullback" in c or "wait on pullback" in c:
        reasons.append("wait_for_pullback")
    return reasons


def chart_quality_mult(context: str, *, strategy_type: str | None = None) -> float:
    """Multiply Signal.quality for ranking. 1.0 = no chart soft tilt."""
    reasons = chart_soft_reasons(context, strategy_type=strategy_type)
    if not reasons:
        return 1.0
    mult = 1.0
    if "avoid" in reasons:
        mult *= CHART_AVOID_QUALITY_MULT
    if "downtrend" in reasons:
        mult *= CHART_DOWNTREND_QUALITY_MULT
    if "wait_for_pullback" in reasons:
        mult *= CHART_PULLBACK_QUALITY_MULT
    return round(mult, 4)


def chart_size_mult(context: str, *, strategy_type: str | None = None) -> float:
    """Multiply position size for gray-zone chart. Never 0; hard_block handles veto."""
    reasons = chart_soft_reasons(context, strategy_type=strategy_type)
    if not reasons:
        return 1.0
    if "avoid" in reasons:
        return CHART_AVOID_SIZE_MULT
    if "downtrend" in reasons:
        return CHART_DOWNTREND_SIZE_MULT
    if "wait_for_pullback" in reasons:
        return CHART_PULLBACK_SIZE_MULT
    return 1.0


def apply_chart_soft_to_signal(sig, context: str, *, strategy_type: str | None = None):
    """Haircut ``sig.quality`` and stamp chart soft meta. Returns ``sig`` (mutated).

    No-op when mult == 1.0 or ``sig`` is None. Never blocks.
    """
    if sig is None:
        return None
    st = strategy_type or (getattr(sig, "meta", None) or {}).get("entry_type")
    mult = chart_quality_mult(context, strategy_type=st)
    reasons = chart_soft_reasons(context, strategy_type=st)
    meta = getattr(sig, "meta", None)
    if meta is None:
        sig.meta = {}
        meta = sig.meta
    meta["chart_quality_mult"] = mult
    meta["chart_size_mult"] = chart_size_mult(context, strategy_type=st)
    meta["chart_soft_reasons"] = reasons
    if mult < 1.0:
        try:
            sig.quality = round(float(sig.quality) * mult, 4)
        except (TypeError, ValueError):
            pass
    return sig


def _is_cacheable_context(context: str | None) -> bool:
    """Only persist real vision text — never cache fail-open unavailable strings.

    Caching ``CHART: unavailable (no gemini key)`` for 60m made live bots look
    broken long after Railway keys were fixed.
    """
    low = (context or "").strip().lower()
    if not low:
        return False
    if low in {
        "chart data unavailable.",
        "chart generation failed.",
        "chart: unavailable",
    }:
        return False
    if low.startswith("chart: unavailable") or low.startswith("chart data unavailable"):
        return False
    if "unavailable" in low or "failed" in low:
        return False
    return True


# ── cache ────────────────────────────────────────────────────────────────
def _cache_file(symbol: str) -> Path:
    return _cache_dir() / f"chart_ctx_{symbol.replace('/', '_')}.json"


def _get_cached(symbol: str, now: float = time.time()) -> str | None:
    if symbol in _context_cache:
        context, ts = _context_cache[symbol]
        if now - ts < CACHE_INTERVAL_S and _is_cacheable_context(context):
            return context
        if not _is_cacheable_context(context):
            _context_cache.pop(symbol, None)
    fp = _cache_file(symbol)
    if fp.exists():
        try:
            data = json.loads(fp.read_text(encoding="utf-8"))
            ctx = data.get("context")
            if now - data.get("ts", 0) < CACHE_INTERVAL_S and _is_cacheable_context(ctx):
                _context_cache[symbol] = (ctx, data["ts"])
                return ctx
            # Drop poisoned fail-open cache entries so keys/API recovery can retry.
            if not _is_cacheable_context(ctx):
                with contextlib.suppress(OSError):
                    fp.unlink()
        except Exception:  # noqa: BLE001 — corrupt cache is not fatal
            pass
    return None


def _set_cached(symbol: str, context: str, now: float = time.time()) -> None:
    if not _is_cacheable_context(context):
        return
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
    import warnings

    import matplotlib

    matplotlib.use("Agg")
    # Prefer a font that exists in slim Railway images (avoids findfont spam).
    with contextlib.suppress(Exception):
        from matplotlib import rcParams

        rcParams["font.family"] = "DejaVu Sans"
        rcParams["font.weight"] = "normal"
    import mplfinance as mpf

    try:
        cache_path = _cache_dir() / f"chart_{symbol.replace('/', '_')}.png"
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
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=".*identical low and high ylims.*",
                category=UserWarning,
            )
            warnings.filterwarnings("ignore", message=".*findfont:.*")
            mpf.plot(
                df,
                type="candle",
                style=style,
                title=f"\n{symbol} — 1H Chart",
                ylabel="Price",
                volume=True,
                addplot=add_plots,
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
    api_key = _gemini_key()
    if not api_key:
        print(f"[chart_vision] {symbol}: GEMINI_API_KEY missing at call time", flush=True)
        return "CHART: unavailable (no gemini key)"
    import base64

    import httpx

    try:
        img_b64 = base64.b64encode(Path(png_path).read_bytes()).decode("utf-8")
    except OSError as exc:
        print(f"[chart_vision] {symbol}: png read failed: {exc!r}", flush=True)
        return None
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": CHART_PROMPT},
                    {"inline_data": {"mime_type": "image/png", "data": img_b64}},
                ]
            }
        ]
    }
    try:
        resp = httpx.post(_gemini_url(), json=payload, params={"key": api_key}, timeout=30)
        if resp.status_code == 429:
            print(f"[chart_vision] {symbol}: gemini rate limited", flush=True)
            return "CHART: unavailable (rate limited)"
        if resp.status_code >= 400:
            detail = (resp.text or "")[:160].replace("\n", " ")
            print(
                f"[chart_vision] {symbol}: gemini HTTP {resp.status_code} {detail}",
                flush=True,
            )
            return f"CHART: unavailable (gemini http {resp.status_code})"
        data = resp.json()
        cands = data.get("candidates") or []
        if not cands:
            block = (data.get("promptFeedback") or {}).get("blockReason") or "no_candidates"
            print(f"[chart_vision] {symbol}: gemini blocked/empty ({block})", flush=True)
            return f"CHART: unavailable (gemini {block})"
        parts = ((cands[0].get("content") or {}).get("parts")) or []
        text = ""
        for part in parts:
            if isinstance(part, dict) and part.get("text"):
                text = str(part["text"]).strip()
                break
        if not text:
            print(f"[chart_vision] {symbol}: gemini empty text", flush=True)
            return "CHART: unavailable (gemini empty)"
        return _parse_chart_response(text)
    except Exception as exc:  # noqa: BLE001 — primary failure falls through to fallback
        print(f"[chart_vision] {symbol}: gemini error {exc!r}", flush=True)
        return None


def analyze_chart_groq(png_path, symbol: str) -> str | None:
    """FALLBACK vision call (Groq vision model). Returns structured context or None."""
    api_key = _groq_key()
    if not api_key:
        print(f"[chart_vision] {symbol}: GROQ_API_KEY missing at call time", flush=True)
        return None
    import base64

    import httpx

    try:
        img_b64 = base64.b64encode(Path(png_path).read_bytes()).decode("utf-8")
    except OSError as exc:
        print(f"[chart_vision] {symbol}: png read failed: {exc!r}", flush=True)
        return None
    model = _groq_model()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": CHART_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                ],
            }
        ],
        "max_tokens": 300,
        "temperature": 0.3,
    }
    try:
        resp = httpx.post(
            _groq_url(),
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=45,
        )
        if resp.status_code >= 400:
            detail = (resp.text or "")[:160].replace("\n", " ")
            print(
                f"[chart_vision] {symbol}: groq/{model} HTTP {resp.status_code} {detail}",
                flush=True,
            )
            return f"CHART: unavailable (groq http {resp.status_code})"
        text = resp.json()["choices"][0]["message"]["content"].strip()
        return _parse_chart_response(text)
    except Exception as exc:  # noqa: BLE001
        print(f"[chart_vision] {symbol}: groq error {exc!r}", flush=True)
        return None


def analyze_chart(png_path, symbol: str) -> str | None:
    """[GUARD L14/L16 source] Gemini PRIMARY -> Groq vision FALLBACK."""
    ctx = analyze_chart_gemini(png_path, symbol)
    if ctx is None or "unavailable" in (ctx or "").lower() or "failed" in (ctx or "").lower():
        fb = analyze_chart_groq(png_path, symbol)
        if fb:
            return fb
        # Prefer a specific gemini reason over bare None when fallback also fails.
        return ctx
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
