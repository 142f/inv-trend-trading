"""Backtest performance metrics."""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_backtest_metrics(
    equity_curve: pd.Series,
    trades: pd.DataFrame,
) -> dict[str, float]:
    if equity_curve.empty:
        return {}
    returns = equity_curve.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    running_max = equity_curve.cummax()
    drawdown = equity_curve / running_max - 1.0
    total_return = equity_curve.iloc[-1] / equity_curve.iloc[0] - 1.0
    years = max((equity_curve.index[-1] - equity_curve.index[0]).days / 365.25, 1 / 365.25)
    periods_per_year = len(returns) / years if years > 0 else 0.0
    cagr = (1.0 + total_return) ** (1.0 / years) - 1.0
    vol = returns.std(ddof=0) * np.sqrt(periods_per_year) if periods_per_year > 0 else 0.0
    if not np.isfinite(vol):
        vol = 0.0
    sharpe = (returns.mean() * periods_per_year / vol) if vol > 0 else 0.0
    max_dd = float(drawdown.min()) if not drawdown.empty else 0.0
    mar = cagr / abs(max_dd) if max_dd < 0 else 0.0
    return {
        "total_return": float(total_return),
        "cagr": float(cagr),
        "max_drawdown": max_dd,
        "volatility": float(vol),
        "sharpe_like": float(sharpe),
        "mar": float(mar),
        "trade_count": float(0 if trades.empty else len(trades)),
        "periods_per_year": float(periods_per_year),
    }
