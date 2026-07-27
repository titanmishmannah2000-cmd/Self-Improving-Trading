"""Session 8 / Phase 8 tests for the chart vision engine.

Network-free: the pipeline functions (fetch/generate/analyze) are monkeypatched
so tests run without yfinance/mplfinance/httpx or any API key, while still
exercising the real get_chart_context / hard_block / soft_block logic.

Required blueprint names kept verbatim:
  test_context_contains_trend, test_hard_block_avoid, test_cache_no_second_call,
  test_groq_fallback.
"""

from __future__ import annotations

import contextlib

import pytest

import hermes_core.engines.chart_vision as cv
from hermes_core.engines import hard_block, soft_block


def test_default_chart_model_ids(monkeypatch):
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    monkeypatch.delenv("CHART_GROQ_MODEL", raising=False)
    monkeypatch.delenv("GROQ_VISION_MODEL", raising=False)
    assert "gemini-2.5-flash" in cv._gemini_url()
    # Chart fallback uses a vision model — not the L2 text 8B.
    assert "llama-4-scout" in cv._groq_model()


def test_api_keys_read_at_call_time(monkeypatch, tmp_path):
    """Keys must not be frozen empty at import (load_env may run later)."""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("GROQ_API_KEY", "")
    assert cv._gemini_key() == ""
    monkeypatch.setenv("GEMINI_API_KEY", "late-key")
    assert cv._gemini_key() == "late-key"
    png = tmp_path / "x.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    # Missing key returns an explicit unavailable reason (not silent None-only).
    monkeypatch.setenv("GEMINI_API_KEY", "")
    out = cv.analyze_chart_gemini(png, "EUR/USD")
    assert out is not None and "no gemini key" in out


def test_symbol_map_covers_soak_bots():
    for pair in (
        "EUR/USD",
        "GBP/USD",
        "AUD/USD",
        "GBP/JPY",
        "XAU/USD",
        "XAG/USD",
        "BTC/USD",
        "ETH/USD",
    ):
        assert pair in cv.SYMBOL_MAP
        assert cv.SYMBOL_MAP[pair]


def test_runner_wires_chart_context_fn():
    """Production runner must inject get_chart_context into run_cycle."""
    import inspect
    from bots import _runner as runner

    src = inspect.getsource(runner.run_bot)
    assert "chart_context_fn=get_chart_context" in src
    assert runner.get_chart_context is cv.get_chart_context


# --- guard predicates -------------------------------------------------------
def test_hard_block_avoid():
    # blueprint: hard_block returns True on "avoid entirely"
    assert hard_block("...recommendation: avoid entirely...") is True
    assert hard_block("trend: downtrend (conf=0.60). Rec: avoid entirely") is True
    assert hard_block("trend: uptrend (conf=0.80). Rec: enter long") is False
    assert hard_block("") is False


def test_hard_block_downtrend():
    # L14 must also block a clear downtrend
    assert hard_block("trend: downtrend (conf=0.70). SR: support at 1.08.") is True


def test_soft_block_low_quality_sell():
    # L16: "sell" + quality<5 -> skip; confident sell passes through
    assert soft_block("trend: sideways (conf=0.20). Rec: sell") is True
    assert soft_block("trend: downtrend (conf=0.90). Rec: sell") is False
    assert soft_block("trend: uptrend (conf=0.80). Rec: enter long") is False


def test_context_contains_trend():
    # blueprint: parsed context must carry a trend token
    for trend in ("uptrend", "downtrend", "sideways"):
        c = cv._parse_chart_response(
            f'{{"trend": "{trend}", "confidence": 0.5, "sr_level": "", "recommendation": "wait"}}'
        )
        assert trend in c


# --- pipeline (monkeypatched, network-free) ---------------------------------
def _fake_df():
    """Minimal stand-in that satisfies get_chart_context's len(df) >= 10 check.

    generate_chart_png / analyze_chart are monkeypatched in the tests, so the
    df contents don't matter — only that it is non-None and long enough.
    """
    return [0.0] * 20


@pytest.fixture(autouse=True)
def _clear_cache():
    cv._context_cache.clear()
    yield
    cv._context_cache.clear()


def test_groq_fallback(monkeypatch):
    # blueprint: primary (Gemini) dies -> Groq fallback used, "sideways" returned.
    # get_chart_context() calls the analyze_chart orchestrator, which fans out to
    # analyze_chart_gemini (PRIMARY) then analyze_chart_groq (FALLBACK).
    monkeypatch.setattr(cv, "analyze_chart_gemini", lambda p, s: None)
    monkeypatch.setattr(
        cv,
        "analyze_chart_groq",
        lambda p, s: "trend: sideways (conf=0.50). SR: . Rec: wait for pullback",
    )
    monkeypatch.setattr(cv, "fetch_ohlcv", lambda s: _fake_df())
    monkeypatch.setattr(cv, "generate_chart_png", lambda df, s: cv._cache_dir() / "fake.png")
    c = cv.get_chart_context("EUR/USD")
    assert "sideways" in c


@pytest.fixture(autouse=True)
def _clear_cache():
    cv._context_cache.clear()
    # also wipe on-disk cache so a prior run can't satisfy _get_cached
    for fp in cv._cache_dir().glob("chart_ctx_*.json"):
        with contextlib.suppress(OSError):
            fp.unlink()
    yield
    cv._context_cache.clear()


def test_cache_no_second_call(monkeypatch):
    # blueprint: second call within 60m returns cached value, LLM not re-called
    calls = {"n": 0}

    def fake_analyze(png, sym):
        calls["n"] += 1
        return "trend: uptrend (conf=0.80). SR: . Rec: enter long"

    monkeypatch.setattr(cv, "fetch_ohlcv", lambda s: _fake_df())
    monkeypatch.setattr(cv, "generate_chart_png", lambda df, s: cv._cache_dir() / "fake.png")
    monkeypatch.setattr(cv, "analyze_chart", fake_analyze)

    c1 = cv.get_chart_context("EUR/USD")
    c2 = cv.get_chart_context("EUR/USD")
    assert c1 == c2
    assert calls["n"] == 1  # only the first call hit the analyzer


def test_fail_open_on_pipeline_error(monkeypatch):
    # FAIL-OPEN: fetch/analyze failure must yield a benign string, never raise
    monkeypatch.setattr(cv, "fetch_ohlcv", lambda s: None)
    c = cv.get_chart_context("EUR/USD")
    assert isinstance(c, str) and c

    monkeypatch.setattr(cv, "fetch_ohlcv", lambda s: _fake_df())
    monkeypatch.setattr(cv, "generate_chart_png", lambda df, s: None)
    c2 = cv.get_chart_context("GBP/USD")
    assert isinstance(c2, str) and c2


def test_entry_hard_block_blocks_signal():
    # wiring: a hard-block context must prevent an entry even when MR conditions hold
    from hermes_core.engines import evaluate_entry

    prices = [1.10] * 40 + [1.05]  # oversold-ish tail
    strat = {
        "strategy_type": "mean_reversion",
        "session_filter": "24h",
        "entry": {"threshold": 30},
        "position_size_r": 0.4,
    }
    # valid market context would give a signal; an L14 context must not
    sig = evaluate_entry(
        "EUR/USD",
        prices,
        strat,
        context="downtrend avoid entirely",
        session_token="LDN",
        current_cycle=1,
    )
    assert sig is None
    # and the soft filter also blocks a low-quality sell
    sig2 = evaluate_entry(
        "EUR/USD",
        prices,
        strat,
        context="sideways (conf=0.10). Rec: sell",
        session_token="LDN",
        current_cycle=1,
    )
    assert sig2 is None
