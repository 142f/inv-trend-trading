"""Causal, in-memory OHLCV feature preparation shared by all use cases.

The module deliberately accepts only an already-loaded :class:`pandas.DataFrame`.
It has no provider, file-system, or strategy dependency, so callers cannot turn a
feature calculation into an implicit data read or use data from a different run.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Iterable

import numpy as np
import pandas as pd

from .math_utils import (
    directional_movement_index,
    donchian_channels,
    exponential_moving_average,
    macd,
    simple_moving_average,
    true_range,
    wilder_average,
)


@dataclass(frozen=True)
class FeatureRequest:
    """The explicit, hashable feature contract for one prepared bar frame.

    ``sma_lags`` contains ``(period, lag)`` pairs.  The resulting column name
    includes both values (for example ``sma_20_lag_1``), avoiding accidental
    reuse of a close-confirmed average where a prior-bar average is required.
    """

    atr_period: int | None = None
    donchian_periods: tuple[int, ...] = ()
    sma_lags: tuple[tuple[int, int], ...] = ()
    ema_periods: tuple[int, ...] = ()
    macd_periods: tuple[int, int, int] | None = None
    dmi_period: int | None = None
    volume_sma_lags: tuple[tuple[int, int], ...] = ()
    include_true_range: bool = False

    def __post_init__(self) -> None:
        if self.atr_period is not None and self.atr_period < 2:
            raise ValueError("atr_period must be >= 2")
        if any(period < 2 for period in self.donchian_periods):
            raise ValueError("Donchian periods must be >= 2")
        if any(period < 2 or lag < 0 for period, lag in self.sma_lags):
            raise ValueError("SMA periods must be >= 2 and lags must be non-negative")
        if any(period < 2 for period in self.ema_periods):
            raise ValueError("EMA periods must be >= 2")
        if self.macd_periods is not None:
            fast, slow, signal = self.macd_periods
            if not 1 < fast < slow or signal < 2:
                raise ValueError("invalid MACD periods")
        if self.dmi_period is not None and self.dmi_period < 2:
            raise ValueError("dmi_period must be >= 2")
        if any(period < 2 or lag < 0 for period, lag in self.volume_sma_lags):
            raise ValueError("volume SMA periods must be >= 2 and lags must be non-negative")

    @classmethod
    def turtle(
        cls,
        *,
        atr_period: int,
        channel_periods: Iterable[int],
        sma_lags: Iterable[tuple[int, int]] = (),
    ) -> "FeatureRequest":
        return cls(
            atr_period=atr_period,
            donchian_periods=tuple(sorted(set(channel_periods))),
            sma_lags=tuple(sorted(set(sma_lags))),
            include_true_range=True,
        )


@dataclass(frozen=True)
class PreparedBars:
    """One immutable feature view tied to an exact in-memory OHLCV snapshot."""

    frame: pd.DataFrame
    request: FeatureRequest
    input_fingerprint: str

    @classmethod
    def build(cls, bars: pd.DataFrame, request: FeatureRequest) -> "PreparedBars":
        _validate_ohlcv(bars)
        out = bars.copy()
        fingerprint = ohlcv_fingerprint(out)
        high, low, close = out["high"], out["low"], out["close"]

        if request.include_true_range or request.atr_period is not None:
            out["tr"] = true_range(high, low, close)
        if request.atr_period is not None:
            out["atr"] = wilder_average(out["tr"], request.atr_period)
        if request.donchian_periods:
            channels = donchian_channels(high, low, request.donchian_periods)
            for column in channels:
                out[column] = channels[column]
        for period, lag in request.sma_lags:
            out[f"sma_{period}_lag_{lag}"] = simple_moving_average(close, period, lag=lag)
        for period in request.ema_periods:
            out[f"ema_{period}"] = exponential_moving_average(close, period)
        if request.macd_periods is not None:
            fast, slow, signal = request.macd_periods
            values = macd(close, fast, slow, signal)
            for column in values:
                out[column] = values[column]
        if request.dmi_period is not None:
            values = directional_movement_index(high, low, close, request.dmi_period)
            for column in values:
                out[column] = values[column]
        if request.volume_sma_lags:
            if "volume" not in out:
                for period, lag in request.volume_sma_lags:
                    out[f"volume_sma_{period}_lag_{lag}"] = np.nan
            else:
                volume = pd.to_numeric(out["volume"], errors="coerce")
                for period, lag in request.volume_sma_lags:
                    out[f"volume_sma_{period}_lag_{lag}"] = simple_moving_average(
                        volume, period, lag=lag
                    )
        out.attrs["_feature_request"] = request
        out.attrs["_ohlcv_fingerprint"] = fingerprint
        return cls(frame=out, request=request, input_fingerprint=fingerprint)

    def column(self, name: str) -> pd.Series:
        return self.frame[name]


class FeatureCache:
    """Run-scoped feature cache.

    The cache is deliberately owned by a scanner/backtest/application service;
    it is not global.  Its key contains a content fingerprint and the complete
    request, preventing reuse between changed bars, datasets, or feature specs.
    """

    def __init__(self) -> None:
        self._prepared: dict[tuple[str, FeatureRequest], PreparedBars] = {}

    def prepare(self, bars: pd.DataFrame, request: FeatureRequest) -> PreparedBars:
        fingerprint = ohlcv_fingerprint(bars)
        key = (fingerprint, request)
        prepared = self._prepared.get(key)
        if prepared is None:
            prepared = PreparedBars.build(bars, request)
            self._prepared[key] = prepared
        return prepared

    def clear(self) -> None:
        self._prepared.clear()


def ohlcv_fingerprint(bars: pd.DataFrame) -> str:
    """Return a stable in-memory fingerprint for cache isolation and manifests."""

    _validate_ohlcv(bars)
    columns = [column for column in ("open", "high", "low", "close", "volume", "is_complete") if column in bars]
    hashed = pd.util.hash_pandas_object(bars.loc[:, columns], index=True).values
    digest = hashlib.sha256()
    digest.update(str(tuple(columns)).encode())
    digest.update(np.asarray(hashed, dtype="uint64").tobytes())
    return digest.hexdigest()


def _validate_ohlcv(bars: pd.DataFrame) -> None:
    if not isinstance(bars, pd.DataFrame):
        raise TypeError("bars must be a pandas DataFrame")
    required = {"open", "high", "low", "close"}
    missing = sorted(required - set(bars.columns))
    if missing:
        raise ValueError(f"bars require OHLC columns: {missing}")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("bars require a DatetimeIndex at the feature boundary")
    if bars.index.has_duplicates or not bars.index.is_monotonic_increasing:
        raise ValueError("bars require unique, increasing timestamps")
    values = bars[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(values.to_numpy(dtype=float)).all():
        raise ValueError("bars contain non-finite OHLC values")
