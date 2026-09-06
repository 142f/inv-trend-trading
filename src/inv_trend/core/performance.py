"""Shared equity-curve calculations used by detector and portfolio backtests."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class EquityAnalysis:
    """One calculation shared by numerical metrics and application projections."""

    curve: pd.Series
    returns: pd.Series
    drawdown: pd.Series
    metrics: dict[str, float | None]


def analyze_equity(equity_curve: pd.Series) -> EquityAnalysis:
    """Prepare equity mathematics once, without consumer-specific null policy."""

    if equity_curve.empty:
        empty = pd.Series(dtype=float, index=equity_curve.index)
        return EquityAnalysis(empty, empty, empty, {})
    curve = equity_curve.astype(float)
    if not curve.index.is_monotonic_increasing:
        curve = curve.sort_index()
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
    metrics = {
        "total_return": float(total_return),
        "annualized_return": float(annualized_return),
        "max_drawdown": float(drawdown.min()) if not drawdown.empty else 0.0,
        "volatility": volatility,
        "sharpe_ratio": sharpe,
        "periods_per_year": float(periods_per_year),
        "ending_equity": ending_equity,
    }
    return EquityAnalysis(curve, returns, drawdown, metrics)


def equity_metric_kernel(equity_curve: pd.Series) -> dict[str, float | None]:
    """Compatibility entry point for detector and portfolio backtests."""
    return analyze_equity(equity_curve).metrics


def drawdown_duration(drawdown: pd.Series) -> tuple[int, int | None]:
    """Longest underwater run and recovery bars after the global worst trough.

    Equal troughs use the first occurrence. A later, deeper trough supersedes
    any earlier recovery; an unrecovered global trough returns None.
    """
    values = drawdown.to_numpy(dtype=float, na_value=np.nan)
    negative = values < 0
    if not negative.any():
        return 0, None
    # Pair the starts/ends of underwater episodes instead of allocating
    # multiple integer arrays covering every equity observation.
    boundaries = np.flatnonzero(np.diff(np.r_[False, negative, False]))
    longest = int((boundaries[1::2] - boundaries[::2]).max())
    trough_position = int(np.argmin(np.where(negative, values, np.inf)))
    recovered = np.flatnonzero(~negative[trough_position + 1:])
    recovery = int(recovered[0] + 1) if recovered.size else None
    return longest, recovery


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


__all__ = [
    "EquityAnalysis", "analyze_equity", "drawdown_duration",
    "equity_metric_kernel", "equity_statistics",
]
