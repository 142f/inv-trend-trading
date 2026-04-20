from __future__ import annotations

import numpy as np
import pandas as pd

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
from turtle_multi_asset.strategy import _risk_sized_qty


def _trend_bars(periods: int = 90, start: float = 100.0, end: float = 160.0) -> pd.DataFrame:
    index = pd.bdate_range("2024-01-01", periods=periods)
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


def test_indicators_shift_breakout_levels() -> None:
    rules = TurtleRules(fast_entry=5, slow_entry=10, fast_exit=3, slow_exit=5, n_period=5)
    bars = _trend_bars(periods=20)
    out = compute_turtle_indicators(bars, rules)
    row = out.iloc[10]
    prev_window_high = bars["high"].iloc[5:10].max()
    assert row["high_5"] == prev_window_high


def test_n_uses_wilder_smoothing() -> None:
    rules = TurtleRules(fast_entry=3, slow_entry=5, fast_exit=2, slow_exit=3, n_period=3)
    bars = _trend_bars(periods=6)
    out = compute_turtle_indicators(bars, rules)
    first_n = out["tr"].iloc[:3].mean()
    second_n = (first_n * 2 + out["tr"].iloc[3]) / 3
    assert out["n"].iloc[2] == first_n
    assert out["n"].iloc[3] == second_n


def test_risk_sizing_allows_exact_minimum_quantity() -> None:
    qty = _risk_sized_qty(
        equity=10_000,
        unit_1n_risk_pct=0.01,
        n=100,
        point_value=1,
        qty_step=1,
    )
    spec = AssetSpec("XAU", "metal", "precious_metals", min_qty=1, qty_step=1)
    assert qty == spec.min_qty


def test_strategy_emits_breakout_order() -> None:
    rules = TurtleRules(
        fast_entry=5,
        slow_entry=10,
        fast_exit=3,
        slow_exit=5,
        n_period=5,
        skip_fast_after_win=False,
    )
    specs = {"XAU": AssetSpec("XAU", "metal", "precious_metals", qty_step=0.01)}
    strategy = MultiAssetTurtleStrategy(specs, rules)
    orders = strategy.generate_orders(
        {"XAU": _trend_bars(periods=20)},
        PortfolioState(),
        equity=100_000,
    )
    assert len(orders) == 1
    assert orders[0].symbol == "XAU"
    assert orders[0].action == "open"
    assert orders[0].side == 1
    assert orders[0].qty > 0


def test_backtester_runs_and_produces_equity_curve() -> None:
    rules = TurtleRules(
        fast_entry=10,
        slow_entry=20,
        fast_exit=5,
        slow_exit=10,
        n_period=10,
        skip_fast_after_win=False,
        max_total_1n_risk_pct=0.05,
    )
    data = {"SPY": _trend_bars(periods=80, end=220)}
    specs = {"SPY": AssetSpec("SPY", "etf", "us_index", qty_step=1)}
    result = TurtleBacktester(data, specs, rules, initial_equity=100_000).run()
    assert not result.equity_curve.empty
    assert result.metrics["trade_count"] >= 1
    assert result.equity_curve.iloc[-1] > 0


def test_mark_equity_uses_last_available_price_for_closed_market() -> None:
    rules = TurtleRules(fast_entry=3, slow_entry=5, fast_exit=2, slow_exit=3, n_period=3)
    xau = pd.DataFrame(
        {
            "open": [100.0, 110.0],
            "high": [101.0, 111.0],
            "low": [99.0, 109.0],
            "close": [100.0, 110.0],
        },
        index=pd.to_datetime(["2024-01-01", "2024-01-02"], utc=True),
    )
    btc = pd.DataFrame(
        {
            "open": [200.0, 201.0, 202.0],
            "high": [201.0, 202.0, 203.0],
            "low": [199.0, 200.0, 201.0],
            "close": [200.0, 201.0, 202.0],
        },
        index=pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"], utc=True),
    )
    specs = {
        "XAU": AssetSpec("XAU", "metal", "precious_metals", qty_step=1),
        "BTC": AssetSpec("BTC", "crypto", "crypto", qty_step=1),
    }
    backtester = TurtleBacktester({"XAU": xau, "BTC": btc}, specs, rules)
    state = PortfolioState(
        positions={
            "XAU": Position(
                symbol="XAU",
                side=1,
                system="fast",
                units=[PositionUnit(qty=2, entry_price=100.0, n_at_entry=2.0)],
                last_add_price=100.0,
                stop_price=96.0,
            )
        }
    )
    equity = backtester._mark_equity(pd.Timestamp("2024-01-03", tz="UTC"), 10_000, state)
    assert equity == 10_020
