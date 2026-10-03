from datetime import date, datetime

import pandas as pd
import pytest

from inv_trend.application.asset_config import detector_asset_from_instrument
from inv_trend.data.models import CANONICAL_COLUMNS, InstrumentConfig, date_iso, to_rfc3339_utc
from inv_trend.adapters.multi_asset.data.normalizer import normalize_ohlcv_frame, to_persistent_bars


def instrument(**overrides):
    values = dict(symbol="BTC", source_symbol="BTCUSDT", asset_class="crypto", market="binance",
                  instrument_type="spot", quote_currency="USDT", timezone="UTC", session="24x7",
                  primary_source="binance", earliest_valid_date="2017-08-17", instrument_id="BTC.BINANCE.SPOT")
    values.update(overrides)
    return InstrumentConfig(**values)


def test_instrument_to_detector_mapping_preserves_identity():
    result = detector_asset_from_instrument(instrument(), timeframes=("D1",))
    assert result.instrument == "BTC.BINANCE.SPOT"
    assert result.source_symbols == ("BTCUSDT",)
    assert result.price_type == "spot"
    assert result.data_source == "binance"
    assert result.timeframes == ("D1",)


def test_unknown_asset_class_fails_closed():
    with pytest.raises(ValueError, match="unsupported detector asset_class"):
        detector_asset_from_instrument(instrument(asset_class="rates"))


def test_persistent_conversion_uses_authoritative_columns_and_keeps_spread():
    boundary = normalize_ohlcv_frame(pd.DataFrame({"date": ["2026-01-01"], "open": [1], "high": [2],
        "low": [0.5], "close": [1.5], "spread": [0.1]}), "BTC", "vendor", "D1")
    result = to_persistent_bars(boundary, instrument())
    assert list(result.columns) == [*CANONICAL_COLUMNS, "spread"]
    assert str(result.loc[0, "timestamp"].tz) == "UTC"
    assert result.loc[0, "data_source"] == "vendor"
    assert result.loc[0, "instrument_id"] == "BTC.BINANCE.SPOT"


def test_utc_and_date_serialization_are_unambiguous():
    assert to_rfc3339_utc(datetime.fromisoformat("2026-07-01T08:00:00+08:00")) == "2026-07-01T00:00:00.000000+00:00"
    with pytest.raises(ValueError, match="naive timestamps"):
        to_rfc3339_utc(datetime(2026, 1, 1))
    assert date_iso(date(2026, 9, 14)) == "2026-09-14"
