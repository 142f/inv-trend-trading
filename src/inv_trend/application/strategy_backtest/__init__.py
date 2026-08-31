"""Independent, reproducible strategy-backtest application stage."""

from .artifacts import BacktestArtifactWriter, render_backtest_html
from .grid import combination_id, expand_parameter_grid, signal_parameters
from .models import (
    BacktestBatchResult,
    BacktestCombinationResult,
    BacktestPlan,
    BacktestSignalBundle,
    BacktestSourceBundle,
    FoldResult,
    FoldWindow,
    RankingPolicy,
    SourceInstrument,
    ValidationPolicy,
)
from .service import StrategyBacktestService
from .source import load_source_bundle, load_versioned_data

__all__ = [
    "BacktestArtifactWriter", "BacktestBatchResult", "BacktestCombinationResult",
    "BacktestPlan", "BacktestSignalBundle", "BacktestSourceBundle", "FoldResult",
    "FoldWindow", "RankingPolicy", "SourceInstrument", "StrategyBacktestService",
    "ValidationPolicy", "combination_id", "expand_parameter_grid",
    "load_source_bundle", "load_versioned_data", "render_backtest_html",
    "signal_parameters",
]
