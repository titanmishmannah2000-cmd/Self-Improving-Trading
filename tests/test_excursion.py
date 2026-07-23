"""HIF MFE/MAE peak tracking + exit-intel giveback overlay + scoreboard."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_core.engines.excursion import (
    excursion_from_position,
    mfe_tracking_enabled,
    update_position_excursions,
)
from hermes_core.engines.exit_intel import apply_exit_intel


def test_flag_default_on(monkeypatch):
    monkeypatch.delenv("MFE_TRACKING", raising=False)
    assert mfe_tracking_enabled() is True


def test_flag_explicit_off(monkeypatch):
    monkeypatch.setenv("MFE_TRACKING", "0")
    assert mfe_tracking_enabled() is False


def test_update_peaks_mfe_and_mae():
    pos: dict = {}
    update_position_excursions(pos, 0.5)
    assert pos["peak_mfe_pct"] == pytest.approx(0.5)
    assert pos["trough_mae_pct"] == pytest.approx(0.0)
    update_position_excursions(pos, 1.2)
    assert pos["peak_mfe_pct"] == pytest.approx(1.2)
    update_position_excursions(pos, -0.8)
    assert pos["peak_mfe_pct"] == pytest.approx(1.2)
    assert pos["trough_mae_pct"] == pytest.approx(-0.8)


def test_giveback_snapshot():
    pos = {"peak_mfe_pct": 2.0, "trough_mae_pct": -0.5, "unrealised_pct": 0.8}
    snap = excursion_from_position(pos, 0.8)
    assert snap["mfe_pct"] == pytest.approx(2.0)
    assert snap["giveback_pct"] == pytest.approx(1.2)
    assert snap["giveback_frac"] == pytest.approx(0.6)
    assert snap["mfe_capture"] == pytest.approx(0.4)  # 0.8/2.0


def test_time_exit_underwater_after_mfe_shows_poor_capture():
    """Gold-style leak: MFE 0.8% then time_exit at -0.1% → capture negative."""
    pos = {"peak_mfe_pct": 0.8, "trough_mae_pct": -0.2}
    snap = excursion_from_position(pos, -0.1)
    assert snap["giveback_frac"] == pytest.approx(1.125)  # (0.8-(-0.1))/0.8? wait giveback=max(0,mfe-pnl)=0.9, frac=0.9/0.8=1.125
    assert snap["mfe_capture"] == pytest.approx(-0.1 / 0.8)


def test_cortex_excursion_stats(tmp_path, monkeypatch):
    import hermes_core.engines.decision_cortex as dc

    monkeypatch.setattr(dc, "CORTEX_DIR", tmp_path / "cortex")
    monkeypatch.setattr(dc, "EXILE_PATH", tmp_path / "cortex" / "exile.json")
    monkeypatch.setattr(dc, "MEMORY_PATH", tmp_path / "cortex" / "mem.json")
    c = dc.Cortex(bot="forex")
    c.record_outcome(
        "EUR/USD", "mean_reversion", 0.5,
        mfe_pct=2.0, mae_pct=-0.3, giveback_pct=1.5, giveback_frac=0.75,
    )
    c.record_outcome(
        "EUR/USD", "mean_reversion", 1.0,
        mfe_pct=1.5, mae_pct=-0.1, giveback_pct=0.5, giveback_frac=0.33,
    )
    st = c.excursion_stats("EUR/USD", "mean_reversion")
    assert st["n"] == 2
    assert st["avg_giveback_frac"] == pytest.approx((0.75 + 0.33) / 2, rel=1e-3)
    assert st["avg_mfe_capture"] is not None
    board = c.summary()["excursion"]
    assert board["n"] == 2
    assert board["avg_mfe_capture"] is not None
    assert "mean_reversion" in board["by_entry_type"]


def test_exit_intel_high_giveback_tightens():
    def edge(*_a, **_k):
        return {
            "wins": 10, "losses": 5, "n": 15,
            "avg_win": 2.0, "avg_loss": 1.0,
        }

    def exc(*_a, **_k):
        return {
            "n": 5, "avg_mfe": 2.0, "avg_mae": -0.5,
            "avg_giveback": 1.0, "avg_giveback_frac": 0.55,
            "avg_mfe_capture": 0.3,
        }

    cortex = SimpleNamespace(edge_stats=edge, excursion_stats=exc)
    out = apply_exit_intel(
        enabled=True,
        pair="EUR/USD",
        entry_type="mean_reversion",
        strategy={},
        cortex=cortex,
    )
    assert out["exit_intel_mode"] == "soft"
    assert out["be_trigger_frac"] == pytest.approx(0.35)
    assert any("high_giveback" in r for r in out["exit_intel_reasons"])
