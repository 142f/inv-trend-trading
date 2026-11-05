"""Investment-decision panel data derivation over the US top-100 snapshot.

The rating and family votes come from the daily strategy checks pipeline (the
single authoritative scoring source); this package only derives the panel
specific extras (breakout distance, stop levels, exit channels, suggested
quantity, market aggregates and the temperature history) as pure functions.
"""

from .决策推导 import derive_symbol_decision
from .市场聚合 import aggregate_market, append_history
from .组合风控 import DEFAULT_CAPS, allocate_portfolio

__all__ = [
    "DEFAULT_CAPS",
    "allocate_portfolio",
    "append_history",
    "aggregate_market",
    "derive_symbol_decision",
]
