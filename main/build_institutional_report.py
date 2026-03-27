from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from backtest.institutional_report import build_institutional_metrics, metrics_to_dict


def _args():
    p = argparse.ArgumentParser(description="Build institutional metrics from fills/equity CSV")
    p.add_argument("--fills", required=True, help="Path to fills.csv")
    p.add_argument("--equity", required=True, help="Path to equity.csv")
    p.add_argument("--initial-equity", type=float, required=True)
    p.add_argument("--output", required=True, help="Path to output JSON")
    return p.parse_args()


def main():
    args = _args()
    fills_path = Path(args.fills)
    equity_path = Path(args.equity)
    out_path = Path(args.output)

    fills = pd.DataFrame()
    if fills_path.exists() and fills_path.stat().st_size > 0:
        fills = pd.read_csv(fills_path)
        if "timestamp" in fills.columns:
            fills["timestamp"] = pd.to_datetime(fills["timestamp"], utc=True)

    eq = pd.read_csv(equity_path, index_col=0)
    if eq.shape[1] == 1:
        equity = eq.iloc[:, 0]
    else:
        equity = eq["equity"] if "equity" in eq.columns else eq.iloc[:, -1]
    equity.index = pd.to_datetime(equity.index, utc=True)
    equity = equity.astype(float)

    metrics = build_institutional_metrics(equity=equity, fills=fills, initial_equity=args.initial_equity)
    payload = metrics_to_dict(metrics)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("Institutional Metrics Summary")
    for k in [
        "final_equity",
        "total_return",
        "cagr",
        "annual_volatility",
        "sharpe_rf0",
        "sortino_rf0",
        "max_drawdown",
        "calmar",
        "trade_count",
        "win_rate",
        "profit_factor",
        "avg_net_pnl_per_trade",
        "total_fees",
        "total_slippage",
    ]:
        print(f"{k}: {payload.get(k)}")
    print(f"institutional_metrics_json: {out_path}")


if __name__ == "__main__":
    main()
