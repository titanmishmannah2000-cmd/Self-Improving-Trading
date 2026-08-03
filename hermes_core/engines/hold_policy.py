"""Small inspectable hold policy (L3) + optional deep stub (L7e)."""

from __future__ import annotations

import json
from pathlib import Path

from hermes_core.env import get_env


def hold_model_mode() -> str:
    return (get_env("HOLD_MODEL", "small") or "small").strip().lower()


def default_weights() -> list[float]:
    return [0.45, 0.35, 0.20]


def load_hold_policy(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {"n": 0, "weights": default_weights(), "p_hit_bias": 0.0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"n": 0, "weights": default_weights(), "p_hit_bias": 0.0}
        return data
    except Exception:  # noqa: BLE001
        return {"n": 0, "weights": default_weights(), "p_hit_bias": 0.0}


def save_hold_policy(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def ema_update_weights(
    weights: list[float],
    *,
    progress: float,
    fresh: float,
    capture: float,
    y_hold: float,
    lr: float = 0.05,
) -> list[float]:
    """Nudge weights toward features that align with hold/bank label."""
    w = list(weights) if weights and len(weights) == 3 else default_weights()
    feats = [progress, fresh, capture]
    # If should hold, reinforce high fresh+progress; if bank, reinforce low fresh
    target = feats if y_hold >= 0.5 else [1.0 - f for f in feats]
    out = []
    for wi, ti in zip(w, target):
        out.append(max(0.1, min(0.6, wi * (1 - lr) + ti * lr)))
    s = sum(out) or 1.0
    return [x / s for x in out]


def predict_p_hold(features: dict, policy: dict) -> float:
    """Logistic-ish score from weights + optional deep coeffs."""
    w = policy.get("weights") or default_weights()
    if len(w) != 3:
        w = default_weights()
    progress = float(features.get("progress") or 0.0)
    fresh = float(features.get("fresh") or 0.0)
    capture = float(features.get("capture") or 0.0)
    base = w[0] * progress + w[1] * fresh + w[2] * capture
    if hold_model_mode() == "deep" and policy.get("deep_coeffs"):
        c = policy["deep_coeffs"]
        # extra: atr, funding, oi, dist_res
        extra = (
            float(c.get("atr", 0)) * float(features.get("atr_pct") or 0)
            + float(c.get("funding", 0)) * float(features.get("funding") or 0)
            + float(c.get("oi", 0)) * float(features.get("oi_z") or 0)
            + float(c.get("dist", 0)) * float(features.get("dist_res") or 0)
        )
        base = 0.7 * base + 0.3 * (0.5 + extra)
    return max(0.0, min(1.0, float(base)))


def predict_p_hit_tp(features: dict, policy: dict) -> float:
    """L6/L7 short-horizon TP hit probability prior."""
    p_hold = predict_p_hold(features, policy)
    bias = float(policy.get("p_hit_bias") or 0.0)
    progress = float(features.get("progress") or 0.0)
    return max(0.0, min(1.0, 0.5 * p_hold + 0.4 * progress + bias))


def fit_from_labels(policy: dict, labels: list[dict], features_rows: list[dict]) -> dict:
    """EMA-fit weights from counterfactual labels (offline)."""
    w = list(policy.get("weights") or default_weights())
    n = int(policy.get("n") or 0)
    for lab, feat in zip(labels, features_rows):
        w = ema_update_weights(
            w,
            progress=float(feat.get("progress") or 0),
            fresh=float(feat.get("fresh") or 0),
            capture=float(feat.get("capture") or 0),
            y_hold=float(lab.get("y_hold") or 0),
        )
        n += 1
    out = dict(policy)
    out["weights"] = w
    out["n"] = n
    return out
