from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CANONICAL_COLUMNS = (
    "symbol", "asset_class", "market", "instrument_type", "timestamp", "timezone",
    "timeframe", "open", "high", "low", "close", "adjusted_close", "volume",
    "quote_currency", "data_source", "is_complete", "ingested_at",
)


class SurvivorshipBiasError(ValueError):
    """Raised when a historical holdings snapshot does not exist."""


@dataclass(frozen=True)
class InstrumentConfig:
    symbol: str
    source_symbol: str
    asset_class: str
    market: str
    instrument_type: str
    quote_currency: str
    timezone: str
    session: str
    primary_source: str
    fallback_sources: tuple[str, ...] = ()
    cross_validation_sources: tuple[str, ...] = ()
    earliest_valid_date: str | None = None
    adjustment_policy: str = "none"
    license: str = "provider terms apply"

    def __post_init__(self) -> None:
        if self.instrument_type not in {"spot", "cfd", "future", "equity", "etf", "perpetual"}:
            raise ValueError(f"unsupported instrument_type: {self.instrument_type}")
        if self.asset_class == "crypto" and not self.earliest_valid_date:
            raise ValueError("crypto instruments require a verified earliest_valid_date")


@dataclass(frozen=True)
class DownloadRequest:
    instrument: InstrumentConfig
    timeframe: str
    start: datetime
    end: datetime


@dataclass
class ProviderResult:
    frame: Any
    actual_symbol: str
    source: str
    license: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Manifest:
    run_id: str
    data_source: str
    requested_symbol: str
    actual_symbol: str
    requested_start: str
    requested_end: str
    actual_start: str | None
    actual_end: str | None
    timeframe: str
    row_count: int
    file_paths: list[str]
    file_hashes: dict[str, str]
    downloaded_at: str
    license: str
    missing_intervals: list[str]
    anomaly_count: int
    cleaning_rule_version: str
    quality_passed: bool
    provider_metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QualityReport:
    symbol: str
    timeframe: str
    actual_start: str | None
    actual_end: str | None
    theoretical_bars: int
    actual_bars: int
    missing_count: int
    missing_ratio: float
    duplicate_count: int
    ohlc_anomaly_count: int
    extreme_jump_count: int
    source_deviation_bps_max: float | None
    timezone_valid: bool
    calendar_valid: bool
    adjustment_complete: bool
    backtest_suitable: bool
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def path_text(path: Path) -> str:
    return path.resolve().as_posix()
