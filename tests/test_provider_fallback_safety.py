from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

from inv_trend.data.api import HistoricalDataService
from inv_trend.data.calendar import is_nyse_trading_day
from inv_trend.data.models import InstrumentConfig, ProviderIdentityError, ProviderResult


UTC = timezone.utc


def _instrument() -> InstrumentConfig:
    return InstrumentConfig(
        symbol="AAA", source_symbol="AAA", instrument_id="AAA.XNAS.EQUITY",
        asset_class="equity", market="xnas", venue="xnas", instrument_type="equity",
        quote_currency="USD", currency="USD", timezone="America/New_York",
        session_timezone="America/New_York", session="regular", primary_source="primary",
        fallback_sources=("fallback",), adjustment_policy="provider_adjusted_close",
        adjustment_method="provider_adjusted_close", earliest_valid_date="2020-01-01",
    )


def _bars() -> pd.DataFrame:
    candidates = pd.date_range("2024-01-02", periods=90, freq="D", tz="UTC")
    stamps = pd.DatetimeIndex([stamp for stamp in candidates if is_nyse_trading_day(stamp)])[:50]
    return pd.DataFrame({
        "timestamp": stamps, "open": [100.0] * 50, "high": [101.0] * 50,
        "low": [99.0] * 50, "close": [100.0] * 50,
        "adjusted_close": [100.0] * 50, "volume": [1000.0] * 50,
    })


def _identity(**overrides):
    values = {
        "instrument_type": "equity", "venue": "xnas", "currency": "USD",
        "price_basis": "last", "adjustment_method": "provider_adjusted_close",
        "session_timezone": "America/New_York", "bar_close_rule": "provider_native",
        "ohlc_definition": "provider_native",
    }
    values.update(overrides)
    return values


class Provider:
    def __init__(self, name, *, error=None, identity=None):
        self.name, self.error, self.identity, self.calls = name, error, identity, 0
    def fetch(self, request):
        self.calls += 1
        if self.error:
            raise self.error
        return ProviderResult(
            _bars(), "AAA", self.name, "test",
            {"instrument_identity": self.identity},
        )


def test_primary_success_never_calls_fallback(tmp_path) -> None:
    primary = Provider("primary", identity=None)
    fallback = Provider("fallback", identity=_identity())
    service = HistoricalDataService(
        tmp_path, instruments={"AAA": _instrument()},
        providers={"primary": primary, "fallback": fallback},
    )
    service.ingest("AAA", "D1", datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 3, 14, tzinfo=UTC))
    assert primary.calls == 1
    assert fallback.calls == 0


def test_primary_failure_accepts_identity_matching_fallback(tmp_path) -> None:
    primary = Provider("primary", error=TimeoutError("timeout"))
    fallback = Provider("fallback", identity=_identity())
    service = HistoricalDataService(
        tmp_path, instruments={"AAA": _instrument()},
        providers={"primary": primary, "fallback": fallback},
    )
    manifest = service.ingest(
        "AAA", "D1", datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 3, 14, tzinfo=UTC), retries=1
    )
    assert manifest.data_source == "fallback"
    assert manifest.provider_metadata["fallback_used"] is True


def test_fallback_identity_mismatch_is_blocked_and_raw_is_preserved(tmp_path) -> None:
    primary = Provider("primary", error=TimeoutError("timeout"))
    fallback = Provider("fallback", identity=_identity(venue="xnys"))
    service = HistoricalDataService(
        tmp_path, instruments={"AAA": _instrument()},
        providers={"primary": primary, "fallback": fallback},
    )
    with pytest.raises(ProviderIdentityError, match="identity mismatch"):
        service.ingest(
            "AAA", "D1", datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 3, 14, tzinfo=UTC), retries=1
        )
    assert list((tmp_path / "raw").rglob("*.parquet"))
    assert service.lake.current_version("AAA", "D1") is None
