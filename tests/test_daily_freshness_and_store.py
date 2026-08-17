from datetime import datetime, timezone

from inv_trend.data.models import InstrumentConfig
from inv_trend.adapters.detector.freshness import FreshnessPolicy


def _instrument(*, session: str, market: str) -> InstrumentConfig:
    return InstrumentConfig(
        symbol="X", source_symbol="X", asset_class="equity" if session == "regular" else "crypto",
        market=market, instrument_type="equity" if session == "regular" else "spot",
        quote_currency="USD", timezone="UTC", session=session, primary_source="fake",
        earliest_valid_date="2020-01-01",
    )


def test_crypto_freshness_requires_previous_utc_day() -> None:
    result = FreshnessPolicy().evaluate(
        _instrument(session="24x7", market="crypto"), "2024-04-18T00:00:00Z",
        now=datetime(2024, 4, 19, 9, tzinfo=timezone.utc),
    )
    assert result.is_fresh
    assert result.expected_date == "2024-04-18"


def test_equity_freshness_skips_weekend_and_holiday() -> None:
    result = FreshnessPolicy().evaluate(
        _instrument(session="regular", market="xnas"), "2024-07-03T00:00:00Z",
        now=datetime(2024, 7, 5, 12, tzinfo=timezone.utc),
    )
    assert result.is_fresh
    assert result.expected_date == "2024-07-03"
