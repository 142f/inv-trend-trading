"""Validated public models for the detector boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import math
from typing import Any, Mapping


class Market(str, Enum):
    CRYPTO = "crypto"
    PRECIOUS_METAL = "precious_metal"
    US_EQUITY = "us_equity"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    NONE = "none"


class SignalType(str, Enum):
    APPROACHING_BREAKOUT = "APPROACHING_BREAKOUT"
    SYSTEM1_BREAKOUT = "SYSTEM1_BREAKOUT"
    SYSTEM2_BREAKOUT = "SYSTEM2_BREAKOUT"
    CLOSE_CONFIRMED = "CLOSE_CONFIRMED"
    RETEST_CONFIRMED = "RETEST_CONFIRMED"
    PYRAMID_ADD = "PYRAMID_ADD"
    OVEREXTENDED = "OVEREXTENDED"
    FALSE_BREAKOUT = "FALSE_BREAKOUT"
    EXIT_SIGNAL = "EXIT_SIGNAL"
    ATR_STOP = "ATR_STOP"
    TREND_INVALIDATED = "TREND_INVALIDATED"
    NO_SIGNAL = "NO_SIGNAL"


class ConfirmationStatus(str, Enum):
    NONE = "none"
    INTRADAY_TRIGGERED = "intraday_triggered"
    CLOSE_CONFIRMED = "close_confirmed"
    RETEST_CONFIRMED = "retest_confirmed"


class PositionStatus(str, Enum):
    FLAT = "flat"
    PENDING_CONFIRMATION = "pending_confirmation"
    ENTERED = "entered"
    EXITED = "exited"
    INVALIDATED = "invalidated"


@dataclass(frozen=True)
class AssetConfig:
    symbol: str
    instrument: str
    market: Market
    data_source: str
    timeframes: tuple[str, ...]
    source_symbols: tuple[str, ...] = ()
    timezone: str = "UTC"
    session: str = "24x7"
    price_type: str = "spot"
    adjustment: str = "none"
    currency: str = "USD"
    allow_short: bool = True
    min_volume: float = 0.0
    point_value: float = 1.0
    risk_unit_pct: float = 0.01
    max_symbol_risk_pct: float = 0.04

    def __post_init__(self) -> None:
        if not self.symbol or not self.instrument or not self.data_source:
            raise ValueError("symbol, instrument and data_source are required")
        if not self.timeframes:
            raise ValueError("at least one timeframe is required")
        if self.price_type not in {"spot", "cfd", "future", "etf", "equity", "perpetual"}:
            raise ValueError(f"unsupported price_type: {self.price_type}")
        if self.market is Market.US_EQUITY and self.adjustment not in {
            "forward_adjusted",
            "back_adjusted",
        }:
            raise ValueError("US equity data must declare an adjustment policy")
        for name in ("point_value", "risk_unit_pct", "max_symbol_risk_pct"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.risk_unit_pct > self.max_symbol_risk_pct:
            raise ValueError("risk unit cannot exceed symbol risk cap")

    @property
    def identity(self) -> tuple[str, str, str, str]:
        """Identity used to prevent accidental price-series mixing."""

        return self.instrument, self.data_source, self.price_type, self.adjustment


@dataclass(frozen=True)
class StrategyConfig:
    atr_period: int = 20
    system1_entry: int = 20
    system2_entry: int = 55
    system1_exit: int = 10
    system2_exit: int = 20
    confirmation_mode: str = "close"
    skip_system1_after_win: bool = True
    stop_atr: float = 2.0
    pyramid_step_atr: float = 0.5
    max_additions: int = 3
    stop_mode: str = "unified"
    approaching_atr: float = 0.25
    overextended_atr: float = 1.5
    retest_tolerance_atr: float = 0.25
    false_breakout_bars: int = 3
    max_holding_bars: int = 0
    volatility_lookback: int = 120
    volume_filter: bool = False
    volume_lookback: int = 20
    min_volume_ratio: float = 1.0
    close_location_filter: bool = False
    min_close_location: float = 0.65
    trend_ma_period: int = 60
    long_trend_ma_period: int = 120
    trend_filter: bool = False
    max_gap_atr: float = 1.5

    def __post_init__(self) -> None:
        for name in (
            "atr_period",
            "system1_entry",
            "system2_entry",
            "system1_exit",
            "system2_exit",
            "volume_lookback",
            "trend_ma_period",
            "long_trend_ma_period",
            "volatility_lookback",
        ):
            if getattr(self, name) < 2:
                raise ValueError(f"{name} must be >= 2")
        if self.confirmation_mode not in {"close", "intraday"}:
            raise ValueError("confirmation_mode must be close or intraday")
        if self.stop_mode not in {"unified", "layered"}:
            raise ValueError("stop_mode must be unified or layered")
        if self.stop_atr <= 0 or self.pyramid_step_atr <= 0:
            raise ValueError("ATR stop and pyramid step must be positive")
        if self.max_additions < 0 or self.false_breakout_bars < 1:
            raise ValueError("invalid addition or false-breakout limits")

    @property
    def warmup_bars(self) -> int:
        return max(
            self.atr_period,
            self.system2_entry,
            self.system2_exit,
            self.volatility_lookback,
            self.long_trend_ma_period if self.trend_filter else 0,
        )


@dataclass
class DetectorState:
    symbol: str
    timeframe: str
    status: PositionStatus = PositionStatus.FLAT
    direction: Direction = Direction.NONE
    system: int = 0
    entry_time: str | None = None
    entry_price: float | None = None
    breakout_level: float | None = None
    atr_at_entry: float | None = None
    stop_price: float | None = None
    next_add_price: float | None = None
    additions: int = 0
    holding_bars: int = 0
    last_system1_won: bool = False
    last_signal_key: str | None = None
    pending_since: str | None = None
    last_processed_time: str | None = None


@dataclass(frozen=True)
class TurtleSignal:
    symbol: str
    instrument: str
    market: str
    timeframe: str
    signal_type: SignalType
    raw_signal_type: SignalType
    direction: Direction
    signal_time: str
    trigger_price: float
    channel_high: float | None
    channel_low: float | None
    atr: float
    atr_pct: float
    stop_price: float | None
    next_add_price: float | None
    distance_to_breakout_atr: float
    volatility_percentile: float
    suggested_risk_unit: float
    trend_status: str
    confirmation_status: ConfirmationStatus
    data_source: str
    generated_at: str
    tradeable: bool
    filtered_reasons: tuple[str, ...] = ()
    confidence_note: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "signal_type",
            "raw_signal_type",
            "direction",
            "confirmation_status",
        ):
            payload[key] = getattr(self, key).value
        payload["filtered_reasons"] = list(self.filtered_reasons)
        return payload

    @staticmethod
    def now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class DetectionResult:
    signal: TurtleSignal
    state: DetectorState
    state_changed: bool
