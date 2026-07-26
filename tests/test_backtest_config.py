from __future__ import annotations

import numpy as np
import pandas as pd

from turtle_multi_asset.backtest import TurtleBacktester
from turtle_multi_asset.config import BacktestConfig
from turtle_multi_asset.models import AssetSpec, TurtleRules


def _bars() -> pd.DataFrame:
    close = np.linspace(100.0, 120.0, 30)
    return pd.DataFrame(
        {
            "open": np.r_[close[0], close[:-1]],
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
        },
        index=pd.date_range("2024-01-01", periods=len(close), freq="D", tz="UTC"),
    )


def _backtester(**kwargs: object) -> TurtleBacktester:
    return TurtleBacktester(
        {"TEST": _bars()},
        {"TEST": AssetSpec("TEST", "synthetic", "test")},
        **kwargs,
    )


def test_backtester_uses_runtime_values_from_config() -> None:
    config = BacktestConfig(
        initial_equity=12_345.0,
        cash_model="cash",
        liquidate_at_end=False,
        rules={"fast_entry": 5},
    )

    backtester = _backtester(config=config)

    assert backtester.initial_equity == 12_345.0
    assert backtester.cash_model == "cash"
    assert backtester.liquidate_at_end is False
    assert backtester.rules.fast_entry == 5
    assert not backtester.run().equity_curve.empty


def test_explicit_backtest_arguments_override_config() -> None:
    config = BacktestConfig(
        initial_equity=12_345.0,
        cash_model="cash",
        liquidate_at_end=False,
        rules={"fast_entry": 5},
    )
    explicit_rules = TurtleRules(fast_entry=7)

    backtester = _backtester(
        rules=explicit_rules,
        initial_equity=54_321.0,
        cash_model="derivative",
        liquidate_at_end=True,
        config=config,
    )

    assert backtester.initial_equity == 54_321.0
    assert backtester.cash_model == "derivative"
    assert backtester.liquidate_at_end is True
    assert backtester.rules is explicit_rules
