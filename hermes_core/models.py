"""Shared dataclass models for engine boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Trade:
    """Open or closed trade record."""

    id: str
    pair: str
    direction: str
    entry_price: float
    size: float


@dataclass
class Strategy:
    """Per-pair strategy configuration."""

    pair: str
    strategy_type: str
    params: dict[str, Any]


@dataclass
class Goal:
    """Bot-level performance and reflection goals."""

    target_return_30d: float
    max_drawdown: float
    min_sharpe: float
    reflection_every: int
    one_variable_only: bool


@dataclass
class Indicator:
    """Discovered or configured indicator definition."""

    name: str
    expression: str
    weight: float


@dataclass
class Signal:
    """Entry signal emitted by EntryEngine."""

    type: str
    quality: float
    size: float


@dataclass
class Exit:
    """Exit decision emitted by ExitEngine."""

    reason: str
    price: float


@dataclass
class Policy:
    """Policy suppressions applied at entry gate."""

    suppressions: dict[str, str]
