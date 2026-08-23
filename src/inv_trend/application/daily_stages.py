"""Compatibility imports for the modular D1 stage implementations.

New code should import concrete stages from :mod:`inv_trend.application.daily`.
This module intentionally owns no D1 calculation, data-refresh, or decision
logic so established callers retain their import path while the implementation
remains split by processing responsibility.
"""

from .daily.data_update import DataUpdateStage, DailyDataUpdateService
from .daily.strategy_screening import (
    ExecutionContext,
    StrategyScreeningService,
    StrategyScreeningStage,
)
from .daily.trend_decision import TrendDecisionService

__all__ = [
    "DailyDataUpdateService",
    "DataUpdateStage",
    "ExecutionContext",
    "StrategyScreeningService",
    "StrategyScreeningStage",
    "TrendDecisionService",
]
