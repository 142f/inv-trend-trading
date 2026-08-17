from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from inv_trend.data.api import HistoricalDataService
from inv_trend.data.legacy import migrate_legacy_csv
from inv_trend.data.models import DataQualityError, InstrumentNotImplementedError, ProviderResult
from inv_trend.data.providers import CsvBarsProvider

UTC = timezone.utc


class ResearchProvider:
    name = "yahoo_chart"

    def fetch(self, request):
        return ProviderResult(pd.DataFrame({
            "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03"], utc=True),
            "open": [10, 11], "high": [12, 13], "low": [9, 10], "close": [11, 12],
            "adjusted_close": [11, 12], "volume": [100, 100], "is_complete": [True, True],
        }), request.instrument.source_symbol, self.name, "test", {"research_only": True})


def test_research_data_requires_explicit_opt_in(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"yahoo_chart": ResearchProvider()})
    service.ingest("QQQ", "D1", datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    with pytest.raises(DataQualityError):
        service.load_bars("QQQ", "D1")
    assert len(service.load_bars("QQQ", "D1", allow_research=True)) == 2


def test_csv_provider_honours_requested_range(tmp_path: Path):
    path = tmp_path / "bars.csv"
    pd.DataFrame({
        "timestamp": ["2024-01-01", "2024-01-02", "2024-01-03"],
        "open": [1, 2, 3], "high": [2, 3, 4], "low": [0.5, 1.5, 2.5], "close": [1.5, 2.5, 3.5],
    }).to_csv(path, index=False)
    service = HistoricalDataService(tmp_path / "lake", providers={"licensed_csv": CsvBarsProvider(path)})
    # Temporarily use a licensed source definition through XAU's configured identity.
    service.instruments["XAU"] = service.instruments["XAU"].__class__(
        **{**service.instruments["XAU"].__dict__, "primary_source": "licensed_csv"}
    )
    manifest = service.ingest("XAU", "D1", datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
    assert manifest.row_count == 1


def test_reserved_instrument_is_explicitly_rejected(tmp_path: Path):
    service = HistoricalDataService(tmp_path)
    with pytest.raises(InstrumentNotImplementedError):
        service.load_bars("600519", "D1")


def test_legacy_migration_is_not_default_readable(tmp_path: Path):
    source = tmp_path / "processed_data" / "cleaned" / "sample"
    source.mkdir(parents=True)
    pd.DataFrame({
        "date": ["2024-01-01", "2024-01-02"], "symbol": ["OLD", "OLD"], "timeframe": ["D1", "D1"],
        "open": [1, 2], "high": [2, 3], "low": [0.5, 1.5], "close": [1.5, 2.5], "volume": [1, 1],
    }).to_csv(source / "old_cleaned.csv", index=False)
    result = migrate_legacy_csv(tmp_path / "processed_data", tmp_path / "lake")
    assert result["count"] == 1
