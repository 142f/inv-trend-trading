"""Application-side adaptation from canonical data identity to scan settings."""

from __future__ import annotations

from pathlib import Path

from inv_trend.data.config import load_instruments
from inv_trend.adapters.detector.models import AssetConfig
from .契约映射 import DetectorSettings, to_detector_asset


def detector_asset_from_instrument(
    instrument,
    *,
    timeframes: tuple[str, ...] | None = None,
) -> AssetConfig:
    """Build the detector boundary view from canonical instrument identity."""
    selected = timeframes or (("D1", "W1") if instrument.asset_class == "equity" else ("D1", "H4"))
    return to_detector_asset(instrument, DetectorSettings(
        timeframes=selected, allow_short=True, min_volume=0.0,
        point_value=1.0, risk_unit_pct=0.01, max_symbol_risk_pct=0.04,
    ))


def load_detector_asset_configs(path: str | Path | None = None) -> dict[str, AssetConfig]:
    """Use the data registry by default; parse a legacy file only if explicit."""
    if path is not None:
        from inv_trend.adapters.detector.config.loader import load_asset_configs

        return load_asset_configs(path)
    assets: dict[str, AssetConfig] = {}
    for symbol, instrument in load_instruments().items():
        if instrument.asset_class not in {"crypto", "precious_metal", "equity"}:
            continue
        assets[symbol] = detector_asset_from_instrument(instrument)
    return assets
