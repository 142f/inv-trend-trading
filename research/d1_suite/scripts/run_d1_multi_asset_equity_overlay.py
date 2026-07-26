"""Run D1 Turtle tests for metals, crypto, and Yahoo equities."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from research.d1_suite.scripts.d1_backtest_common import (
    CORE_SYMBOLS,
    EQUITY_SYMBOLS,
    align_data,
    build_specs,
    load_universe,
    max_leverage_stats,
    rules_3x,
    symbol_trade_stats,
    trim_data,
    write_backtest_outputs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-data-dir", default="data_external_xau_btc_xag_eth")
    parser.add_argument("--equity-data-dir", default="data_external_equities")
    parser.add_argument("--out-dir", default="outputs/d1_equity_overlay_x3")
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    all_data = load_universe(
        core_data_dir=Path(args.core_data_dir),
        equity_data_dir=Path(args.equity_data_dir),
    )
    all_data = trim_data(all_data, start=args.start, end=args.end)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = [
        {
            "run": "core4_recommended_d1",
            "symbols": [symbol for symbol in CORE_SYMBOLS if symbol in all_data],
            "policy": "metals_long_only_crypto_long_short",
            "align_start": False,
            "equity_short": False,
            "include_equities": False,
        },
        {
            "run": "core4_common_btc_d1",
            "symbols": [symbol for symbol in CORE_SYMBOLS if symbol in all_data],
            "policy": "metals_long_only_crypto_long_short_common_from_btc",
            "align_start": True,
            "equity_short": False,
            "include_equities": False,
        },
        {
            "run": "equities_only_long_only",
            "symbols": [symbol for symbol in EQUITY_SYMBOLS if symbol in all_data],
            "policy": "equities_long_only",
            "align_start": False,
            "equity_short": False,
            "include_equities": True,
        },
        {
            "run": "core4_plus_equities_long_only",
            "symbols": [symbol for symbol in CORE_SYMBOLS + EQUITY_SYMBOLS if symbol in all_data],
            "policy": "metals_long_only_crypto_long_short_equities_long_only",
            "align_start": False,
            "equity_short": False,
            "include_equities": True,
        },
        {
            "run": "equities_only_common_2016_no_pltr_sndk",
            "symbols": [
                symbol
                for symbol in EQUITY_SYMBOLS
                if symbol in all_data and symbol not in {"PLTR", "SNDK"}
            ],
            "policy": "equities_long_only_common_2016_universe",
            "align_start": True,
            "equity_short": False,
            "include_equities": True,
        },
        {
            "run": "core4_plus_equities_common_2017_no_pltr_sndk",
            "symbols": [
                symbol
                for symbol in CORE_SYMBOLS + EQUITY_SYMBOLS
                if symbol in all_data and symbol not in {"PLTR", "SNDK"}
            ],
            "policy": "metals_long_only_crypto_long_short_equities_long_only_common_2017_universe",
            "align_start": True,
            "equity_short": False,
            "include_equities": True,
        },
        {
            "run": "core4_plus_equities_long_short_probe",
            "symbols": [symbol for symbol in CORE_SYMBOLS + EQUITY_SYMBOLS if symbol in all_data],
            "policy": "diagnostic_equities_long_short",
            "align_start": False,
            "equity_short": True,
            "include_equities": True,
        },
    ]

    summary_rows: list[dict] = []
    for run_config in runs:
        data = {symbol: all_data[symbol] for symbol in run_config["symbols"]}
        data = align_data(data, align_start=bool(run_config["align_start"]), align_end=True)
        if len(data) < 2:
            continue
        specs = build_specs(
            list(data),
            equity_short=bool(run_config["equity_short"]),
            include_equities=bool(run_config["include_equities"]),
        )
        rules = rules_3x(include_equities=bool(run_config["include_equities"]))
        from turtle_multi_asset import TurtleBacktester

        result = TurtleBacktester(data=data, specs=specs, rules=rules, initial_equity=args.initial_equity).run()

        run_dir = out_dir / str(run_config["run"])
        write_backtest_outputs(result, run_dir)

        row = {
            "run": run_config["run"],
            "policy": run_config["policy"],
            "symbols": "+".join(data),
            "symbol_count": len(data),
            "start": min(df.index[0] for df in data.values()),
            "end": max(df.index[-1] for df in data.values()),
            "common_start": max(df.index[0] for df in data.values()),
            "common_end": min(df.index[-1] for df in data.values()),
            "final_equity": float(result.equity_curve.iloc[-1]),
            "orders": int(len(result.orders)),
            **result.metrics,
        }
        row.update(symbol_trade_stats(result.trades, list(data)))
        row.update(max_leverage_stats(result.orders, result.equity_curve))
        summary_rows.append(row)

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out_dir / "summary.csv", index=False)
    print(summary[[
        "run",
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
    ]].to_string(index=False))
    print(f"Wrote outputs to: {out_dir.resolve()}")
if __name__ == "__main__":
    main()
