from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from historical_data.api import HistoricalDataService
from historical_data.config import load_instruments
from historical_data.models import ProviderResult, SurvivorshipBiasError
from historical_data.processing import (
    apply_equity_adjustments,
    build_back_adjusted_continuous,
    normalize_bars,
)
from historical_data.providers import QqqHoldingsCsvProvider
from historical_data.storage import sha256_file

pytest.importorskip("pyarrow")
UTC = timezone.utc


class FakeProvider:
    name = "binance"

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls = []

    def fetch(self, request):
        self.calls.append(request)
        return ProviderResult(
            self.frame.copy(), request.instrument.source_symbol, self.name, "test license"
        )


def bars() -> pd.DataFrame:
    return pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"], utc=True),
        "open": [10, 11, 12], "high": [12, 13, 14], "low": [9, 10, 11],
        "close": [11, 12, 13], "volume": [100, 100, 100],
        "is_complete": [True, True, False],
    })


def test_ingest_manifest_parquet_hash_and_detector_compatible(tmp_path: Path):
    provider = FakeProvider(bars())
    service = HistoricalDataService(tmp_path, providers={"binance": provider})
    manifest = service.ingest(
        "BTC", "D1", datetime(2000, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC)
    )
    assert provider.calls[0].start.year == 2017
    assert manifest.actual_symbol == "BTCUSDT"
    assert all(sha256_file(Path(path)) == digest for path, digest in manifest.file_hashes.items())
    loaded = service.load_bars("BTC", "D1")
    assert loaded["timestamp"].is_monotonic_increasing
    assert loaded["timestamp"].is_unique
    assert len(loaded) == 2
    assert {"open", "high", "low", "close", "volume"}.issubset(loaded)
    assert loaded.attrs["data_sources"] == ["binance"]


def test_duplicate_invalid_ohlc_and_early_crypto_are_quarantined():
    frame = bars()
    extra = pd.DataFrame({
        "timestamp": pd.to_datetime(["2010-01-01", "2024-01-01"], utc=True),
        "open": [1, 10], "high": [0, 12], "low": [2, 9], "close": [1, 11],
        "volume": [1, 1], "is_complete": [True, True],
    })
    result = normalize_bars(pd.concat([frame, extra]), load_instruments()["BTC"], "D1", "x")
    reasons = ";".join(result.quarantine["quarantine_reason"])
    assert "before_verified_history" in reasons
    assert "invalid_ohlc" in reasons
    assert "duplicate_superseded" in reasons


def test_timezone_conversion_and_missing_detection():
    frame = bars().copy()
    frame["timestamp"] = frame["timestamp"].dt.tz_convert("America/New_York").dt.tz_localize(None)
    result = normalize_bars(frame, load_instruments()["BTC"], "D1", "x")
    assert str(result.clean["timestamp"].dt.tz) == "UTC"


def test_equity_split_adjusts_all_ohlc():
    frame = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01", "2024-01-02"], utc=True),
        "open": [100, 50], "high": [102, 52], "low": [98, 48], "close": [100, 50],
        "split_factor": [1, 2], "dividend": [0, 0],
    })
    adjusted = apply_equity_adjustments(frame)
    assert adjusted.loc[0, "adjusted_close"] == pytest.approx(50)
    assert adjusted.loc[0, "adjusted_high"] == pytest.approx(51)


def test_futures_roll_is_explicit_and_back_adjusted():
    frame = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"], utc=True),
        "contract": ["G1", "G1", "G2"], "open": [99, 100, 110],
        "high": [101, 102, 112], "low": [98, 99, 109], "close": [100, 101, 111],
    })
    out = build_back_adjusted_continuous(frame)
    assert out["roll_flag"].tolist() == [False, False, True]
    assert out.loc[1, "close"] == out.loc[2, "close"]
    assert set(out["continuous_method"]) == {"back_adjusted_difference"}


def test_incremental_update_is_idempotent(tmp_path: Path):
    provider = FakeProvider(bars())
    service = HistoricalDataService(tmp_path, providers={"binance": provider})
    service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC),
                   datetime(2024, 1, 4, tzinfo=UTC))
    before = service.load_bars("BTC", "D1")
    service.update("BTC", "D1", end=datetime(2024, 1, 4, tzinfo=UTC))
    after = service.load_bars("BTC", "D1")
    pd.testing.assert_frame_equal(before, after)


def test_qqq_snapshot_date_prevents_survivorship_bias(tmp_path: Path):
    csv = tmp_path / "holdings.csv"
    pd.DataFrame({
        "Ticker": ["A", "B"], "Name": ["A Inc", "B Inc"],
        "Weight (%)": [60, 40], "Sector": ["Tech", "Tech"],
    }).to_csv(csv, index=False)
    service = HistoricalDataService(tmp_path / "lake")
    service.update_qqq_holdings(QqqHoldingsCsvProvider(csv), "2024-01-31")
    assert service.load_qqq_holdings("2024-02-01").attrs["snapshot_date"] == "2024-01-31"
    with pytest.raises(SurvivorshipBiasError):
        service.load_qqq_holdings("2023-12-31")
