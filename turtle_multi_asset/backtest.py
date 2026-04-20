"""Minimal daily backtester for the multi-asset Turtle strategy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from .strategy import (
    LONG,
    SHORT,
    AssetSpec,
    MultiAssetTurtleStrategy,
    Order,
    PortfolioState,
    Position,
    PositionUnit,
    TurtleRules,
)


@dataclass(frozen=True)
class BacktestResult:
    equity_curve: pd.Series
    trades: pd.DataFrame
    orders: pd.DataFrame
    metrics: dict[str, float]


class TurtleBacktester:
    """Close-confirmed, next-open fill daily backtester.

    This intentionally stays simple. It is a research scaffold for comparing
    parameter sets, not an exchange-grade fill simulator.
    """

    def __init__(
        self,
        data: Mapping[str, pd.DataFrame],
        specs: Mapping[str, AssetSpec],
        rules: TurtleRules | None = None,
        initial_equity: float = 100_000.0,
        liquidate_at_end: bool = True,
    ) -> None:
        self.data = {symbol: df.sort_index().copy() for symbol, df in data.items()}
        self.specs = dict(specs)
        self.rules = rules or TurtleRules()
        self.strategy = MultiAssetTurtleStrategy(self.specs, self.rules)
        self.initial_equity = float(initial_equity)
        self.liquidate_at_end = liquidate_at_end

    def run(self) -> BacktestResult:
        dates = self._calendar()
        cash = self.initial_equity
        state = PortfolioState()
        pending_orders: list[Order] = []
        equity_points: list[tuple[pd.Timestamp, float]] = []
        order_rows: list[dict] = []
        trade_rows: list[dict] = []

        for date in dates:
            cash = self._execute_orders(date, pending_orders, cash, state, order_rows, trade_rows)
            pending_orders = []
            cash = self._process_intraday_stops(date, cash, state, order_rows, trade_rows)
            cash = self._apply_carry_costs(date, cash, state)
            equity = self._mark_equity(date, cash, state)
            equity_points.append((date, equity))
            histories = self._histories_through(date)
            pending_orders = self.strategy.generate_orders(histories, state, equity)

        if self.liquidate_at_end and dates:
            last_date = dates[-1]
            exit_orders = [
                Order(
                    symbol=symbol,
                    action="exit",
                    side=position.side,
                    qty=position.total_qty,
                    reason="end_of_test",
                    system=position.system,
                    signal_price=self._price(last_date, symbol, "close"),
                    n_at_signal=position.units[-1].n_at_entry,
                )
                for symbol, position in list(state.positions.items())
            ]
            cash = self._execute_orders(
                last_date,
                exit_orders,
                cash,
                state,
                order_rows,
                trade_rows,
                price_column="close",
            )
            equity_points.append((last_date, cash))

        equity_curve = pd.Series(
            [point[1] for point in equity_points],
            index=[point[0] for point in equity_points],
            name="equity",
        )
        trades = pd.DataFrame(trade_rows)
        orders = pd.DataFrame(order_rows)
        return BacktestResult(
            equity_curve=equity_curve,
            trades=trades,
            orders=orders,
            metrics=_metrics(equity_curve, trades),
        )

    def _execute_orders(
        self,
        date: pd.Timestamp,
        orders: list[Order],
        cash: float,
        state: PortfolioState,
        order_rows: list[dict],
        trade_rows: list[dict],
        price_column: str = "open",
    ) -> float:
        for order in orders:
            if order.symbol not in self.specs:
                continue
            try:
                fill_price = self._price(date, order.symbol, price_column)
            except KeyError:
                continue
            if not np.isfinite(fill_price) or fill_price <= 0:
                continue
            spec = self.specs[order.symbol]
            cost = _trade_cost(order.qty, fill_price, spec)
            if order.action in {"open", "add"}:
                cash -= order.side * order.qty * fill_price * spec.point_value
                cash -= cost
                self._apply_entry_fill(date, order, fill_price, state)
            elif order.action == "exit":
                position = state.positions.get(order.symbol)
                if position is None:
                    continue
                pnl = position.unrealized_pnl(fill_price, spec.point_value)
                cash += position.side * position.total_qty * fill_price * spec.point_value
                cash -= cost
                if position.system == "fast":
                    state.last_fast_trade_won[order.symbol] = pnl > 0
                del state.positions[order.symbol]
                trade_rows.append(
                    {
                        "time": date,
                        "symbol": order.symbol,
                        "system": position.system,
                        "side": position.side,
                        "qty": position.total_qty,
                        "avg_entry": position.avg_entry_price,
                        "exit_price": fill_price,
                        "pnl": pnl - cost,
                        "reason": order.reason,
                    }
                )
            order_rows.append(
                {
                    "time": date,
                    "symbol": order.symbol,
                    "action": order.action,
                    "side": order.side,
                    "qty": order.qty,
                    "fill_price": fill_price,
                    "cost": cost,
                    "reason": order.reason,
                    "system": order.system,
                    "risk_1n_pct": order.risk_1n_pct,
                }
            )
        return cash

    def _apply_entry_fill(
        self,
        date: pd.Timestamp,
        order: Order,
        fill_price: float,
        state: PortfolioState,
    ) -> None:
        stop = (
            fill_price - self.rules.stop_n * order.n_at_signal
            if order.side == LONG
            else fill_price + self.rules.stop_n * order.n_at_signal
        )
        unit = PositionUnit(
            qty=order.qty,
            entry_price=fill_price,
            n_at_entry=order.n_at_signal,
            entry_time=date,
        )
        position = state.positions.get(order.symbol)
        if position is None:
            state.positions[order.symbol] = Position(
                symbol=order.symbol,
                side=order.side,
                system=order.system,
                units=[unit],
                last_add_price=fill_price,
                stop_price=stop,
            )
            return
        if position.side != order.side:
            raise ValueError("cannot add to opposite-side position")
        position.units.append(unit)
        position.last_add_price = fill_price
        if position.side == LONG:
            position.stop_price = max(position.stop_price, stop)
        else:
            position.stop_price = min(position.stop_price, stop)

    def _process_intraday_stops(
        self,
        date: pd.Timestamp,
        cash: float,
        state: PortfolioState,
        order_rows: list[dict],
        trade_rows: list[dict],
    ) -> float:
        stop_orders: list[Order] = []
        for symbol, position in list(state.positions.items()):
            df = self.data.get(symbol)
            if df is None or date not in df.index:
                continue
            row = df.loc[date]
            open_price = float(row["open"])
            if position.side == LONG and float(row["low"]) <= position.stop_price:
                stop_price = min(open_price, position.stop_price) if open_price < position.stop_price else position.stop_price
            elif position.side == SHORT and float(row["high"]) >= position.stop_price:
                stop_price = max(open_price, position.stop_price) if open_price > position.stop_price else position.stop_price
            else:
                continue
            stop_orders.append(
                Order(
                    symbol=symbol,
                    action="exit",
                    side=position.side,
                    qty=position.total_qty,
                    reason="intraday_stop",
                    system=position.system,
                    signal_price=stop_price,
                    n_at_signal=position.units[-1].n_at_entry,
                )
            )
            self.data[symbol].loc[date, "_forced_stop_price"] = stop_price
        if not stop_orders:
            return cash
        return self._execute_stop_orders(date, stop_orders, cash, state, order_rows, trade_rows)

    def _execute_stop_orders(
        self,
        date: pd.Timestamp,
        orders: list[Order],
        cash: float,
        state: PortfolioState,
        order_rows: list[dict],
        trade_rows: list[dict],
    ) -> float:
        for order in orders:
            spec = self.specs[order.symbol]
            position = state.positions.get(order.symbol)
            if position is None:
                continue
            fill_price = float(self.data[order.symbol].loc[date, "_forced_stop_price"])
            cost = _trade_cost(position.total_qty, fill_price, spec)
            pnl = position.unrealized_pnl(fill_price, spec.point_value)
            cash += position.side * position.total_qty * fill_price * spec.point_value
            cash -= cost
            if position.system == "fast":
                state.last_fast_trade_won[order.symbol] = pnl > 0
            del state.positions[order.symbol]
            order_rows.append(
                {
                    "time": date,
                    "symbol": order.symbol,
                    "action": "exit",
                    "side": position.side,
                    "qty": position.total_qty,
                    "fill_price": fill_price,
                    "cost": cost,
                    "reason": order.reason,
                    "system": position.system,
                    "risk_1n_pct": order.risk_1n_pct,
                }
            )
            trade_rows.append(
                {
                    "time": date,
                    "symbol": order.symbol,
                    "system": position.system,
                    "side": position.side,
                    "qty": position.total_qty,
                    "avg_entry": position.avg_entry_price,
                    "exit_price": fill_price,
                    "pnl": pnl - cost,
                    "reason": order.reason,
                }
            )
        return cash

    def _apply_carry_costs(
        self,
        date: pd.Timestamp,
        cash: float,
        state: PortfolioState,
    ) -> float:
        for symbol, position in state.positions.items():
            spec = self.specs[symbol]
            df = self.data.get(symbol)
            if df is None or date not in df.index:
                continue
            price = float(df.loc[date, "close"])
            notional = abs(position.total_qty * price * spec.point_value)
            if spec.funding_rate_column and spec.funding_rate_column in df.columns:
                rate = float(df.loc[date, spec.funding_rate_column])
                if np.isfinite(rate):
                    cash -= position.side * notional * rate
            if (
                position.side == SHORT
                and spec.borrow_rate_column
                and spec.borrow_rate_column in df.columns
            ):
                rate = float(df.loc[date, spec.borrow_rate_column])
                if np.isfinite(rate):
                    cash -= notional * rate
        return cash

    def _mark_equity(
        self,
        date: pd.Timestamp,
        cash: float,
        state: PortfolioState,
    ) -> float:
        equity = cash
        for symbol, position in state.positions.items():
            spec = self.specs[symbol]
            try:
                price = self._price(date, symbol, "close")
            except KeyError:
                continue
            equity += position.market_value(price, spec.point_value)
        return float(equity)

    def _histories_through(self, date: pd.Timestamp) -> dict[str, pd.DataFrame]:
        return {
            symbol: df.loc[:date].drop(columns=["_forced_stop_price"], errors="ignore")
            for symbol, df in self.data.items()
            if not df.loc[:date].empty
        }

    def _calendar(self) -> list[pd.Timestamp]:
        all_dates: set[pd.Timestamp] = set()
        for df in self.data.values():
            all_dates.update(pd.Timestamp(x) for x in df.index)
        return sorted(all_dates)

    def _price(self, date: pd.Timestamp, symbol: str, column: str) -> float:
        df = self.data[symbol]
        if date not in df.index:
            raise KeyError(symbol)
        if column not in df.columns:
            column = "close"
        return float(df.loc[date, column])


def _trade_cost(qty: float, price: float, spec: AssetSpec) -> float:
    notional = abs(qty * price * spec.point_value)
    return notional * (spec.cost_bps + spec.slippage_bps) / 10000.0


def _metrics(equity_curve: pd.Series, trades: pd.DataFrame) -> dict[str, float]:
    if equity_curve.empty:
        return {}
    returns = equity_curve.pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    running_max = equity_curve.cummax()
    drawdown = equity_curve / running_max - 1.0
    total_return = equity_curve.iloc[-1] / equity_curve.iloc[0] - 1.0
    years = max((equity_curve.index[-1] - equity_curve.index[0]).days / 365.25, 1 / 365.25)
    cagr = (1.0 + total_return) ** (1.0 / years) - 1.0
    vol = returns.std() * np.sqrt(252) if not returns.empty else 0.0
    sharpe = (returns.mean() * 252 / vol) if vol > 0 else 0.0
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
    }
