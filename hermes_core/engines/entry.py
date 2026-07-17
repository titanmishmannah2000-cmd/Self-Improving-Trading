"""Entry engine (Session 4 / Phase 4) — pure signal evaluator.

Single implementation shared by the live loop, the backtester, and the dashboard
export (discipline 1.5 + roadmap S4). NO I/O, NO network, NO hidden state:
given a price series, a resolved strategy, market context and cycle bookkeeping,
it returns either a ``Signal`` or ``None``.

Guards enforced here (tagged so tools/verify_guard_tags.py can find them):
  L04  session filter (MR only inside its session window)
  L13  ensemble-context skip — an MR long is blocked when the discovered-indicator
       ensemble consensus is bearish/strong_bearish (the v06→v07 cliff guard)
  L15  re-entry cooldown — stopped-out pair may not re-enter within 30 cycles
  L18  multi-pair confluence — RSI-momentum needs >=2 oversold pairs
  L23  stop-loss cooldown — a stop-loss exit blocks re-entry for 30 cycles
  L14  chart hard-block — context containing "avoid"/"downtrend" -> skip (from chart vision)
  L16  chart soft-filter — context containing "sell" + low quality (<5) -> skip
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hermes_core.engines.chart_vision import hard_block, soft_block
from hermes_core.indicators import compute_all

# Session tokens resolved upstream by _get_session(): LDN/NY/ASIA/OTHER.
# Maps a strategy's session_filter to the allowed token set.
_SESSION_MAP: dict[str, set[str]] = {
    "london_only": {"LDN"},
    "ny_only": {"NY"},
    "asian_only": {"ASIA"},
    "24h": {"LDN", "NY", "ASIA", "OTHER"},
}

# Ensemble consensus values that forbid an MR long (L13).
_BEARISH_CONSENSUS = {"bearish", "strong_bearish"}

REENTRY_COOLDOWN_CYCLES = 30  # L15 / L23


@dataclass
class Signal:
    type: str                 # "mean_reversion" | "rsi_momentum"
    quality: float            # 0..1 composite quality score
    size: float               # position size fraction (from strategy)
    pair: str = ""
    meta: dict = field(default_factory=dict)


def _session_allowed(strategy: dict, session_token: str) -> bool:
    """[GUARD L04] MR/RSI entries only inside the strategy's session window."""
    filt = strategy.get("session_filter", "24h")
    allowed = _SESSION_MAP.get(filt, {"LDN", "NY", "ASIA", "OTHER"})
    return session_token in allowed


def _cooldown_active(reentry: dict, pair: str, current_cycle: int) -> bool:
    """[GUARD L15]/[GUARD L23] Re-entry blocked within 30 cycles of last exit."""
    rec = (reentry or {}).get(pair)
    if not rec:
        return False
    last = rec.get("last_exit_cycle")
    if last is None:
        return False
    return (current_cycle - last) < REENTRY_COOLDOWN_CYCLES


def evaluate_entry(
    pair: str,
    prices: list[float],
    strategy: dict,
    context: str = "",
    ensemble_consensus: str = "neutral",
    oversold_pairs: int = 0,
    vol_above: bool = False,
    reentry: dict | None = None,
    current_cycle: int = 0,
    session_token: str = "LDN",
) -> Signal | None:
    """Evaluate a single entry. Returns a Signal or None.

    Pure: identical args -> identical result. The live loop supplies
    ``session_token`` (from _get_session) and ``current_cycle``; tests pass them
    directly for determinism.
    """
    if not prices or not strategy:
        return None

    # [GUARD L14] chart hard-block: vision flagged this asset as untradeable.
    if hard_block(context):
        return None

    # [GUARD L16] chart soft-filter: a low-quality "sell" -> skip (weaker than L14).
    if soft_block(context):
        return None

    # [GUARD L04] session window
    if not _session_allowed(strategy, session_token):
        return None

    # [GUARD L15]/[GUARD L23] re-entry cooldown
    if _cooldown_active(reentry, pair, current_cycle):
        return None

    ind = compute_all(prices)
    rsi = ind["rsi"]
    adx = ind["adx"]
    bb = ind["bb"]
    last = prices[-1]

    stype = strategy.get("strategy_type")
    threshold = (strategy.get("entry") or {}).get("threshold", 50)
    size = strategy.get("position_size_r", 0.1)

    if stype == "mean_reversion":
        # [GUARD L13] ensemble-context skip — the v06->v07 cliff guard.
        if ensemble_consensus in _BEARISH_CONSENSUS:
            return None
        at_band = last <= bb["lower"]
        oversold = rsi <= threshold
        calm = adx < 25  # range regime favours reversion
        if at_band and oversold and calm:
            quality = (1 - rsi / 100.0) * 0.6 + 0.4
            return Signal("mean_reversion", round(quality, 4), size, pair,
                          {"rsi": rsi, "adx": adx, "bb_lower": bb["lower"]})
        return None

    if stype == "rsi_momentum":
        # [GUARD L18] multi-pair confluence gate
        if oversold_pairs < 2:
            return None
        oversold = rsi <= threshold
        if oversold and vol_above:
            quality = 0.5 + min(oversold_pairs, 5) * 0.1
            return Signal("rsi_momentum", round(quality, 4), size, pair,
                          {"rsi": rsi, "oversold_pairs": oversold_pairs})
        return None

    return None
