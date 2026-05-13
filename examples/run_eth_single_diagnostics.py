"""Run standalone ETH Turtle diagnostics across D1/H4 and direction modes."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path

import pandas as pd

from examples.download_mt5_data import data_quality
from examples.run_d1_multi_asset_equity_overlay import h4_to_d1, load_csv
from turtle_multi_asset import AssetSpec, TurtleBacktester, TurtleRules


SYMBOL = "ETHUSDT_BINANCE"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data_external_xau_btc_xag_eth")
    parser.add_argument("--out-dir", default="outputs/eth_single_diagnostics")
    parser.add_argument("--initial-equity", type=float, default=10_000.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    h4_path = Path(args.data_dir) / "processed" / "external" / "H4" / f"{SYMBOL}.csv"
    h4 = load_csv(h4_path)
    d1 = h4_to_d1(h4)

    quality_rows = [
        data_quality(h4.reset_index(), symbol=SYMBOL, timeframe="H4", point=0.01),
        data_quality(d1.reset_index(), symbol=SYMBOL, timeframe="D1", point=0.01),
    ]
    pd.DataFrame(quality_rows).to_csv(out_dir / "data_quality.csv", index=False)

    experiments = []
    for timeframe, data, rules in [
        ("D1", d1, d1_rules()),
        ("H4_120_330", h4, h4_daily_equivalent_rules()),
        ("H4_60_165", h4, h4_half_daily_equivalent_rules()),
    ]:
        for cap_profile in ["portfolio_caps", "single_relaxed_3x"]:
            for direction in ["long_short", "long_only", "short_only"]:
                experiments.append(
                    {
                        "run": f"{timeframe}_{cap_profile}_{direction}",
                        "timeframe": timeframe,
                        "data": data,
                        "rules": rules,
                        "cap_profile": cap_profile,
                        "direction": direction,
                    }
                )

    summary_rows: list[dict] = []
    for config in experiments:
        spec = eth_spec(config["direction"], config["cap_profile"])
        rules = apply_cap_profile(config["rules"], config["cap_profile"])
        result = TurtleBacktester(
            data={SYMBOL: config["data"]},
            specs={SYMBOL: spec},
            rules=rules,
            initial_equity=args.initial_equity,
        ).run()
        run_dir = out_dir / config["run"]
        run_dir.mkdir(parents=True, exist_ok=True)
        result.equity_curve.to_csv(run_dir / "equity_curve.csv")
        result.orders.to_csv(run_dir / "orders.csv", index=False)
        result.trades.to_csv(run_dir / "trades.csv", index=False)
        result.trade_details.to_csv(run_dir / "trade_details.csv", index=False)
        (run_dir / "metrics.json").write_text(
            json.dumps(result.metrics, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        row = {
            "run": config["run"],
            "timeframe": config["timeframe"],
            "cap_profile": config["cap_profile"],
            "direction": config["direction"],
            "start": result.equity_curve.index[0],
            "end": result.equity_curve.index[-1],
            "final_equity": float(result.equity_curve.iloc[-1]),
            "orders": int(len(result.orders)),
            **result.metrics,
            **side_stats(result.trades),
            **exit_stats(result.trades),
            **concentration_stats(result.trades, result.equity_curve),
            **drawdown_window(result.equity_curve),
        }
        summary_rows.append(row)

    summary = pd.DataFrame(summary_rows).sort_values(
        ["timeframe", "cap_profile", "direction"]
    )
    summary.to_csv(out_dir / "summary.csv", index=False)
    print(
        summary[
            [
                "run",
                "start",
                "end",
                "final_equity",
                "cagr",
                "max_drawdown",
                "sharpe_like",
                "mar",
                "trade_count",
                "long_pnl",
                "short_pnl",
                "top1_contrib_pct",
                "top3_contrib_pct",
            ]
        ].to_string(index=False)
    )
    print(f"Wrote outputs to: {out_dir.resolve()}")


def d1_rules() -> TurtleRules:
    return TurtleRules(
        n_period=20,
        fast_entry=20,
        slow_entry=55,
        fast_exit=10,
        slow_exit=20,
        stop_n=2.0,
        pyramid_step_n=0.5,
        trigger_mode="close",
        allow_short=True,
        max_total_1n_risk_pct=0.12,
        max_direction_1n_risk_pct=0.08,
        cluster_1n_risk_pct={"crypto": 0.04},
        max_total_leverage=3.0,
        max_direction_leverage=2.0,
        cluster_leverage={"crypto": 1.5},
    )


def h4_daily_equivalent_rules() -> TurtleRules:
    return replace(
        d1_rules(),
        n_period=120,
        fast_entry=120,
        slow_entry=330,
        fast_exit=60,
        slow_exit=120,
    )


def h4_half_daily_equivalent_rules() -> TurtleRules:
    return replace(
        d1_rules(),
        n_period=60,
        fast_entry=60,
        slow_entry=165,
        fast_exit=30,
        slow_exit=60,
    )


def apply_cap_profile(rules: TurtleRules, cap_profile: str) -> TurtleRules:
    if cap_profile == "portfolio_caps":
        return rules
    if cap_profile == "single_relaxed_3x":
        return replace(
            rules,
            max_direction_leverage=3.0,
            cluster_leverage={"crypto": 3.0},
        )
    raise ValueError(f"unsupported cap profile: {cap_profile}")


def eth_spec(direction: str, cap_profile: str) -> AssetSpec:
    if direction not in {"long_short", "long_only", "short_only"}:
        raise ValueError(f"unsupported direction: {direction}")
    can_long = direction in {"long_short", "long_only"}
    can_short = direction in {"long_short", "short_only"}
    max_symbol_leverage = 1.5 if cap_profile == "portfolio_caps" else 3.0
    return AssetSpec(
        symbol=SYMBOL,
        asset_class="crypto",
        cluster="crypto",
        point_value=1.0,
        qty_step=0.001,
        min_qty=0.001,
        can_long=can_long,
        can_short=can_short,
        max_units=4,
        unit_1n_risk_pct=0.01,
        max_symbol_1n_risk_pct=0.04,
        max_symbol_leverage=max_symbol_leverage,
        cost_bps=4.0,
        slippage_bps=8.0,
    )


def side_stats(trades: pd.DataFrame) -> dict[str, float]:
    stats = {
        "long_trades": 0,
        "long_pnl": 0.0,
        "short_trades": 0,
        "short_pnl": 0.0,
    }
    if trades.empty:
        return stats
    for side in ["long", "short"]:
        part = trades.loc[trades["side_name"] == side]
        stats[f"{side}_trades"] = int(len(part))
        stats[f"{side}_pnl"] = float(part["pnl"].sum()) if not part.empty else 0.0
    return stats


def exit_stats(trades: pd.DataFrame) -> dict[str, float]:
    stats = {
        "stop_trades": 0,
        "stop_pnl": 0.0,
        "trend_exit_trades": 0,
        "trend_exit_pnl": 0.0,
    }
    if trades.empty:
        return stats
    for exit_type, prefix in [("stop", "stop"), ("trend_exit", "trend_exit")]:
        part = trades.loc[trades["exit_type"] == exit_type]
        stats[f"{prefix}_trades"] = int(len(part))
        stats[f"{prefix}_pnl"] = float(part["pnl"].sum()) if not part.empty else 0.0
    return stats


def concentration_stats(trades: pd.DataFrame, equity_curve: pd.Series) -> dict[str, float]:
    if trades.empty:
        return {
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
        "top1_pnl": top1,
        "top3_pnl": top3,
        "top1_contrib_pct": top1 / denominator if denominator else 0.0,
        "top3_contrib_pct": top3 / denominator if denominator else 0.0,
    }


def drawdown_window(equity_curve: pd.Series) -> dict[str, object]:
    drawdown = equity_curve / equity_curve.cummax() - 1.0
    trough = drawdown.idxmin()
    peak = equity_curve.loc[:trough].idxmax()
    recovery_slice = equity_curve.loc[trough:]
    recovery = recovery_slice[recovery_slice >= equity_curve.loc[peak]]
    return {
        "dd_peak": peak,
        "dd_trough": trough,
        "dd_recovery": recovery.index[0] if not recovery.empty else "",
    }


if __name__ == "__main__":
    main()
