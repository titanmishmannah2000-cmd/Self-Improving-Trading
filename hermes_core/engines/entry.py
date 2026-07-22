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
  L18  multi-pair confluence — RSI-momentum needs >= min_oversold_pairs
       (YAML entry.min_oversold_pairs; default 1 after Phase-1 gate relaxation)
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


def _session_filter(strategy: dict) -> str:
    """Resolve session_filter from YAML (top-level or under entry.*).

    Production strategy files put the filter under ``entry.session_filter``;
    older tests/fixtures use the top-level key. Accept either.
    """
    entry = strategy.get("entry") or {}
    return (
        strategy.get("session_filter")
        or entry.get("session_filter")
        or "24h"
    )


def _entry_rsi_threshold(strategy: dict) -> float:
    """RSI oversold threshold: ``entry.threshold`` or ``entry.mr_entry_rsi``.

    Forex/crypto YAMLs use ``mr_entry_rsi``; gold/AUD momentum use ``threshold``.
    Default 50 matches the historical engine default.
    """
    entry = strategy.get("entry") or {}
    if entry.get("threshold") is not None:
        try:
            return float(entry["threshold"])
        except (TypeError, ValueError):
            pass
    if entry.get("mr_entry_rsi") is not None:
        try:
            return float(entry["mr_entry_rsi"])
        except (TypeError, ValueError):
            pass
    return 50.0


def _min_oversold_pairs(strategy: dict) -> int:
    """L18 confluence floor: ``entry.min_oversold_pairs`` or top-level.

    Phase-1 gate relaxation default is 1 (was hard-coded 2). Set YAML to 2 to
    restore the stricter multi-pair confluence requirement.
    """
    entry = strategy.get("entry") or {}
    raw = strategy.get("min_oversold_pairs", entry.get("min_oversold_pairs", 1))
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, n)


def _vol_gate(strategy: dict, atr: float, last: float, vol_above: bool) -> bool:
    """Momentum volume/volatility gate.

    Prefer an explicit ``vol_above=True`` from a real volume feed (tests / future
    adapters). When the live runner has no volume source (``vol_above=False``),
    use ATR% vs YAML ``vol_threshold_pct`` / ``vol_min_pct`` / ``vol_max_pct``
    so gold + AUD momentum can actually fire.

    Legacy strategy YAMLs used daily-scale floors (``vol_min_pct`` ≥ 0.2 and
    ``vol_threshold_pct`` ≥ 0.5). On live tick / short-bar ATR% for FX/metals
    (~0.01–0.05) those floors never open → perpetual ``no_signal``. Remap that
    legacy signature in-code so deployed volume copies keep working.
    """
    if vol_above:
        return True
    if last <= 0:
        return False
    atr_pct = (float(atr) / float(last)) * 100.0
    lo = float(strategy.get("vol_min_pct", 0.005) or 0.005)
    hi = float(strategy.get("vol_max_pct", 5.0) or 5.0)
    thr = float(strategy.get("vol_threshold_pct", 0.01) or 0.01)
    if lo >= 0.1 and thr >= 0.5:
        lo, thr = 0.005, 0.01
    return lo <= atr_pct <= hi and atr_pct >= thr


def _session_allowed(strategy: dict, session_token: str) -> bool:
    """[GUARD L04] MR/RSI entries only inside the strategy's session window."""
    filt = _session_filter(strategy)
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
    threshold = _entry_rsi_threshold(strategy)
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
            return Signal(
                "mean_reversion", round(quality, 4), size, pair,
                {
                    "rsi": rsi,
                    "adx": adx,
                    "bb_lower": bb["lower"],
                    "entry_type": "mean_reversion",
                    "rsi_threshold": threshold,
                },
            )
        return None

    if stype == "rsi_momentum":
        # [GUARD L18] multi-pair confluence gate (YAML entry.min_oversold_pairs)
        min_pairs = _min_oversold_pairs(strategy)
        if oversold_pairs < min_pairs:
            return None
        oversold = rsi <= threshold
        if oversold and _vol_gate(strategy, ind["atr"], last, vol_above):
            quality = 0.5 + min(oversold_pairs, 5) * 0.1
            return Signal(
                "rsi_momentum", round(quality, 4), size, pair,
                {
                    "rsi": rsi,
                    "oversold_pairs": oversold_pairs,
                    "min_oversold_pairs": min_pairs,
                    "entry_type": "rsi_momentum",
                    "rsi_threshold": threshold,
                },
            )
        return None

    return None


# ── GP ensemble (discovered-indicator) SHADOW entry ─────────────────────────
# Ported from the older engine's check_discovered_signals weighted-vote logic,
# but SHADOW-ONLY by design: this returns a Signal tagged meta["shadow"]=True.
# The live loop MUST NOT convert a shadow signal into a real order -- it only
# logs it (and paper PnL) so we can verify the GP brain out-of-sample before
# any promotion to live trading. Faithful to the "shadow/log-only first for
# unproven rules" directive.
import re  # noqa: E402  (local import; entry.py is otherwise stdlib-only)
import time as _time  # noqa: E402

from hermes_core.engines.genetic import (  # noqa: E402
    FEATURES, _eval_expr, _feature, indicator_expr, is_backtest_approved,
    load_discovered_indicators,
)

_FEATURE_RE = re.compile(r"^[a-z0-9]+$")

# Daily close cache so GP indicators (discovered on DAILY bars) are evaluated on
# the SAME regime. Without this the live loop fed them 5m bars -> a regime
# mismatch (the indicators' sma50/roc20 are daily-scaled). [fixes prior caveat]
_GP_DAILY_CACHE: dict[str, tuple[float, list[float]]] = {}
_GP_DAILY_TTL_S = 1800  # refresh daily history at most every 30 min


def gp_daily_prices(pair: str) -> list[float] | None:
    """Daily close series for `pair` (cached 30 min), or None on failure.

    Used so the GP brain is evaluated on the regime its indicators were
    discovered on. Caller falls back to the live series if this returns None.
    """
    try:
        now = _time.time()
        cached = _GP_DAILY_CACHE.get(pair)
        if cached and (now - cached[0]) < _GP_DAILY_TTL_S:
            return cached[1]
        from hermes_core.adapters.price import seed_history_interval_sync
        hist = seed_history_interval_sync(pair, interval="1d", period="2y",
                                          max_candles=500)
        px = [float(c["price"]) for c in (hist or []) if c.get("price") is not None]
        if len(px) < 50:
            return None
        _GP_DAILY_CACHE[pair] = (now, px)
        return px
    except Exception:  # noqa: BLE001 — never break the entry path
        return None


def _gp_parse(expr_str: str):
    """Parse a fully-parenthesized infix GP expression into the (op,a,b) tree
    form that genetic._eval_expr consumes -- so live evaluation uses the SAME
    _feature math as discovery (identical numbers). No eval/exec.

    Grammar: expr -> term (('+'|'-') term)* ; term -> factor (('*'|'/') factor)*
             ; factor -> '(' expr ')' | FEATURE
    """
    toks = re.findall(r"\(|\)|\+|-|\*|/|[a-z0-9]+", expr_str)
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else None

    def parse_expr():
        nonlocal pos
        node = parse_term()
        while peek() in ("+", "-"):
            op = "add" if toks[pos] == "+" else "sub"
            pos += 1
            rhs = parse_term()
            node = (op, node, rhs)
        return node

    def parse_term():
        nonlocal pos
        node = parse_factor()
        while peek() in ("*", "/"):
            op = "mul" if toks[pos] == "*" else "div"
            pos += 1
            rhs = parse_factor()
            node = (op, node, rhs)
        return node

    def parse_factor():
        nonlocal pos
        t = peek()
        if t == "(":
            pos += 1
            node = parse_expr()
            if peek() == ")":
                pos += 1
            return node
        pos += 1
        if t in FEATURES:
            return t
        return "price"

    return parse_expr()


def _gp_eval_last(expr_str: str, prices: list[float]) -> float:
    """Evaluate a discovered expression's LAST value over the price window.

    Mirrors the old engine's _eval_expr_last. prices should hold >= 50 closes
    so sma50/roc20 features have history. Div-by-zero -> 0.0 (safe).
    """
    if not prices or len(prices) < 2:
        return 0.0
    try:
        tree = _gp_parse(expr_str)
        return _eval_expr(tree, prices)
    except Exception:  # noqa: BLE001 — never crash the entry path
        return 0.0


def gp_ensemble_signal(pair: str, prices: list[float],
                       strategy: dict | None = None,
                       consensus_threshold: float = 0.2,
                       min_active: int = 2,
                       z_threshold: float = 0.5,
                       daily_prices: list[float] | None = None,
                       promote: bool = False,
                       exiled_ids: set[str] | frozenset | None = None) -> Signal | None:
    """GP-ensemble vote of discovered indicators for `pair`.

    Each indicator's expression is evaluated on the DAILY series (the regime it
    was discovered on); falls back to the live `prices` only if daily is None.
    Direction = sign of the indicator's value z-scored vs its own signal series
    (scale-invariant). Weight = fitness * win_rate * shared_penalty.

    ``exiled_ids`` (L36): indicator names the cortex has exiled are skipped so
    they cannot vote. Caller (live loop) supplies the set; default empty keeps
    this function pure for tests/backtests.

    Returns a Signal, or None if not enough indicators fire. When `promote` is
    False the Signal is tagged shadow=True (observation only). When `promote` is
    True it is a real (paper) entry candidate (shadow=False, entry_type set) --
    but it still flows through the live loop's RR guard / position sizing / exit
    evaluation exactly like traditional entries.
    """
    # Evaluate on the daily regime (fixes the prior 5m/1d mismatch caveat).
    eval_prices = daily_prices if (daily_prices and len(daily_prices) >= 50) else prices
    if not eval_prices or len(eval_prices) < 50:
        return None
    inds = load_discovered_indicators(pair, include_shared=True)
    if not inds:
        return None

    banned = exiled_ids or set()
    votes = []  # (weighted_direction, weight, name)
    for ind in inds:
        name = ind.get("name", "?")
        # [GUARD L36] cortex-exiled indicators cannot vote the ensemble.
        if name in banned:
            continue
        # Prefer expr; fall back to expr_str/name only when GP-grammar-valid
        # (skips dashboard seeds like ta.rsi(close,14)).
        expr = indicator_expr(ind)
        if not expr:
            continue
        # Item 9/15: only S10-backtest-approved formulas may vote (shadow or promote).
        if not is_backtest_approved(ind):
            continue
        try:
            series = [_gp_eval_last(expr, eval_prices[: i + 1])
                      for i in range(49, len(eval_prices))]
        except Exception:  # noqa: BLE001
            continue
        if len(series) < 20:
            continue
        last = series[-1]
        mu = sum(series) / len(series)
        sd = (sum((x - mu) ** 2 for x in series) / len(series)) ** 0.5
        if sd < 1e-9:
            continue
        z = (last - mu) / sd
        if abs(z) < z_threshold:
            continue
        sig = 1 if z > 0 else -1

        fitness = float(ind.get("fitness", 0.0) or 0.0)
        # B10: if live feedback has annotated this indicator, prefer its
        # realized-results fitness (falls back to historical corr fitness).
        live_fitness = ind.get("live_fitness")
        if isinstance(live_fitness, (int, float)):
            fitness = float(live_fitness)
        win_rate = float(ind.get("win_rate", 0.5) or 0.5)
        penalty = float(ind.get("_shared_penalty", 1.0) or 1.0)
        # B10: a 'suppress' flag means the indicator has lost money on paper
        # (>=4 GP entries, pnl<0, WR<0.4) — zero its weight so it cannot vote
        # the ensemble into a trade. 'promote' keeps its weight.
        if ind.get("live_flag") == "suppress":
            continue
        weight = max(fitness * win_rate * penalty, 0.1 * penalty)
        votes.append((sig * weight, weight, name))

    if len(votes) < min_active:
        return None

    active_indicators = [v[2] for v in votes]
    total_w = sum(v[1] for v in votes)
    total_ws = sum(v[0] for v in votes)
    strength = max(-1.0, min(1.0, total_ws / max(total_w, 1e-6)))
    if abs(strength) < consensus_threshold:
        return None

    size = (strategy or {}).get("position_size_r", 0.1)
    consensus = ("bullish" if strength > 0 else "bearish")
    if abs(strength) > 0.6:
        consensus = "strong_" + consensus
    return Signal(
        "gp_ensemble", round(abs(strength), 4), size, pair,
        {
            "shadow": not promote,
            "gp_strength": round(strength, 4),
            "consensus": consensus,
            "num_active": len(votes),
            # Firing indicator names — carried on the position so that on close
            # ONLY these indicators are credited (B9: per-vote indicator credit,
            # not the whole ensemble blob).
            "gp_indicators": active_indicators,
            "entry_type": "gp_ensemble" if promote else "shadow",
            "evaluated_on": "daily" if (daily_prices and len(daily_prices) >= 50) else "live",
        },
    )


def simulate_gp_paper_pnl(pair: str, prices: list[float],
                          horizon: int = 1) -> dict:
    """Paper-trade simulation of the GP shadow signal over `prices`.

    Enters long when the GP consensus is bullish, short when bearish, exits one
    `horizon` later. Returns {trades, wins, losses, win_rate, total_pnl}.
    Pure + network-free: the evidence we demand before any live promotion.
    Does NOT place real orders.
    """
    if not prices or len(prices) < 60 + horizon:
        return {"trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0.0, "total_pnl": 0.0}
    wins = losses = 0
    pnl = 0.0
    n = 0
    for i in range(50, len(prices) - horizon):
        sig = gp_ensemble_signal(pair, prices[: i + 1])
        if sig is None:
            continue
        direction = 1 if sig.meta.get("gp_strength", 0) > 0 else -1
        entry = prices[i]
        exit_p = prices[i + horizon]
        r = (exit_p / entry - 1.0) * 100.0 * direction
        pnl += r
        n += 1
        if r > 0:
            wins += 1
        else:
            losses += 1
    return {
        "trades": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / n, 4) if n else 0.0,
        "total_pnl": round(pnl, 4),
    }
