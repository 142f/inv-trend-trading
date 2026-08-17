"""Strict normalization without forward-filling prices."""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..models import AssetConfig


REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}


def normalize_bars(
    bars: pd.DataFrame,
    asset: AssetConfig,
    timeframe: str,
) -> pd.DataFrame:
    """Return UTC-indexed bars and reject identity/schema violations."""

    timeframe = timeframe.upper()
    if timeframe not in asset.timeframes:
        raise ValueError(f"{asset.symbol} is not configured for {timeframe}")
    out = bars.copy()
    timestamp_column = next(
        (column for column in ("timestamp", "time", "date") if column in out.columns),
        None,
    )
    if timestamp_column is not None:
        timestamps = pd.to_datetime(
            out.pop(timestamp_column),
            utc=True,
            errors="coerce",
        )
        out.index = pd.DatetimeIndex(timestamps)
    elif not isinstance(out.index, pd.DatetimeIndex):
        raise ValueError("bars require a timestamp/time column or DatetimeIndex")
    elif out.index.tz is None:
        raise ValueError("naive timestamps are forbidden; localize at the provider boundary")
    else:
        out.index = out.index.tz_convert("UTC")

    if out.index.hasnans or out.index.has_duplicates or not out.index.is_monotonic_increasing:
        raise ValueError("timestamps must be unique, valid and increasing")
    missing = REQUIRED_COLUMNS - set(out.columns)
    if missing:
        raise ValueError(f"missing bar columns: {sorted(missing)}")

    if "symbol" in out.columns:
        values = set(out["symbol"].dropna().astype(str))
        allowed = {asset.symbol, asset.instrument, *asset.source_symbols}
        if len(values) > 1 or not values.issubset(allowed):
            raise ValueError(
                f"mixed or mismatched symbol: expected one of {sorted(allowed)}, "
                f"got {sorted(values)}"
            )

    for identity, expected in {
        "instrument": asset.instrument,
        "data_source": asset.data_source,
        "price_type": asset.price_type,
        "adjustment": asset.adjustment,
    }.items():
        if identity in out.columns:
            values = set(out[identity].dropna().astype(str))
            if values and values != {expected}:
                raise ValueError(
                    f"mixed or mismatched {identity}: expected {expected}, got {sorted(values)}"
                )
    if "timeframe" in out.columns:
        values = set(out["timeframe"].dropna().astype(str).str.upper())
        if values and values != {timeframe}:
            raise ValueError(
                f"mixed or mismatched timeframe: expected {timeframe}, got {sorted(values)}"
            )

    for column in REQUIRED_COLUMNS:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    ohlcv = out[list(REQUIRED_COLUMNS)].to_numpy(dtype=float)
    if not np.isfinite(ohlcv).all():
        raise ValueError("bars contain missing or non-finite OHLCV values")
    if (out[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("prices must be positive")
    valid = (
        (out["high"] >= out["low"])
        & (out["open"].between(out["low"], out["high"]))
        & (out["close"].between(out["low"], out["high"]))
        & (out["volume"] >= 0)
    )
    if not bool(valid.all()):
        raise ValueError("bars contain invalid OHLC or volume rows")

    out["symbol"] = asset.symbol
    out["instrument"] = asset.instrument
    out["market"] = asset.market.value
    out["timeframe"] = timeframe
    out["timezone"] = asset.timezone
    out["data_source"] = asset.data_source
    out["price_type"] = asset.price_type
    out["adjustment"] = asset.adjustment
    return out


def apply_split_adjustment(
    bars: pd.DataFrame,
    split_time: str | pd.Timestamp,
    split_ratio: float,
) -> pd.DataFrame:
    """Back-adjust pre-split OHLC and volume for a ratio such as 4-for-1 = 4."""

    if split_ratio <= 0:
        raise ValueError("split_ratio must be positive")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("split adjustment requires a DatetimeIndex")
    timestamp = pd.Timestamp(split_time)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    out = bars.copy()
    before = out.index < timestamp
    out.loc[before, ["open", "high", "low", "close"]] /= split_ratio
    if "volume" in out:
        out.loc[before, "volume"] *= split_ratio
    return out
