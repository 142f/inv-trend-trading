"""Causal aggregation of completed D1 sessions into larger bars."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .行情校验 import validate_bar_index


def aggregate_completed_sessions(
    bars: pd.DataFrame,
    sessions_per_bar: int,
    *,
    anchor: pd.Timestamp | str,
) -> pd.DataFrame:
    """Aggregate complete D1 sessions from a fixed, persisted anchor.

    ``anchor`` must identify a completed session in ``bars``.  Sessions before
    it are intentionally ignored: when an older history backfill is later
    added, the existing D2/D5/D7 boundaries therefore remain unchanged.  The
    final incomplete group is dropped rather than exposed as a closed bar.
    """

    validate_bar_index(bars)
    if isinstance(sessions_per_bar, bool) or not isinstance(sessions_per_bar, (int, np.integer)) or sessions_per_bar < 2:
        raise ValueError("sessions_per_bar must be an integer >= 2")

    anchor_timestamp = pd.Timestamp(anchor)
    if anchor_timestamp.tzinfo is None:
        anchor_timestamp = anchor_timestamp.tz_localize("UTC")
    else:
        anchor_timestamp = anchor_timestamp.tz_convert("UTC")
    if bars.index.tz is None:
        anchor_timestamp = anchor_timestamp.tz_localize(None)
    if anchor_timestamp not in bars.index:
        raise ValueError("session anchor must be present in bars")

    anchored = bars.iloc[bars.index.get_loc(anchor_timestamp):]
    complete_groups = len(anchored) // sessions_per_bar
    if complete_groups == 0:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume", "session_start", "session_end", "session_count"],
            index=pd.DatetimeIndex([], tz="UTC", name=bars.index.name),
        )
    anchored = anchored.iloc[: complete_groups * sessions_per_bar]
    groups = np.arange(len(anchored), dtype=int) // sessions_per_bar
    extrema = anchored.groupby(groups, sort=False).agg({"high": "max", "low": "min"})
    endpoints = anchored.index[sessions_per_bar - 1::sessions_per_bar]
    output_index = (
        endpoints.tz_localize("UTC") if endpoints.tz is None else endpoints.tz_convert("UTC")
    ).copy(deep=True)
    # The legacy constructor did not infer a frequency. Preserve its metadata
    # without converting every timestamp to a Python object.
    output_index.freq = None
    volume = (
        pd.to_numeric(anchored["volume"], errors="coerce")
        .groupby(groups, sort=False).sum(min_count=1).to_numpy(dtype=float)
        if "volume" in anchored else np.nan
    )
    # Positional endpoints retain NaN open/close values; groupby.first/last
    # would skip them and silently substitute a different session's price.
    return pd.DataFrame(
        {
            "open": anchored["open"].iloc[::sessions_per_bar].to_numpy(dtype=float),
            "high": extrema["high"].to_numpy(dtype=float),
            "low": extrema["low"].to_numpy(dtype=float),
            "close": anchored["close"].iloc[sessions_per_bar - 1::sessions_per_bar].to_numpy(dtype=float),
            "volume": volume,
            "session_start": anchored.index[::sessions_per_bar],
            "session_end": endpoints,
            "session_count": sessions_per_bar,
        }, index=output_index,
    )


__all__ = ["aggregate_completed_sessions"]
