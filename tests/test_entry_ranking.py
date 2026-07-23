"""HIF Layer B — entry ranking (quality ranks, never hard-blocks)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_core.engines.entry_ranking import (
    entry_ranking_enabled,
    rank_candidates,
    score_candidate,
)


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("ENTRY_RANKING", raising=False)
    assert entry_ranking_enabled() is False
    monkeypatch.setenv("ENTRY_RANKING", "1")
    assert entry_ranking_enabled() is True


def test_score_blend_prefers_edge_and_quality():
    weak = score_candidate(
        entry_type="mean_reversion",
        quality=0.3,
        p_bayes=0.4,
        expert_weight=1.0,
    )
    strong = score_candidate(
        entry_type="gp_ensemble",
        quality=0.9,
        p_bayes=0.7,
        expert_weight=1.0,
        gp_strength=0.8,
    )
    assert strong["score"] > weak["score"]
    assert strong["components"]["edge_src"] == "p_bayes"
    assert weak["components"]["quality"] == pytest.approx(0.3)


def test_neutral_edge_when_no_history():
    out = score_candidate(entry_type="mean_reversion", quality=0.8)
    assert out["components"]["edge"] == pytest.approx(0.5)
    assert out["components"]["edge_src"] == "neutral"
    assert 0.0 <= out["score"] <= 1.0


def test_rank_picks_higher_score():
    a = {
        "sig": SimpleNamespace(quality=0.5),
        "entry_type": "mean_reversion",
        "score": 0.55,
        "components": {},
    }
    b = {
        "sig": SimpleNamespace(quality=0.6),
        "entry_type": "gp_ensemble",
        "score": 0.72,
        "components": {},
    }
    picked = rank_candidates([a, b])
    assert picked["winner"]["entry_type"] == "gp_ensemble"
    assert picked["n_candidates"] == 2
    assert "best_score=0.72" in picked["reason"]


def test_tie_break_prefers_traditional():
    trad = {
        "sig": SimpleNamespace(quality=0.8),
        "entry_type": "mean_reversion",
        "score": 0.6,
    }
    gp = {
        "sig": SimpleNamespace(quality=0.8),
        "entry_type": "gp_ensemble",
        "score": 0.6,
    }
    picked = rank_candidates([gp, trad])
    assert picked["winner"]["entry_type"] == "mean_reversion"


def test_empty_candidates_fail_open():
    picked = rank_candidates([])
    assert picked["winner"] is None
    assert picked["reason"] == "no_candidates"


def test_single_candidate_still_wins():
    only = {
        "sig": SimpleNamespace(quality=0.4),
        "entry_type": "mean_reversion",
        "score": 0.5,
    }
    picked = rank_candidates([only])
    assert picked["winner"] is only
    assert picked["n_candidates"] == 1
