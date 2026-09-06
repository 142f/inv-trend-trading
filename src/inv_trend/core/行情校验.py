"""Shared structural contract for in-memory OHLC bars; no strategy or I/O policy."""
from __future__ import annotations

import pandas as pd


def validate_bar_index(bars: pd.DataFrame) -> None:
    if not isinstance(bars, pd.DataFrame):
        raise TypeError("bars must be a pandas DataFrame")
    missing = sorted({"open", "high", "low", "close"} - set(bars.columns))
    if missing:
        raise ValueError(f"bars require OHLC columns: {missing}")
    if bars.columns.has_duplicates:
        raise ValueError("bars require unique column names")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("bars require a DatetimeIndex")
    if bars.index.has_duplicates or not bars.index.is_monotonic_increasing or bars.index.hasnans:
        raise ValueError("bars require unique, increasing timestamps")
