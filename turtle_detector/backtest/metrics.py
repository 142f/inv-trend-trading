"""Per-asset detector/backtest metrics."""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_metrics(
    equity: pd.Series,
    trades: pd.DataFrame,
    signal_count: int,
    false_breakouts: int,
) -> dict[str, float]:
    if equity.empty:
        return {}
    returns = equity.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    running_max = equity.cummax()
    drawdown = equity / running_max - 1.0
    years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1 / 365.25)
    periods = len(returns) / years
    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1)
    cagr = (1 + total_return) ** (1 / years) - 1 if total_return > -1 else -1.0
    volatility = float(returns.std(ddof=0) * np.sqrt(periods)) if len(returns) else 0.0
    sharpe = float(returns.mean() * periods / volatility) if volatility > 0 else 0.0
    wins = trades["pnl"] > 0 if not trades.empty else pd.Series(dtype=bool)
    gross_profit = float(trades.loc[trades["pnl"] > 0, "pnl"].sum()) if not trades.empty else 0.0
    gross_loss = float(-trades.loc[trades["pnl"] < 0, "pnl"].sum()) if not trades.empty else 0.0
    return {
        "total_return": total_return,
        "cagr": float(cagr),
        "max_drawdown": float(drawdown.min()),
        "sharpe": sharpe,
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
