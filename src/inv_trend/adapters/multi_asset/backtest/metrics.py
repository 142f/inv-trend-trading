"""Backtest performance metrics."""

from __future__ import annotations

import pandas as pd

from inv_trend.core.performance import equity_statistics


def compute_backtest_metrics(
    equity_curve: pd.Series,
    trades: pd.DataFrame,
) -> dict[str, float]:
    if equity_curve.empty:
        return {}
    stats = equity_statistics(equity_curve)
    max_dd = stats["max_drawdown"]
    mar = stats["cagr"] / abs(max_dd) if max_dd < 0 else 0.0
    return {
        "total_return": stats["total_return"],
        "cagr": stats["cagr"],
        "max_drawdown": max_dd,
        "volatility": stats["volatility"],
        "sharpe_like": stats["sharpe"],
        "mar": float(mar),
        "trade_count": float(0 if trades.empty else len(trades)),
        "periods_per_year": stats["periods_per_year"],
    }
