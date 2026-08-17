"""Run D1 Turtle experiments for pruned risk-factor universes."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from research.d1_suite.scripts.d1_backtest_common import (
    CORE_SYMBOLS,
    CONSUMER_ETF_WEAK,
    CRYPTO,
    EQUITY_SYMBOLS,
    METALS,
    PLATFORM_CORE,
    SEMIS,
    SHORT_HISTORY,
    build_run_specs,  # noqa: F401 - compatibility export used by regression tests
    drawdown_window,
    load_universe,
    run_backtest,
    symbol_trade_stats,
    write_backtest_outputs,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-data-dir", default="data_external_xau_btc_xag_eth")
    parser.add_argument("--equity-data-dir", default="data_external_equities")
    parser.add_argument("--out-dir", default="outputs/d1_pruned_universe_x3")
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    all_data = load_universe(
        core_data_dir=Path(args.core_data_dir),
        equity_data_dir=Path(args.equity_data_dir),
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    runs = [
        {
            "run": "core4_macro_crypto",
            "group": "1_core_macro_crypto",
            "symbols": METALS + CRYPTO,
            "eth_mode": "long_short",
            "description": "XAU/XAG long only + BTC/ETH long short",
        },
        {
            "run": "core3_no_eth",
            "group": "1b_core_without_eth",
            "symbols": METALS + ["BTCUSDT_BINANCE"],
            "eth_mode": "excluded",
            "description": "XAU/XAG long only + BTC long short",
        },
        {
            "run": "core4_eth_short_only",
            "group": "1c_core_eth_short_only",
            "symbols": METALS + CRYPTO,
            "eth_mode": "short_only",
            "description": "XAU/XAG long only + BTC long short + ETH short only",
        },
        {
            "run": "core4_plus_semis",
            "group": "2_add_semis",
            "symbols": METALS + CRYPTO + SEMIS,
            "eth_mode": "long_short",
            "description": "Core 4 + NVDA/AMD/MU/TSM/AVGO",
        },
        {
            "run": "core4_plus_semis_eth_short_only",
            "group": "2b_add_semis_eth_short_only",
            "symbols": METALS + CRYPTO + SEMIS,
            "eth_mode": "short_only",
            "description": "Core 4 + semis, but ETH short only",
        },
        {
            "run": "core4_plus_semis_platform",
            "group": "3_add_platform",
            "symbols": METALS + CRYPTO + SEMIS + PLATFORM_CORE,
            "eth_mode": "long_short",
            "description": "Core 4 + semis + MSFT/META/GOOGL/AMZN",
        },
        {
            "run": "pruned_final_eth_short_only",
            "group": "3b_pruned_final",
            "symbols": METALS + CRYPTO + SEMIS + PLATFORM_CORE,
            "eth_mode": "short_only",
            "description": "Pruned final candidate: no C/D names, ETH short only",
        },
        {
            "run": "compact_a_grade",
            "group": "a_grade_compact",
            "symbols": METALS + ["BTCUSDT_BINANCE"] + ["NVDA", "AMD", "MU", "MSFT", "META"],
            "eth_mode": "excluded",
            "description": "A-grade compact set only",
        },
        {
            "run": "add_consumer_etf_weak_bucket",
            "group": "4_add_c_bucket",
            "symbols": METALS + CRYPTO + SEMIS + PLATFORM_CORE + CONSUMER_ETF_WEAK,
            "eth_mode": "long_short",
            "description": "Group 3 plus SPY/QQQ/XLY/AAPL/TSLA/NFLX/ORCL",
        },
        {
            "run": "previous_full20_no_pltr_sndk",
            "group": "previous_reference",
            "symbols": CORE_SYMBOLS + [s for s in EQUITY_SYMBOLS if s not in SHORT_HISTORY],
            "eth_mode": "long_short",
            "description": "Previous 20-symbol common reference without PLTR/SNDK",
        },
    ]

    summary_rows: list[dict] = []
    for run in runs:
        result, data, _, _ = run_backtest(
            all_data=all_data,
            symbols=run["symbols"],
            initial_equity=args.initial_equity,
            align_start=True,
            align_end=True,
            eth_mode=str(run["eth_mode"]),
        )
        run_dir = out_dir / str(run["run"])
        write_backtest_outputs(result, run_dir)

        row = {
            "run": run["run"],
            "group": run["group"],
            "description": run["description"],
            "symbols": "+".join(data),
            "symbol_count": len(data),
            "start": max(df.index[0] for df in data.values()),
            "end": min(df.index[-1] for df in data.values()),
            "final_equity": float(result.equity_curve.iloc[-1]),
            "orders": int(len(result.orders)),
            **result.metrics,
        }
        row.update(symbol_trade_stats(result.trades, list(data)))
        row.update(drawdown_window(result.equity_curve))
        summary_rows.append(row)

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out_dir / "summary.csv", index=False)
    print(
        summary[
            [
                "run",
                "symbol_count",
                "start",
                "end",
                "final_equity",
                "total_return",
                "cagr",
                "max_drawdown",
                "sharpe_like",
                "mar",
                "trade_count",
            ]
        ].to_string(index=False)
    )
    print(f"Wrote outputs to: {out_dir.resolve()}")
if __name__ == "__main__":
    main()
