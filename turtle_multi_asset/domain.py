"""Domain models shared by strategy and backtest components."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


LONG = 1
SHORT = -1


@dataclass(frozen=True)
class AssetSpec:
    """Contract, cost, permission, and risk budget settings for one symbol."""

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
    """Parameterized Turtle rules and portfolio-level caps."""

    n_period: int = 20
    fast_entry: int = 20
    slow_entry: int = 55
    fast_exit: int = 10
    slow_exit: int = 20
    stop_n: float = 2.0
    pyramid_step_n: float = 0.5
    trigger_mode: str = "close"
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
    action: str
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
