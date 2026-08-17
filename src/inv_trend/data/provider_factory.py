"""Default provider registration shared by market-data entry points."""

from __future__ import annotations

import os
from pathlib import Path

from .providers import (
    AlphaVantageProvider,
    BarsProvider,
    BinanceKlineProvider,
    CsvBarsProvider,
    DukascopyBarsProvider,
    YahooChartProvider,
)


def create_default_providers(
    csv: str | Path | None = None,
    *,
    timeout: int | None = None,
    retries: int | None = None,
) -> dict[str, BarsProvider]:
    """Create the standard market-data provider registry.

    Alpha Vantage remains opt-in because the provider requires an API key.
    Supplying ``csv`` adds the licensed CSV provider without replacing the
    configured primary providers.
    """
    if timeout is not None and timeout < 1:
        raise ValueError("provider timeout must be positive")
    if retries is not None and retries < 1:
        raise ValueError("provider retries must be positive")
    network = {} if timeout is None else {"timeout": timeout}
    retrying = {} if retries is None else {"retries": retries}
    providers: dict[str, BarsProvider] = {
        "binance": BinanceKlineProvider(**network, **retrying),
        "dukascopy": DukascopyBarsProvider(**network, **retrying),
        "yahoo_chart": YahooChartProvider(**network, **retrying),
    }
    if os.getenv("ALPHAVANTAGE_API_KEY"):
        providers["alpha_vantage"] = AlphaVantageProvider(**network, **retrying)
    if csv:
        providers["licensed_csv"] = CsvBarsProvider(csv)
    return providers
