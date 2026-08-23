"""Compatibility exports for pure D1 strategy checks.

The implementation lives in :mod:`inv_trend.core.strategy.daily.strategy_checks`.
This adapter path is retained for detector callers during the migration.
"""

from inv_trend.core.strategy.daily.strategy_checks import (
    DailyChecksConfigLike,
    PreparedDailyStrategyChecks,
    TURTLE_PERIODS,
    analyze_prepared_strategy_checks,
    daily_strategy_feature_request,
    prepare_daily_strategy_checks,
)

__all__ = [
    "DailyChecksConfigLike",
    "PreparedDailyStrategyChecks",
    "TURTLE_PERIODS",
    "analyze_prepared_strategy_checks",
    "daily_strategy_feature_request",
    "prepare_daily_strategy_checks",
]
