from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from inv_trend.core.math_utils import directional_movement_index, rolling_percentile, true_range
from inv_trend.core.resampling import aggregate_completed_sessions
from inv_trend.core.features import FeatureCache, FeatureRequest, PreparedBars
from inv_trend.core.math_utils import macd, simple_moving_average
from inv_trend.core.performance import analyze_equity, drawdown_duration, equity_metric_kernel
from inv_trend.application.backtest.metrics import performance_metrics


@pytest.mark.parametrize("lookback,minimum", [(2, 2), (5, 2), (20, 20), (120, 30)])
@pytest.mark.parametrize("kind", ["random", "constant", "missing", "infinite", "empty"])
def test_percentile_matches_independent_window_reference(lookback, minimum, kind):
    values = np.random.default_rng(42).integers(-3, 4, 300).astype(float)
    if kind == "constant":
        values[:] = 4
    elif kind == "missing":
        values[::7] = np.nan
    elif kind == "infinite":
        values[::7], values[::11] = np.inf, -np.inf
    elif kind == "empty":
        values = values[:0]
    series = pd.Series(values, name="percentile")

    def reference(window):
        clean = window[np.isfinite(window)]
        if not np.isfinite(window[-1]):
            return np.nan
        return ((clean < window[-1]).sum() + 0.5 * (clean == window[-1]).sum()) / len(clean)

    expected = series.rolling(lookback, min_periods=minimum).apply(reference, raw=True)
    actual = rolling_percentile(series, lookback, min_periods=minimum)
    pd.testing.assert_series_equal(actual, expected, check_exact=True)
    pd.testing.assert_series_equal(
        rolling_percentile(series.iloc[:150], lookback, min_periods=minimum), actual.iloc[:150]
    )


@pytest.mark.parametrize("dtype", ["float64", "float32", "Float64"])
@pytest.mark.parametrize("size", [0, 1, 200])
def test_true_range_preserves_dtype_missing_values_and_index(dtype, size):
    rng = np.random.default_rng(23)
    index = pd.date_range("2024-01-01", periods=size, tz="Asia/Shanghai")
    high = pd.Series(rng.normal(10, 1, size), index=index, dtype=dtype)
    low, close = high - 2, high - 1
    if size > 10:
        high.iloc[3:5], low.iloc[4:6], close.iloc[2:5] = np.nan, np.nan, np.nan
    expected = pd.concat((high - low, (high - close.shift()).abs(),
                          (low - close.shift()).abs()), axis=1).max(axis=1)
    pd.testing.assert_series_equal(true_range(high, low, close), expected)


def test_empty_dmi_has_complete_output_schema():
    empty = pd.Series([], index=pd.DatetimeIndex([], tz="UTC"), dtype=float)
    result = directional_movement_index(empty, empty, empty)
    assert result.empty
    assert list(result) == ["plus_dm", "minus_dm", "plus_di", "minus_di", "dx", "adx"]


def test_ohlc_indicator_rejects_misaligned_indexes():
    high, low = pd.Series([3., 4.]), pd.Series([1., 2.], index=[1, 2])
    for operation in (true_range, directional_movement_index):
        with pytest.raises(ValueError, match="indexes must match"):
            operation(high, low, high)


def _bars(size=100, tz="UTC"):
    close = np.linspace(100, 120, size)
    return pd.DataFrame(dict(open=close, high=close + 2, low=close - 2,
                             close=close, volume=1000.),
                        index=pd.date_range("2024-01-01", periods=size, tz=tz, name="date"))


@pytest.mark.parametrize("tz", ["UTC", "Asia/Shanghai", None])
@pytest.mark.parametrize("group_size", [2, 5, 7, 200])
def test_session_aggregation_matches_independent_groups(tz, group_size):
    frame = _bars(tz=tz)
    frame.loc[frame.index[3:11], "volume"] = np.nan
    frame.loc[frame.index[3], "open"] = np.nan
    frame.loc[frame.index[7], "close"] = np.nan
    original = frame.copy(deep=True)
    actual = aggregate_completed_sessions(frame, group_size, anchor=frame.index[3])
    groups = [frame.iloc[start:start + group_size]
              for start in range(3, len(frame) - group_size + 1, group_size)]
    assert len(actual) == len(groups)
    for (_, row), group in zip(actual.iterrows(), groups):
        np.testing.assert_allclose(
            row[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float),
            [group.open.iloc[0], group.high.max(), group.low.min(), group.close.iloc[-1],
             group.volume.sum(min_count=1)], equal_nan=True,
        )
        assert row.session_start == group.index[0]
        assert row.session_end == group.index[-1]
    pd.testing.assert_frame_equal(frame, original)


@pytest.mark.parametrize("invalid", [True, 2.5, 1, "5"])
def test_session_count_rejects_non_integral_values(invalid):
    frame = _bars()
    with pytest.raises(ValueError, match="integer"):
        aggregate_completed_sessions(frame, invalid, anchor=frame.index[0])


def test_session_anchor_backfill_and_no_volume():
    frame = _bars().drop(columns="volume")
    first = aggregate_completed_sessions(frame.iloc[10:], 5, anchor=frame.index[10])
    second = aggregate_completed_sessions(frame, 5, anchor=frame.index[10])
    pd.testing.assert_frame_equal(first, second)
    assert first.volume.isna().all()


def test_shared_feature_averages_match_independent_indicators():
    frame = _bars()
    frame["sma_20_lag_1"] = -999.
    frame.attrs["source"] = {"version": 1}
    original = frame.copy(deep=True)
    spec = FeatureRequest(sma_lags=((20, 0), (20, 1), (20, 2)),
                          ema_periods=(26, 12), macd_periods=(12, 26, 9),
                          volume_sma_lags=((20, 0), (20, 1)))
    actual = PreparedBars.build(frame, spec).frame
    for lag in (0, 1, 2):
        pd.testing.assert_series_equal(actual[f"sma_20_lag_{lag}"],
                                       simple_moving_average(frame.close, 20, lag=lag),
                                       check_names=False)
    for name, expected in macd(frame.close).items():
        pd.testing.assert_series_equal(actual[name], expected)
    assert list(actual)[:len(frame.columns)] == list(frame.columns)
    actual.attrs["source"]["version"] = 2
    pd.testing.assert_frame_equal(frame, original)
    assert frame.attrs == original.attrs


@pytest.mark.parametrize("change", ["auxiliary", "attrs", "timezone", "index_name", "dtype", "columns", "frequency"])
def test_feature_cache_preserves_snapshot_payload(change):
    frame = _bars()
    if change == "auxiliary":
        frame["provider"] = "old"
    if change == "attrs":
        frame.attrs["dataset_version"] = "old"
    cache, spec = FeatureCache(), FeatureRequest(sma_lags=((20, 0),))
    cache.prepare(frame, spec)
    changed = frame.copy()
    if change == "auxiliary":
        changed["provider"] = "new"
    elif change == "attrs":
        changed.attrs["dataset_version"] = "new"
    elif change == "timezone":
        changed.index = changed.index.tz_convert("Asia/Shanghai")
    elif change == "index_name":
        changed.index = changed.index.rename("new_date")
    elif change == "dtype":
        changed["volume"] = changed.volume.astype("int64")
    elif change == "frequency":
        changed.index = changed.index.copy(deep=True)
        changed.index.freq = None
    else:
        changed = changed[list(reversed(changed.columns))]
    actual = cache.prepare(changed, spec).frame
    expected = PreparedBars.build(changed, spec).frame
    pd.testing.assert_frame_equal(actual, expected)
    assert actual.attrs == expected.attrs


def test_feature_and_resampling_reject_duplicate_columns():
    frame = _bars()
    duplicate = pd.concat([frame, frame[["close"]]], axis=1)
    with pytest.raises(ValueError, match="unique column"):
        PreparedBars.build(duplicate, FeatureRequest())
    with pytest.raises(ValueError, match="unique column"):
        aggregate_completed_sessions(duplicate, 5, anchor=frame.index[0])


@pytest.mark.parametrize("values,expected", [
    ([], (0, None)), ([0., 0.], (0, None)),
    ([0., -.1, 0., -.5, -.3, -.2, 0.], (3, 3)),
    ([0., -.1, 0., -.5, -.3], (2, None)),
    ([0., -.5, -.5, 0.], (2, 2)),
    ([-.2, -.3, 0.], (2, 1)),
])
def test_global_drawdown_recovery(values, expected):
    assert drawdown_duration(pd.Series(values, dtype=float)) == expected


def test_drawdown_duration_matches_independent_episode_reference():
    rng = np.random.default_rng(913)
    for _ in range(100):
        values = rng.choice([0., -.1, -.2, -.3], size=100)
        lengths, current = [], 0
        for value in values:
            current = current + 1 if value < 0 else 0
            lengths.append(current)
        trough = min(range(len(values)), key=lambda i: values[i])
        recovery = next((i - trough for i in range(trough + 1, len(values))
                         if values[i] >= 0), None)
        assert drawdown_duration(pd.Series(values)) == (max(lengths), recovery)


def test_performance_uses_global_trough_and_does_not_mutate_inputs():
    equity = pd.Series([100., 90., 100., 50., 70., 80., 100.],
                       index=pd.date_range("2024-01-01", periods=7))
    trades = pd.DataFrame(dict(pnl=[10., -5.], holding_bars=[2, 3]))
    original = trades.copy()
    metrics, _ = performance_metrics(equity, trades)
    assert metrics["max_drawdown_recovery_bars"] == 3
    assert metrics["max_drawdown_duration_bars"] == 3
    assert metrics["max_drawdown"] == -.5
    analysis = analyze_equity(equity.iloc[::-1])
    assert analysis.metrics == equity_metric_kernel(equity)
    pd.testing.assert_series_equal(analysis.curve, equity)
    pd.testing.assert_frame_equal(trades, original)


def test_empty_and_bankrupt_equity_contract():
    assert equity_metric_kernel(pd.Series(dtype=float)) == {}
    equity = pd.Series([100., 0.], index=pd.date_range("2024-01-01", periods=2))
    assert equity_metric_kernel(equity)["annualized_return"] == -1.
    with pytest.raises(ValueError, match="above zero"):
        equity_metric_kernel(equity.iloc[::-1].reset_index(drop=True))
