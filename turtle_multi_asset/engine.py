"""Order generation and portfolio budget allocation for Turtle rules."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd

from .domain import (
    LONG,
    SHORT,
    AssetSpec,
    EntrySignal,
    Order,
    PortfolioState,
    Position,
    TurtleRules,
)
from .indicators import _with_indicators
from .sizing import _risk_sized_qty


class MultiAssetTurtleStrategy:
    """Signal engine and portfolio-level risk allocator."""

    def __init__(
        self,
        specs: Mapping[str, AssetSpec],
        rules: TurtleRules | None = None,
    ) -> None:
        self.specs = dict(specs)
        self.rules = rules or TurtleRules()

    def generate_orders(
        self,
        rows_by_symbol: Mapping[str, Mapping[str, Any] | pd.Series | pd.DataFrame],
        state: PortfolioState,
        equity: float,
        tradable_symbols: set[str] | None = None,
    ) -> list[Order]:
        if equity <= 0:
            return []
        rows = self._rows_by_symbol(rows_by_symbol)
        active_symbols = set(rows) if tradable_symbols is None else set(tradable_symbols)

        exit_orders: list[Order] = []
        blocked_symbols: set[str] = set()
        for symbol, position in list(state.positions.items()):
            if symbol not in active_symbols:
                continue
            spec = self.specs.get(symbol)
            row = rows.get(symbol)
            if spec is None or row is None:
                continue
            order = self._exit_order(symbol, row, position, spec, equity)
            if order is not None:
                exit_orders.append(order)
                blocked_symbols.add(symbol)

        add_candidates: list[Order] = []
        entry_candidates: list[Order] = []
        for symbol, spec in self.specs.items():
            if symbol in blocked_symbols or symbol not in active_symbols:
                continue
            row = rows.get(symbol)
            if row is None:
                continue
            position = state.positions.get(symbol)
            if position is None:
                signal = self._entry_signal(symbol, row, spec, state)
                if signal is not None:
                    order = self._entry_order(signal, spec, equity, action="open")
                    if order is not None:
                        entry_candidates.append(order)
            else:
                order = self._add_order(symbol, row, position, spec, equity)
                if order is not None:
                    add_candidates.append(order)

        current_risk = self.risk_usage(state, equity, excluding=blocked_symbols)
        current_leverage = self.leverage_usage(
            state,
            rows,
            equity,
            excluding=blocked_symbols,
        )
        accepted = self._allocate_by_budget(
            add_candidates + entry_candidates,
            current_risk,
            current_leverage,
            equity,
        )
        return exit_orders + accepted

    def risk_usage(
        self,
        state: PortfolioState,
        equity: float,
        excluding: set[str] | None = None,
    ) -> dict[str, Any]:
        excluding = excluding or set()
        usage: dict[str, Any] = {
            "total": 0.0,
            "long": 0.0,
            "short": 0.0,
            "clusters": {},
            "symbols": {},
        }
        if equity <= 0:
            return usage
        for symbol, position in state.positions.items():
            if symbol in excluding:
                continue
            spec = self.specs.get(symbol)
            if spec is None:
                continue
            risk_pct = position.one_n_risk_value(spec.point_value) / equity
            usage["total"] += risk_pct
            if position.side == LONG:
                usage["long"] += risk_pct
            else:
                usage["short"] += risk_pct
            usage["clusters"][spec.cluster] = (
                usage["clusters"].get(spec.cluster, 0.0) + risk_pct
            )
            usage["symbols"][symbol] = usage["symbols"].get(symbol, 0.0) + risk_pct
        return usage

    def leverage_usage(
        self,
        state: PortfolioState,
        rows_by_symbol: Mapping[str, Mapping[str, Any]],
        equity: float,
        excluding: set[str] | None = None,
    ) -> dict[str, Any]:
        excluding = excluding or set()
        usage: dict[str, Any] = {
            "total": 0.0,
            "long": 0.0,
            "short": 0.0,
            "clusters": {},
            "symbols": {},
        }
        if equity <= 0:
            return usage
        for symbol, position in state.positions.items():
            if symbol in excluding:
                continue
            spec = self.specs.get(symbol)
            row = rows_by_symbol.get(symbol)
            if spec is None or row is None:
                continue
            price = _finite_float(row.get("close"))
            if price is None or price <= 0:
                continue
            leverage = abs(position.total_qty * price * spec.point_value) / equity
            usage["total"] += leverage
            if position.side == LONG:
                usage["long"] += leverage
            else:
                usage["short"] += leverage
            usage["clusters"][spec.cluster] = (
                usage["clusters"].get(spec.cluster, 0.0) + leverage
            )
            usage["symbols"][symbol] = usage["symbols"].get(symbol, 0.0) + leverage
        return usage

    def _entry_signal(
        self,
        symbol: str,
        row: Mapping[str, Any],
        spec: AssetSpec,
        state: PortfolioState,
    ) -> EntrySignal | None:
        n = _finite_float(row.get("n"))
        close = _finite_float(row.get("close"))
        if n is None or close is None or n <= 0:
            return None

        event_frozen = False
        if spec.entry_freeze_column and spec.entry_freeze_column in row:
            event_frozen = bool(row[spec.entry_freeze_column])
        if event_frozen:
            return None

        slow = self._breakout_signal(row, self.rules.slow_entry)
        fast = self._breakout_signal(row, self.rules.fast_entry)
        if self.rules.trigger_mode == "intraday":
            signal_price = _finite_float(row.get("high")) or close
            short_signal_price = _finite_float(row.get("low")) or close
        else:
            signal_price = close
            short_signal_price = close

        if self.rules.fast_system_enabled and fast == LONG:
            if spec.can_long and not self._skip_fast(symbol, state):
                level = float(row[f"high_{self.rules.fast_entry}"])
                return EntrySignal(
                    symbol=symbol,
                    side=LONG,
                    system="fast",
                    close=signal_price,
                    n=n,
                    breakout_level=level,
                    strength=max((signal_price - level) / n, 0.0),
                    reason=f"long_{self.rules.fast_entry}d_breakout",
                )
        if self.rules.fast_system_enabled and fast == SHORT:
            if self.rules.allow_short and spec.can_short and not self._skip_fast(symbol, state):
                level = float(row[f"low_{self.rules.fast_entry}"])
                return EntrySignal(
                    symbol=symbol,
                    side=SHORT,
                    system="fast",
                    close=short_signal_price,
                    n=n,
                    breakout_level=level,
                    strength=max((level - short_signal_price) / n, 0.0),
                    reason=f"short_{self.rules.fast_entry}d_breakout",
                )

        if self.rules.slow_system_enabled and slow == LONG and spec.can_long:
            level = float(row[f"high_{self.rules.slow_entry}"])
            return EntrySignal(
                symbol=symbol,
                side=LONG,
                system="slow",
                close=signal_price,
                n=n,
                breakout_level=level,
                strength=max((signal_price - level) / n, 0.0),
                reason=f"long_{self.rules.slow_entry}d_breakout",
            )
        if (
            self.rules.slow_system_enabled
            and slow == SHORT
            and self.rules.allow_short
            and spec.can_short
        ):
            level = float(row[f"low_{self.rules.slow_entry}"])
            return EntrySignal(
                symbol=symbol,
                side=SHORT,
                system="slow",
                close=short_signal_price,
                n=n,
                breakout_level=level,
                strength=max((level - short_signal_price) / n, 0.0),
                reason=f"short_{self.rules.slow_entry}d_breakout",
            )
        return None

    def _entry_order(
        self,
        signal: EntrySignal,
        spec: AssetSpec,
        equity: float,
        action: str,
    ) -> Order | None:
        qty = _risk_sized_qty(
            equity=equity,
            unit_1n_risk_pct=spec.unit_1n_risk_pct,
            n=signal.n,
            point_value=spec.point_value,
            qty_step=spec.qty_step,
        )
        if qty < spec.min_qty:
            return None
        notional = qty * signal.close * spec.point_value
        if notional < spec.min_notional:
            return None
        stop_price = (
            signal.close - self.rules.stop_n * signal.n
            if signal.side == LONG
            else signal.close + self.rules.stop_n * signal.n
        )
        risk_pct = qty * signal.n * spec.point_value / equity
        score = signal.strength
        if signal.system == "slow":
            score += 0.25
        score -= (spec.cost_bps + spec.slippage_bps) / 10000
        return Order(
            symbol=signal.symbol,
            action=action,
            side=signal.side,
            qty=qty,
            reason=signal.reason,
            system=signal.system,
            signal_price=signal.close,
            n_at_signal=signal.n,
            stop_price=stop_price,
            score=score,
            risk_1n_pct=risk_pct,
            metadata={"breakout_level": signal.breakout_level},
        )

    def _add_order(
        self,
        symbol: str,
        row: Mapping[str, Any],
        position: Position,
        spec: AssetSpec,
        equity: float,
    ) -> Order | None:
        if position.unit_count >= spec.max_units:
            return None
        n = _finite_float(row.get("n"))
        close = _finite_float(row.get("close"))
        if n is None or close is None or n <= 0:
            return None
        trigger = position.last_add_price + position.side * self.rules.pyramid_step_n * n
        if self.rules.trigger_mode == "intraday":
            high = _finite_float(row.get("high"))
            low = _finite_float(row.get("low"))
            if position.side == LONG:
                should_add = high is not None and high >= trigger
            else:
                should_add = low is not None and low <= trigger
            signal_price = trigger
        else:
            should_add = close >= trigger if position.side == LONG else close <= trigger
            signal_price = close
        if not should_add:
            return None
        signal = EntrySignal(
            symbol=symbol,
            side=position.side,
            system=position.system,
            close=signal_price,
            n=n,
            breakout_level=trigger,
            strength=abs(signal_price - trigger) / n,
            reason=f"add_{self.rules.pyramid_step_n:g}n",
        )
        return self._entry_order(signal, spec, equity, action="add")

    def _exit_order(
        self,
        symbol: str,
        row: Mapping[str, Any],
        position: Position,
        spec: AssetSpec,
        equity: float,
    ) -> Order | None:
        close = _finite_float(row.get("close"))
        n = _finite_float(row.get("n")) or position.units[-1].n_at_entry
        if close is None:
            return None

        if position.side == LONG and close <= position.stop_price:
            reason = "close_below_stop"
        elif position.side == SHORT and close >= position.stop_price:
            reason = "close_above_stop"
        else:
            exit_period = (
                self.rules.fast_exit if position.system == "fast" else self.rules.slow_exit
            )
            reverse = self._exit_signal(row, exit_period, position.side)
            if reverse is None:
                return None
            reason = reverse

        risk_pct = position.one_n_risk_value(spec.point_value) / equity if equity > 0 else 0.0
        return Order(
            symbol=symbol,
            action="exit",
            side=position.side,
            qty=position.total_qty,
            reason=reason,
            system=position.system,
            signal_price=close,
            n_at_signal=n,
            stop_price=None,
            score=10.0,
            risk_1n_pct=risk_pct,
        )

    def _allocate_by_budget(
        self,
        candidates: list[Order],
        usage: dict[str, Any],
        leverage_usage: dict[str, Any],
        equity: float,
    ) -> list[Order]:
        if equity <= 0:
            return []
        accepted: list[Order] = []
        candidates = sorted(candidates, key=lambda order: order.score, reverse=True)
        for order in candidates:
            spec = self.specs[order.symbol]
            risk = order.risk_1n_pct
            leverage = abs(order.qty * order.signal_price * spec.point_value) / equity
            direction_key = "long" if order.side == LONG else "short"
            cluster_limit = self.rules.cluster_1n_risk_pct.get(
                spec.cluster,
                self.rules.default_cluster_1n_risk_pct,
            )
            cluster_leverage_limit = self.rules.cluster_leverage.get(
                spec.cluster,
                self.rules.default_cluster_leverage,
            )
            symbol_risk = usage["symbols"].get(order.symbol, 0.0)
            cluster_risk = usage["clusters"].get(spec.cluster, 0.0)
            symbol_leverage = leverage_usage["symbols"].get(order.symbol, 0.0)
            cluster_leverage = leverage_usage["clusters"].get(spec.cluster, 0.0)
            if usage["total"] + risk > self.rules.max_total_1n_risk_pct:
                continue
            if usage[direction_key] + risk > self.rules.max_direction_1n_risk_pct:
                continue
            if cluster_risk + risk > cluster_limit:
                continue
            if symbol_risk + risk > spec.max_symbol_1n_risk_pct:
                continue
            if leverage_usage["total"] + leverage > self.rules.max_total_leverage:
                continue
            if leverage_usage[direction_key] + leverage > self.rules.max_direction_leverage:
                continue
            if cluster_leverage + leverage > cluster_leverage_limit:
                continue
            if symbol_leverage + leverage > spec.max_symbol_leverage:
                continue
            accepted.append(order)
            usage["total"] += risk
            usage[direction_key] += risk
            usage["clusters"][spec.cluster] = cluster_risk + risk
            usage["symbols"][order.symbol] = symbol_risk + risk
            leverage_usage["total"] += leverage
            leverage_usage[direction_key] += leverage
            leverage_usage["clusters"][spec.cluster] = cluster_leverage + leverage
            leverage_usage["symbols"][order.symbol] = symbol_leverage + leverage
        return accepted

    def _skip_fast(self, symbol: str, state: PortfolioState) -> bool:
        return self.rules.skip_fast_after_win and state.last_fast_trade_won.get(symbol, False)

    def _breakout_signal(self, row: Mapping[str, Any], period: int) -> int | None:
        high_level = _finite_float(row.get(f"high_{period}"))
        low_level = _finite_float(row.get(f"low_{period}"))
        if high_level is None or low_level is None:
            return None
        if self.rules.trigger_mode == "intraday":
            high = _finite_float(row.get("high"))
            low = _finite_float(row.get("low"))
            long_hit = high is not None and high > high_level
            short_hit = low is not None and low < low_level
            if long_hit and short_hit:
                return None
            if long_hit:
                return LONG
            if short_hit:
                return SHORT
        else:
            close = _finite_float(row.get("close"))
            if close is not None and close > high_level:
                return LONG
            if close is not None and close < low_level:
                return SHORT
        return None

    def _exit_signal(self, row: Mapping[str, Any], period: int, position_side: int) -> str | None:
        high_level = _finite_float(row.get(f"high_{period}"))
        low_level = _finite_float(row.get(f"low_{period}"))
        close = _finite_float(row.get("close"))
        if high_level is None or low_level is None or close is None:
            return None
        if position_side == LONG and close < low_level:
            return f"long_exit_{period}d_low"
        if position_side == SHORT and close > high_level:
            return f"short_exit_{period}d_high"
        return None

    def _rows_by_symbol(
        self,
        rows_by_symbol: Mapping[str, Mapping[str, Any] | pd.Series | pd.DataFrame],
    ) -> dict[str, Mapping[str, Any]]:
        rows: dict[str, Mapping[str, Any]] = {}
        for symbol, value in rows_by_symbol.items():
            if value is None:
                continue
            if isinstance(value, pd.DataFrame):
                if value.empty:
                    continue
                rows[symbol] = _with_indicators(value, self.rules).iloc[-1]
            else:
                rows[symbol] = value
        return rows


def _finite_float(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(value):
        return None
    return value
