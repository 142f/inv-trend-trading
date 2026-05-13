"""使用本地 processed CSV 文件运行海龟回测。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from turtle_multi_asset import AssetSpec, TurtleBacktester, turtle_rules
from turtle_multi_asset.mt5_data import _infer_asset_fields


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--timeframe", default="H4")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--out-dir", default="outputs/local_turtle_h4")
    parser.add_argument("--equity", type=float, default=10_000.0)
    parser.add_argument(
        "--align-start",
        action="store_true",
        help="Trim all symbols to the latest first timestamp so the test uses a common sample.",
    )
    parser.add_argument(
        "--align-end",
        action="store_true",
        help="Trim all symbols to the earliest last timestamp.",
    )
    parser.add_argument(
        "--rule-profile",
        choices=["classic-bars", "h4-daily-equivalent"],
        default="classic-bars",
        help="classic-bars uses 20/55/10/20 H4 bars directly; h4-daily-equivalent scales by 6.",
    )
    parser.add_argument(
        "--long-only",
        action="store_true",
        help="Disable short entries for all symbols.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    timeframe = args.timeframe.upper()
    data = {
        symbol: load_processed_csv(args.data_dir, timeframe, symbol)
        for symbol in args.symbols
    }
    data = align_data(data, align_start=args.align_start, align_end=args.align_end)
    specs = load_asset_specs(args.data_dir, args.symbols)
    rules = turtle_rules(
        args.rule_profile,
        allow_short=not args.long_only,
    )

    result = TurtleBacktester(
        data=data,
        specs=specs,
        rules=rules,
        initial_equity=args.equity,
    ).run()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result.equity_curve.to_csv(out_dir / "equity_curve.csv")
    result.orders.to_csv(out_dir / "orders.csv", index=False)
    result.trades.to_csv(out_dir / "trades.csv", index=False)
    result.trade_details.to_csv(out_dir / "trade_details.csv", index=False)
    (out_dir / "metrics.json").write_text(
        json.dumps(result.metrics, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("Metrics")
    for key, value in result.metrics.items():
        print(f"{key}: {value:.6f}")
    print("\nData ranges")
    for symbol, df in data.items():
        print(f"{symbol}: {df.index[0]} -> {df.index[-1]} ({len(df)} bars)")
    print(f"\nWrote outputs to: {out_dir.resolve()}")


def load_processed_csv(data_dir: str, timeframe: str, symbol: str) -> pd.DataFrame:
    path = Path(data_dir) / "processed" / "mt5" / timeframe / f"{symbol}.csv"
    if not path.exists():
        raise FileNotFoundError(f"missing processed data file: {path}")
    df = pd.read_csv(path, parse_dates=["time"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    return df[["open", "high", "low", "close", "volume", "spread"]]


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
            raise ValueError(f"no data left for {symbol} after alignment")
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


if __name__ == "__main__":
    main()
