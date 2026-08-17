from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CANONICAL_COLUMNS = (
    "symbol", "instrument_id", "asset_class", "market", "instrument_type",
    "source_symbol", "timeframe", "timestamp", "bar_end", "timezone", "open",
    "high", "low", "close", "adjusted_close", "volume", "currency",
    "quote_currency", "data_source", "price_basis", "adjustment_method",
    "is_complete", "quality_status", "quality_score", "quality_flags",
    "raw_file_hash", "request_id", "raw_snapshot_id", "source_run_id",
    "dataset_version", "curated_version", "ingested_at",
)


class SurvivorshipBiasError(ValueError):
    """Raised when a historical holdings snapshot does not exist."""


class InstrumentNotImplementedError(NotImplementedError):
    """Raised when a registry instrument is reserved for a later release."""


class DataQualityError(ValueError):
    """Raised when no published data meets the requested quality policy."""


class DataConflictError(ValueError):
    """Raised when conflicting OHLCV bars share one timestamp and cannot be resolved silently."""


class DataLineageError(RuntimeError):
    """Raised when a required immutable lineage artifact is missing or inconsistent."""


class ProviderIdentityError(ValueError):
    """Raised when fallback data does not describe the registered instrument."""


@dataclass(frozen=True)
class HoldingsSnapshot:
    fund: str
    snapshot_date: str
    top: int
    path: str
    row_count: int


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
    instrument_id: str = ""
    currency: str = ""
    price_basis: str = "last"
    adjustment_method: str = "none"
    session_timezone: str = "UTC"
    bar_close_rule: str = "provider_native"
    status: str = "ACTIVE"
    revision_overlap_bars: int | None = None
    base_asset: str = ""
    quote_asset: str = ""
    venue: str = ""

    def __post_init__(self) -> None:
        if self.instrument_type not in {"spot", "cfd", "future", "equity", "etf", "perpetual"}:
            raise ValueError(f"unsupported instrument_type: {self.instrument_type}")
        if self.asset_class == "crypto" and not self.earliest_valid_date:
            raise ValueError("crypto instruments require a verified earliest_valid_date")
        if self.status not in {"ACTIVE", "RESERVED"}:
            raise ValueError("status must be ACTIVE or RESERVED")
        if not self.instrument_id:
            object.__setattr__(self, "instrument_id", self.source_symbol)
        if not self.currency:
            object.__setattr__(self, "currency", self.quote_currency)
        if not self.base_asset:
            object.__setattr__(self, "base_asset", self.symbol)
        if not self.quote_asset:
            object.__setattr__(self, "quote_asset", self.quote_currency)
        if not self.venue:
            object.__setattr__(self, "venue", self.market)
        if self.revision_overlap_bars is not None and self.revision_overlap_bars < 0:
            raise ValueError("revision_overlap_bars must be non-negative")


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
    raw_payload: bytes | None = None
    raw_payload_suffix: str = ".json"


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
    instrument_id: str = ""
    dataset_version: str = ""
    quality_score: float = 0.0
    quality_status: str = ""
    request_id: str = ""
    raw_snapshot_id: str = ""
    processing_run_id: str = ""
    curated_version: str = ""
    config_hash: str = ""
    provider_metadata_hash: str = ""
    listing_start: str = ""
    provider_available_start: str = ""
    requested_start_explicit: str = ""
    requested_end_explicit: str = ""
    actual_end_explicit: str = ""
    session_timezone: str = ""
    bar_close_rule: str = ""
    provider_available_start_explicit: str = ""
    manifest_type: str = "ingestion_run"
    parent_dataset_version: str | None = None
    publication_run_id: str = ""
    dataset_manifest_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DatasetManifest:
    """Immutable description of one published, strategy-visible dataset."""

    dataset_version: str
    parent_dataset_version: str | None
    publication_run_id: str
    symbol: str
    instrument_id: str
    timeframe: str
    full_actual_start: str
    full_actual_end: str
    row_count: int
    curated_path: str
    curated_sha256: str
    quality_report_path: str
    quality_report_sha256: str
    published_at: str
    cleaning_rule_version: str

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
    quality_score: float = 0.0
    quality_status: str = "QUARANTINED"
    missing_intervals: list[dict[str, Any]] = field(default_factory=list)
    provider: str = ""
    invalid_rows: int = 0
    coverage_ratio: float = 0.0
    stored_row_count: int = 0
    complete_row_count: int = 0
    incomplete_row_count: int = 0
    latest_stored_bar: str | None = None
    latest_complete_bar: str | None = None
    expected_weekend_bars: int = 0
    expected_holiday_bars: int = 0
    gap_weekend: int = 0
    gap_holiday: int = 0
    gap_provider: int = 0
    gap_unknown: int = 0
    listing_start: str = ""
    provider_available_start: str = ""
    requested_start: str = ""
    requested_end: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["expected_rows"] = self.theoretical_bars
        return payload


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def path_text(path: Path, root: Path | None = None) -> str:
    """Return a portable path, root-relative when ``root`` is given.

    Manifests and current pointers must not depend on the working directory;
    callers pass the data root so artifacts stay relocatable.
    """
    resolved = path.resolve()
    if root is not None:
        try:
            return resolved.relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            pass
    return resolved.as_posix()
