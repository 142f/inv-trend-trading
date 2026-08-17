"""Prepared market data access for backtests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from ..models.domain import TurtleRules
from ..strategy.indicators import compute_turtle_indicators


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
        self._events_by_date = self._index_events()

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

    def row_at_previous(
        self,
        symbol: str,
        date: pd.Timestamp,
    ) -> Mapping[str, object] | None:
        """Return only the bar completed before ``date``; never today's close."""
        symbol_data = self.by_symbol.get(symbol)
        if symbol_data is None:
            return None
        pos = int(symbol_data.index.searchsorted(date, side="left")) - 1
        return None if pos < 0 else symbol_data.records[pos]

    def timeline(self, dates: list[pd.Timestamp] | None = None):
        """Yield as-of snapshots and tradable symbols in calendar order.

        This is intentionally event-driven: the backtest no longer materializes
        a dictionary/set pair for every date before it starts, nor does it run
        ``searchsorted`` for every ``(date, symbol)`` pair.  The current
        snapshot is updated only when a symbol has an actual bar.
        """
        snapshots: dict[str, Mapping[str, object]] = {}
        wanted = None if dates is None else set(dates)
        for date in self.calendar:
            tradable: set[str] = set()
            for symbol, pos in self._events_by_date[date]:
                symbol_data = self.by_symbol[symbol]
                snapshots[symbol] = symbol_data.records[pos]
                if symbol_data.index[pos] == date and pos < len(symbol_data.index) - 1:
                    tradable.add(symbol)
            if wanted is None or date in wanted:
                yield date, snapshots, tradable

    def _index_events(self) -> dict[pd.Timestamp, list[tuple[str, int]]]:
        events: dict[pd.Timestamp, list[tuple[str, int]]] = {
            date: [] for date in self.calendar
        }
        for symbol, symbol_data in self.by_symbol.items():
            for position, timestamp in enumerate(symbol_data.index):
                events[timestamp].append((symbol, position))
        return events

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
