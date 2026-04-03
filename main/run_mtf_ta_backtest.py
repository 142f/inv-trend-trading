from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from backtest.mtf_technical_strategy import run_batch, save_result


def _args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run multi-timeframe technical-analysis backtests")
    p.add_argument("--symbols", nargs="+", default=["BTCUSD", "XAUUSD"])
    p.add_argument("--periods", nargs="+", default=["180d", "365d"])
    p.add_argument("--base-interval", default="60m", help="yfinance interval, e.g. 5m/15m/30m/60m/1d")
    p.add_argument("--entry-timeframe", default="1h", help="entry timeframe built from base data")
    p.add_argument("--trend-timeframe", default="4h", help="higher timeframe used as direction filter")
    p.add_argument("--initial-capital", type=float, default=10_000.0)
    p.add_argument("--leverage", type=float, default=1.0, help="notional leverage multiplier")
    p.add_argument("--leverages", nargs="+", type=float, default=None, help="run a leverage grid, e.g. 1 2 3 4 5")
    p.add_argument("--dynamic-leverage", action="store_true", help="use per-trade dynamic leverage")
    p.add_argument("--min-leverage", type=float, default=1.0, help="minimum per-trade leverage")
    p.add_argument("--max-leverage", type=float, default=1.0, help="maximum per-trade leverage")
    p.add_argument("--long-only", action="store_true")
    p.add_argument("--use-synthetic", action="store_true", help="offline demo using synthetic OHLCV")
    p.add_argument("--output-dir", default="artifacts/mtf_ta")
    return p.parse_args()


def _format_summary(summary: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "symbol",
        "data_source",
        "period",
        "entry_timeframe",
        "trend_timeframe",
        "dynamic_leverage",
        "leverage",
        "min_leverage",
        "max_leverage",
        "data_start",
        "data_end",
        "total_trades",
        "avg_leverage",
        "leverage_1x_trades",
        "leverage_2x_trades",
        "leverage_3x_trades",
        "long_trades",
        "short_trades",
        "win_rate",
        "long_win_rate",
        "short_win_rate",
        "total_return",
        "annual_return",
        "sharpe_ratio",
        "max_drawdown",
        "profit_factor",
        "avg_holding_days",
    ]
    out = summary[cols].copy()
    pct_cols = ["win_rate", "long_win_rate", "short_win_rate", "total_return", "annual_return", "max_drawdown"]
    for col in pct_cols:
        out[col] = out[col].map(lambda x: f"{x:.2%}" if pd.notna(x) else "nan")
    float_cols = ["leverage", "min_leverage", "max_leverage", "avg_leverage", "sharpe_ratio", "profit_factor", "avg_holding_days"]
    for col in float_cols:
        out[col] = out[col].map(lambda x: f"{x:.2f}" if pd.notna(x) else "nan")
    out["dynamic_leverage"] = out["dynamic_leverage"].map(lambda x: "yes" if bool(x) else "no")
    return out


def main() -> None:
    args = _args()
    try:
        summary, results = run_batch(
            symbols=args.symbols,
            periods=args.periods,
            base_interval=args.base_interval,
            entry_timeframe=args.entry_timeframe,
            trend_timeframe=args.trend_timeframe,
            initial_capital=args.initial_capital,
            leverage=args.leverage,
            leverages=args.leverages,
            dynamic_leverage=args.dynamic_leverage,
            min_leverage=args.min_leverage,
            max_leverage=args.max_leverage,
            allow_short=not args.long_only,
            use_synthetic=args.use_synthetic,
        )
    except Exception as exc:
        print(f"Backtest failed: {exc}")
        if not args.use_synthetic:
            print("Tip: Yahoo Finance may rate limit intraday requests. Retry later or add --use-synthetic.")
        return

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for result, period in zip(results, summary["period"].tolist()):
        save_result(result=result, output_dir=out_dir, period_label=period)

    summary.to_csv(out_dir / "summary.csv", index=False)

    print("MTF Technical Strategy Summary")
    print(_format_summary(summary).to_string(index=False))
    print(f"\nArtifacts saved to: {out_dir}")


if __name__ == "__main__":
    main()
