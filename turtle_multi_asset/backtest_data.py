"""Prepared market data access for backtests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from .domain import TurtleRules
from .indicators import compute_turtle_indicators


@dataclass(frozen=True)
class SymbolBacktestData:
    bars: pd.DataFrame
    index: pd.DatetimeIndex
    records: list[dict]
    positions: dict[pd.Timestamp, int]


class BacktestDataStore:
    """Caches bar records and calendar lookups used by the backtest loop."""

    def __init__(
        self,
        data: Mapping[str, pd.DataFrame],
        rules: TurtleRules,
    ) -> None:
        self.by_symbol = {
            symbol: _prepare_bars(symbol, df, rules)
            for symbol, df in data.items()
        }
        self.symbols = tuple(self.by_symbol)
        self.calendar = _calendar(symbol_data.index for symbol_data in self.by_symbol.values())
        self._last_positions_by_date, self._tradable_by_date = self._build_calendar_maps()

    def row_at_date(
        self,
        symbol: str,
        date: pd.Timestamp,
    ) -> Mapping[str, object] | None:
        symbol_data = self.by_symbol.get(symbol)
        if symbol_data is None:
            return None
        pos = symbol_data.positions.get(date)
        if pos is None:
            return None
        return symbol_data.records[pos]

    def snapshots_through(self, date: pd.Timestamp) -> dict[str, Mapping[str, object]]:
        positions = self._last_positions_by_date.get(date, {})
        return {
            symbol: self.by_symbol[symbol].records[pos]
            for symbol, pos in positions.items()
        }

    def tradable_symbols(self, date: pd.Timestamp) -> set[str]:
        return set(self._tradable_by_date.get(date, ()))

    def price(self, date: pd.Timestamp, symbol: str, column: str) -> float:
        row = self.row_at_date(symbol, date)
        if row is None:
            raise KeyError(symbol)
        if column not in row:
            column = "close"
        return float(row[column])

    def last_price_on_or_before(
        self,
        date: pd.Timestamp,
        symbol: str,
        column: str,
    ) -> float:
        pos = self.last_pos_on_or_before(symbol, date)
        if pos is None:
            raise KeyError(symbol)
        row = self.by_symbol[symbol].records[pos]
        if column not in row:
            column = "close"
        return float(row[column])

    def last_pos_on_or_before(
        self,
        symbol: str,
        date: pd.Timestamp,
    ) -> int | None:
        symbol_data = self.by_symbol[symbol]
        pos = int(symbol_data.index.searchsorted(date, side="right")) - 1
        if pos < 0:
            return None
        return pos

    def holding_bars(
        self,
        symbol: str,
        entry_time: pd.Timestamp,
        exit_time: pd.Timestamp,
    ) -> int | None:
        symbol_data = self.by_symbol.get(symbol)
        if symbol_data is None:
            return None
        start = symbol_data.positions.get(entry_time)
        end = symbol_data.positions.get(exit_time)
        if start is None or end is None:
            return None
        return int(end - start)

    def _build_calendar_maps(
        self,
    ) -> tuple[dict[pd.Timestamp, dict[str, int]], dict[pd.Timestamp, set[str]]]:
        last_positions_by_date: dict[pd.Timestamp, dict[str, int]] = {}
        tradable_by_date: dict[pd.Timestamp, set[str]] = {}
        for date in self.calendar:
            positions: dict[str, int] = {}
            tradable: set[str] = set()
            for symbol, symbol_data in self.by_symbol.items():
                pos = int(symbol_data.index.searchsorted(date, side="right")) - 1
                if pos < 0:
                    continue
                positions[symbol] = pos
                exact_pos = symbol_data.positions.get(date)
                if exact_pos is not None and exact_pos < len(symbol_data.index) - 1:
                    tradable.add(symbol)
            last_positions_by_date[date] = positions
            tradable_by_date[date] = tradable
        return last_positions_by_date, tradable_by_date


def _prepare_bars(
    symbol: str,
    df: pd.DataFrame,
    rules: TurtleRules,
) -> SymbolBacktestData:
    if df.empty:
        raise ValueError(f"{symbol} has no bars")
    missing = {"open", "high", "low", "close"} - set(df.columns)
    if missing:
        raise ValueError(f"{symbol} missing OHLC columns: {sorted(missing)}")
    if df.index.has_duplicates:
        raise ValueError(f"{symbol} has duplicate timestamps")

    out = df.sort_index().copy()
    for column in ["open", "high", "low", "close"]:
        out[column] = pd.to_numeric(out[column], errors="coerce")

    ohlc = out[["open", "high", "low", "close"]]
    finite = np.isfinite(ohlc.to_numpy(dtype=float)).all(axis=1)
    positive = (ohlc > 0).all(axis=1).to_numpy()
    ordered = (
        (out["high"] >= out["low"])
        & (out["open"] <= out["high"])
        & (out["open"] >= out["low"])
        & (out["close"] <= out["high"])
        & (out["close"] >= out["low"])
    ).to_numpy()
    if not bool((finite & positive & ordered).all()):
        raise ValueError(f"{symbol} has invalid OHLC rows")

    bars = compute_turtle_indicators(out, rules)
    index = bars.index
    return SymbolBacktestData(
        bars=bars,
        index=index,
        records=bars.to_dict("records"),
        positions={timestamp: pos for pos, timestamp in enumerate(index)},
    )


def _calendar(indexes: object) -> list[pd.Timestamp]:
    all_dates: set[pd.Timestamp] = set()
    for index in indexes:
        all_dates.update(pd.Timestamp(x) for x in index)
    return sorted(all_dates)
