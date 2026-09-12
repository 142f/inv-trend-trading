"""Public facade for the unified, reproducible backtest application module."""

from .artifacts import BacktestArtifactWriter, render_backtest_html
from .execution import UnifiedBacktestExecutor
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
from .ports import BacktestExecutorFactory, StrategySignalAdapter
from .reporting import BacktestReportModel, build_report_model
from .service import BacktestBatchService, StrategyBacktestService
from .single import BacktestRun
from .source import load_source_bundle, load_versioned_data
from .strategy_contracts import EMAContractStrategy, TurtleContractStrategy

__all__ = [
    "BacktestArtifactWriter", "BacktestBatchResult", "BacktestBatchService",
    "BacktestCombinationResult", "BacktestExecutorFactory", "BacktestPlan",
    "BacktestReportModel", "BacktestRun", "BacktestSignalBundle", "BacktestSourceBundle",
    "FoldResult", "FoldWindow", "RankingPolicy", "SourceInstrument",
    "StrategyBacktestService", "StrategySignalAdapter", "UnifiedBacktestExecutor",
    "ValidationPolicy",
    "EMAContractStrategy", "TurtleContractStrategy",
    "build_report_model", "combination_id", "expand_parameter_grid",
    "load_source_bundle", "load_versioned_data", "render_backtest_html",
    "signal_parameters",
]
