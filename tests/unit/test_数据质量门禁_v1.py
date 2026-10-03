import pandas as pd
import pytest

from inv_trend.data.models import DownloadRequest, InstrumentConfig
from inv_trend.data.processing import compare_sources
from inv_trend.data.providers import AdapterDefaults


class Provider(AdapterDefaults):
    name = "contract-test"


def test_compare_sources_aligns_utc_and_calculates_bps():
    left = pd.DataFrame({"timestamp": ["2026-01-01T00:00:00Z"], "close": [100.0]})
    right = pd.DataFrame({"timestamp": ["2026-01-01T08:00:00+08:00"], "close": [101.0]})
    result = compare_sources(left, right)
    assert len(result) == 1
    assert result.loc[0, "deviation_bps"] == pytest.approx(99.50248756)


def test_compare_sources_rejects_duplicate_identity():
    duplicate = pd.DataFrame({"timestamp": ["2026-01-01T00:00:00Z"] * 2, "close": [1, 1]})
    with pytest.raises(ValueError, match="duplicate timestamps"):
        compare_sources(duplicate, duplicate.iloc[:1])


def test_provider_contract_rejects_non_numeric_prices():
    instrument = InstrumentConfig(symbol="XAU", source_symbol="XAUUSD", asset_class="precious_metal",
        market="otc", instrument_type="cfd", quote_currency="USD", timezone="UTC", session="24x5",
        primary_source="test")
    request = DownloadRequest(instrument, "D1", pd.Timestamp("2026-01-01", tz="UTC").to_pydatetime(),
                              pd.Timestamp("2026-01-02", tz="UTC").to_pydatetime())
    frame = pd.DataFrame({"timestamp": ["2026-01-01T00:00:00Z"], "open": ["bad"],
                          "high": [2], "low": [1], "close": [1.5]})
    with pytest.raises(ValueError, match="non-numeric open"):
        Provider().validate_response(frame, request)
