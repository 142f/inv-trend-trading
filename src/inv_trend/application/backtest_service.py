"""Deprecated forwarding module for the unified backtest application."""

from __future__ import annotations

import warnings

from .backtest.single import BacktestRun, BacktestService as _BacktestService


class BacktestService(_BacktestService):
    """One-release compatibility alias for legacy single-run callers."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        warnings.warn(
            "BacktestService is deprecated; use BacktestBatchService from "
            "inv_trend.application.backtest",
            DeprecationWarning,
            stacklevel=2,
        )
        super().__init__(*args, **kwargs)


__all__ = ["BacktestRun", "BacktestService"]
