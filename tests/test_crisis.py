"""Session 12 / Phase 12 tests for the crisis learning engine.

Network-free; the crisis DB and flatline log are redirected to a temp dir via
an autouse fixture so pre-seeded + lived crises never touch real state.

Blueprint exact names preserved:
  test_9dim_finite, test_covid_nn, test_flatline_saves, test_zero_history_safe
plus the L21 novel-regime flatline guard (roadmap exit gate).
"""

from __future__ import annotations

import pytest

import hermes_core.engines.crisis_learning as cl


@pytest.fixture(autouse=True)
def _tmp_state(tmp_path, monkeypatch):
    """Redirect crisis DB + flatline log into an isolated temp dir."""
    db = tmp_path / "crisis_embeddings.json"
    log = tmp_path / "flatline_log.jsonl"
    monkeypatch.setattr(cl, "DB_PATH", db)
    monkeypatch.setattr(cl, "FLATLINE_LOG", log)
    yield


def _known_series(n=120, start=1.10, drift=-0.0005, vol=0.002, seed=5):
    """A calm-ish price series with enough bars for feature extraction."""
    import random
    rng = random.Random(seed)
    out = [start]
    for _ in range(1, n):
        out.append(out[-1] * (1 + drift + rng.uniform(-vol, vol)))
    return out


# ── blueprint Phase 12 success criteria ───────────────────────────────────
def test_9dim_finite():
    sig = cl._extract_crisis_features(_known_series(), None)
    assert sig is not None
    assert len(sig) == cl.CRISIS_SIGNATURE_LENGTH
    import math
    assert all(math.isfinite(x) for x in sig)


def test_covid_nn():
    # the exact pre-seeded COVID fingerprint -> nearest crisis MUST be COVID-19
    rec = cl.get_crisis_recommendation(list(cl.COVID_SIG))
    assert rec is not None
    assert rec["crisis_name"] == "COVID-19"      # pre-seeded id resolved
    assert rec["novel"] is False
    assert rec["recommended_stop_pct"] == 2.5


def test_flatline_saves():
    hist = _known_series(n=80)
    cid = cl.save_lived_crisis("EUR/USD", -3.0, hist, None)
    assert cid is not None
    assert cid.startswith("lived_EUR/USD_")
    crises = cl._load_crises()
    assert cid in crises                         # append-only persisted


def test_zero_history_safe():
    rec = cl.get_crisis_recommendation([])       # zero crisis history
    assert isinstance(rec, dict)                 # safe defaults, no exception
    assert rec["novel"] is True
    rec2 = cl.get_crisis_recommendation(None)
    assert isinstance(rec2, dict)


# ── L21 novel-regime flatline guard (roadmap exit gate) ───────────────────
def test_novel_regime_flatlines_and_logs():
    # a wildly aberrant series: flat then a 90% single-bar collapse -> novel
    prices = [1.10] * 60 + [1.10 * (1 - 0.9)] + [1.10 * 0.1] * 30
    result = cl.check_novel_regime("GBP/JPY", prices, None)
    assert result["flatlined"] is True
    assert result["pause_cycles"] == cl.FLATLINE_CYCLES
    assert result["novel"] is True
    # flatline event was appended to the log
    assert cl.FLATLINE_LOG.exists()
    lines = cl.FLATLINE_LOG.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    import json
    assert json.loads(lines[0])["reason"] == "NOVEL_REGIME"


def test_known_regime_not_flatlined():
    # a fingerprint identical to a KNOWN crisis is the opposite of novel ->
    # the L21 flatline guard must NOT trigger.
    result = cl.check_novel_regime("EUR/USD", list(cl.COVID_SIG), None)
    assert result["flatlined"] is False
    assert result["pause_cycles"] == 0
    assert result["novel"] is False


def test_save_lived_crisis_append_only_keeps_prior():
    h = _known_series(n=80)
    c1 = cl.save_lived_crisis("EUR/USD", -3.0, h, None)
    c2 = cl.save_lived_crisis("USD/JPY", -8.0, h, None)
    crises = cl._load_crises()
    # both lived entries present + the 3 pre-seeded known crises untouched
    assert c1 in crises and c2 in crises
    assert "covid_crash_2020" in crises and "nfp_spike_2023" in crises


def test_crisis_learning_class_wrapper():
    cl_eng = cl.CrisisLearning()
    sig = cl_eng.signature(_known_series(), None)
    assert sig is not None and len(sig) == cl.CRISIS_SIGNATURE_LENGTH
    match = cl_eng.nearest(sig)
    assert match is None or match.crisis_id  # nearest may be None if novel
