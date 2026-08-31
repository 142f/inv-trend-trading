"""Ports that keep strategy evidence separate from execution and reporting."""

from __future__ import annotations

from typing import Any, Mapping, Protocol

import pandas as pd

from inv_trend.adapters.multi_asset.backtest.data_store import BacktestDataStore

from .models import BacktestSignalBundle, BacktestSourceBundle


class StrategySignalAdapter(Protocol):
    """Project causal strategy evidence without executing portfolio accounting."""

    @property
    def bundles(self) -> tuple[BacktestSignalBundle, ...]: ...

    def project(
        self,
        data: Mapping[str, pd.DataFrame],
        parameters: Mapping[str, Any],
    ) -> tuple[BacktestSignalBundle, BacktestDataStore, Any]: ...

    def strategy(
        self,
        bundle: BacktestSignalBundle,
        specs: Mapping[str, Any],
        rules: Any,
    ) -> Any: ...


class StrategySignalAdapterFactory(Protocol):
    def __call__(self, source: BacktestSourceBundle) -> StrategySignalAdapter: ...


class BacktestExecutorFactory(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


__all__ = [
    "BacktestExecutorFactory", "StrategySignalAdapter", "StrategySignalAdapterFactory",
]
