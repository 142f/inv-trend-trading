"""Repeatable local performance audit for the refactor's hot paths.

Uses deterministic in-memory D1 bars, performs one warm-up and seven measured
runs per scenario, then prints machine-readable medians.  It deliberately
contains no disk or network I/O inside the measured loops.
"""

from __future__ import annotations

import gc
import json
from statistics import median
import time
import tracemalloc

import numpy as np
import pandas as pd

from inv_trend.core.features import FeatureRequest, PreparedBars
from inv_trend.adapters.detector.engine.scanner import TurtleScanner
from inv_trend.adapters.detector.models import AssetConfig, DetectorState, Market, StrategyConfig
from inv_trend.adapters.multi_asset.backtest.data_store import BacktestDataStore
from inv_trend.adapters.multi_asset.models.domain import TurtleRules


RUNS = 7


def _bars(periods: int = 720, *, offset: float = 0.0) -> pd.DataFrame:
    index = pd.date_range("2021-01-01", periods=periods, freq="D", tz="UTC")
    phase = np.linspace(0.0, 28.0, periods)
    close = 100.0 + offset + np.linspace(0.0, 65.0, periods) + np.sin(phase) * 5.0
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 1.0,
            "low": np.minimum(open_, close) - 1.0,
            "close": close,
            "volume": 1_000.0 + np.arange(periods) % 100,
        },
        index=index,
    )


def _measure(operation):  # type: ignore[no-untyped-def]
    operation()  # warm-up
    timings: list[float] = []
    peaks: list[int] = []
    for _ in range(RUNS):
        gc.collect()
        tracemalloc.start()
        started = time.perf_counter()
        operation()
        timings.append(time.perf_counter() - started)
        _, peak = tracemalloc.get_traced_memory()
        peaks.append(peak)
        tracemalloc.stop()
    return {"seconds_median": median(timings), "peak_bytes_median": median(peaks)}


def _comparison(before, after):  # type: ignore[no-untyped-def]
    return {
        "before": before,
        "after": after,
        "time_speedup": before["seconds_median"] / after["seconds_median"],
        "peak_memory_ratio": before["peak_bytes_median"] / after["peak_bytes_median"],
    }


def _feature_benchmark(bars: pd.DataFrame) -> dict[str, object]:
    turtle = FeatureRequest.turtle(
        atr_period=20, channel_periods=(10, 20, 55), sma_lags=((20, 0),)
    )
    detector = FeatureRequest.turtle(
        atr_period=20, channel_periods=(10, 20, 55), sma_lags=((60, 1), (120, 1))
    )
    daily = FeatureRequest(
        donchian_periods=(20, 55),
        sma_lags=((10, 0), (20, 0)),
        macd_periods=(12, 26, 9),
    )
    shared = FeatureRequest(
        atr_period=20,
        donchian_periods=(10, 20, 55),
        sma_lags=((10, 0), (20, 0), (20, 1), (60, 1), (120, 1)),
        macd_periods=(12, 26, 9),
        include_true_range=True,
    )

    def legacy() -> None:
        PreparedBars.build(bars, turtle)
        PreparedBars.build(bars, detector)
        PreparedBars.build(bars, daily)

    def refactored() -> None:
        PreparedBars.build(bars, shared)

    return _comparison(_measure(legacy), _measure(refactored))


def _detector_benchmark(bars: pd.DataFrame) -> dict[str, object]:
    asset = AssetConfig(
        symbol="BENCH", instrument="BENCH_SPOT", market=Market.CRYPTO,
        data_source="fixture", timeframes=("D1",), adjustment="none",
    )
    config = StrategyConfig(
        atr_period=20, system1_entry=20, system2_entry=55, system1_exit=10,
        system2_exit=20, volatility_lookback=60, trend_ma_period=60,
        long_trend_ma_period=120,
    )

    def old_prefix_replay() -> tuple[str, ...]:
        scanner = TurtleScanner(config)
        prepared = scanner.prepare(bars, asset, "D1")
        state = DetectorState(asset.symbol, "D1")
        events: list[str] = []
        for position in range(max(1, config.warmup_bars - 1), len(prepared)):
            result = scanner.detect_prepared(prepared.iloc[: position + 1], asset, "D1", state)
            state = result.state
            if result.signal.signal_type.value != "NO_SIGNAL":
                events.append(result.signal.signal_type.value)
        return tuple(events)

    def row_replay() -> tuple[str, ...]:
        scanner = TurtleScanner(config)
        prepared = scanner.prepare(bars, asset, "D1")
        state = DetectorState(asset.symbol, "D1")
        events: list[str] = []
        for position in range(max(1, config.warmup_bars - 1), len(prepared)):
            result = scanner.detect_row(prepared.iloc[position], asset, "D1", state)
            state = result.state
            if result.signal.signal_type.value != "NO_SIGNAL":
                events.append(result.signal.signal_type.value)
        return tuple(events)

    assert old_prefix_replay() == row_replay()
    return _comparison(_measure(old_prefix_replay), _measure(row_replay))


def _timeline_benchmark(bars: pd.DataFrame) -> dict[str, object]:
    data = {
        f"S{number}": bars.assign(
            open=bars["open"] + number,
            high=bars["high"] + number,
            low=bars["low"] + number,
            close=bars["close"] + number,
        )
        for number in range(8)
    }
    rules = TurtleRules(n_period=20, fast_entry=20, slow_entry=55, fast_exit=10, slow_exit=20)
    store = BacktestDataStore(data, rules)

    def old_searchsorted_maps():
        positions_by_date: dict[pd.Timestamp, dict[str, int]] = {}
        tradable_by_date: dict[pd.Timestamp, set[str]] = {}
        for date in store.calendar:
            positions: dict[str, int] = {}
            tradable: set[str] = set()
            for symbol, symbol_data in store.by_symbol.items():
                position = int(symbol_data.index.searchsorted(date, side="right")) - 1
                if position < 0:
                    continue
                positions[symbol] = position
                if symbol_data.index[position] == date and position < len(symbol_data.index) - 1:
                    tradable.add(symbol)
            positions_by_date[date] = positions
            tradable_by_date[date] = tradable
        return positions_by_date, tradable_by_date

    def event_timeline():
        return list(store.timeline())

    return _comparison(_measure(old_searchsorted_maps), _measure(event_timeline))


def main() -> None:
    bars = _bars()
    report = {
        "environment": {"python": __import__("sys").version.split()[0], "runs": RUNS, "bars": len(bars)},
        "feature_preparation": _feature_benchmark(bars),
        "detector_replay": _detector_benchmark(bars),
        "multi_asset_timeline": _timeline_benchmark(bars),
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
