"""Timeframe and market-session validation helpers."""

from __future__ import annotations

import pandas as pd

from ..models import AssetConfig, Market


TIMEFRAME_FREQUENCIES = {
    "H1": pd.Timedelta(hours=1),
    "H4": pd.Timedelta(hours=4),
    "D1": pd.Timedelta(days=1),
    "W1": pd.Timedelta(days=7),
}


def validate_market_calendar(index: pd.DatetimeIndex, asset: AssetConfig, timeframe: str) -> None:
    if index.tz is None:
        raise ValueError("calendar index must be timezone-aware")
    if timeframe.upper() not in TIMEFRAME_FREQUENCIES:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    if asset.market is Market.CRYPTO and asset.session != "24x7":
        raise ValueError("crypto defaults require a 24x7 calendar")
    if asset.market is Market.US_EQUITY and asset.session != "regular":
        raise ValueError("US equity defaults require regular-session bars")


def timeframe_rank(timeframe: str) -> int:
    ranks = {"H1": 1, "H4": 2, "D1": 3, "W1": 4}
    try:
        return ranks[timeframe.upper()]
    except KeyError as exc:
        raise ValueError(f"unsupported timeframe: {timeframe}") from exc
