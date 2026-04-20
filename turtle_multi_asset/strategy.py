"""Turtle-style multi-asset trend-following strategy.

The numeric defaults in this module are conservative candidate baselines.
They are not conclusions. Use walk-forward, out-of-sample, cost-stressed
tests before promoting any parameter set to production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd


LONG = 1
SHORT = -1


@dataclass(frozen=True)
class AssetSpec:
    """Trading metadata and risk budget for one symbol."""

    symbol: str
    asset_class: str
    cluster: str
    point_value: float = 1.0
    qty_step: float = 1.0
    min_qty: float = 0.0
    min_notional: float = 0.0
    can_long: bool = True
    can_short: bool = True
    max_units: int = 3
    unit_1n_risk_pct: float = 0.005
    max_symbol_1n_risk_pct: float = 0.02
    cost_bps: float = 1.0
    slippage_bps: float = 2.0
    entry_freeze_column: str | None = None
    funding_rate_column: str | None = None
    borrow_rate_column: str | None = None


@dataclass(frozen=True)
class TurtleRules:
    """Parameterized Turtle rule set.

    Classic Turtle references:
    - 20/55 day breakout entries
    - 10/20 day reverse breakout exits
    - N based on 20 day true range smoothing
    - 0.5N pyramiding
    - 2N protective stop

    Modern extensions:
    - cluster and direction risk budgets
    - close-confirmed or intraday breakout triggers
    - optional fast breakout skip after a winning fast trade
    """

    n_period: int = 20
    fast_entry: int = 20
    slow_entry: int = 55
    fast_exit: int = 10
    slow_exit: int = 20
    stop_n: float = 2.0
    pyramid_step_n: float = 0.5
    trigger_mode: str = "close"  # "close" or "intraday"
    fast_system_enabled: bool = True
    slow_system_enabled: bool = True
    skip_fast_after_win: bool = True
    allow_short: bool = True
    max_total_1n_risk_pct: float = 0.12
    max_direction_1n_risk_pct: float = 0.08
    default_cluster_1n_risk_pct: float = 0.04
    cluster_1n_risk_pct: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.trigger_mode not in {"close", "intraday"}:
            raise ValueError("trigger_mode must be 'close' or 'intraday'")
        for name in (
            "n_period",
            "fast_entry",
            "slow_entry",
            "fast_exit",
            "slow_exit",
        ):
            if getattr(self, name) < 2:
                raise ValueError(f"{name} must be >= 2")
        if self.stop_n <= 0 or self.pyramid_step_n <= 0:
            raise ValueError("stop_n and pyramid_step_n must be positive")


@dataclass
class PositionUnit:
    qty: float
    entry_price: float
    n_at_entry: float
    entry_time: Any = None


@dataclass
class Position:
    symbol: str
    side: int
    system: str
    units: list[PositionUnit]
    last_add_price: float
    stop_price: float

    @property
    def total_qty(self) -> float:
        return float(sum(unit.qty for unit in self.units))

    @property
    def avg_entry_price(self) -> float:
        qty = self.total_qty
        if qty <= 0:
            return 0.0
        return sum(unit.qty * unit.entry_price for unit in self.units) / qty

    @property
    def unit_count(self) -> int:
        return len(self.units)

    def market_value(self, price: float, point_value: float) -> float:
        return self.side * self.total_qty * price * point_value

    def unrealized_pnl(self, price: float, point_value: float) -> float:
        return (
            self.side
            * self.total_qty
            * (price - self.avg_entry_price)
            * point_value
        )

    def one_n_risk_value(self, point_value: float) -> float:
        return sum(unit.qty * unit.n_at_entry * point_value for unit in self.units)


@dataclass
class PortfolioState:
    positions: dict[str, Position] = field(default_factory=dict)
    last_fast_trade_won: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class Order:
    symbol: str
    action: str  # open, add, exit
    side: int
    qty: float
    reason: str
    system: str
    signal_price: float
    n_at_signal: float
    stop_price: float | None = None
    score: float = 0.0
    risk_1n_pct: float = 0.0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntrySignal:
    symbol: str
    side: int
    system: str
    close: float
    n: float
    breakout_level: float
    strength: float
    reason: str
    event_frozen: bool = False


def compute_turtle_indicators(bars: pd.DataFrame, rules: TurtleRules) -> pd.DataFrame:
    """Return OHLC bars with N, breakout, and exit levels.

    Breakout levels are shifted by one row so today's signal never uses today's
    high or low as its own threshold.
    """

    _require_columns(bars, {"open", "high", "low", "close"})
    out = bars.copy()
    prev_close = out["close"].shift(1)
    true_range = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["tr"] = true_range
    out["n"] = true_range.ewm(
        alpha=1 / rules.n_period,
        adjust=False,
        min_periods=rules.n_period,
    ).mean()

    periods = {
        rules.fast_entry,
        rules.slow_entry,
        rules.fast_exit,
        rules.slow_exit,
    }
    for period in periods:
        out[f"high_{period}"] = out["high"].rolling(period).max().shift(1)
        out[f"low_{period}"] = out["low"].rolling(period).min().shift(1)

    return out


class MultiAssetTurtleStrategy:
    """Signal engine plus portfolio-level risk allocator."""

    def __init__(
        self,
        specs: Mapping[str, AssetSpec],
        rules: TurtleRules | None = None,
    ) -> None:
        self.specs = dict(specs)
        self.rules = rules or TurtleRules()

    def generate_orders(
        self,
        bars_by_symbol: Mapping[str, pd.DataFrame],
        state: PortfolioState,
        equity: float,
    ) -> list[Order]:
        """Generate next-session orders from close-confirmed histories.

        This method is suitable for daily research and paper execution. A live
        implementation should place protective stops in the broker/exchange
        layer immediately after fills.
        """

        if equity <= 0:
            return []

        exit_orders: list[Order] = []
        blocked_symbols: set[str] = set()
        for symbol, position in list(state.positions.items()):
            spec = self.specs.get(symbol)
            bars = bars_by_symbol.get(symbol)
            if spec is None or bars is None or bars.empty:
                continue
            order = self._exit_order(symbol, bars, position, spec, equity)
            if order is not None:
                exit_orders.append(order)
                blocked_symbols.add(symbol)

        add_candidates: list[Order] = []
        entry_candidates: list[Order] = []
        for symbol, spec in self.specs.items():
            if symbol in blocked_symbols:
                continue
            bars = bars_by_symbol.get(symbol)
            if bars is None or bars.empty:
                continue
            position = state.positions.get(symbol)
            if position is None:
                signal = self._entry_signal(symbol, bars, spec, state)
                if signal is not None:
                    order = self._entry_order(signal, spec, equity, action="open")
                    if order is not None:
                        entry_candidates.append(order)
            else:
                order = self._add_order(symbol, bars, position, spec, equity)
                if order is not None:
                    add_candidates.append(order)

        current_risk = self.risk_usage(state, equity, excluding=blocked_symbols)
        accepted = self._allocate_by_budget(
            add_candidates + entry_candidates,
            current_risk,
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

    def _entry_signal(
        self,
        symbol: str,
        bars: pd.DataFrame,
        spec: AssetSpec,
        state: PortfolioState,
    ) -> EntrySignal | None:
        data = compute_turtle_indicators(bars, self.rules)
        row = data.iloc[-1]
        n = _finite_float(row.get("n"))
        close = _finite_float(row.get("close"))
        if n is None or close is None or n <= 0:
            return None

        event_frozen = False
        if spec.entry_freeze_column and spec.entry_freeze_column in row.index:
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
        if qty <= spec.min_qty:
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
        bars: pd.DataFrame,
        position: Position,
        spec: AssetSpec,
        equity: float,
    ) -> Order | None:
        if position.unit_count >= spec.max_units:
            return None
        data = compute_turtle_indicators(bars, self.rules)
        row = data.iloc[-1]
        n = _finite_float(row.get("n"))
        close = _finite_float(row.get("close"))
        if n is None or close is None or n <= 0:
            return None
        trigger = position.last_add_price + position.side * self.rules.pyramid_step_n * n
        should_add = close >= trigger if position.side == LONG else close <= trigger
        if not should_add:
            return None
        signal = EntrySignal(
            symbol=symbol,
            side=position.side,
            system=position.system,
            close=close,
            n=n,
            breakout_level=trigger,
            strength=abs(close - trigger) / n,
            reason=f"add_{self.rules.pyramid_step_n:g}n",
        )
        return self._entry_order(signal, spec, equity, action="add")

    def _exit_order(
        self,
        symbol: str,
        bars: pd.DataFrame,
        position: Position,
        spec: AssetSpec,
        equity: float,
    ) -> Order | None:
        data = compute_turtle_indicators(bars, self.rules)
        row = data.iloc[-1]
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
        equity: float,
    ) -> list[Order]:
        accepted: list[Order] = []
        candidates = sorted(candidates, key=lambda order: order.score, reverse=True)
        for order in candidates:
            spec = self.specs[order.symbol]
            risk = order.risk_1n_pct
            direction_key = "long" if order.side == LONG else "short"
            cluster_limit = self.rules.cluster_1n_risk_pct.get(
                spec.cluster,
                self.rules.default_cluster_1n_risk_pct,
            )
            symbol_risk = usage["symbols"].get(order.symbol, 0.0)
            cluster_risk = usage["clusters"].get(spec.cluster, 0.0)
            if usage["total"] + risk > self.rules.max_total_1n_risk_pct:
                continue
            if usage[direction_key] + risk > self.rules.max_direction_1n_risk_pct:
                continue
            if cluster_risk + risk > cluster_limit:
                continue
            if symbol_risk + risk > spec.max_symbol_1n_risk_pct:
                continue
            accepted.append(order)
            usage["total"] += risk
            usage[direction_key] += risk
            usage["clusters"][spec.cluster] = cluster_risk + risk
            usage["symbols"][order.symbol] = symbol_risk + risk
        return accepted

    def _skip_fast(self, symbol: str, state: PortfolioState) -> bool:
        return self.rules.skip_fast_after_win and state.last_fast_trade_won.get(symbol, False)

    def _breakout_signal(self, row: pd.Series, period: int) -> int | None:
        high_level = _finite_float(row.get(f"high_{period}"))
        low_level = _finite_float(row.get(f"low_{period}"))
        if high_level is None or low_level is None:
            return None
        if self.rules.trigger_mode == "intraday":
            high = _finite_float(row.get("high"))
            low = _finite_float(row.get("low"))
            if high is not None and high > high_level:
                return LONG
            if low is not None and low < low_level:
                return SHORT
        else:
            close = _finite_float(row.get("close"))
            if close is not None and close > high_level:
                return LONG
            if close is not None and close < low_level:
                return SHORT
        return None

    def _exit_signal(self, row: pd.Series, period: int, position_side: int) -> str | None:
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


def _risk_sized_qty(
    equity: float,
    unit_1n_risk_pct: float,
    n: float,
    point_value: float,
    qty_step: float,
) -> float:
    if equity <= 0 or unit_1n_risk_pct <= 0 or n <= 0 or point_value <= 0:
        return 0.0
    raw_qty = equity * unit_1n_risk_pct / (n * point_value)
    return _round_down(raw_qty, qty_step)


def _round_down(value: float, step: float) -> float:
    if step <= 0:
        return float(value)
    return float(np.floor(value / step) * step)


def _finite_float(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(value):
        return None
    return value


def _require_columns(df: pd.DataFrame, columns: set[str]) -> None:
    missing = sorted(columns - set(df.columns))
    if missing:
        raise ValueError(f"missing required bar columns: {missing}")
