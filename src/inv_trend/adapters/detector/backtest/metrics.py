"""Per-asset detector/backtest metrics."""

from __future__ import annotations

import pandas as pd

from inv_trend.core.performance import equity_statistics


def compute_metrics(
    equity: pd.Series,
    trades: pd.DataFrame,
    signal_count: int,
    false_breakouts: int,
) -> dict[str, float]:
    if equity.empty:
        return {}
    stats = equity_statistics(equity)
    wins = trades["pnl"] > 0 if not trades.empty else pd.Series(dtype=bool)
    gross_profit = float(trades.loc[trades["pnl"] > 0, "pnl"].sum()) if not trades.empty else 0.0
    gross_loss = float(-trades.loc[trades["pnl"] < 0, "pnl"].sum()) if not trades.empty else 0.0
    return {
        "total_return": stats["total_return"],
        "cagr": stats["cagr"],
        "max_drawdown": stats["max_drawdown"],
        "sharpe": stats["sharpe"],
        "win_rate": float(wins.mean()) if len(wins) else 0.0,
        "payoff_ratio": gross_profit / gross_loss if gross_loss > 0 else 0.0,
        "average_holding_bars": (
            float(trades["holding_bars"].mean()) if not trades.empty else 0.0
        ),
        "trade_count": float(len(trades)),
        "signal_count": float(signal_count),
        "false_breakout_rate": (
            false_breakouts / signal_count if signal_count else 0.0
        ),
    }
