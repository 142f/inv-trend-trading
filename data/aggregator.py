from __future__ import annotations

from typing import Dict

import numpy as np
import pandas as pd


def _day_bucket(ts: pd.Timestamp, n_days: int) -> int:
    epoch_days = int(ts.timestamp() // 86400)
    return epoch_days // n_days


def aggregate_from_daily(daily_df: pd.DataFrame, n_days: int) -> pd.DataFrame:
    if daily_df.empty:
        return daily_df.copy()

    df = daily_df.sort_index().copy()
    if "close_ts" not in df.columns:
        # for daily bars, close timestamp is open timestamp + 1 day
        df["close_ts"] = df.index + pd.Timedelta(days=1)

    # bucket by UTC day boundary anchored to epoch 00:00 UTC
    open_days = (df["close_ts"] - pd.Timedelta(days=1)).dt.floor("D")
    group_key = open_days.map(lambda x: _day_bucket(x, n_days))

    grouped = df.groupby(group_key)
    agg = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        close_ts=("close_ts", "last"),
        count=("open", "count"),
    )
    agg = agg[agg["count"] == n_days].drop(columns=["count"])
    if agg.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "close_ts"])

    # Use synthetic open_ts index from close_ts - n_days
    agg.index = agg["close_ts"] - pd.to_timedelta(n_days, unit="D")
    agg.index.name = "open_ts"
    return agg[["open", "high", "low", "close", "volume", "close_ts"]].sort_index()


def aggregate_daily_frames(frames: Dict[str, pd.DataFrame]) -> Dict[str, pd.DataFrame]:
    daily = frames["1d"]
    out = dict(frames)
    out["2d"] = aggregate_from_daily(daily, 2)
    out["5d"] = aggregate_from_daily(daily, 5)
    out["7d"] = aggregate_from_daily(daily, 7)
    return out

