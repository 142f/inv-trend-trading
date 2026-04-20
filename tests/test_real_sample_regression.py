from __future__ import annotations

from pathlib import Path

import pytest

from examples.run_local_turtle_backtest import align_data, load_asset_specs, load_processed_csv
from turtle_multi_asset import TurtleBacktester, turtle_rules


def test_2022_xau_btc_h4_sample_keeps_structural_regression() -> None:
    data_dir = Path("data_2022_xau_btc")
    symbols = ["XAUUSDc", "BTCUSDc"]
    if not all((data_dir / "processed" / "mt5" / "H4" / f"{symbol}.csv").exists() for symbol in symbols):
        pytest.skip("local 2022 XAU/BTC H4 sample is not available")

    data = {
        symbol: load_processed_csv(str(data_dir), "H4", symbol)
        for symbol in symbols
    }
    data = align_data(data, align_start=True, align_end=True)
    specs = load_asset_specs(str(data_dir), symbols)
    rules = turtle_rules("h4-daily-equivalent")

    result = TurtleBacktester(
        data=data,
        specs=specs,
        rules=rules,
        initial_equity=10_000.0,
    ).run()

    final_equity = float(result.equity_curve.iloc[-1])
    assert 12_000.0 < final_equity < 20_000.0
    assert 120 <= len(result.orders) <= 220
    assert 50 <= len(result.trades) <= 110
    assert set(result.trades["symbol"]) == set(symbols)
    assert set(result.trades["side"]) == {-1, 1}
    assert {"stop", "trend_exit", "end_of_test"} & set(result.trades["exit_type"])
    assert result.metrics["max_drawdown"] > -0.30
    assert result.metrics["trade_count"] == pytest.approx(float(len(result.trades)))
