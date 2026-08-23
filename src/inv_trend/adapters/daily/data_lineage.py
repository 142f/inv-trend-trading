"""Data-lineage adapter for the modular D1 data-update stage."""

from __future__ import annotations

from typing import Any

from inv_trend.application.daily.ports import MarketDataPort
from inv_trend.data.lineage import load_current_lineage


class CurrentLineageAdapter:
    """Adapt the data lake's current-version verifier to ``LineagePort``."""

    def verify_current(
        self,
        market_data: MarketDataPort,
        symbol: str,
        timeframe: str,
        *,
        bars: Any,
    ) -> Any:
        return load_current_lineage(market_data.lake, symbol, timeframe, bars=bars)


__all__ = ["CurrentLineageAdapter"]
