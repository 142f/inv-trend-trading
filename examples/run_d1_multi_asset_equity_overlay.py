"""Run D1 Turtle tests for metals, crypto, and Yahoo equities."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from turtle_multi_asset import AssetSpec, TurtleBacktester, TurtleRules
from turtle_multi_asset.strategy import SHORT


CORE_SYMBOLS = [
    "XAUUSD_DUKAS",
    "XAGUSD_DUKAS",
    "BTCUSDT_BINANCE",
    "ETHUSDT_BINANCE",
]

EQUITY_SYMBOLS = [
    "NVDA",
    "MU",
    "AMD",
    "TSM",
    "SNDK",
    "AVGO",
    "QQQ",
    "SPY",
    "XLY",
    "ORCL",
    "MSFT",
    "PLTR",
    "NFLX",
    "META",
    "AAPL",
    "TSLA",
    "GOOGL",
    "AMZN",
]

EQUITY_CLUSTERS = {
    "NVDA": "semiconductors",
    "MU": "semiconductors",
    "AMD": "semiconductors",
    "TSM": "semiconductors",
    "SNDK": "semiconductors",
    "AVGO": "semiconductors",
    "QQQ": "broad_equity",
    "SPY": "broad_equity",
    "XLY": "consumer",
    "NFLX": "consumer",
    "TSLA": "consumer",
    "AMZN": "consumer",
    "ORCL": "software",
    "MSFT": "software",
    "PLTR": "software",
    "META": "communication",
    "GOOGL": "communication",
    "AAPL": "mega_cap_tech",
}


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
    if args.start:
        start = pd.Timestamp(args.start, tz="UTC")
        all_data = {symbol: df.loc[df.index >= start] for symbol, df in all_data.items()}
    if args.end:
        end = pd.Timestamp(args.end, tz="UTC")
        all_data = {symbol: df.loc[df.index <= end] for symbol, df in all_data.items()}
    all_data = {symbol: df for symbol, df in all_data.items() if not df.empty}

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
        result = TurtleBacktester(
            data=data,
            specs=specs,
            rules=rules,
            initial_equity=args.initial_equity,
        ).run()

        run_dir = out_dir / str(run_config["run"])
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
        row.update(max_leverage_stats(result.orders, result.equity_curve, specs))
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


def load_universe(core_data_dir: Path, equity_data_dir: Path) -> dict[str, pd.DataFrame]:
    data: dict[str, pd.DataFrame] = {}
    for symbol in CORE_SYMBOLS:
        path = core_data_dir / "processed" / "external" / "H4" / f"{symbol}.csv"
        if path.exists():
            data[symbol] = h4_to_d1(load_csv(path))
    for symbol in EQUITY_SYMBOLS:
        for source in ("nasdaq", "yahoo"):
            path = equity_data_dir / "processed" / source / "D1" / f"{symbol}.csv"
            if path.exists():
                data[symbol] = load_csv(path)
                break
    return data


def load_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=["time"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time").sort_index()[["open", "high", "low", "close", "volume", "spread"]]


def h4_to_d1(df: pd.DataFrame) -> pd.DataFrame:
    grouped = df.resample("1D", label="left", closed="left")
    out = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(min_count=1),
            "spread": grouped["spread"].median(),
        }
    )
    out = out.dropna(subset=["open", "high", "low", "close"])
    out = out[
        (out["open"] > 0)
        & (out["high"] > 0)
        & (out["low"] > 0)
        & (out["close"] > 0)
        & (out["high"] >= out["low"])
        & (out["open"] <= out["high"])
        & (out["open"] >= out["low"])
        & (out["close"] <= out["high"])
        & (out["close"] >= out["low"])
    ]
    return out


def align_data(
    data: dict[str, pd.DataFrame],
    align_start: bool,
    align_end: bool,
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
        if not out.empty:
            aligned[symbol] = out
    return aligned


def rules_3x(include_equities: bool) -> TurtleRules:
    cluster_1n = {
        "precious_metals": 0.04,
        "crypto": 0.04,
        "semiconductors": 0.025,
        "broad_equity": 0.025,
        "consumer": 0.025,
        "software": 0.025,
        "communication": 0.02,
        "mega_cap_tech": 0.02,
    }
    cluster_leverage = {
        "precious_metals": 1.5,
        "crypto": 1.5,
        "semiconductors": 0.8,
        "broad_equity": 0.8,
        "consumer": 0.8,
        "software": 0.8,
        "communication": 0.6,
        "mega_cap_tech": 0.5,
    }
    if not include_equities:
        cluster_1n = {"precious_metals": 0.04, "crypto": 0.04}
        cluster_leverage = {"precious_metals": 1.5, "crypto": 1.5}

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
        default_cluster_1n_risk_pct=0.02,
        cluster_1n_risk_pct=cluster_1n,
        max_total_leverage=3.0,
        max_direction_leverage=2.0,
        default_cluster_leverage=0.5,
        cluster_leverage=cluster_leverage,
    )


def build_specs(
    symbols: list[str],
    equity_short: bool,
    include_equities: bool,
) -> dict[str, AssetSpec]:
    specs: dict[str, AssetSpec] = {}
    for symbol in symbols:
        if symbol == "XAUUSD_DUKAS":
            specs[symbol] = AssetSpec(
                symbol=symbol,
                asset_class="metal",
                cluster="precious_metals",
                point_value=1.0,
                qty_step=0.01,
                min_qty=0.01,
                can_short=False,
                max_units=4,
                unit_1n_risk_pct=0.01,
                max_symbol_1n_risk_pct=0.04,
                max_symbol_leverage=1.5,
                cost_bps=1.0,
                slippage_bps=3.0,
            )
        elif symbol == "XAGUSD_DUKAS":
            specs[symbol] = AssetSpec(
                symbol=symbol,
                asset_class="metal",
                cluster="precious_metals",
                point_value=1.0,
                qty_step=1.0,
                min_qty=1.0,
                can_short=False,
                max_units=4,
                unit_1n_risk_pct=0.01,
                max_symbol_1n_risk_pct=0.04,
                max_symbol_leverage=1.5,
                cost_bps=1.5,
                slippage_bps=5.0,
            )
        elif symbol == "BTCUSDT_BINANCE":
            specs[symbol] = AssetSpec(
                symbol=symbol,
                asset_class="crypto",
                cluster="crypto",
                point_value=1.0,
                qty_step=0.0001,
                min_qty=0.0001,
                can_short=True,
                max_units=4,
                unit_1n_risk_pct=0.01,
                max_symbol_1n_risk_pct=0.04,
                max_symbol_leverage=1.5,
                cost_bps=4.0,
                slippage_bps=8.0,
            )
        elif symbol == "ETHUSDT_BINANCE":
            specs[symbol] = AssetSpec(
                symbol=symbol,
                asset_class="crypto",
                cluster="crypto",
                point_value=1.0,
                qty_step=0.001,
                min_qty=0.001,
                can_short=True,
                max_units=4,
                unit_1n_risk_pct=0.01,
                max_symbol_1n_risk_pct=0.04,
                max_symbol_leverage=1.5,
                cost_bps=4.0,
                slippage_bps=8.0,
            )
        else:
            cluster = EQUITY_CLUSTERS.get(symbol, "single_stock")
            specs[symbol] = AssetSpec(
                symbol=symbol,
                asset_class="etf" if symbol in {"QQQ", "SPY", "XLY"} else "equity",
                cluster=cluster if include_equities else "us_equity",
                point_value=1.0,
                qty_step=1.0,
                min_qty=1.0,
                can_short=equity_short,
                max_units=3,
                unit_1n_risk_pct=0.0025,
                max_symbol_1n_risk_pct=0.01,
                max_symbol_leverage=0.30 if symbol not in {"QQQ", "SPY"} else 0.50,
                cost_bps=1.0,
                slippage_bps=5.0,
            )
    return specs


def symbol_trade_stats(trades: pd.DataFrame, symbols: list[str]) -> dict[str, float]:
    stats: dict[str, float] = {}
    if trades.empty:
        return stats
    for symbol in symbols:
        part = trades.loc[trades["symbol"] == symbol]
        if part.empty:
            stats[f"{symbol}_pnl"] = 0.0
            stats[f"{symbol}_trades"] = 0
            stats[f"{symbol}_short_pnl"] = 0.0
            continue
        stats[f"{symbol}_pnl"] = float(part["pnl"].sum())
        stats[f"{symbol}_trades"] = int(len(part))
        stats[f"{symbol}_short_pnl"] = float(part.loc[part["side"] == SHORT, "pnl"].sum())
    return stats


def max_leverage_stats(
    orders: pd.DataFrame,
    equity_curve: pd.Series,
    specs: dict[str, AssetSpec],
) -> dict[str, float]:
    if orders.empty:
        return {
            "max_total_order_notional_to_equity": 0.0,
            "max_single_order_notional_to_equity": 0.0,
        }
    orders = orders.copy()
    orders["time"] = pd.to_datetime(orders["time"], utc=True)
    orders["equity"] = orders["time"].map(equity_curve)
    orders["notional_to_equity"] = orders["notional"] / orders["equity"]
    return {
        "max_single_order_notional_to_equity": float(orders["notional_to_equity"].max()),
        "sum_order_notional_to_initial_equity": float(orders["notional"].sum() / equity_curve.iloc[0]),
    }


if __name__ == "__main__":
    main()
