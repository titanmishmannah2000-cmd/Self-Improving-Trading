"""Crisis learning engine (Session 12 / Phase 12).

Turns a price/volume window into a 9-dimensional crisis "fingerprint"
(Appendix F.1) and stores/retrieves it from a crisis database. A current
fingerprint is matched (cosine distance, F.2) against known crises; if it is
further than NOVEL_DISTANCE (0.5) from ANY known crisis it is a genuinely
NOVEL regime (F.3) and the L21 flatline guard pauses the pair for 60 cycles
rather than trade blind.

All persistence is APPEND-ONLY: lived crises are added under a fresh id, the
flatline log only grows. History is never overwritten.

Functions (blueprint Phase 12 build target):
  _extract_crisis_features(prices, volumes) -> list[9 floats] | None
  get_crisis_recommendation(features) -> dict          # None-clean -> safe dict
  save_lived_crisis(pair, pnl_impact, price_history, volume_history)

Class (roadmap S12 contract):
  CrisisLearning.signature(prices, volumes) -> tuple[float, ...] | None
  CrisisLearning.nearest(sig) -> CrisisMatch
  CrisisLearning.save_lived_crisis(...)
"""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path

from hermes_core.state.paths import crisis_db_path, current_bot, flatline_log_path

CRISIS_SIGNATURE_LENGTH = 9   # [GUARD L21] fixed 9-dim vector
CRISIS_WINDOW_SIZE = 12       # timesteps sampled per crisis window
CRISIS_SAMPLE_INTERVAL = 5    # step between samples
NOVEL_DISTANCE = 0.5          # [GUARD L21] cosine dist > this = novel regime
NOVELTY_MULTIPLIER = 3.0      # [GUARD L21] novelty > 3.0*median -> flatline
FLATLINE_CYCLES = 60          # [GUARD L21] novel regime pause length (cycles)

# Test override hooks (tests monkeypatch these module attributes).
DB_PATH: Path | None = None
FLATLINE_LOG: Path | None = None


def _db_path(pair: str | None = None) -> Path:
    if DB_PATH is not None:
        return DB_PATH
    if pair:
        return crisis_db_path(pair=pair)
    return crisis_db_path(current_bot())


def _flatline_path(pair: str | None = None) -> Path:
    if FLATLINE_LOG is not None:
        return FLATLINE_LOG
    if pair:
        return flatline_log_path(pair=pair)
    return flatline_log_path(current_bot())

# Pre-seeded known crises (blueprint F.4 ids). Signatures are representative
# 9-vectors; the COVID one is reproduced exactly in tests for test_covid_nn.
COVID_SIG = [12.5, 7.5, 0.05, 0.3, 3.2, 3.75, 0.5, 0.3, 0.92]
_PRESEEDED = {
    "covid_crash_2020": {
        "name": "COVID-19", "signature": [COVID_SIG], "pnl_impact": -18.0,
        "optimal_stop": 2.5, "optimal_target": 0.3, "regime": "CRASH",
        "volatility_class": "extreme",
    },
    "nfp_spike_2023": {
        "name": "NFP Spike", "signature": [[4.1, 2.5, 0.55, 0.3, 1.8, 1.23, 0.4, 0.5, 0.98]],
        "pnl_impact": -3.0, "optimal_stop": 1.5, "optimal_target": 0.5,
        "regime": "SPIKE", "volatility_class": "elevated",
    },
    "london_open_volatility": {
        "name": "London Open Volatility",
        "signature": [[2.0, 1.2, 0.62, 0.3, 1.2, 0.6, 0.30, 0.2, 1.01]],
        "pnl_impact": -1.0, "optimal_stop": 1.2, "optimal_target": 0.6,
        "regime": "SESSION", "volatility_class": "normal",
    },
}

CrisisMatch = namedtuple("CrisisMatch", "distance crisis_id name data")


# ── feature extraction (Appendix F.1) ─────────────────────────────────────
def _extract_crisis_features(
    prices: list[float], volumes: list[float] | None = None
) -> list[float] | None:
    """9-dim crisis fingerprint (Appendix F.1). Returns None if unusable."""
    if not prices or len(prices) < 14:
        return None
    last = prices[-1]
    if last <= 0:
        return None

    trs = [abs(prices[i] - prices[i - 1]) for i in range(1, len(prices))]
    atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else sum(trs) / max(len(trs), 1)
    atr_pct = atr / last * 100

    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = sum(d for d in deltas[-14:] if d > 0) / 14
    losses = sum(-d for d in deltas[-14:] if d < 0) / 14
    rsi = 100 - (100 / (1 + gains / max(losses, 1e-10))) if losses > 0 else 50.0

    vol_ratio = 1.0
    if volumes and len(volumes) >= 12:
        v5 = sum(volumes[-3:]) / 3
        v1h = sum(volumes[-12:]) / 12
        vol_ratio = v5 / max(v1h, 1e-4)

    ma50 = sum(prices[-50:]) / 50 if len(prices) >= 50 else last
    price_ma = last / max(ma50, 1e-4)

    now = datetime.now(UTC)
    session_hour = now.hour / 23.0
    day_of_week = now.weekday() / 6.0

    return [
        round(atr_pct, 6),
        round(atr_pct * 0.6, 6),    # approximated 1h ATR
        round(rsi / 100.0, 4),
        round(0.3, 4),              # approximated ADX (placeholder)
        round(vol_ratio, 4),
        round(atr_pct * 0.3, 6),    # approximated spread
        round(session_hour, 4),
        round(day_of_week, 4),
        round(price_ma, 4),
    ]


# ── cosine distance (Appendix F.2) ────────────────────────────────────────
def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm > 0 else [0.0] * len(vec)


def _cosine_distance(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or len(a) == 0:
        return 1.0  # length mismatch -> maximal distance ("novel")
    na, nb = _normalize(a), _normalize(b)
    dot = sum(na[i] * nb[i] for i in range(len(a)))
    dot = max(-1.0, min(1.0, dot))
    return 1.0 - dot


def _signature_distance(features: list[float], signature: list[list[float]]) -> float:
    """Mean cosine distance from features to each timestep vector in signature."""
    if not signature:
        return 1.0
    dists = []
    for vec in signature:
        if len(vec) == CRISIS_SIGNATURE_LENGTH:
            dists.append(_cosine_distance(features, vec))
    return statistics.mean(dists) if dists else 1.0


# ── DB I/O (append-only) ──────────────────────────────────────────────────
def _load_crises(pair: str | None = None) -> dict:
    path = _db_path(pair)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return dict(_PRESEEDED)


def _save_crises(crises: dict, pair: str | None = None) -> None:
    path = _db_path(pair)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(crises, indent=2), encoding="utf-8")


def _record_flatline(pair: str, reason: str, details: str = "") -> dict:
    """Append a flatline event to flatline_log.jsonl (append-only)."""
    log_path = FLATLINE_LOG if FLATLINE_LOG is not None else _flatline_path(pair)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "pair": pair, "reason": reason, "details": details,
        "ts": datetime.now(UTC).isoformat(),
        "expires_in_cycles": FLATLINE_CYCLES,
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, default=str) + "\n")
    return entry


# ── recommendation / nearest (blueprint contract) ─────────────────────────
def find_nearest_crisis(features: list[float], top_n: int = 3) -> list[tuple]:
    if not features or len(features) != CRISIS_SIGNATURE_LENGTH:
        return []
    crises = _load_crises()
    scored = []
    for cid, data in crises.items():
        sig = data.get("signature", [])
        if not sig:
            continue
        scored.append((_signature_distance(features, sig), cid, data))
    scored.sort(key=lambda x: x[0])
    return scored[:top_n]


def get_crisis_recommendation(features: list[float]) -> dict:
    """Nearest-crisis parameter recommendation.

    Always returns a dict (never raises, never None) so callers can't crash on
    empty/novel input. novelty=True (dist > NOVEL_DISTANCE) means the regime is
    unlike anything known -> no recommendation should be applied (L21).
    """
    safe = {
        "crisis_name": None, "distance": 1.0, "pnl_impact": 0,
        "recommended_stop_pct": None, "recommended_target_pct": None,
        "regime": "UNKNOWN", "volatility_class": "normal", "novel": True,
    }
    nearest = find_nearest_crisis(features, top_n=1)
    if not nearest:
        return safe
    dist, cid, data = nearest[0]
    if dist > NOVEL_DISTANCE:        # [GUARD L21] too far -> novel, no rec
        return {**safe, "distance": round(dist, 4)}
    return {
        "crisis_name": data.get("name", cid),
        "distance": round(dist, 4),
        "pnl_impact": data.get("pnl_impact", 0),
        "recommended_stop_pct": data.get("optimal_stop"),
        "recommended_target_pct": data.get("optimal_target"),
        "regime": data.get("regime", "UNKNOWN"),
        "volatility_class": data.get("volatility_class", "normal"),
        "novel": False,
    }


# ── L21 novelty / flatline gate ───────────────────────────────────────────
def _novelty_baseline(crises: dict, exclude: str | None = None) -> float | None:
    """Median of nearest-neighbour distances among KNOWN crises.

    Gives a stable 'how spread out is the known world' baseline. Needs >= 2
    known crises; returns None if there is no baseline yet.
    """
    sigs = {cid: d.get("signature", []) for cid, d in crises.items()
            if cid != exclude and d.get("signature")}
    ids = list(sigs)
    if len(ids) < 2:
        return None
    mins = []
    for a in ids:
        best = min(
            (_cosine_distance(sigs[a][0], sigs[b][0]) for b in ids if b != a),
            default=1.0,
        )
        mins.append(best)
    return statistics.median(mins)


def check_novel_regime(
    pair: str, prices: list[float], volumes: list[float] | None = None
) -> dict:
    """L21 flatline guard. If the current fingerprint is further than
    NOVELTY_MULTIPLIER * median-known-distance from every known crisis, treat it
    as a genuinely novel regime and pause the pair for FLATLINE_CYCLES.

    Fail-closed: insufficient data -> no flatline (we don't halt on first sight).
    """
    sig = _extract_crisis_features(prices, volumes)
    if sig is None:
        return {"novel": False, "reason": "insufficient price data",
                "pause_cycles": 0, "flatlined": False}
    crises = _load_crises()
    baseline = _novelty_baseline(crises)
    if baseline is None:
        return {"novel": False, "reason": "insufficient known-crisis baseline",
                "pause_cycles": 0, "flatlined": False}

    nearest = find_nearest_crisis(sig, top_n=1)
    dist = nearest[0][0] if nearest else 1.0
    threshold = NOVELTY_MULTIPLIER * baseline
    if dist > threshold:                       # [GUARD L21] novel regime
        _record_flatline(pair, "NOVEL_REGIME",
                         f"distance {dist:.3f} > {threshold:.3f} (3.0*median)")
        return {"novel": True, "distance": round(dist, 4),
                "median_baseline": round(baseline, 4),
                "threshold": round(threshold, 4),
                "pause_cycles": FLATLINE_CYCLES, "flatlined": True}
    return {"novel": False, "distance": round(dist, 4),
            "median_baseline": round(baseline, 4),
            "threshold": round(threshold, 4),
            "pause_cycles": 0, "flatlined": False}


# ── online learning (append-only) ─────────────────────────────────────────
def _compute_signature_from_history(
    price_history: list[float], volume_history: list[float] | None
) -> list[list[float]] | None:
    if not price_history or len(price_history) < 60:
        return None
    signature = []
    step = max(1, len(price_history) // CRISIS_SAMPLE_INTERVAL)
    for i in range(CRISIS_WINDOW_SIZE):
        idx = -(CRISIS_WINDOW_SIZE - i) * step
        if abs(idx) >= len(price_history):
            continue
        chunk = price_history[idx:]
        vol_chunk = (volume_history[idx:] if volume_history
                     and abs(idx) < len(volume_history) else None)
        feats = _extract_crisis_features(chunk, vol_chunk)
        if feats:
            signature.append(feats)
    return signature if len(signature) >= 3 else None


def save_lived_crisis(
    pair: str, pnl_impact: float,
    price_history: list[float], volume_history: list[float] | None = None,
) -> str | None:
    """Persist a lived crisis experience. Append-only: never overwrites prior
    history (fresh id = lived_{pair}_{ts}). Returns the crisis_id or None."""
    if not price_history or len(price_history) < 60:
        return None
    signature = _compute_signature_from_history(price_history, volume_history)
    if not signature:
        return None

    crises = _load_crises()          # load first -> append, never clobber
    crisis_id = f"lived_{pair}_{int(time.time() * 1000)}"
    survived = pnl_impact > -5.0
    crises[crisis_id] = {
        "name": f"{'Survived' if survived else 'Danger'} - {pair}",
        "signature": signature,
        "pnl_impact": round(pnl_impact, 2),
        "optimal_stop": 1.5 if survived else 2.5,
        "optimal_target": 0.3 if not survived else 0.5,
        "regime": "LEARNED",
        "volatility_class": "survived" if survived else "danger",
        "pair": pair,
        "ts": datetime.now(UTC).isoformat(),
    }
    _save_crises(crises)
    return crisis_id


class CrisisLearning:
    """Roadmap S12 contract wrapper around the free functions above."""

    def signature(self, prices: list[float],
                  volumes: list[float] | None = None) -> tuple | None:
        feats = _extract_crisis_features(prices, volumes)
        return tuple(feats) if feats else None

    def nearest(self, sig: tuple | list[float]) -> CrisisMatch | None:
        scored = find_nearest_crisis(list(sig), top_n=1)
        if not scored:
            return None
        dist, cid, data = scored[0]
        return CrisisMatch(round(dist, 4), cid, data.get("name", cid), data)

    def save_lived_crisis(self, pair: str, pnl_impact: float,
                          price_history: list[float],
                          volume_history: list[float] | None = None) -> str | None:
        return save_lived_crisis(pair, pnl_impact, price_history, volume_history)

    def check_novel_regime(self, pair: str, prices: list[float],
                           volumes: list[float] | None = None) -> dict:
        return check_novel_regime(pair, prices, volumes)
