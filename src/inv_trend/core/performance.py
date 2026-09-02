"""Shared equity-curve calculations used by detector and portfolio backtests."""

from __future__ import annotations

import numpy as np
import pandas as pd


def equity_metric_kernel(equity_curve: pd.Series) -> dict[str, float | None]:
    """Return shared equity mathematics without consumer-specific null policy."""

    if equity_curve.empty:
        return {}
    curve = equity_curve.astype(float).sort_index()
    starting_equity = float(curve.iloc[0])
    ending_equity = float(curve.iloc[-1])
    if starting_equity <= 0:
        raise ValueError("equity must start above zero")
    returns = curve.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    drawdown = curve / curve.cummax() - 1.0
    years = max((curve.index[-1] - curve.index[0]).days / 365.25, 1 / 365.25)
    periods_per_year = len(returns) / years if years > 0 else 0.0
    total_return = ending_equity / starting_equity - 1.0
    annualized_return = (
        -1.0
        if ending_equity <= 0
        else (ending_equity / starting_equity) ** (1.0 / years) - 1.0
    )
    raw_volatility = (
        returns.std(ddof=0) * np.sqrt(periods_per_year)
        if periods_per_year else None
    )
    volatility = (
        float(raw_volatility)
        if raw_volatility is not None and np.isfinite(raw_volatility)
        else None
    )
    sharpe = (
        float(returns.mean() * periods_per_year / volatility)
        if volatility is not None and volatility > 0
        else None
    )
    return {
        "total_return": float(total_return),
        "annualized_return": float(annualized_return),
        "max_drawdown": float(drawdown.min()) if not drawdown.empty else 0.0,
        "volatility": volatility,
        "sharpe_ratio": sharpe,
        "periods_per_year": float(periods_per_year),
        "ending_equity": ending_equity,
    }


def equity_statistics(equity_curve: pd.Series) -> dict[str, float]:
    """Calculate causal return statistics without assigning policy-specific names."""

    values = equity_metric_kernel(equity_curve)
    if not values:
        return {}
    return {
        "total_return": float(values["total_return"]),
        "cagr": float(values["annualized_return"]),
        "max_drawdown": float(values["max_drawdown"]),
        "volatility": float(values["volatility"] or 0.0),
        "sharpe": float(values["sharpe_ratio"] or 0.0),
        "periods_per_year": float(values["periods_per_year"]),
    }


__all__ = ["equity_metric_kernel", "equity_statistics"]
