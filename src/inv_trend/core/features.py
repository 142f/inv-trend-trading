"""Causal, in-memory OHLCV feature preparation shared by all use cases.

The module deliberately accepts only an already-loaded :class:`pandas.DataFrame`.
It has no provider, file-system, or strategy dependency, so callers cannot turn a
feature calculation into an implicit data read or use data from a different run.
"""

from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import hashlib
from typing import Iterable

import numpy as np
import pandas as pd

from .math_utils import (
    _macd_from_averages,
    directional_movement_index,
    donchian_channels,
    exponential_moving_average,
    simple_moving_average,
    true_range,
    wilder_average,
)
from .行情校验 import validate_bar_index


def _moving_averages(values: pd.Series, specs: tuple[tuple[int, int], ...], prefix: str):
    averages: dict[int, pd.Series] = {}
    columns: dict[str, pd.Series] = {}
    for period, lag in specs:
        if period not in averages:
            averages[period] = simple_moving_average(values, period)
        average = averages[period]
        columns[f"{prefix}_{period}_lag_{lag}"] = average.shift(lag) if lag else average
    return columns


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
    def merge(cls, *requests: "FeatureRequest") -> "FeatureRequest":
        """Return the minimal superset request required by all callers.

        Conflicting singular feature definitions are rejected instead of silently
        selecting one, so a merged request cannot change strategy semantics.
        """

        if not requests:
            return cls()

        def singular(name: str):
            values = {getattr(request, name) for request in requests if getattr(request, name) is not None}
            if len(values) > 1:
                raise ValueError(f"cannot merge conflicting {name}: {sorted(values)!r}")
            return next(iter(values)) if values else None

        return cls(
            atr_period=singular("atr_period"),
            donchian_periods=tuple(
                sorted({period for request in requests for period in request.donchian_periods})
            ),
            sma_lags=tuple(
                sorted({item for request in requests for item in request.sma_lags})
            ),
            ema_periods=tuple(
                sorted({period for request in requests for period in request.ema_periods})
            ),
            macd_periods=singular("macd_periods"),
            dmi_period=singular("dmi_period"),
            volume_sma_lags=tuple(
                sorted({item for request in requests for item in request.volume_sma_lags})
            ),
            include_true_range=any(request.include_true_range for request in requests),
        )

    def required_columns(self) -> tuple[str, ...]:
        """Return deterministic generated-column names for diagnostics/tests."""

        columns: list[str] = []
        if self.include_true_range or self.atr_period is not None:
            columns.append("tr")
        if self.atr_period is not None:
            columns.append("atr")
        for period in self.donchian_periods:
            columns.extend((f"channel_high_{period}", f"channel_low_{period}"))
        columns.extend(f"sma_{period}_lag_{lag}" for period, lag in self.sma_lags)
        columns.extend(f"ema_{period}" for period in self.ema_periods)
        if self.macd_periods is not None:
            fast, slow, _ = self.macd_periods
            columns.extend((f"ema_{fast}", f"ema_{slow}", "dif", "dea", "histogram", "macd_bar"))
        if self.dmi_period is not None:
            columns.extend(("plus_di", "minus_di", "dx", "adx"))
        columns.extend(
            f"volume_sma_{period}_lag_{lag}" for period, lag in self.volume_sma_lags
        )
        return tuple(dict.fromkeys(columns))

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
        return cls._build_validated(bars, request, ohlcv_fingerprint(bars))

    @classmethod
    def _build_validated(
        cls, bars: pd.DataFrame, request: FeatureRequest, fingerprint: str
    ) -> "PreparedBars":
        """Internal path: the caller has validated and fingerprinted this snapshot."""
        generated: dict[str, pd.Series | float] = {}
        high, low, close = bars["high"], bars["low"], bars["close"]

        if request.include_true_range or request.atr_period is not None:
            generated["tr"] = true_range(high, low, close)
        if request.atr_period is not None:
            generated["atr"] = wilder_average(generated["tr"], request.atr_period)
        if request.donchian_periods:
            channels = donchian_channels(high, low, request.donchian_periods)
            for column in channels:
                generated[column] = channels[column]
        generated.update(_moving_averages(close, request.sma_lags, "sma"))
        ema_periods = dict.fromkeys(request.ema_periods)
        if request.macd_periods is not None:
            ema_periods.update(dict.fromkeys(request.macd_periods[:2]))
        for period in ema_periods:
            generated[f"ema_{period}"] = exponential_moving_average(close, period)
        if request.macd_periods is not None:
            fast, slow, signal = request.macd_periods
            values = _macd_from_averages(
                generated[f"ema_{fast}"], generated[f"ema_{slow}"], fast, slow, signal
            )
            for column in values:
                generated[column] = values[column]
        if request.dmi_period is not None:
            values = directional_movement_index(high, low, close, request.dmi_period)
            for column in values:
                generated[column] = values[column]
        if request.volume_sma_lags:
            if "volume" not in bars:
                for period, lag in request.volume_sma_lags:
                    generated[f"volume_sma_{period}_lag_{lag}"] = np.nan
            else:
                volume = pd.to_numeric(bars["volume"], errors="coerce")
                generated.update(_moving_averages(volume, request.volume_sma_lags, "volume_sma"))
        # Build one feature block, preserving both original column positions
        # and the old overwrite semantics for pre-existing generated columns.
        columns = list(dict.fromkeys((*bars.columns, *generated)))
        out = pd.concat(
            [bars.drop(columns=[name for name in generated if name in bars]),
             pd.DataFrame(generated, index=bars.index, copy=False)], axis=1,
        ).reindex(columns=columns)
        out.attrs = deepcopy(bars.attrs)
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
        self._prepared: dict[tuple, PreparedBars] = {}

    def prepare(self, bars: pd.DataFrame, request: FeatureRequest) -> PreparedBars:
        fingerprint = ohlcv_fingerprint(bars)
        # The public OHLC fingerprint intentionally excludes auxiliary columns
        # and attrs. Do not reuse their pass-through payload across snapshots.
        if bars.attrs or any(column not in _FINGERPRINT_COLUMNS for column in bars):
            return PreparedBars._build_validated(bars, request, fingerprint)
        key = (fingerprint, request, tuple(bars.columns), tuple(map(str, bars.dtypes)),
               str(bars.index.dtype), bars.index.name, bars.index.freqstr)
        prepared = self._prepared.get(key)
        if prepared is None:
            prepared = PreparedBars._build_validated(bars, request, fingerprint)
            self._prepared[key] = prepared
        return prepared

    def clear(self) -> None:
        self._prepared.clear()


_FINGERPRINT_COLUMNS = ("open", "high", "low", "close", "volume", "is_complete")


def ohlcv_fingerprint(bars: pd.DataFrame) -> str:
    """Return a stable in-memory fingerprint for cache isolation and manifests."""

    _validate_ohlcv(bars)
    columns = [column for column in _FINGERPRINT_COLUMNS if column in bars]
    hashed = pd.util.hash_pandas_object(bars.loc[:, columns], index=True).values
    digest = hashlib.sha256()
    digest.update(str(tuple(columns)).encode())
    digest.update(np.asarray(hashed, dtype="uint64").tobytes())
    return digest.hexdigest()


def _validate_ohlcv(bars: pd.DataFrame) -> None:
    validate_bar_index(bars)
    values = bars[["open", "high", "low", "close"]]
    if not all(pd.api.types.is_numeric_dtype(dtype) for dtype in values.dtypes):
        raise ValueError("OHLC columns must have numeric dtypes")
    if not np.isfinite(values.to_numpy(dtype=float)).all():
        raise ValueError("bars contain non-finite OHLC values")
