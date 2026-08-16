from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from research.d1_suite.scripts.d1_backtest_common import load_csv
from inv_trend_application import BacktestService
from turtle_multi_asset import AssetSpec, TurtleRules
from turtle_multi_asset.config import BacktestConfig
from turtle_multi_asset.data.core_dataset import summarize_backtest_result


SYMBOLS = ["QQQ", "SPY"]
WINDOW_YEARS = [2, 3, 5, 10, 15, 20, 30]
DEFAULT_DATA_DIR = Path("processed_data/cleaned/data_external_equities")
DEFAULT_OUT_DIR = Path("research/strategy_variants/outputs/qqq_spy_trend_windows")
DEFAULT_INITIAL_EQUITY = 10_000.0
DEFAULT_MAX_LEVERAGE = 3.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--initial-equity", type=float, default=DEFAULT_INITIAL_EQUITY)
    parser.add_argument("--max-leverage", type=float, default=DEFAULT_MAX_LEVERAGE)
    return parser.parse_args()


def resolve_symbol_path(data_dir: Path, symbol: str) -> Path:
    matches = sorted(data_dir.glob(f"*_{symbol.lower()}_*_d1_cleaned.csv"))
    if not matches:
        raise FileNotFoundError(f"missing cleaned D1 file for {symbol} under {data_dir}")
    return matches[0]


def build_single_symbol_spec(symbol: str, max_leverage: float) -> AssetSpec:
    return AssetSpec(
        symbol=symbol,
        asset_class="etf",
        cluster="broad_equity",
        qty_step=1.0,
        min_qty=1.0,
        can_long=True,
        can_short=False,
        max_units=12,
        unit_1n_risk_pct=0.01,
        max_symbol_1n_risk_pct=0.12,
        max_symbol_leverage=max_leverage,
        cost_bps=1.0,
        slippage_bps=5.0,
    )


def build_single_symbol_rules(max_leverage: float) -> TurtleRules:
    return TurtleRules(
        n_period=20,
        fast_entry=20,
        slow_entry=55,
        fast_exit=10,
        slow_exit=20,
        stop_n=2.0,
        pyramid_step_n=0.5,
        trigger_mode="close",
        allow_short=False,
        max_total_1n_risk_pct=0.12,
        max_direction_1n_risk_pct=0.12,
        default_cluster_1n_risk_pct=0.12,
        cluster_1n_risk_pct={"broad_equity": 0.12},
        max_total_leverage=max_leverage,
        max_direction_leverage=max_leverage,
        default_cluster_leverage=max_leverage,
        cluster_leverage={"broad_equity": max_leverage},
    )


def buy_and_hold_metrics(window_data: pd.DataFrame, initial_equity: float) -> dict[str, float]:
    entry_price = float(window_data["close"].iloc[0])
    exit_price = float(window_data["close"].iloc[-1])
    total_return = exit_price / entry_price - 1.0
    years = max((window_data.index[-1] - window_data.index[0]).days / 365.25, 1 / 365.25)
    cagr = (1.0 + total_return) ** (1.0 / years) - 1.0
    running_max = window_data["close"].cummax()
    drawdown = window_data["close"] / running_max - 1.0
    return {
        "buy_hold_final_equity": initial_equity * (1.0 + total_return),
        "buy_hold_total_return": total_return,
        "buy_hold_cagr": cagr,
        "buy_hold_max_drawdown": float(drawdown.min()),
    }


def run_symbol_window(
    symbol: str,
    years: int,
    initial_equity: float,
    data_dir: Path,
    max_leverage: float,
) -> dict[str, object]:
    path = resolve_symbol_path(data_dir, symbol)
    full_data = load_csv(path)
    available_years = (full_data.index[-1] - full_data.index[0]).days / 365.25
    end_date = full_data.index[-1]
    start_cutoff = end_date - pd.DateOffset(years=years)

    if full_data.index[0] > start_cutoff:
        return {
            "symbol": symbol,
            "window_years": years,
            "status": "insufficient_history",
            "available_years": round(available_years, 2),
            "data_start": full_data.index[0].date().isoformat(),
            "data_end": end_date.date().isoformat(),
            "initial_equity": float(initial_equity),
        }

    window_data = full_data.loc[full_data.index >= start_cutoff].copy()
    specs = {symbol: build_single_symbol_spec(symbol, max_leverage)}
    rules = build_single_symbol_rules(max_leverage)
    result = BacktestService().run(
        data={symbol: window_data}, specs=specs, rules=rules,
        config=BacktestConfig(initial_equity=initial_equity),
    ).result
    summary = summarize_backtest_result(result)
    benchmark = buy_and_hold_metrics(window_data, initial_equity)
    return {
        "symbol": symbol,
        "window_years": years,
        "status": "ok",
        "available_years": round(available_years, 2),
        "data_start": full_data.index[0].date().isoformat(),
        "data_end": end_date.date().isoformat(),
        "test_start": window_data.index[0].date().isoformat(),
        "test_end": window_data.index[-1].date().isoformat(),
        "initial_equity": float(initial_equity),
        "final_equity": float(summary["final_equity"]),
        "total_return": float(summary["total_return"]),
        "cagr": float(summary["cagr"]),
        "max_drawdown": float(summary["max_drawdown"]),
        "trade_count": int(summary["trade_count"]),
        "signal_count": int(summary["signal_count"]),
        "win_rate": float(summary["win_rate"]),
        "payoff_ratio": float(summary["payoff_ratio"]),
        "avg_holding_days": float(summary["avg_holding_days"]),
        **benchmark,
    }


def format_pct(value: object) -> str:
    if value is None or value == "":
        return ""
    return f"{float(value) * 100:.2f}%"


def build_markdown(rows: list[dict[str, object]], initial_equity: float) -> str:
    lines = [
        "# QQQ / SPY Trend Backtest",
        "",
        f"- Initial equity: {initial_equity:.2f}",
        "- Strategy: single-symbol turtle trend, long-only, max leverage 3x",
        "- Data source: `processed_data/cleaned/data_external_equities`",
        "",
        "| Symbol | Window | Status | Test Start | Test End | Trend Final | Trend Return | Trend CAGR | BuyHold Final | BuyHold Return | BuyHold CAGR |",
        "| --- | ---: | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in rows:
        if row["status"] != "ok":
            lines.append(
                f"| {row['symbol']} | {row['window_years']}y | {row['status']} | {row.get('data_start', '')} | {row.get('data_end', '')} | - | - | - | - | - | - |"
            )
            continue
        lines.append(
            f"| {row['symbol']} | {row['window_years']}y | ok | {row['test_start']} | {row['test_end']} | "
            f"{float(row['final_equity']):.2f} | {format_pct(row['total_return'])} | {format_pct(row['cagr'])} | "
            f"{float(row['buy_hold_final_equity']):.2f} | {format_pct(row['buy_hold_total_return'])} | {format_pct(row['buy_hold_cagr'])} |"
        )
    lines.append("")
    lines.append("`insufficient_history` means the local cleaned dataset does not have enough years to support that window.")
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, object]] = []
    for symbol in SYMBOLS:
        for years in WINDOW_YEARS:
            rows.append(run_symbol_window(symbol, years, args.initial_equity, data_dir, args.max_leverage))

    frame = pd.DataFrame(rows)
    frame.to_csv(out_dir / "qqq_spy_trend_window_summary.csv", index=False)
    (out_dir / "qqq_spy_trend_window_summary.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (out_dir / "qqq_spy_trend_window_summary.md").write_text(
        build_markdown(rows, args.initial_equity),
        encoding="utf-8",
    )

    printable = frame.copy()
    for column in ["total_return", "cagr", "max_drawdown", "win_rate"]:
        if column in printable.columns:
            printable[column] = printable[column].map(lambda value: "" if pd.isna(value) else f"{float(value) * 100:.2f}%")
    print(printable.to_string(index=False))
    print(f"\nWrote outputs to: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
