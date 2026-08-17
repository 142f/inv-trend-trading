"""Shared equity-curve calculations used by detector and portfolio backtests."""

from __future__ import annotations

import numpy as np
import pandas as pd


def equity_statistics(equity_curve: pd.Series) -> dict[str, float]:
    """Calculate causal return statistics without assigning policy-specific names."""

    if equity_curve.empty:
        return {}
    starting_equity = float(equity_curve.iloc[0])
    ending_equity = float(equity_curve.iloc[-1])
    if starting_equity <= 0:
        raise ValueError("equity must start above zero")
    returns = equity_curve.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    drawdown = equity_curve / equity_curve.cummax() - 1.0
    years = max((equity_curve.index[-1] - equity_curve.index[0]).days / 365.25, 1 / 365.25)
    periods_per_year = len(returns) / years if years > 0 else 0.0
    total_return = ending_equity / starting_equity - 1.0
    cagr = -1.0 if ending_equity <= 0 else (ending_equity / starting_equity) ** (1.0 / years) - 1.0
    volatility = float(returns.std(ddof=0) * np.sqrt(periods_per_year)) if periods_per_year else 0.0
    if not np.isfinite(volatility):
        volatility = 0.0
    sharpe = float(returns.mean() * periods_per_year / volatility) if volatility > 0 else 0.0
    return {
        "total_return": float(total_return),
        "cagr": float(cagr),
        "max_drawdown": float(drawdown.min()) if not drawdown.empty else 0.0,
        "volatility": volatility,
        "sharpe": sharpe,
        "periods_per_year": float(periods_per_year),
    }
