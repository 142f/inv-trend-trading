"""Pluggable data-provider contracts; providers never contain signal logic."""

from __future__ import annotations

from typing import Mapping, Protocol

import pandas as pd

from ..models import AssetConfig


class DataProvider(Protocol):
    def bars(self, asset: AssetConfig, timeframe: str) -> pd.DataFrame:
        """Return one instrument/timeframe price series."""


class InMemoryProvider:
    def __init__(self, data: Mapping[tuple[str, str], pd.DataFrame]) -> None:
        self._data = dict(data)

    def bars(self, asset: AssetConfig, timeframe: str) -> pd.DataFrame:
        key = (asset.instrument, timeframe.upper())
        if key not in self._data:
            raise KeyError(f"no bars for {key}")
        return self._data[key].copy()
