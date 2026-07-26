"""Unified D1 backtest CLI for common presets and custom symbol sets."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from research.d1_suite.scripts.d1_backtest_common import (
    PRESET_RUNS,
    load_universe,
    max_leverage_stats,
    run_backtest,
    symbol_trade_stats,
    write_backtest_outputs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-data-dir", default="data_external_xau_btc_xag_eth")
    parser.add_argument("--equity-data-dir", default="data_external_equities")
    parser.add_argument("--preset", choices=sorted(PRESET_RUNS), default="revised_8_no_eth")
    parser.add_argument("--symbols", nargs="+", help="Override preset symbols with an explicit list.")
    parser.add_argument("--eth-mode", choices=["long_short", "short_only", "excluded"], default=None)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--out-dir", default="outputs/d1_standard_backtest")
    parser.add_argument("--list-presets", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.list_presets:
        for name, config in sorted(PRESET_RUNS.items()):
            print(f"{name}: {config['description']}")
        return

    all_data = load_universe(
        core_data_dir=Path(args.core_data_dir),
        equity_data_dir=Path(args.equity_data_dir),
    )
    preset = PRESET_RUNS[args.preset]
    symbols = args.symbols or preset["symbols"]
    eth_mode = args.eth_mode or preset["eth_mode"]

    result, data, _, _ = run_backtest(
        all_data=all_data,
        symbols=symbols,
        initial_equity=args.initial_equity,
        align_start=bool(preset.get("align_start", True)),
        align_end=True,
        start=args.start,
        end=args.end,
        eth_mode=eth_mode,
    )

    out_dir = Path(args.out_dir)
    write_backtest_outputs(result, out_dir)

    summary = {
        "preset": args.preset,
        "symbols": list(data),
        "symbol_count": len(data),
        "start": str(min(df.index[0] for df in data.values())),
        "end": str(max(df.index[-1] for df in data.values())),
        "common_start": str(max(df.index[0] for df in data.values())),
        "common_end": str(min(df.index[-1] for df in data.values())),
        "final_equity": float(result.equity_curve.iloc[-1]),
        **result.metrics,
    }
    summary.update(symbol_trade_stats(result.trades, list(data)))
    summary.update(max_leverage_stats(result.orders, result.equity_curve))
    pd.DataFrame([summary]).to_csv(out_dir / "summary.csv", index=False)

    columns = [
        "preset",
        "symbol_count",
        "start",
        "common_start",
        "end",
        "final_equity",
        "total_return",
        "cagr",
        "max_drawdown",
        "sharpe_like",
        "mar",
        "trade_count",
    ]
    print(pd.DataFrame([summary])[columns].to_string(index=False))
    print(f"Wrote outputs to: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
