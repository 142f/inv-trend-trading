"""Backward-compatible exports for the Turtle strategy API.

The implementation now lives in focused modules:

- domain.py: dataclasses and direction constants
- indicators.py: N/TR and channel calculations
- sizing.py: position sizing helpers
- engine.py: order generation and budget allocation
"""

from __future__ import annotations

from .domain import (
    LONG,
    SHORT,
    AssetSpec,
    EntrySignal,
    Order,
    PortfolioState,
    Position,
    PositionUnit,
    TurtleRules,
)
from .engine import MultiAssetTurtleStrategy, _finite_float
from .indicators import (
    _indicator_columns,
    _indicator_rules_key,
    _require_columns,
    _wilder_average,
    _with_indicators,
    compute_turtle_indicators,
)
from .sizing import _risk_sized_qty, _round_down


__all__ = [
    "LONG",
    "SHORT",
    "AssetSpec",
    "EntrySignal",
    "Order",
    "PortfolioState",
    "Position",
    "PositionUnit",
    "TurtleRules",
    "MultiAssetTurtleStrategy",
    "compute_turtle_indicators",
    "_finite_float",
    "_indicator_columns",
    "_indicator_rules_key",
    "_require_columns",
    "_risk_sized_qty",
    "_round_down",
    "_wilder_average",
    "_with_indicators",
]
