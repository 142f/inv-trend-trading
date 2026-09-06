"""Canonical performance metrics for comparable backtest combinations."""

from __future__ import annotations

import math
from typing import Any, Iterable

import numpy as np
import pandas as pd

from inv_trend.core.performance import analyze_equity, drawdown_duration as _drawdown_duration


def performance_metrics(
    equity: pd.Series,
    trades: pd.DataFrame,
    orders: pd.DataFrame | None = None,
    *,
    open_position_count: int = 0,
    unrealized_pnl: float = 0.0,
    pending_intent_count: int = 0,
) -> tuple[dict[str, Any], dict[str, str]]:
    unavailable: dict[str, str] = {}
    if equity.empty:
        return {}, {"all": "权益曲线为空"}
    analysis = analyze_equity(equity)
    curve, returns, shared = analysis.curve, analysis.returns, analysis.metrics
    end = float(shared["ending_equity"])
    periods_per_year = float(shared["periods_per_year"])
    total_return = float(shared["total_return"])
    cagr = float(shared["annualized_return"])
    volatility = _finite_or_none(shared["volatility"])
    sharpe = _finite_or_none(shared["sharpe_ratio"])
    if sharpe is None:
        unavailable["sharpe_ratio"] = "收益波动率为零"
    downside = returns[returns < 0]
    downside_deviation = (
        float(np.sqrt(np.mean(np.square(downside))) * math.sqrt(periods_per_year))
        if len(downside) and periods_per_year else None
    )
    sortino = _ratio(
        returns.mean() * periods_per_year if periods_per_year else None,
        downside_deviation,
        "sortino_ratio",
        unavailable,
        "没有可用的下行波动",
    )
    max_drawdown = float(shared["max_drawdown"])
    mar = _ratio(
        cagr, abs(max_drawdown), "mar_ratio", unavailable, "最大回撤为零"
    )
    dd_duration, dd_recovery = _drawdown_duration(analysis.drawdown)

    frame = trades
    pnls = pd.to_numeric(frame.get("pnl", pd.Series(dtype=float)), errors="coerce").dropna()
    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    trade_count = int(len(pnls))
    win_rate = float(len(wins) / trade_count) if trade_count else None
    average_win = float(wins.mean()) if len(wins) else None
    average_loss = float(losses.mean()) if len(losses) else None
    payoff = _ratio(
        average_win,
        abs(average_loss) if average_loss is not None else None,
        "payoff_ratio",
        unavailable,
        "缺少盈利交易或亏损交易",
    )
    gross_profit, gross_loss = float(wins.sum()), float(-losses.sum())
    profit_factor = _ratio(
        gross_profit if len(wins) else None,
        gross_loss if len(losses) else None,
        "profit_factor",
        unavailable,
        "缺少盈利交易或亏损交易",
    )
    expectancy = float(pnls.mean()) if trade_count else None
    if not trade_count:
        unavailable.update({
            "win_rate": "没有已完成交易",
            "average_win": "没有盈利交易",
            "average_loss": "没有亏损交易",
            "expectancy": "没有已完成交易",
            "average_holding_bars": "没有已完成交易",
        })
    else:
        if average_win is None:
            unavailable["average_win"] = "没有盈利交易"
        if average_loss is None:
            unavailable["average_loss"] = "没有亏损交易"
    holding = pd.to_numeric(frame.get("holding_bars", pd.Series(dtype=float)), errors="coerce")
    average_holding = float(holding.dropna().mean()) if holding.notna().any() else None
    total_cost = _column_sum(frame, "total_cost")
    notional = _column_sum(frame, "notional_at_exit")
    average_equity = float(curve.mean())
    turnover = notional * 2 / average_equity if average_equity > 0 else None
    held_bars = float(holding.dropna().sum()) if holding.notna().any() else 0.0
    symbol_count = (
        max(int(frame["symbol"].nunique()), 1)
        if not frame.empty and "symbol" in frame else 1
    )
    exposure = min(1.0, held_bars / max(len(curve) * symbol_count, 1))
    metrics = {
        "total_return": float(total_return), "annualized_return": float(cagr),
        "max_drawdown": max_drawdown, "max_drawdown_duration_bars": dd_duration,
        "max_drawdown_recovery_bars": dd_recovery, "volatility": volatility,
        "sharpe_ratio": sharpe, "sortino_ratio": sortino, "mar_ratio": mar,
        "periods_per_year": float(periods_per_year), "ending_equity": end,
        "trade_count": trade_count, "closed_trade_count": trade_count,
        "open_position_count": int(open_position_count),
        "unrealized_pnl": float(unrealized_pnl),
        "pending_intent_count": int(pending_intent_count),
        "win_rate": win_rate,
        "average_win": average_win, "average_loss": average_loss,
        "payoff_ratio": payoff, "profit_factor": profit_factor,
        "expectancy": expectancy, "average_holding_bars": average_holding,
        "exposure": float(exposure), "turnover": turnover,
        "total_cost": total_cost,
        "order_count": int(0 if orders is None or orders.empty else len(orders)),
        "bankrupt": bool((curve <= 0).any()),
    }
    return _json_numbers(metrics), unavailable


def trade_breakdown(trades: pd.DataFrame, columns: Iterable[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if trades.empty:
        return result
    for column in columns:
        if column not in trades:
            continue
        groups: dict[str, Any] = {}
        for name, frame in trades.groupby(column, dropna=False):
            pnl = pd.to_numeric(frame["pnl"], errors="coerce").dropna()
            groups[str(name)] = {
                "trade_count": int(len(pnl)),
                "pnl": float(pnl.sum()),
                "win_rate": float((pnl > 0).mean()) if len(pnl) else None,
            }
        result[column] = groups
    return result


def side_metrics(trades: pd.DataFrame, side: str) -> dict[str, Any]:
    if trades.empty or "side_name" not in trades:
        return {"trade_count": 0, "pnl": 0.0, "win_rate": None}
    selected = trades.loc[trades["side_name"].astype(str) == side]
    pnl = pd.to_numeric(selected.get("pnl", pd.Series(dtype=float)), errors="coerce").dropna()
    return {
        "trade_count": int(len(pnl)), "pnl": float(pnl.sum()),
        "win_rate": float((pnl > 0).mean()) if len(pnl) else None,
        "average_pnl": float(pnl.mean()) if len(pnl) else None,
    }


def curve_rows(equity: pd.Series) -> tuple[dict[str, Any], ...]:
    return tuple(
        {"time": pd.Timestamp(time).isoformat(), "equity": float(value)}
        for time, value in equity.items()
    )


def drawdown_rows(equity: pd.Series) -> tuple[dict[str, Any], ...]:
    curve = equity.astype(float)
    drawdown = curve / curve.cummax() - 1.0
    return tuple(
        {"time": pd.Timestamp(time).isoformat(), "drawdown": float(value)}
        for time, value in drawdown.items()
    )


def _ratio(
    numerator: float | None,
    denominator: float | None,
    name: str,
    unavailable: dict[str, str],
    reason: str,
) -> float | None:
    if numerator is None or denominator is None or not math.isfinite(float(denominator)) or denominator == 0:
        unavailable[name] = reason
        return None
    value = float(numerator) / float(denominator)
    if not math.isfinite(value):
        unavailable[name] = reason
        return None
    return value


def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _json_numbers(values: dict[str, Any]) -> dict[str, Any]:
    return {key: _finite_or_none(value) if isinstance(value, (float, np.floating)) else value for key, value in values.items()}


def _column_sum(frame: pd.DataFrame, column: str) -> float:
    if frame.empty or column not in frame:
        return 0.0
    return float(pd.to_numeric(frame[column], errors="coerce").fillna(0).sum())


__all__ = [
    "curve_rows", "drawdown_rows", "performance_metrics", "side_metrics",
    "trade_breakdown",
]
