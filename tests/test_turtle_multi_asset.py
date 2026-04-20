from __future__ import annotations

import numpy as np
import pandas as pd

from turtle_multi_asset import (
    AssetSpec,
    MultiAssetTurtleStrategy,
    PortfolioState,
    TurtleBacktester,
    TurtleRules,
    compute_turtle_indicators,
)


def _trend_bars(periods: int = 90, start: float = 100.0, end: float = 160.0) -> pd.DataFrame:
    index = pd.bdate_range("2024-01-01", periods=periods)
    close = np.linspace(start, end, periods)
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame(
        {
            "open": open_,
            "high": close + 1.0,
            "low": close - 1.0,
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
