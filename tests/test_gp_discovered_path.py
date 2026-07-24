"""GP discovered path + expr normalization (audit items 5–7).

Network-free. Covers:
  * canonical underscore path (EUR_USD.json)
  * legacy slash / bots seed migration (votable only)
  * expr_str / name fallback for voting
  * invent gate ignores non-GP seed fixtures
"""

from __future__ import annotations

import json

import pytest

import hermes_core.engines.genetic as gp
from hermes_core.engines.entry import gp_ensemble_signal
from hermes_core.engines.genetic import indicator_expr, load_discovered_indicators


@pytest.fixture(autouse=True)
def _tmp_discovered(tmp_path, monkeypatch):
    monkeypatch.setattr(gp, "DISCOVERED_DIR", tmp_path / "discovered")
    yield tmp_path / "discovered"


def test_canonical_path_is_underscore(_tmp_discovered):
    path = gp._discovered_path("EUR/USD")
    assert path.name == "EUR_USD.json"
    assert path.parent == _tmp_discovered


def test_save_writes_expr_and_expr_str(_tmp_discovered):
    gp._save_discovered(
        "EUR/USD",
        [
            {
                "pair": "EUR/USD",
                "name": "(price-sma20)",
                "expr": "(price-sma20)",
                "fitness": 0.3,
                "win_rate": 0.55,
                "oos_corr": 0.3,
                "source": "genetic",
            }
        ],
    )
    path = _tmp_discovered / "EUR_USD.json"
    assert path.exists()
    rows = json.loads(path.read_text(encoding="utf-8"))
    assert rows[0]["expr"] == "(price-sma20)"
    assert rows[0]["expr_str"] == "(price-sma20)"
    assert rows[0]["source"] == "genetic"


def test_legacy_slash_path_migrates_votable(_tmp_discovered):
    legacy = _tmp_discovered / "EUR" / "USD.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            [
                {
                    "name": "(ema20-sma20)",
                    "expr": "(ema20-sma20)",
                    "fitness": 0.25,
                    "win_rate": 0.5,
                    "oos_corr": 0.28,
                }
            ]
        ),
        encoding="utf-8",
    )

    loaded = load_discovered_indicators("EUR/USD", include_shared=False)
    assert len(loaded) == 1
    assert indicator_expr(loaded[0]) == "(ema20-sma20)"
    canon = _tmp_discovered / "EUR_USD.json"
    assert canon.exists()
    assert json.loads(canon.read_text(encoding="utf-8"))[0]["expr"] == "(ema20-sma20)"


def test_seed_fixture_does_not_migrate_or_block_invent(_tmp_discovered):
    """Dashboard seeds like ta.rsi(close,14) must not become canonical invent-blockers."""
    legacy = _tmp_discovered / "EUR" / "USD.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            [
                {
                    "name": "rsi_14",
                    "expr_str": "ta.rsi(close,14)",
                    "fitness": 0.82,
                    "win_rate": 0.61,
                    "source": "seed",
                }
            ]
        ),
        encoding="utf-8",
    )

    loaded = load_discovered_indicators("EUR/USD", include_shared=False)
    assert len(loaded) == 1
    assert indicator_expr(loaded[0]) is None
    assert not (_tmp_discovered / "EUR_USD.json").exists()


def test_canon_seed_pollution_is_scrubbed(_tmp_discovered):
    """Seed rows wrongly saved to EUR_USD.json must be deleted on load."""
    canon = _tmp_discovered / "EUR_USD.json"
    canon.parent.mkdir(parents=True, exist_ok=True)
    canon.write_text(
        json.dumps(
            [
                {
                    "name": "rsi_14",
                    "expr_str": "ta.rsi(close,14)",
                    "fitness": 0.82,
                    "win_rate": 0.61,
                    "source": "seed",
                }
            ]
        ),
        encoding="utf-8",
    )
    loaded = load_discovered_indicators("EUR/USD", include_shared=False)
    assert loaded == []
    assert not canon.exists()


def test_live_feedback_does_not_persist_seed_fixtures(_tmp_discovered, monkeypatch):
    """B10 must not re-write ta.*/seed fixtures into the canonical path."""
    import hermes_core.engines.decision_cortex as cx

    monkeypatch.setattr(cx, "CORTEX_DIR", _tmp_discovered / "cortex")
    monkeypatch.setattr(cx, "MEMORY_PATH", _tmp_discovered / "cortex" / "m.json")
    monkeypatch.setattr(cx, "EXILE_PATH", _tmp_discovered / "cortex" / "e.json")
    legacy = _tmp_discovered / "EUR" / "USD.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            [
                {
                    "name": "rsi_14",
                    "expr_str": "ta.rsi(close,14)",
                    "fitness": 0.82,
                    "win_rate": 0.61,
                    "source": "seed",
                }
            ]
        ),
        encoding="utf-8",
    )
    n = gp.apply_live_feedback("EUR/USD", cx.Cortex())
    assert n == 0
    assert not (_tmp_discovered / "EUR_USD.json").exists()


def test_expr_str_fallback_votes(_tmp_discovered):
    """Indicators that only have expr_str (valid GP) still produce a shadow signal."""
    gp._save_discovered(
        "EUR/USD",
        [
            {
                "name": "a",
                "expr_str": "(price-sma20)",
                "fitness": 0.4,
                "win_rate": 0.6,
                "backtest_approved": True,
                "interval": "1d",
                "horizon": 10,
            },
            {
                "name": "b",
                "expr_str": "(ema20-sma20)",
                "fitness": 0.35,
                "win_rate": 0.55,
                "backtest_approved": True,
                "interval": "1d",
                "horizon": 10,
            },
        ],
    )
    # Re-load normalizes expr_str → expr
    rows = load_discovered_indicators("EUR/USD", include_shared=False)
    assert all(indicator_expr(r) for r in rows)

    prices = [100.0 + 0.2 * i for i in range(110)] + [112.0 + 0.5 * j for j in range(10)]
    sig = gp_ensemble_signal("EUR/USD", prices, promote=False)
    assert sig is not None
    assert sig.meta.get("shadow") is True
    assert sig.meta.get("num_active", 0) >= 2


def test_is_gp_expr_rejects_ta_seeds():
    assert gp._is_gp_expr("(price-sma20)") is True
    assert gp._is_gp_expr("rsi") is True
    assert gp._is_gp_expr("ta.rsi(close,14)") is False
    assert gp._is_gp_expr("mom(close,5)") is False
    assert gp._is_gp_expr("") is False


def test_discover_persists_canonical_schema(_tmp_discovered):
    import math
    import random

    rng = random.Random(3)
    prices = [1.10]
    for i in range(1, 400):
        wave = 0.004 * math.sin(i / 7.0)
        prices.append(prices[-1] * (1 + wave + rng.uniform(-0.001, 0.001)))

    # Match live forex invent regime (daily / horizon-10).
    inds = gp.discover(
        "EUR/USD",
        prices,
        generations=40,
        pop_size=40,
        top_k=3,
        horizon=10,
        interval="1d",
    )
    assert len(inds) >= 1
    path = _tmp_discovered / "EUR_USD.json"
    assert path.exists()
    disk = json.loads(path.read_text(encoding="utf-8"))
    for row in disk:
        assert "expr" in row and gp._is_gp_expr(row["expr"])
        assert row.get("expr_str") == row["expr"]
        assert row.get("source") == "genetic"
        assert row.get("backtest_approved") is True
        assert row.get("interval") == "1d"
        assert int(row.get("horizon")) == 10
        assert "oos_corr" in row
        assert "perm_pvalue" in row
