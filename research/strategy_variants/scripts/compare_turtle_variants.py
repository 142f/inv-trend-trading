from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import pandas as pd

from inv_trend.application import BacktestService
from inv_trend.adapters.multi_asset import AssetSpec, TurtleRules, turtle_rules
from inv_trend.adapters.multi_asset.config import BacktestConfig
from inv_trend.adapters.multi_asset.data.loader import load_backtest_ready_csv
from inv_trend.adapters.multi_asset.data.core_dataset import summarize_backtest_result
from inv_trend.adapters.multi_asset.profiles.asset_profiles import infer_asset_fields


DEFAULT_DATASETS = [
    "processed_data/backtest_ready/metal_tech_core/metal_tech_core_d1_backtest_ready.csv",
    "processed_data/backtest_ready/data_external_xau_btc_xag_eth/data_external_xau_btc_xag_eth_external_btcusdt_binance_ethusdt_binance_xagusd_dukas_xauusd_dukas_2005_2026_h4_backtest_ready.csv",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", nargs="+", default=DEFAULT_DATASETS)
    parser.add_argument("--out-dir", default="outputs/turtle_variant_comparison")
    parser.add_argument("--equity", type=float, default=100_000.0)
    return parser.parse_args()


def build_asset_specs(symbols: list[str]) -> dict[str, AssetSpec]:
    specs: dict[str, AssetSpec] = {}
    for symbol in symbols:
        inferred = infer_asset_fields(symbol)
        specs[symbol] = AssetSpec(
            symbol=symbol,
            asset_class=str(inferred["asset_class"]),
            cluster=str(inferred["cluster"]),
            point_value=1.0,
            qty_step=1.0,
            min_qty=0.0,
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


def variant_rules():
    base = TurtleRules(
        n_period=20,
        fast_entry=20,
        slow_entry=55,
        fast_exit=10,
        slow_exit=20,
        stop_n=2.0,
        pyramid_step_n=0.5,
        trigger_mode="close",
        allow_short=True,
        max_total_1n_risk_pct=0.08,
        max_direction_1n_risk_pct=0.06,
        cluster_1n_risk_pct={
            "precious_metals": 0.025,
            "crypto": 0.015,
            "us_equity": 0.035,
            "other": 0.02,
        },
        max_total_leverage=1.5,
        max_direction_leverage=1.2,
        cluster_leverage={
            "precious_metals": 1.0,
            "crypto": 0.5,
            "us_equity": 1.0,
            "other": 0.5,
        },
    )
    return {
        "baseline": base,
        "trend_filter_200": replace(base, entry_ma_period=200),
        "buffered_breakout_0_5n": turtle_rules("classic-bars"),
        "slow_only_wide_stop": replace(base, fast_system_enabled=False, stop_n=2.5, pyramid_step_n=1.0),
    }


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict[str, object]] = []
    by_dataset: dict[str, dict[str, dict[str, object]]] = {}

    for dataset_arg in args.datasets:
        dataset_path = Path(dataset_arg)
        dataset_name = dataset_path.stem.replace("_backtest_ready", "")
        data = load_backtest_ready_csv(dataset_path)
        specs = build_asset_specs(sorted(data))
        by_dataset[dataset_name] = {}
        for name, rules in variant_rules().items():
            result = BacktestService().run(
                data, specs, rules, config=BacktestConfig(initial_equity=args.equity)
            ).result
            summary = summarize_backtest_result(result)
            by_dataset[dataset_name][name] = summary
            all_rows.append(
                {
                    "dataset": dataset_name,
                    "variant": name,
                    "final_equity": summary["final_equity"],
                    "total_return": summary["total_return"],
                    "cagr": summary["cagr"],
                    "max_drawdown": summary["max_drawdown"],
                    "sharpe_like": summary["sharpe_like"],
                    "mar": summary["mar"],
                    "trade_count": summary["trade_count"],
                    "win_rate": summary["win_rate"],
                    "payoff_ratio": summary["payoff_ratio"],
                }
            )

    comparison = pd.DataFrame(all_rows).sort_values(["dataset", "variant"]).reset_index(drop=True)
    comparison.to_csv(out_dir / "variant_metrics.csv", index=False)
    (out_dir / "variant_metrics.json").write_text(
        json.dumps(by_dataset, indent=2, ensure_ascii=False, default=float),
        encoding="utf-8",
    )

    print(comparison.to_string(index=False))
    print(f"\nWrote comparison to: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
