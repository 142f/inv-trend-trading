"""Audit the 9-symbol D1 Turtle candidate for overfitting risk."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import pandas as pd

from examples.run_d1_multi_asset_equity_overlay import (
    CORE_SYMBOLS,
    EQUITY_SYMBOLS,
    align_data,
    build_specs,
    load_universe,
    rules_3x,
    symbol_trade_stats,
)
from examples.run_d1_pruned_universe_experiments import drawdown_window
from turtle_multi_asset import AssetSpec, TurtleBacktester, TurtleRules


BASE_SYMBOLS = [
    "XAUUSD_DUKAS",
    "XAGUSD_DUKAS",
    "BTCUSDT_BINANCE",
    "ETHUSDT_BINANCE",
    "NVDA",
    "AMD",
    "MU",
    "TSM",
    "AVGO",
]

BASELINES = {
    "reference_core4": CORE_SYMBOLS,
    "reference_core4_plus_semis": BASE_SYMBOLS,
    "reference_compact_a_grade": [
        "XAUUSD_DUKAS",
        "XAGUSD_DUKAS",
        "BTCUSDT_BINANCE",
        "NVDA",
        "AMD",
        "MU",
        "MSFT",
        "META",
    ],
    "reference_previous20": CORE_SYMBOLS
    + [
        symbol
        for symbol in EQUITY_SYMBOLS
        if symbol not in {"PLTR", "SNDK"}
    ],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-data-dir", default="data_external_xau_btc_xag_eth")
    parser.add_argument("--equity-data-dir", default="data_external_equities")
    parser.add_argument("--out-dir", default="outputs/d1_candidate9_audit")
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

    experiments = build_experiments()
    summary_rows: list[dict] = []
    concentration_rows: list[dict] = []

    for experiment in experiments:
        result, data, specs = run_experiment(
            all_data=all_data,
            initial_equity=args.initial_equity,
            **experiment,
        )
        run_dir = out_dir / str(experiment["name"])
        run_dir.mkdir(parents=True, exist_ok=True)
        result.equity_curve.to_csv(run_dir / "equity_curve.csv")
        result.orders.to_csv(run_dir / "orders.csv", index=False)
        result.trades.to_csv(run_dir / "trades.csv", index=False)
        result.trade_details.to_csv(run_dir / "trade_details.csv", index=False)
        (run_dir / "metrics.json").write_text(
            json.dumps(result.metrics, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        concentration = concentration_stats(result.trades, result.equity_curve)
        row = {
            "experiment": experiment["name"],
            "category": experiment["category"],
            "notes": experiment["notes"],
            "symbols": "+".join(data),
            "symbol_count": len(data),
            "start": result.equity_curve.index[0],
            "end": result.equity_curve.index[-1],
            "final_equity": float(result.equity_curve.iloc[-1]),
            "orders": int(len(result.orders)),
            **result.metrics,
            **drawdown_window(result.equity_curve),
            **concentration,
        }
        row.update(symbol_trade_stats(result.trades, list(data)))
        summary_rows.append(row)
        concentration_rows.extend(
            concentration_adjustments(
                experiment["name"],
                result.trades,
                result.equity_curve,
                args.initial_equity,
            )
        )

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out_dir / "summary.csv", index=False)
    pd.DataFrame(concentration_rows).to_csv(
        out_dir / "concentration_adjustments.csv",
        index=False,
    )

    print(
        summary[
            [
                "experiment",
                "category",
                "symbol_count",
                "start",
                "end",
                "final_equity",
                "cagr",
                "max_drawdown",
                "sharpe_like",
                "mar",
                "trade_count",
                "top1_contrib_pct",
                "top3_contrib_pct",
            ]
        ].to_string(index=False)
    )
    print(f"Wrote outputs to: {out_dir.resolve()}")


def build_experiments() -> list[dict]:
    experiments: list[dict] = [
        experiment("base_9", BASE_SYMBOLS, "base", "Current 9-symbol candidate"),
    ]

    for symbol in BASE_SYMBOLS:
        name = {
            "XAUUSD_DUKAS": "minus_xau",
            "XAGUSD_DUKAS": "minus_xag",
            "BTCUSDT_BINANCE": "minus_btc",
            "ETHUSDT_BINANCE": "minus_eth_short",
        }.get(symbol, f"minus_{symbol.lower()}")
        experiments.append(
            experiment(
                name,
                [item for item in BASE_SYMBOLS if item != symbol],
                "single_asset_removal",
                f"Remove {symbol}",
            )
        )

    experiments.extend(
        [
            experiment(
                "minus_metals",
                [s for s in BASE_SYMBOLS if s not in {"XAUUSD_DUKAS", "XAGUSD_DUKAS"}],
                "cluster_removal",
                "Remove XAU and XAG",
            ),
            experiment(
                "minus_btc_family_tail",
                [s for s in BASE_SYMBOLS if s != "ETHUSDT_BINANCE"],
                "cluster_removal",
                "Remove ETH short, keep BTC",
            ),
            experiment(
                "minus_crypto",
                [s for s in BASE_SYMBOLS if s not in {"BTCUSDT_BINANCE", "ETHUSDT_BINANCE"}],
                "cluster_removal",
                "Remove BTC and ETH short",
            ),
            experiment(
                "minus_semis",
                [s for s in BASE_SYMBOLS if s not in {"NVDA", "AMD", "MU", "TSM", "AVGO"}],
                "cluster_removal",
                "Remove semiconductor sleeve",
            ),
            experiment(
                "segment_1_2017_2019",
                BASE_SYMBOLS,
                "time_segment",
                "2017-08-17 to 2019-12-31",
                start="2017-08-17",
                end="2019-12-31",
            ),
            experiment(
                "segment_2_2020_2022",
                BASE_SYMBOLS,
                "time_segment",
                "2020-01-01 to 2022-12-31",
                start="2020-01-01",
                end="2022-12-31",
            ),
            experiment(
                "segment_3_2023_2026",
                BASE_SYMBOLS,
                "time_segment",
                "2023-01-01 to 2026-04-20",
                start="2023-01-01",
                end="2026-04-20",
            ),
            experiment(
                "roll_2017_2021",
                BASE_SYMBOLS,
                "rolling_window",
                "2017-08-17 to 2021-12-31",
                start="2017-08-17",
                end="2021-12-31",
            ),
            experiment(
                "roll_2019_2023",
                BASE_SYMBOLS,
                "rolling_window",
                "2019-01-01 to 2023-12-31",
                start="2019-01-01",
                end="2023-12-31",
            ),
            experiment(
                "roll_2021_2026",
                BASE_SYMBOLS,
                "rolling_window",
                "2021-01-01 to 2026-04-20",
                start="2021-01-01",
                end="2026-04-20",
            ),
            experiment(
                "cost_1p5x",
                BASE_SYMBOLS,
                "cost_stress",
                "All costs multiplied by 1.5",
                cost_multiplier=1.5,
            ),
            experiment(
                "cost_2x",
                BASE_SYMBOLS,
                "cost_stress",
                "All costs multiplied by 2.0",
                cost_multiplier=2.0,
            ),
            experiment(
                "metals_cap_1p0",
                BASE_SYMBOLS,
                "cluster_cap_stress",
                "Precious metals leverage cap set to 1.0x",
                cluster_leverage={"precious_metals": 1.0},
            ),
            experiment(
                "crypto_cap_1p0",
                BASE_SYMBOLS,
                "cluster_cap_stress",
                "Crypto leverage cap set to 1.0x",
                cluster_leverage={"crypto": 1.0},
            ),
            experiment(
                "semis_cap_1p0",
                BASE_SYMBOLS,
                "cluster_cap_stress",
                "Semiconductor leverage cap set to 1.0x",
                cluster_leverage={"semiconductors": 1.0},
            ),
            experiment(
                "all_clusters_tighter",
                BASE_SYMBOLS,
                "cluster_cap_stress",
                "Metals/crypto/semis caps tightened",
                cluster_leverage={
                    "precious_metals": 1.0,
                    "crypto": 1.0,
                    "semiconductors": 0.6,
                },
            ),
            experiment(
                "entry_18_50",
                BASE_SYMBOLS,
                "rule_perturbation",
                "Entry windows 18/50",
                rule_overrides={"fast_entry": 18, "slow_entry": 50},
            ),
            experiment(
                "entry_22_60",
                BASE_SYMBOLS,
                "rule_perturbation",
                "Entry windows 22/60",
                rule_overrides={"fast_entry": 22, "slow_entry": 60},
            ),
            experiment(
                "exit_9_18",
                BASE_SYMBOLS,
                "rule_perturbation",
                "Exit windows 9/18",
                rule_overrides={"fast_exit": 9, "slow_exit": 18},
            ),
            experiment(
                "exit_12_24",
                BASE_SYMBOLS,
                "rule_perturbation",
                "Exit windows 12/24",
                rule_overrides={"fast_exit": 12, "slow_exit": 24},
            ),
            experiment(
                "stop_1p8n",
                BASE_SYMBOLS,
                "rule_perturbation",
                "Stop 1.8N",
                rule_overrides={"stop_n": 1.8},
            ),
            experiment(
                "stop_2p2n",
                BASE_SYMBOLS,
                "rule_perturbation",
                "Stop 2.2N",
                rule_overrides={"stop_n": 2.2},
            ),
        ]
    )

    for name, symbols in BASELINES.items():
        experiments.append(
            experiment(
                name,
                symbols,
                "benchmark",
                f"Benchmark {name}",
                eth_mode="short_only" if name == "reference_core4_plus_semis" else "long_short",
            )
        )
    return experiments


def experiment(
    name: str,
    symbols: list[str],
    category: str,
    notes: str,
    eth_mode: str = "short_only",
    start: str | None = None,
    end: str | None = None,
    cost_multiplier: float = 1.0,
    cluster_leverage: dict[str, float] | None = None,
    rule_overrides: dict[str, float | int] | None = None,
) -> dict:
    return {
        "name": name,
        "symbols": symbols,
        "category": category,
        "notes": notes,
        "eth_mode": eth_mode,
        "start": start,
        "end": end,
        "cost_multiplier": cost_multiplier,
        "cluster_leverage": cluster_leverage or {},
        "rule_overrides": rule_overrides or {},
    }


def run_experiment(
    all_data: dict[str, pd.DataFrame],
    initial_equity: float,
    name: str,
    symbols: list[str],
    category: str,
    notes: str,
    eth_mode: str,
    start: str | None,
    end: str | None,
    cost_multiplier: float,
    cluster_leverage: dict[str, float],
    rule_overrides: dict[str, float | int],
) -> tuple:
    del name, category, notes
    available = [symbol for symbol in symbols if symbol in all_data]
    data = {symbol: all_data[symbol] for symbol in available}
    if start:
        start_ts = pd.Timestamp(start, tz="UTC")
        data = {symbol: df.loc[df.index >= start_ts] for symbol, df in data.items()}
    if end:
        end_ts = pd.Timestamp(end, tz="UTC")
        data = {symbol: df.loc[df.index <= end_ts] for symbol, df in data.items()}
    data = align_data(data, align_start=True, align_end=True)
    if len(data) < 2:
        raise ValueError(f"not enough data for symbols: {symbols}")

    specs = build_run_specs(list(data), eth_mode=eth_mode, cost_multiplier=cost_multiplier)
    rules = build_rules(list(data), cluster_leverage, rule_overrides)
    result = TurtleBacktester(
        data=data,
        specs=specs,
        rules=rules,
        initial_equity=initial_equity,
    ).run()
    return result, data, specs


def build_run_specs(
    symbols: list[str],
    eth_mode: str,
    cost_multiplier: float,
) -> dict[str, AssetSpec]:
    specs = build_specs(
        symbols,
        equity_short=False,
        include_equities=any(symbol in EQUITY_SYMBOLS for symbol in symbols),
    )
    for symbol, spec in list(specs.items()):
        specs[symbol] = replace(
            spec,
            cost_bps=spec.cost_bps * cost_multiplier,
            slippage_bps=spec.slippage_bps * cost_multiplier,
        )
    if "ETHUSDT_BINANCE" in specs and eth_mode == "short_only":
        specs["ETHUSDT_BINANCE"] = replace(
            specs["ETHUSDT_BINANCE"],
            can_long=False,
            can_short=True,
        )
    return specs


def build_rules(
    symbols: list[str],
    cluster_leverage: dict[str, float],
    rule_overrides: dict[str, float | int],
) -> TurtleRules:
    rules = rules_3x(include_equities=any(symbol in EQUITY_SYMBOLS for symbol in symbols))
    if cluster_leverage:
        merged = dict(rules.cluster_leverage)
        merged.update(cluster_leverage)
        rules = replace(rules, cluster_leverage=merged)
    if rule_overrides:
        rules = replace(rules, **rule_overrides)
    return rules


def concentration_stats(trades: pd.DataFrame, equity_curve: pd.Series) -> dict[str, float]:
    if trades.empty:
        return {
            "net_trade_pnl": 0.0,
            "top1_pnl": 0.0,
            "top3_pnl": 0.0,
            "top1_contrib_pct": 0.0,
            "top3_contrib_pct": 0.0,
        }
    net = float(trades["pnl"].sum())
    winners = trades.loc[trades["pnl"] > 0, "pnl"].sort_values(ascending=False)
    top1 = float(winners.head(1).sum())
    top3 = float(winners.head(3).sum())
    denominator = abs(net) if net else abs(equity_curve.iloc[-1] - equity_curve.iloc[0])
    return {
        "net_trade_pnl": net,
        "top1_pnl": top1,
        "top3_pnl": top3,
        "top1_contrib_pct": top1 / denominator if denominator else 0.0,
        "top3_contrib_pct": top3 / denominator if denominator else 0.0,
    }


def concentration_adjustments(
    experiment_name: str,
    trades: pd.DataFrame,
    equity_curve: pd.Series,
    initial_equity: float,
) -> list[dict]:
    if trades.empty:
        return []
    years = max((equity_curve.index[-1] - equity_curve.index[0]).days / 365.25, 1 / 365.25)
    adjustments = [
        ("remove_top1_trade_approx", trades.sort_values("pnl", ascending=False).head(1)),
        ("remove_top3_trades_approx", trades.sort_values("pnl", ascending=False).head(3)),
        ("remove_xag_trades_approx", trades.loc[trades["symbol"] == "XAGUSD_DUKAS"]),
        (
            "remove_btc_long_trades_approx",
            trades.loc[(trades["symbol"] == "BTCUSDT_BINANCE") & (trades["side_name"] == "long")],
        ),
        ("remove_mu_trades_approx", trades.loc[trades["symbol"] == "MU"]),
    ]
    rows: list[dict] = []
    for name, removed in adjustments:
        removed_pnl = float(removed["pnl"].sum()) if not removed.empty else 0.0
        adjusted_final = float(equity_curve.iloc[-1] - removed_pnl)
        adjusted_return = adjusted_final / initial_equity - 1.0
        adjusted_cagr = (
            (1.0 + adjusted_return) ** (1.0 / years) - 1.0
            if adjusted_return > -1.0
            else -1.0
        )
        rows.append(
            {
                "experiment": experiment_name,
                "adjustment": name,
                "removed_trades": int(len(removed)),
                "removed_pnl": removed_pnl,
                "adjusted_final_equity_approx": adjusted_final,
                "adjusted_total_return_approx": adjusted_return,
                "adjusted_cagr_approx": adjusted_cagr,
            }
        )
    return rows


if __name__ == "__main__":
    main()
