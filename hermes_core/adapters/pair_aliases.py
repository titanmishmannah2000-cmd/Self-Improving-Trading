"""Canonical pair aliases for venue/feed mapping.

Hermes config may use ``BTC/USDT`` (production naming) while free spot feeds
(Coinbase, Yahoo) still quote ``BTC-USD``. All adapters should resolve through
these helpers so invent, WS, REST, and yfinance stay consistent.
"""

from __future__ import annotations

# Logical HERMES pair → feed pair used by Coinbase/Yahoo spot.
FEED_PAIR_ALIASES: dict[str, str] = {
    "BTC/USDT": "BTC/USD",
    "ETH/USDT": "ETH/USD",
}

# Spot crypto pairs the aggregator / Coinbase sources know about.
CRYPTO_FEED_PAIRS: frozenset[str] = frozenset(
    {
        "BTC/USD",
        "ETH/USD",
        "BTC/USDT",
        "ETH/USDT",
    }
)


def feed_pair(pair: str) -> str:
    """Map a HERMES pair to the Coinbase/Yahoo spot pair."""
    return FEED_PAIR_ALIASES.get(pair, pair)


def is_crypto_pair(pair: str) -> bool:
    return pair in CRYPTO_FEED_PAIRS or feed_pair(pair) in {"BTC/USD", "ETH/USD"}


def yfinance_symbol(pair: str) -> str:
    """Map a HERMES pair to a yfinance ticker."""
    p = feed_pair(pair)
    if p == "BTC/USD":
        return "BTC-USD"
    if p == "ETH/USD":
        return "ETH-USD"
    if "-" in p:
        return p
    if p == "XAU/USD":
        return "GC=F"
    if p == "XAG/USD":
        return "SI=F"
    if "=" in p or p.endswith("-USD"):
        return p
    return p.replace("/", "") + "=X"


def coinbase_product(pair: str) -> str:
    """Map a HERMES pair to a Coinbase product_id (e.g. BTC-USD)."""
    return feed_pair(pair).replace("/", "-").upper()
