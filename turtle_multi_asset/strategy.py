"""多品种海龟趋势策略核心。

本模块只负责生成信号、计算仓位和执行组合层风险预算，不负责真实撮合。
默认参数是保守研究基线，不是收益结论；实盘前必须做样本外、滚动窗口和成本压力测试。
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
    """单个交易品种的合约、成本和风险预算。"""

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
    max_symbol_leverage: float = 1.0
    cost_bps: float = 1.0
    slippage_bps: float = 2.0
    entry_freeze_column: str | None = None
    funding_rate_column: str | None = None
    borrow_rate_column: str | None = None


@dataclass(frozen=True)
class TurtleRules:
    """参数化海龟规则。

    经典规则包括 20/55 突破入场、10/20 反向突破出场、20 周期 Wilder N、
    0.5N 加仓和 2N 保护止损。本实现额外加入方向、品种簇、杠杆和事件冻结约束。
    """

    n_period: int = 20
    fast_entry: int = 20
    slow_entry: int = 55
    fast_exit: int = 10
    slow_exit: int = 20
    stop_n: float = 2.0
    pyramid_step_n: float = 0.5
    trigger_mode: str = "close"  # 可选值为 close 或 intraday
    fast_system_enabled: bool = True
    slow_system_enabled: bool = True
    skip_fast_after_win: bool = True
    allow_short: bool = True
    max_total_1n_risk_pct: float = 0.12
    max_direction_1n_risk_pct: float = 0.08
    default_cluster_1n_risk_pct: float = 0.04
    cluster_1n_risk_pct: Mapping[str, float] = field(default_factory=dict)
    max_total_leverage: float = 2.0
    max_direction_leverage: float = 1.5
    default_cluster_leverage: float = 1.0
    cluster_leverage: Mapping[str, float] = field(default_factory=dict)

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
        for name in (
            "max_total_1n_risk_pct",
            "max_direction_1n_risk_pct",
            "default_cluster_1n_risk_pct",
            "max_total_leverage",
            "max_direction_leverage",
            "default_cluster_leverage",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass
class PositionUnit:
    qty: float
    entry_price: float
    n_at_entry: float
    entry_time: Any = None
    reason: str = ""
    stop_price_at_entry: float = 0.0
    entry_cost: float = 0.0


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

    @property
    def first_entry_time(self) -> Any:
        return self.units[0].entry_time if self.units else None

    @property
    def last_add_time(self) -> Any:
        return self.units[-1].entry_time if self.units else None

    @property
    def first_entry_price(self) -> float:
        return self.units[0].entry_price if self.units else 0.0

    @property
    def entry_reason(self) -> str:
        return self.units[0].reason if self.units else ""

    @property
    def entry_cost(self) -> float:
        return float(sum(unit.entry_cost for unit in self.units))


@dataclass
class PortfolioState:
    positions: dict[str, Position] = field(default_factory=dict)
    last_fast_trade_won: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class Order:
    symbol: str
    action: str  # 可选值为 open、add、exit
    side: int
    qty: float
    reason: str
    system: str
    signal_price: float
    n_at_signal: float
    stop_price: float | None = None
    forced_fill_price: float | None = None
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
    """返回带 N、突破位和出场位的 K 线。

    突破位全部向后移动一根 K 线，确保当前信号不会使用当前 K 线自身的高低点。
    N 使用经典 Wilder 平滑：首个值为前 n 个真实波幅均值，之后递推更新。
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
    out["n"] = _wilder_average(true_range, rules.n_period)

    periods = {
        rules.fast_entry,
        rules.slow_entry,
        rules.fast_exit,
        rules.slow_exit,
    }
    for period in periods:
        out[f"high_{period}"] = out["high"].rolling(period).max().shift(1)
        out[f"low_{period}"] = out["low"].rolling(period).min().shift(1)

    out.attrs["_turtle_rules_key"] = _indicator_rules_key(rules)
    return out


class MultiAssetTurtleStrategy:
    """信号引擎和组合层风险分配器。"""

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
        """基于已完成 K 线生成下一根 K 线开盘执行的订单。

        ``tradable_symbols`` 用于回测多交易日历场景：没有当前 K 线或没有下一根可成交
        K 线的品种不生成新订单，但仍可纳入已有持仓的风险和杠杆预算。
        """

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


def _with_indicators(bars: pd.DataFrame, rules: TurtleRules) -> pd.DataFrame:
    required = _indicator_columns(rules)
    if (
        required.issubset(bars.columns)
        and bars.attrs.get("_turtle_rules_key") == _indicator_rules_key(rules)
    ):
        return bars
    return compute_turtle_indicators(bars, rules)


def _indicator_columns(rules: TurtleRules) -> set[str]:
    periods = {
        rules.fast_entry,
        rules.slow_entry,
        rules.fast_exit,
        rules.slow_exit,
    }
    columns = {"tr", "n"}
    for period in periods:
        columns.add(f"high_{period}")
        columns.add(f"low_{period}")
    return columns


def _indicator_rules_key(rules: TurtleRules) -> tuple[int, int, int, int, int]:
    return (
        rules.n_period,
        rules.fast_entry,
        rules.slow_entry,
        rules.fast_exit,
        rules.slow_exit,
    )


def _wilder_average(values: pd.Series, period: int) -> pd.Series:
    arr = values.to_numpy(dtype=float)
    out = np.full(len(arr), np.nan, dtype=float)
    if len(arr) < period:
        return pd.Series(out, index=values.index)

    seed = arr[:period]
    if not np.all(np.isfinite(seed)):
        return pd.Series(out, index=values.index)
    out[period - 1] = float(np.mean(seed))
    for idx in range(period, len(arr)):
        if np.isfinite(arr[idx]) and np.isfinite(out[idx - 1]):
            out[idx] = (out[idx - 1] * (period - 1) + arr[idx]) / period
    return pd.Series(out, index=values.index)


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
