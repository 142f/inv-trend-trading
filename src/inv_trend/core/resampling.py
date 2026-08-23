"""Causal aggregation of completed D1 sessions into larger bars."""

from __future__ import annotations

import numpy as np
import pandas as pd


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

    if not isinstance(bars, pd.DataFrame):
        raise TypeError("bars must be a pandas DataFrame")
    if sessions_per_bar < 2:
        raise ValueError("sessions_per_bar must be >= 2")
    required = {"open", "high", "low", "close"}
    missing = sorted(required - set(bars.columns))
    if missing:
        raise ValueError(f"bars require OHLC columns: {missing}")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("bars require a DatetimeIndex")
    if bars.index.has_duplicates or not bars.index.is_monotonic_increasing:
        raise ValueError("bars require unique, increasing timestamps")

    anchor_timestamp = pd.Timestamp(anchor)
    if anchor_timestamp.tzinfo is None:
        anchor_timestamp = anchor_timestamp.tz_localize("UTC")
    else:
        anchor_timestamp = anchor_timestamp.tz_convert("UTC")
    if anchor_timestamp not in bars.index:
        raise ValueError("session anchor must be present in bars")

    anchored = bars.loc[anchor_timestamp:].copy()
    complete_groups = len(anchored) // sessions_per_bar
    if complete_groups == 0:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume", "session_start", "session_end", "session_count"],
            index=pd.DatetimeIndex([], tz="UTC", name=bars.index.name),
        )
    anchored = anchored.iloc[: complete_groups * sessions_per_bar]
    groups = np.arange(len(anchored), dtype=int) // sessions_per_bar
    records: list[dict[str, object]] = []
    endpoints: list[pd.Timestamp] = []
    for _, group in anchored.groupby(groups, sort=True):
        records.append(
            {
                "open": float(group["open"].iloc[0]),
                "high": float(group["high"].max()),
                "low": float(group["low"].min()),
                "close": float(group["close"].iloc[-1]),
                "volume": (
                    float(pd.to_numeric(group["volume"], errors="coerce").sum(min_count=1))
                    if "volume" in group
                    else np.nan
                ),
                "session_start": group.index[0],
                "session_end": group.index[-1],
                "session_count": len(group),
            }
        )
        endpoints.append(group.index[-1])
    out = pd.DataFrame(records, index=pd.DatetimeIndex(endpoints, tz="UTC", name=bars.index.name))
    return out


__all__ = ["aggregate_completed_sessions"]
