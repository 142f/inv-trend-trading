from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from inv_trend.adapters.multi_asset.backtest import TurtleBacktester
from inv_trend.adapters.multi_asset.config import BacktestConfig, load_config
from inv_trend.adapters.multi_asset.models import AssetSpec, TurtleRules


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


def test_explicit_missing_config_fails_closed() -> None:
    missing = Path(".definitely_missing_backtest_config.yaml")
    assert not missing.exists()
    with pytest.raises(FileNotFoundError, match="missing config file"):
        load_config(missing)


def test_unknown_config_key_is_rejected() -> None:
    path = Path("virtual-invalid-config.yaml")
    with (
        patch.object(Path, "exists", return_value=True),
        patch.object(
            Path,
            "read_text",
            return_value="initial_equity: 10000\nunknown_option: true\n",
        ),
        pytest.raises(ValueError, match="unknown_option"),
    ):
        load_config(path)


def test_evaluation_start_uses_prior_bars_for_indicator_warmup() -> None:
    rules = TurtleRules(
        n_period=3,
        fast_entry=3,
        slow_entry=5,
        fast_exit=2,
        slow_exit=3,
        skip_fast_after_win=False,
    )
    bars = _bars()
    evaluation_start = bars.index[15]
    previous_close = float(bars.loc[bars.index[14], "close"])
    bars.loc[evaluation_start, ["open", "high", "low", "close"]] = [
        previous_close,
        previous_close + 21.0,
        previous_close - 1.0,
        previous_close + 20.0,
    ]
    result = TurtleBacktester(
        {"TEST": bars},
        {"TEST": AssetSpec("TEST", "synthetic", "test")},
        rules=rules,
        evaluation_start=evaluation_start,
    ).run()

    assert result.equity_curve.index[0] == evaluation_start
    assert not result.orders.empty
    assert result.orders.iloc[0]["time"] == bars.index[16]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"qty_step": 0.0},
        {"cost_bps": -1.0},
        {"max_units": 0},
        {"point_value": float("nan")},
    ],
)
def test_asset_spec_rejects_invalid_trading_parameters(kwargs) -> None:
    with pytest.raises(ValueError):
        AssetSpec("TEST", "synthetic", "test", **kwargs)
