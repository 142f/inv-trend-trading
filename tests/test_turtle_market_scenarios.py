from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from turtle_multi_asset import (
    AssetSpec,
    MultiAssetTurtleStrategy,
    PortfolioState,
    Position,
    PositionUnit,
    TurtleBacktester,
    TurtleRules,
    compute_turtle_indicators,
)


def _scenario_rules(**overrides: object) -> TurtleRules:
    params = {
        "fast_entry": 5,
        "slow_entry": 8,
        "fast_exit": 3,
        "slow_exit": 5,
        "n_period": 5,
        "skip_fast_after_win": False,
        "max_total_1n_risk_pct": 1.0,
        "max_direction_1n_risk_pct": 1.0,
        "default_cluster_1n_risk_pct": 1.0,
        "max_total_leverage": 10.0,
        "max_direction_leverage": 10.0,
        "default_cluster_leverage": 10.0,
    }
    params.update(overrides)
    return TurtleRules(**params)


def _asset_spec(
    symbol: str = "TEST",
    *,
    cost_bps: float = 0.0,
    slippage_bps: float = 0.0,
    max_units: int = 3,
) -> AssetSpec:
    return AssetSpec(
        symbol,
        "synthetic",
        "test",
        qty_step=1,
        cost_bps=cost_bps,
        slippage_bps=slippage_bps,
        max_units=max_units,
        max_symbol_1n_risk_pct=1.0,
        max_symbol_leverage=10.0,
    )


def _trend_bars(periods: int = 40, start: float = 100.0, end: float = 160.0) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=periods, freq="4h", tz="UTC")
    close = np.linspace(start, end, periods)
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + 1.0,
            "low": np.minimum(open_, close) - 1.0,
            "close": close,
        },
        index=index,
    )


def _run_single_symbol(
    bars: pd.DataFrame,
    rules: TurtleRules,
    spec: AssetSpec | None = None,
    initial_equity: float = 10_000.0,
):
    symbol = (spec.symbol if spec is not None else "TEST")
    return TurtleBacktester(
        {symbol: bars},
        {symbol: spec or _asset_spec(symbol)},
        rules,
        initial_equity=initial_equity,
    ).run()


def test_long_and_short_trends_are_symmetric_for_entries_adds_and_pnl() -> None:
    rules = _scenario_rules()
    long_result = _run_single_symbol(_trend_bars(start=100.0, end=160.0), rules)
    short_result = _run_single_symbol(_trend_bars(start=160.0, end=100.0), rules)

    assert long_result.orders["action"].tolist() == ["open", "add", "add", "exit"]
    assert short_result.orders["action"].tolist() == ["open", "add", "add", "exit"]
    assert set(long_result.orders["side"]) == {1}
    assert set(short_result.orders["side"]) == {-1}

    long_trade = long_result.trades.iloc[0]
    short_trade = short_result.trades.iloc[0]
    assert long_trade["side_name"] == "long"
    assert short_trade["side_name"] == "short"
    assert long_trade["unit_count"] == 3
    assert short_trade["unit_count"] == 3
    assert long_trade["exit_reason"] == "end_of_test"
    assert short_trade["exit_reason"] == "end_of_test"
    assert long_trade["pnl"] == pytest.approx(short_trade["pnl"])


def test_long_gap_through_stop_fills_at_worse_open_price() -> None:
    rules = _scenario_rules(
        fast_entry=2,
        slow_entry=3,
        fast_exit=2,
        slow_exit=3,
        n_period=2,
    )
    bars = pd.DataFrame(
        [
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 102.0, 99.0, 101.0),
            (101.0, 106.0, 100.0, 105.0),
            (106.0, 108.0, 104.0, 107.0),
            (94.0, 95.0, 93.0, 95.0),
        ],
        columns=["open", "high", "low", "close"],
        index=pd.date_range("2024-01-01", periods=5, freq="4h", tz="UTC"),
    )

    result = _run_single_symbol(bars, rules, _asset_spec(max_units=1))

    assert result.orders["action"].tolist() == ["open", "exit"]
    assert result.orders.iloc[0]["fill_price"] == 106.0
    assert result.orders.iloc[1]["reason"] == "intraday_stop"
    assert result.orders.iloc[1]["fill_price"] == 94.0

    trade = result.trades.iloc[0]
    assert trade["exit_price"] == 94.0
    assert trade["exit_price"] < trade["final_stop"]
    assert trade["final_stop"] == pytest.approx(97.5)
    assert trade["pnl"] == pytest.approx(-132.0)


def test_short_gap_through_stop_fills_at_worse_open_price() -> None:
    rules = _scenario_rules(
        fast_entry=2,
        slow_entry=3,
        fast_exit=2,
        slow_exit=3,
        n_period=2,
    )
    bars = pd.DataFrame(
        [
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 101.0, 98.0, 99.0),
            (99.0, 100.0, 94.0, 95.0),
            (94.0, 96.0, 92.0, 93.0),
            (106.0, 107.0, 105.0, 106.0),
        ],
        columns=["open", "high", "low", "close"],
        index=pd.date_range("2024-01-01", periods=5, freq="4h", tz="UTC"),
    )

    result = _run_single_symbol(bars, rules, _asset_spec(max_units=1))

    assert result.orders["action"].tolist() == ["open", "exit"]
    assert result.orders.iloc[0]["side"] == -1
    assert result.orders.iloc[0]["fill_price"] == 94.0
    assert result.orders.iloc[1]["reason"] == "intraday_stop"
    assert result.orders.iloc[1]["fill_price"] == 106.0

    trade = result.trades.iloc[0]
    assert trade["exit_price"] == 106.0
    assert trade["exit_price"] > trade["final_stop"]
    assert trade["final_stop"] == pytest.approx(102.5)
    assert trade["pnl"] == pytest.approx(-132.0)


def test_whipsaw_sequence_records_repeated_fast_losses_without_stale_state() -> None:
    rules = _scenario_rules(
        fast_entry=3,
        slow_entry=6,
        fast_exit=2,
        slow_exit=3,
        n_period=3,
        skip_fast_after_win=False,
    )
    bars = pd.DataFrame(
        [
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 102.0, 99.0, 101.0),
            (101.0, 102.0, 100.0, 101.0),
            (101.0, 106.0, 100.0, 105.0),
            (105.0, 106.0, 103.0, 104.0),
            (94.0, 95.0, 93.0, 94.0),
            (94.0, 96.0, 92.0, 93.0),
            (105.0, 106.0, 104.0, 105.0),
            (105.0, 107.0, 104.0, 106.0),
            (95.0, 96.0, 94.0, 95.0),
        ],
        columns=["open", "high", "low", "close"],
        index=pd.date_range("2024-01-01", periods=10, freq="4h", tz="UTC"),
    )

    result = _run_single_symbol(bars, rules, _asset_spec(max_units=1))

    assert result.trades["entry_reason"].tolist() == [
        "long_3d_breakout",
        "short_3d_breakout",
    ]
    assert result.trades["exit_reason"].tolist() == ["intraday_stop", "intraday_stop"]
    assert (result.trades["pnl"] < 0).all()
    assert result.orders["action"].tolist() == ["open", "exit", "open", "exit"]


def test_fast_skip_after_win_falls_back_to_slow_breakout() -> None:
    rules = _scenario_rules(
        fast_entry=3,
        slow_entry=5,
        fast_exit=2,
        slow_exit=3,
        n_period=3,
        skip_fast_after_win=True,
    )
    strategy = MultiAssetTurtleStrategy({"TEST": _asset_spec()}, rules)
    row = {
        "open": 118.0,
        "high": 122.0,
        "low": 117.0,
        "close": 120.0,
        "n": 5.0,
        "high_3": 110.0,
        "low_3": 90.0,
        "high_5": 115.0,
        "low_5": 85.0,
        "high_2": 109.0,
        "low_2": 91.0,
    }
    state = PortfolioState(last_fast_trade_won={"TEST": True})

    orders = strategy.generate_orders({"TEST": row}, state, equity=10_000.0)

    assert len(orders) == 1
    assert orders[0].system == "slow"
    assert orders[0].reason == "long_5d_breakout"


def test_regime_shift_expands_n_reduces_size_and_add_frequency() -> None:
    rules = _scenario_rules(
        fast_entry=3,
        slow_entry=5,
        fast_exit=2,
        slow_exit=3,
        n_period=3,
    )
    index = pd.date_range("2024-01-01", periods=12, freq="4h", tz="UTC")
    low_vol_close = np.linspace(100.0, 101.0, 6)
    high_vol_close = np.linspace(102.0, 112.0, 6)
    close = np.r_[low_vol_close, high_vol_close]
    bars = pd.DataFrame(
        {
            "open": close,
            "high": close + np.r_[np.full(6, 0.5), np.full(6, 8.0)],
            "low": close - np.r_[np.full(6, 0.5), np.full(6, 8.0)],
            "close": close,
        },
        index=index,
    )
    indicators = compute_turtle_indicators(bars, rules)
    assert indicators["n"].iloc[-1] > indicators["n"].iloc[5] * 5

    strategy = MultiAssetTurtleStrategy({"TEST": _asset_spec()}, rules)
    low_n_row = {
        "open": 112.0,
        "high": 113.0,
        "low": 111.0,
        "close": 112.0,
        "n": 2.0,
        "high_3": 108.0,
        "low_3": 99.0,
        "high_5": 109.0,
        "low_5": 98.0,
        "high_2": 107.0,
        "low_2": 100.0,
    }
    high_n_row = dict(low_n_row, n=8.0)

    low_order = strategy.generate_orders({"TEST": low_n_row}, PortfolioState(), 10_000.0)[0]
    high_order = strategy.generate_orders({"TEST": high_n_row}, PortfolioState(), 10_000.0)[0]
    assert high_order.qty < low_order.qty
    assert low_order.signal_price - low_order.stop_price == pytest.approx(4.0)
    assert high_order.signal_price - high_order.stop_price == pytest.approx(16.0)

    state = PortfolioState(
        positions={
            "TEST": Position(
                symbol="TEST",
                side=1,
                system="fast",
                units=[PositionUnit(qty=1.0, entry_price=100.0, n_at_entry=2.0)],
                last_add_price=100.0,
                stop_price=96.0,
            )
        }
    )
    assert strategy.generate_orders({"TEST": dict(low_n_row, close=102.0)}, state, 10_000.0)
    assert not strategy.generate_orders({"TEST": dict(high_n_row, close=102.0)}, state, 10_000.0)


def test_closed_session_symbol_does_not_emit_new_order_on_btc_only_bar() -> None:
    rules = _scenario_rules(
        fast_entry=3,
        slow_entry=5,
        fast_exit=2,
        slow_exit=3,
        n_period=3,
    )
    specs = {
        "XAU": _asset_spec("XAU"),
        "BTC": _asset_spec("BTC"),
    }
    strategy = MultiAssetTurtleStrategy(specs, rules)
    xau_breakout_row = {
        "open": 118.0,
        "high": 122.0,
        "low": 117.0,
        "close": 120.0,
        "n": 5.0,
        "high_3": 110.0,
        "low_3": 90.0,
        "high_5": 115.0,
        "low_5": 85.0,
        "high_2": 109.0,
        "low_2": 91.0,
    }
    btc_row = dict(xau_breakout_row, close=100.0, high=101.0, low=99.0, high_3=110.0, high_5=115.0)

    orders = strategy.generate_orders(
        {"XAU": xau_breakout_row, "BTC": btc_row},
        PortfolioState(),
        equity=10_000.0,
        tradable_symbols={"BTC"},
    )

    assert [order.symbol for order in orders] == []


def test_high_costs_reduce_net_equity_and_are_recorded() -> None:
    rules = _scenario_rules(max_total_1n_risk_pct=1.0)
    bars = _trend_bars(start=100.0, end=150.0)

    zero_cost = _run_single_symbol(
        bars,
        rules,
        _asset_spec("TEST", cost_bps=0.0, slippage_bps=0.0, max_units=1),
    )
    high_cost = _run_single_symbol(
        bars,
        rules,
        _asset_spec("TEST", cost_bps=100.0, slippage_bps=100.0, max_units=1),
    )

    assert zero_cost.trades["total_cost"].sum() == 0.0
    assert high_cost.trades["total_cost"].sum() > 0.0
    assert high_cost.equity_curve.iloc[-1] < zero_cost.equity_curve.iloc[-1]
    assert high_cost.trades["pnl"].iloc[0] < zero_cost.trades["pnl"].iloc[0]
