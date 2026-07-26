"""Donchian channels calculated only from completed prior bars."""

from __future__ import annotations

import pandas as pd


def donchian_channels(bars: pd.DataFrame, periods: set[int]) -> pd.DataFrame:
    out = bars.copy()
    for period in sorted(periods):
        if period < 2:
            raise ValueError("Donchian periods must be >= 2")
        out[f"channel_high_{period}"] = out["high"].rolling(period).max().shift(1)
        out[f"channel_low_{period}"] = out["low"].rolling(period).min().shift(1)
    return out
