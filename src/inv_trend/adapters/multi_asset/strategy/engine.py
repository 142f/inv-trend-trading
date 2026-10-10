"""Order generation and portfolio budget allocation for Turtle rules."""

from __future__ import annotations

from typing import Any, Mapping

import numpy as np
import pandas as pd
from inv_trend.core.math_utils import finite_float as _finite_float
from inv_trend.core.趋势准入 import trend_filter

from inv_trend.core.突破规则 import breakout_direction

from ..models.domain import (
    LONG,
    SHORT,
    AssetSpec,
    EntrySignal,
    Order,
    PortfolioState,
    Position,
    SkippedFastTrade,
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
        resolved_virtual_symbols = self._update_skipped_fast_trades(
            rows,
            state,
            active_symbols,
        )

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
            if (
                symbol in blocked_symbols
                or symbol in resolved_virtual_symbols
                or symbol not in active_symbols
            ):
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

        current_risk = self.risk_usage(state, equity)
        current_leverage = self.leverage_usage(
            state,
            rows,
            equity,
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

        slow = self._breakout_signal(row, self.rules.slow_entry, n)
        fast = self._breakout_signal(row, self.rules.fast_entry, n)
        if self.rules.trigger_mode == "intraday":
            signal_price = _finite_float(row.get("high")) or close
            short_signal_price = _finite_float(row.get("low")) or close
        else:
            signal_price = close
            short_signal_price = close

        if self.rules.fast_system_enabled and fast == LONG:
            if spec.can_long and self._entry_filters_allow(row, LONG, signal_price):
                level = self._entry_level(row, self.rules.fast_entry, LONG)
                if self._skip_fast(symbol, state):
                    self._track_skipped_fast_trade(
                        symbol,
                        LONG,
                        signal_price,
                        n,
                        state,
                    )
                else:
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
            if (
                self.rules.allow_short
                and spec.can_short
                and self._entry_filters_allow(row, SHORT, short_signal_price)
            ):
                level = self._entry_level(row, self.rules.fast_entry, SHORT)
                if self._skip_fast(symbol, state):
                    self._track_skipped_fast_trade(
                        symbol,
                        SHORT,
                        short_signal_price,
                        n,
                        state,
                    )
                else:
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
            level = self._entry_level(row, self.rules.slow_entry, LONG)
            if not self._entry_filters_allow(row, LONG, signal_price):
                return None
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
            level = self._entry_level(row, self.rules.slow_entry, SHORT)
            if not self._entry_filters_allow(row, SHORT, short_signal_price):
                return None
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
        risk_scale = 1.0
        if self.rules.risk_atr_fraction_target > 0.0:
            if (signal.close <= 0.0 or not np.isfinite(signal.close)
                    or signal.n <= 0.0 or not np.isfinite(signal.n)):
                return None
            risk_scale = min(1.0, self.rules.risk_atr_fraction_target / (signal.n / signal.close))
        qty = _risk_sized_qty(
            equity=equity,
            unit_1n_risk_pct=spec.unit_1n_risk_pct * risk_scale,
            n=signal.n,
            point_value=spec.point_value,
            qty_step=spec.qty_step,
        )
        # A zero-sized order cannot become a valid pending intent even when a
        # contract profile deliberately leaves ``min_qty`` at zero.
        if qty <= 0 or qty < spec.min_qty:
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
            score += self.rules.slow_entry_score_bonus
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
            metadata={"breakout_level": signal.breakout_level,
                      **({"risk_scale": risk_scale} if self.rules.risk_atr_fraction_target > 0.0 else {})},
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
        if not self._trend_filter_allows(row, position.side, signal_price):
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

    def _track_skipped_fast_trade(
        self,
        symbol: str,
        side: int,
        entry_price: float,
        n: float,
        state: PortfolioState,
    ) -> None:
        if symbol in state.skipped_fast_trades:
            return
        stop_price = (
            entry_price - self.rules.stop_n * n
            if side == LONG
            else entry_price + self.rules.stop_n * n
        )
        state.skipped_fast_trades[symbol] = SkippedFastTrade(
            side=side,
            entry_price=entry_price,
            n_at_entry=n,
            stop_price=stop_price,
        )

    def _update_skipped_fast_trades(
        self,
        rows: Mapping[str, Mapping[str, Any]],
        state: PortfolioState,
        active_symbols: set[str],
    ) -> set[str]:
        """Close virtual skipped trades and return symbols blocked for this bar."""

        resolved: set[str] = set()
        for symbol, tracker in list(state.skipped_fast_trades.items()):
            if symbol not in active_symbols:
                continue
            row = rows.get(symbol)
            if row is None:
                continue
            close = _finite_float(row.get("close"))
            high = _finite_float(row.get("high"))
            low = _finite_float(row.get("low"))
            if close is None:
                continue

            exit_price: float | None = None
            if tracker.side == LONG and low is not None and low <= tracker.stop_price:
                exit_price = tracker.stop_price
            elif tracker.side == SHORT and high is not None and high >= tracker.stop_price:
                exit_price = tracker.stop_price
            else:
                reason = self._exit_signal(row, self.rules.fast_exit, tracker.side)
                if reason is not None:
                    exit_price = close
            if exit_price is None:
                continue

            state.last_fast_trade_won[symbol] = (
                tracker.side * (exit_price - tracker.entry_price) > 0
            )
            del state.skipped_fast_trades[symbol]
            resolved.add(symbol)
        return resolved

    def _breakout_signal(self, row: Mapping[str, Any], period: int, n: float) -> int | None:
        if self.rules.entry_confirmation_bars > 1:
            long = row.get(f"entry_confirm_long_{period}")
            short = row.get(f"entry_confirm_short_{period}")
            long = isinstance(long, (bool, np.bool_)) and bool(long)
            short = isinstance(short, (bool, np.bool_)) and bool(short)
            if long == short:
                return None
            side = LONG if long else SHORT
            name = "high" if side == LONG else "low"
            level = _finite_float(row.get(f"entry_anchor_{name}_{period}"))
            return side if level is not None else None
        return breakout_direction(row, period, n, buffer_n=self.rules.breakout_buffer_n,
                                  trigger_mode=self.rules.trigger_mode)

    def _entry_level(self, row: Mapping[str, Any], period: int, side: int) -> float:
        name = "high" if side == LONG else "low"
        prefix = "entry_anchor_" if self.rules.entry_confirmation_bars > 1 else ""
        return float(row[f"{prefix}{name}_{period}"])

    def _entry_filters_allow(self, row: Mapping[str, Any], side: int, signal_price: float) -> bool:
        return all(self.entry_filter_conditions(row, side, signal_price).values())

    def entry_filter_conditions(self, row: Mapping[str, Any], side: int, signal_price: float) -> dict[str, bool]:
        """Expose the same gates used by order generation for auditable callers."""
        adx = _finite_float(row.get("adx"))
        percentile = _finite_float(row.get("entry_atr_percentile"))
        return {
            'trend_gate': bool(self._trend_filter_allows(row, side, signal_price)),
            'adx_gate': self.rules.entry_adx_min <= 0 or (
                adx is not None and self.rules.entry_adx_min <= adx <= 100.0
            ),
            'atr_percentile_gate': self.rules.entry_atr_percentile_max >= 1.0 or (
                percentile is not None and 0.0 <= percentile <= self.rules.entry_atr_percentile_max
            ),
        }

    def _trend_filter_allows(self, row: Mapping[str, Any], side: int, signal_price: float) -> bool:
        period = self.rules.entry_ma_period
        if period < 2:
            return True
        moving_average = _finite_float(row.get(f"sma_{period}"))
        if moving_average is None:
            return False
        return trend_filter(side, signal_price, moving_average)

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


