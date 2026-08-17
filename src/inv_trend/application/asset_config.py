"""Application-side adaptation from canonical data identity to scan settings."""

from __future__ import annotations

from pathlib import Path

from inv_trend.data.config import load_instruments
from inv_trend.adapters.detector.models import AssetConfig, Market


def load_detector_asset_configs(path: str | Path | None = None) -> dict[str, AssetConfig]:
    """Use the data registry by default; parse a legacy file only if explicit."""
    if path is not None:
        from inv_trend.adapters.detector.config.loader import load_asset_configs

        return load_asset_configs(path)
    market_by_class = {
        "crypto": Market.CRYPTO,
        "precious_metal": Market.PRECIOUS_METAL,
        "equity": Market.US_EQUITY,
    }
    assets: dict[str, AssetConfig] = {}
    for symbol, instrument in load_instruments().items():
        market = market_by_class.get(instrument.asset_class)
        if market is None:
            continue
        assets[symbol] = AssetConfig(
            symbol=symbol,
            instrument=instrument.instrument_id,
            market=market,
            data_source=instrument.primary_source,
            source_symbols=(instrument.source_symbol,),
            price_type=instrument.instrument_type,
            timeframes=("D1", "H4") if market is not Market.US_EQUITY else ("D1", "W1"),
            timezone=instrument.timezone,
            session=instrument.session,
            adjustment=("back_adjusted" if market is Market.US_EQUITY else "none"),
            currency=instrument.currency,
        )
    return assets
