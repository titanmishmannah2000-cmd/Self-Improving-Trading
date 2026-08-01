"""Venue-aware trading cost model (BTC/USDT Focus Phase 1).

Replaces flat FX-style ``SCORECARD_COST_PCT=0.05`` for BTC with maker/taker
fees + ATR-scaled slippage. FX/gold keep the scorecard flat fallback.

All percents are **percent of notional** (0.1 == 0.1%), matching scorecard /
promote / backtest conventions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from hermes_core.adapters.pair_aliases import feed_pair, is_crypto_pair
from hermes_core.env import get_env

# Binance-spot-like retail VIP0 defaults (conservative: treat both legs as taker).
DEFAULT_BTC_MAKER_FEE_PCT = 0.1
DEFAULT_BTC_TAKER_FEE_PCT = 0.1
DEFAULT_SLIPPAGE_FLOOR_BPS = 1.0  # 1 bp = 0.01%
DEFAULT_SLIPPAGE_ATR_K = 0.05
DEFAULT_COST_STRESS_MULT = 2.0
# FX/gold flat fallback (legacy scorecard default).
DEFAULT_FLAT_COST_PCT = 0.05


def _fenv(name: str, default: float) -> float:
    raw = get_env(name, "")
    if not str(raw).strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def is_btc_pair(pair: str | None) -> bool:
    p = (pair or "").strip().upper()
    if not p:
        return False
    return p.startswith("BTC/") or feed_pair(p) == "BTC/USD"


def stress_mult() -> float:
    return max(1.0, _fenv("COST_STRESS_MULT", DEFAULT_COST_STRESS_MULT))


def btc_maker_fee_pct() -> float:
    return max(0.0, _fenv("BTC_MAKER_FEE_PCT", DEFAULT_BTC_MAKER_FEE_PCT))


def btc_taker_fee_pct() -> float:
    return max(0.0, _fenv("BTC_TAKER_FEE_PCT", DEFAULT_BTC_TAKER_FEE_PCT))


def slippage_floor_pct() -> float:
    """Floor slippage as percent (env is bps)."""
    bps = max(0.0, _fenv("BTC_SLIPPAGE_FLOOR_BPS", DEFAULT_SLIPPAGE_FLOOR_BPS))
    return bps / 100.0  # 1 bp → 0.01%


def slippage_atr_k() -> float:
    return max(0.0, _fenv("BTC_SLIPPAGE_ATR_K", DEFAULT_SLIPPAGE_ATR_K))


def flat_fallback_pct() -> float:
    """FX/gold (and unset) flat round-trip haircut."""
    raw = get_env("SCORECARD_COST_PCT", "")
    if not str(raw).strip():
        return DEFAULT_FLAT_COST_PCT
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_FLAT_COST_PCT


@dataclass(frozen=True)
class CostBreakdown:
    pair: str
    fee_pct_one_way: float
    slippage_pct_one_way: float
    round_trip_pct: float
    entry_haircut_pct: float
    exit_haircut_pct: float
    stress_mult: float
    stressed_round_trip_pct: float
    model: str  # "btc_venue" | "flat"

    def as_dict(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "fee_pct_one_way": self.fee_pct_one_way,
            "slippage_pct_one_way": self.slippage_pct_one_way,
            "round_trip_pct": self.round_trip_pct,
            "entry_haircut_pct": self.entry_haircut_pct,
            "exit_haircut_pct": self.exit_haircut_pct,
            "stress_mult": self.stress_mult,
            "stressed_round_trip_pct": self.stressed_round_trip_pct,
            "model": self.model,
        }


def one_way_slippage_pct(atr_pct: float | None = None) -> float:
    """Slippage one leg: max(floor, k * ATR%)."""
    floor = slippage_floor_pct()
    if atr_pct is None:
        return floor
    try:
        atr = max(0.0, float(atr_pct))
    except (TypeError, ValueError):
        return floor
    return max(floor, slippage_atr_k() * atr)


def estimate(
    pair: str | None = None,
    *,
    atr_pct: float | None = None,
    use_maker: bool = False,
) -> CostBreakdown:
    """Estimate round-trip cost for ``pair``.

    BTC uses venue fees + slippage. Other pairs use flat scorecard fallback
    (split evenly across entry/exit haircuts).
    """
    p = (pair or "").strip() or ""
    sm = stress_mult()
    if is_btc_pair(p) or (is_crypto_pair(p) and p.upper().startswith("BTC")):
        fee = btc_maker_fee_pct() if use_maker else btc_taker_fee_pct()
        slip = one_way_slippage_pct(atr_pct)
        one = fee + slip
        rt = 2.0 * one
        return CostBreakdown(
            pair=p or "BTC/USDT",
            fee_pct_one_way=fee,
            slippage_pct_one_way=slip,
            round_trip_pct=rt,
            entry_haircut_pct=one,
            exit_haircut_pct=one,
            stress_mult=sm,
            stressed_round_trip_pct=rt * sm,
            model="btc_venue",
        )
    flat = flat_fallback_pct()
    half = flat / 2.0
    return CostBreakdown(
        pair=p,
        fee_pct_one_way=half,
        slippage_pct_one_way=0.0,
        round_trip_pct=flat,
        entry_haircut_pct=half,
        exit_haircut_pct=half,
        stress_mult=sm,
        stressed_round_trip_pct=flat * sm,
        model="flat",
    )


def round_trip_pct(
    pair: str | None = None,
    *,
    atr_pct: float | None = None,
    stressed: bool = False,
) -> float:
    b = estimate(pair, atr_pct=atr_pct)
    return b.stressed_round_trip_pct if stressed else b.round_trip_pct


def apply_entry_fill(price: float, side: str, haircut_pct: float) -> float:
    """Adverse fill on entry. Long pays up; short sells down."""
    p = float(price)
    h = max(0.0, float(haircut_pct)) / 100.0
    s = (side or "long").lower()
    if s in ("short", "sell"):
        return p * (1.0 - h)
    return p * (1.0 + h)


def apply_exit_fill(price: float, side: str, haircut_pct: float) -> float:
    """Adverse fill on exit. Long sells down; short buys up."""
    p = float(price)
    h = max(0.0, float(haircut_pct)) / 100.0
    s = (side or "long").lower()
    if s in ("short", "sell"):
        return p * (1.0 + h)
    return p * (1.0 - h)


def net_pnl_pct(gross_pnl_pct: float, round_trip_pct_val: float) -> float:
    return float(gross_pnl_pct) - max(0.0, float(round_trip_pct_val))
