"""多品种海龟趋势系统组件。"""

from .backtest import BacktestResult, TurtleBacktester
from .profiles import classic_bar_rules, h4_daily_equivalent_rules, turtle_rules
from .strategy import (
    AssetSpec,
    MultiAssetTurtleStrategy,
    Order,
    PortfolioState,
    Position,
    PositionUnit,
    TurtleRules,
    compute_turtle_indicators,
)

__all__ = [
    "AssetSpec",
    "BacktestResult",
    "classic_bar_rules",
    "h4_daily_equivalent_rules",
    "MultiAssetTurtleStrategy",
    "Order",
    "PortfolioState",
    "Position",
    "PositionUnit",
    "TurtleBacktester",
    "TurtleRules",
    "turtle_rules",
    "compute_turtle_indicators",
]
