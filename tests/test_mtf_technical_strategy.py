from __future__ import annotations

import math
from types import SimpleNamespace

import pandas as pd

from backtest.mtf_technical_strategy import StrategyConfig, _resolve_entry_leverage, align_trend_to_entry, run_backtest


def test_align_trend_to_entry_uses_only_closed_higher_bar():
    entry_idx = pd.date_range("2025-01-01 00:00:00", periods=6, freq="1h", tz="UTC")
    entry_df = pd.DataFrame(
        {
            "open": [100, 101, 102, 103, 104, 105],
            "high": [101, 102, 103, 104, 105, 106],
            "low": [99, 100, 101, 102, 103, 104],
            "close": [100.5, 101.5, 102.5, 103.5, 104.5, 105.5],
            "volume": [1, 1, 1, 1, 1, 1],
            "close_ts": entry_idx + pd.Timedelta(hours=1),
            "ema144": [1] * 6,
            "ema169": [1] * 6,
            "ma55": [1] * 6,
            "ma55_slope": [1] * 6,
            "macd_line": [1] * 6,
            "macd_signal": [0] * 6,
            "macd_hist": [1] * 6,
        },
        index=entry_idx,
    )

    trend_idx = pd.date_range("2025-01-01 00:00:00", periods=2, freq="4h", tz="UTC")
    trend_df = pd.DataFrame(
        {
            "close_ts": trend_idx + pd.Timedelta(hours=4),
            "trend_score": [3, -3],
            "trend_bias": [1, -1],
            "ema144": [1, 1],
            "ema169": [1, 1],
            "ma55": [1, 1],
            "ma55_slope": [1, -1],
            "macd_line": [1, -1],
            "macd_signal": [0, 0],
            "macd_hist": [1, -1],
        },
        index=trend_idx,
    )

    aligned = align_trend_to_entry(entry_df, trend_df)

    assert pd.isna(aligned.iloc[0]["trend_bias_htf"])
    assert pd.isna(aligned.iloc[1]["trend_bias_htf"])
    assert pd.isna(aligned.iloc[2]["trend_bias_htf"])
    assert aligned.iloc[3]["trend_bias_htf"] == 1
    assert aligned.iloc[4]["trend_bias_htf"] == 1
    assert aligned.iloc[5]["trend_bias_htf"] == 1


def test_run_backtest_executes_on_next_bar_open():
    idx = pd.date_range("2025-01-01 00:00:00", periods=4, freq="1h", tz="UTC")
    signal_frame = pd.DataFrame(
        {
            "open": [10.0, 11.0, 13.0, 14.0],
            "high": [10.5, 13.5, 14.5, 14.5],
            "low": [9.5, 10.5, 12.5, 13.5],
            "close": [10.2, 13.2, 13.8, 14.1],
            "atr": [0.5, 0.5, 0.5, 0.5],
            "long_entry_signal": [True, False, False, False],
            "short_entry_signal": [False, False, False, False],
            "long_exit_signal": [False, False, True, False],
            "short_exit_signal": [False, False, False, False],
        },
        index=idx,
    )

    config = StrategyConfig(
        symbol="BTCUSD",
        initial_capital=100.0,
        fee_rate=0.0,
        slippage_rate=0.0,
        allow_short=False,
        atr_stop_multiple=10.0,
        atr_target_multiple=10.0,
    )
    result = run_backtest(signal_frame, config)

    assert result.total_trades == 1
    trade = result.trades.iloc[0]
    assert trade["entry_time"] == idx[1]
    assert trade["exit_time"] == idx[3]
    assert math.isclose(trade["entry_price"], 11.0)
    assert math.isclose(trade["exit_price"], 14.0)
    assert math.isclose(trade["leverage"], 1.0)
    assert math.isclose(result.final_equity, 100.0 + (14.0 - 11.0) * (100.0 / 11.0))


def test_run_backtest_leverage_scales_position_notional():
    idx = pd.date_range("2025-01-01 00:00:00", periods=3, freq="1h", tz="UTC")
    signal_frame = pd.DataFrame(
        {
            "open": [10.0, 10.0, 12.0],
            "high": [10.5, 12.5, 12.5],
            "low": [9.5, 9.8, 11.8],
            "close": [10.0, 12.0, 12.0],
            "atr": [0.5, 0.5, 0.5],
            "long_entry_signal": [True, False, False],
            "short_entry_signal": [False, False, False],
            "long_exit_signal": [False, True, False],
            "short_exit_signal": [False, False, False],
        },
        index=idx,
    )

    base = StrategyConfig(
        symbol="BTCUSD",
        initial_capital=100.0,
        fee_rate=0.0,
        slippage_rate=0.0,
        allow_short=False,
        leverage=1.0,
        atr_stop_multiple=10.0,
        atr_target_multiple=10.0,
    )
    leveraged = StrategyConfig(
        symbol="BTCUSD",
        initial_capital=100.0,
        fee_rate=0.0,
        slippage_rate=0.0,
        allow_short=False,
        leverage=2.0,
        atr_stop_multiple=10.0,
        atr_target_multiple=10.0,
    )

    result_base = run_backtest(signal_frame, base)
    result_2x = run_backtest(signal_frame, leveraged)

    assert result_base.total_trades == 1
    assert result_2x.total_trades == 1
    assert math.isclose(result_base.trades.iloc[0]["notional"], 100.0)
    assert math.isclose(result_2x.trades.iloc[0]["notional"], 200.0)
    assert math.isclose(result_2x.trades.iloc[0]["pnl"], 2 * result_base.trades.iloc[0]["pnl"])


def test_run_backtest_records_short_trade_stats():
    idx = pd.date_range("2025-01-01 00:00:00", periods=3, freq="1h", tz="UTC")
    signal_frame = pd.DataFrame(
        {
            "open": [10.0, 10.0, 8.0],
            "high": [10.5, 10.2, 8.2],
            "low": [9.5, 7.8, 7.8],
            "close": [10.0, 8.0, 8.0],
            "atr": [0.5, 0.5, 0.5],
            "long_entry_signal": [False, False, False],
            "short_entry_signal": [True, False, False],
            "long_exit_signal": [False, False, False],
            "short_exit_signal": [False, True, False],
        },
        index=idx,
    )

    config = StrategyConfig(
        symbol="BTCUSD",
        initial_capital=100.0,
        fee_rate=0.0,
        slippage_rate=0.0,
        allow_short=True,
        leverage=1.0,
        atr_stop_multiple=10.0,
        atr_target_multiple=10.0,
    )
    result = run_backtest(signal_frame, config)

    assert result.total_trades == 1
    assert result.long_trades == 0
    assert result.short_trades == 1
    assert result.short_win_rate == 1.0
    trade = result.trades.iloc[0]
    assert trade["side"] == "short"
    assert math.isclose(trade["pnl"], 20.0)


def test_dynamic_leverage_resolves_to_1x_2x_3x_tiers():
    config = StrategyConfig(
        symbol="BTCUSD",
        dynamic_leverage=True,
        min_leverage=1.0,
        max_leverage=3.0,
    )

    weak = SimpleNamespace(trend_score_htf=2.0, entry_score=1.0, close=100.0, atr=3.0)
    medium = SimpleNamespace(trend_score_htf=3.0, entry_score=2.0, close=100.0, atr=3.0)
    strong = SimpleNamespace(trend_score_htf=4.0, entry_score=3.0, close=100.0, atr=1.0)

    assert math.isclose(_resolve_entry_leverage(weak, config), 1.0)
    assert math.isclose(_resolve_entry_leverage(medium, config), 2.0)
    assert math.isclose(_resolve_entry_leverage(strong, config), 3.0)
