"""Canonical identity projections with no guessed risk or execution values."""
from __future__ import annotations

from dataclasses import dataclass

from inv_trend.adapters.detector.models import AssetConfig, Market
from inv_trend.adapters.multi_asset.models.domain import AssetSpec
from inv_trend.data.models import InstrumentConfig


@dataclass(frozen=True)
class DetectorSettings:
    timeframes: tuple[str, ...]
    allow_short: bool
    min_volume: float
    point_value: float
    risk_unit_pct: float
    max_symbol_risk_pct: float


@dataclass(frozen=True)
class ExecutionSettings:
    cluster: str
    point_value: float
    qty_step: float
    min_qty: float
    min_notional: float
    can_long: bool
    can_short: bool
    max_units: int
    unit_1n_risk_pct: float
    max_symbol_1n_risk_pct: float
    max_symbol_leverage: float
    cost_bps: float
    slippage_bps: float
    entry_freeze_column: str | None = None
    funding_rate_column: str | None = None
    borrow_rate_column: str | None = None


_MARKETS = {
    "crypto": Market.CRYPTO,
    "precious_metal": Market.PRECIOUS_METAL,
    "equity": Market.US_EQUITY,
}


def to_detector_asset(instrument: InstrumentConfig, settings: DetectorSettings) -> AssetConfig:
    try:
        market = _MARKETS[instrument.asset_class]
    except KeyError as exc:
        raise ValueError(f"unsupported detector asset_class: {instrument.asset_class}") from exc
    adjustment = instrument.adjustment_policy
    if adjustment == "provider_adjusted_close":
        # Detector has a coarser execution-facing vocabulary. The canonical
        # data contract retains the exact provider method.
        adjustment = "back_adjusted"
    if market is Market.US_EQUITY and adjustment not in {"forward_adjusted", "back_adjusted"}:
        raise ValueError("US equity projection requires an explicit adjustment policy")
    return AssetConfig(
        symbol=instrument.symbol, instrument=instrument.instrument_id, market=market,
        data_source=instrument.primary_source,
        timeframes=tuple(value.upper() for value in settings.timeframes),
        source_symbols=(instrument.source_symbol,), timezone=instrument.timezone,
        session=instrument.session, price_type=instrument.instrument_type,
        adjustment=adjustment, currency=instrument.currency,
        allow_short=settings.allow_short, min_volume=settings.min_volume,
        point_value=settings.point_value, risk_unit_pct=settings.risk_unit_pct,
        max_symbol_risk_pct=settings.max_symbol_risk_pct,
    )


def to_asset_spec(instrument: InstrumentConfig, settings: ExecutionSettings) -> AssetSpec:
    return AssetSpec(
        symbol=instrument.symbol, asset_class=instrument.asset_class,
        cluster=settings.cluster, point_value=settings.point_value,
        qty_step=settings.qty_step, min_qty=settings.min_qty,
        min_notional=settings.min_notional, can_long=settings.can_long,
        can_short=settings.can_short, max_units=settings.max_units,
        unit_1n_risk_pct=settings.unit_1n_risk_pct,
        max_symbol_1n_risk_pct=settings.max_symbol_1n_risk_pct,
        max_symbol_leverage=settings.max_symbol_leverage,
        cost_bps=settings.cost_bps, slippage_bps=settings.slippage_bps,
        entry_freeze_column=settings.entry_freeze_column,
        funding_rate_column=settings.funding_rate_column,
        borrow_rate_column=settings.borrow_rate_column,
    )
