"""Application-facing execution facade over the low-level portfolio simulator."""

from __future__ import annotations

from typing import Any

from inv_trend.adapters.multi_asset.backtest.runner import BacktestResult, TurtleBacktester


class UnifiedBacktestExecutor:
    """Execute precomputed strategy intents without calculating report metrics."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._engine = TurtleBacktester(*args, **kwargs)

    def run(self) -> BacktestResult:
        return self._engine.run()


__all__ = ["BacktestResult", "UnifiedBacktestExecutor"]
