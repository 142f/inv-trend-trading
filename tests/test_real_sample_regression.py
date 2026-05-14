from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from turtle_multi_asset import TurtleBacktester, turtle_rules
from turtle_multi_asset.integrations.mt5 import _infer_asset_fields
from turtle_multi_asset.models import AssetSpec


def load_processed_csv(data_dir: str, timeframe: str, symbol: str) -> pd.DataFrame:
    path = Path(data_dir) / "processed" / "mt5" / timeframe / f"{symbol}.csv"
    if not path.exists():
        raise FileNotFoundError(path)
    df = pd.read_csv(path, parse_dates=["time"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time").sort_index()[["open", "high", "low", "close", "volume", "spread"]]


def align_data(
    data: dict[str, pd.DataFrame],
    align_start: bool = False,
    align_end: bool = False,
) -> dict[str, pd.DataFrame]:
    if not data:
        return data
    start = max(df.index[0] for df in data.values()) if align_start else None
    end = min(df.index[-1] for df in data.values()) if align_end else None
    aligned: dict[str, pd.DataFrame] = {}
    for symbol, df in data.items():
        out = df
        if start is not None:
            out = out.loc[out.index >= start]
        if end is not None:
            out = out.loc[out.index <= end]
        if out.empty:
            raise ValueError(f"no data left for {symbol}")
        aligned[symbol] = out
    return aligned


def load_asset_specs(data_dir: str, symbols: list[str]) -> dict[str, AssetSpec]:
    specs_path = Path(data_dir) / "metadata" / "mt5" / "symbol_specs.csv"
    specs_df = pd.read_csv(specs_path) if specs_path.exists() else pd.DataFrame()
    specs: dict[str, AssetSpec] = {}
    for symbol in symbols:
        inferred = _infer_asset_fields(symbol)
        row = specs_df.loc[specs_df["name"] == symbol].tail(1) if not specs_df.empty else pd.DataFrame()
        point_value = float(row["trade_contract_size"].iloc[0]) if not row.empty else 1.0
        qty_step = float(row["volume_step"].iloc[0]) if not row.empty else 1.0
        min_qty = float(row["volume_min"].iloc[0]) if not row.empty else 0.0
        specs[symbol] = AssetSpec(
            symbol=symbol,
            asset_class=str(inferred["asset_class"]),
            cluster=str(inferred["cluster"]),
            point_value=point_value,
            qty_step=qty_step,
            min_qty=min_qty,
            can_long=True,
            can_short=True,
            max_units=int(inferred["max_units"]),
            unit_1n_risk_pct=float(inferred["unit_1n_risk_pct"]),
            max_symbol_1n_risk_pct=float(inferred["max_symbol_1n_risk_pct"]),
            max_symbol_leverage=float(inferred["max_symbol_leverage"]),
            cost_bps=float(inferred["cost_bps"]),
            slippage_bps=float(inferred["slippage_bps"]),
        )
    return specs


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
