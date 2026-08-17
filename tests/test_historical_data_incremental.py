from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from inv_trend.data.api import HistoricalDataService
from inv_trend.data.audit import audit_dataset
from inv_trend.data.config import load_instruments
from inv_trend.data.models import DataQualityError, ProviderResult
from inv_trend.data import providers
from inv_trend.data.providers import BinanceKlineProvider, CsvBarsProvider
from inv_trend.data.storage import DataLake

pytest.importorskip("pyarrow")
UTC = timezone.utc


class FakeProvider:
    name = "binance"

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls = []

    def fetch(self, request):
        self.calls.append(request)
        return ProviderResult(self.frame.copy(), request.instrument.source_symbol, self.name, "test license")


class StatefulProvider(FakeProvider):
    """Returns a deficient frame once, then the complete frame on later calls."""

    def __init__(self, complete: pd.DataFrame, first_frame: pd.DataFrame) -> None:
        super().__init__(complete)
        self.complete = complete
        self.first_frame = first_frame

    def fetch(self, request):
        self.calls.append(request)
        if len(self.calls) == 1:
            return ProviderResult(self.first_frame.copy(), request.instrument.source_symbol, self.name, "test license")
        return ProviderResult(self.complete.copy(), request.instrument.source_symbol, self.name, "test license")


def daily_bars(start: str = "2024-01-01", periods: int = 100) -> pd.DataFrame:
    stamps = pd.date_range(start, periods=periods, freq="D", tz="UTC")
    frame = pd.DataFrame({
        "timestamp": stamps,
        "open": range(10, 10 + periods), "high": range(12, 12 + periods),
        "low": range(9, 9 + periods), "close": range(11, 11 + periods),
        "volume": [100] * periods, "is_complete": [True] * periods,
    })
    frame["open"] = frame["open"].astype(float)
    frame["high"] = frame["high"].astype(float)
    frame["low"] = frame["low"].astype(float)
    frame["close"] = frame["close"].astype(float)
    return frame


# ---------------------------------------------------------------- provider adapter

def test_provider_adapter_normalize_symbol_and_fetch_range():
    binance = BinanceKlineProvider()
    assert binance.normalize_symbol(" btcusdt ") == "BTCUSDT"
    csv = CsvBarsProvider("n/a")
    assert csv.normalize_symbol("aapl") == "AAPL"


def test_binance_validate_response_rejects_bad_or_empty_frames():
    binance = BinanceKlineProvider()
    instrument = load_instruments()["BTC"]
    request = providers.DownloadRequest(instrument, "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
    with pytest.raises(ValueError, match="empty"):
        binance.validate_response(pd.DataFrame(), request)
    bad = pd.DataFrame({"open_time": [1], "open": ["x"]})
    with pytest.raises(ValueError, match="columns"):
        binance.validate_response(bad, request)
    good = pd.DataFrame({
        "open_time": [1704067200000], "open": [1], "high": [2], "low": [0.5],
        "close": [1.5], "volume": [10], "close_time": [1704153600000],
    })
    binance.validate_response(good, request)


def test_get_bytes_with_retry_429_then_success(monkeypatch):
    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    from urllib.error import HTTPError

    calls = {"n": 0}

    def fake_urlopen(request, timeout=30):
        calls["n"] += 1
        if calls["n"] == 1:
            raise HTTPError("http://x", 429, "rate limited", {"Retry-After": "0"}, None)
        return FakeResponse(b"ok")

    monkeypatch.setattr(providers, "urlopen", fake_urlopen)
    monkeypatch.setattr(providers.time, "sleep", lambda _: None)
    assert providers.get_bytes_with_retry("http://x", timeout=1, retries=3) == b"ok"
    assert calls["n"] == 2


def test_get_bytes_with_retry_exhausts_on_persistent_5xx(monkeypatch):
    from urllib.error import HTTPError

    calls = {"n": 0}

    def fake_urlopen(request, timeout=30):
        calls["n"] += 1
        raise HTTPError("http://x", 503, "unavailable", {}, None)

    monkeypatch.setattr(providers, "urlopen", fake_urlopen)
    monkeypatch.setattr(providers.time, "sleep", lambda _: None)
    with pytest.raises(HTTPError):
        providers.get_bytes_with_retry("http://x", timeout=1, retries=3)
    assert calls["n"] == 3


# ---------------------------------------------------------------- missing intervals

def test_missing_intervals_cold_full_download(tmp_path: Path):
    service = HistoricalDataService(tmp_path)
    intervals = service.missing_intervals("BTC", "D1", end=datetime(2024, 1, 1, tzinfo=UTC))
    assert len(intervals) == 1
    assert intervals[0]["classification"] == "full"
    assert intervals[0]["start"] == pd.Timestamp("2017-08-17", tz="UTC").to_pydatetime()


def test_missing_intervals_reports_only_tail_when_complete(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(daily_bars(periods=3))})
    service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    intervals = service.missing_intervals("BTC", "D1", end=datetime(2024, 1, 4, tzinfo=UTC))
    assert len(intervals) == 1
    assert intervals[0]["classification"] == "tail"
    assert intervals[0]["start"] == pd.Timestamp("2024-01-01", tz="UTC").to_pydatetime()
    assert intervals[0]["end"] == pd.Timestamp("2024-01-04", tz="UTC").to_pydatetime()


def test_deficient_crypto_ingest_is_quarantined_and_full_interval_returned(tmp_path: Path):
    """One missing crypto day must fail quality and never publish (strict 24x7)."""
    complete = daily_bars()
    deficient = complete.drop(complete.index[45]).reset_index(drop=True)
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(deficient)})
    manifest = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 4, 9, tzinfo=UTC))
    assert not manifest.quality_passed
    assert manifest.quality_status == "QUARANTINED"
    with pytest.raises(DataQualityError):
        service.load_bars("BTC", "D1")
    intervals = service.missing_intervals("BTC", "D1", end=datetime(2024, 4, 9, tzinfo=UTC))
    assert [i["classification"] for i in intervals] == ["full"]


# ---------------------------------------------------------------- integration

def test_update_repairs_deficient_crypto_via_full_refetch(tmp_path: Path):
    complete = daily_bars()
    deficient = complete.drop(complete.index[45]).reset_index(drop=True)
    service = HistoricalDataService(tmp_path, providers={"binance": StatefulProvider(complete, deficient)})
    first = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 4, 9, tzinfo=UTC))
    assert first.quality_status == "QUARANTINED"
    with pytest.raises(DataQualityError):
        service.load_bars("BTC", "D1")
    service.update("BTC", "D1", end=datetime(2024, 4, 9, tzinfo=UTC))
    bars = service.load_bars("BTC", "D1")
    assert len(bars) == 100
    assert service.status("BTC", "D1")["status"] == "PUBLISHED"
    assert service.coverage("BTC", "D1")["missing_intervals"] == []


def test_update_noop_returns_current_manifest(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(daily_bars(periods=3))})
    first = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    manifest = service.update("BTC", "D1", end=datetime(2024, 1, 3, tzinfo=UTC))
    assert manifest.dataset_version == first.dataset_version
    assert service.status("BTC", "D1")["version"] == first.dataset_version


def test_duplicate_ingest_is_idempotent(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(daily_bars(periods=5))})
    first = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC))
    second = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC))
    assert second.dataset_version == first.dataset_version
    assert len(service.load_bars("BTC", "D1")) == 5


def test_short_overlap_replay_keeps_dataset_manifest_and_full_coverage(tmp_path: Path):
    full = daily_bars(periods=100)
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(full)})
    first = service.ingest(
        "BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 4, 10, tzinfo=UTC)
    )
    current_before = service.lake.current_version("BTC", "D1")
    dataset_manifest_path = service.lake._resolve_root_relative(current_before["dataset_manifest_path"])
    dataset_manifest_before = dataset_manifest_path.read_bytes()
    run_count_before = len(list((tmp_path / "manifests").glob("*.json")))

    service.providers["binance"] = FakeProvider(full.tail(11).reset_index(drop=True))
    second = service.ingest(
        "BTC", "D1", datetime(2024, 3, 30, tzinfo=UTC), datetime(2024, 4, 10, tzinfo=UTC)
    )

    current_after = service.lake.current_version("BTC", "D1")
    assert second.dataset_version == first.dataset_version
    assert current_after == current_before
    assert dataset_manifest_path.read_bytes() == dataset_manifest_before
    assert len(list((tmp_path / "manifests").glob("*.json"))) == run_count_before + 1
    audit = audit_dataset(tmp_path, "BTC", "D1")
    assert audit["stored_rows"] == 100
    assert audit["expected_rows"] == 100
    assert audit["missing_rows"] == 0
    assert audit["coverage_ratio"] == 1.0


def test_provider_available_start_is_not_inferred_from_actual_start(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(daily_bars(periods=5))})
    manifest = service.ingest(
        "BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC)
    )
    report = json.loads(
        (tmp_path / "quality_reports" / f"{manifest.dataset_version}.json").read_text(encoding="utf-8")
    )
    assert manifest.provider_available_start == ""
    assert report["provider_available_start"] == ""


def test_reprocess_raw_reproduces_exact_version(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(daily_bars(periods=5))})
    first = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC))
    reproduced = service.reprocess_raw("BTC", "D1", first.run_id)
    assert reproduced.dataset_version == first.dataset_version
    assert len(service.load_bars("BTC", "D1")) == 5
    again = service.reprocess_raw("BTC", "D1", first.run_id)
    assert again.dataset_version == first.dataset_version


def test_catalog_records_full_lineage_fields(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(daily_bars(periods=5))})
    manifest = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC))
    with service.lake._connect() as db:
        row = db.execute(
            "SELECT symbol, timeframe, source, status, version, asset_class, start_time, end_time, "
            "row_count, schema_version, checksum, raw_source FROM datasets WHERE run_id=?",
            (manifest.run_id,),
        ).fetchone()
    assert row is not None
    symbol, timeframe, source, status, version, asset_class, start_time, end_time, row_count, schema_version, checksum, raw_source = row
    assert (symbol, timeframe, source) == ("BTC", "D1", "binance")
    assert status == "CURATED"
    assert version == manifest.dataset_version
    assert asset_class == "crypto"
    assert start_time and end_time
    assert row_count == 5
    assert schema_version == "1.1.2"
    assert checksum
    assert raw_source.endswith(".parquet")


# ---------------------------------------------------------------- failure containment

def test_provider_failure_writes_nothing(tmp_path: Path):
    class FailingProvider:
        name = "binance"

        def fetch(self, request):
            raise RuntimeError("upstream unavailable")

    service = HistoricalDataService(tmp_path, providers={"binance": FailingProvider()})
    with pytest.raises(RuntimeError, match="upstream unavailable"):
        service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC))
    assert service.status("BTC", "D1")["status"] == "MISSING"
    assert not list((tmp_path / "manifests").glob("*.json"))


def test_invalid_schema_never_publishes(tmp_path: Path):
    frame = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01"], utc=True),
        "open": [1], "high": [2], "low": [0.5],
    })
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(frame)})
    with pytest.raises(ValueError, match="missing required columns"):
        service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC))
    assert service.status("BTC", "D1")["status"] == "MISSING"
    assert not list((tmp_path / "curated").rglob("bars.parquet"))


def test_partial_download_is_quarantined_not_published(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(daily_bars(periods=2))})
    manifest = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 11, tzinfo=UTC))
    assert not manifest.quality_passed
    assert manifest.quality_status == "QUARANTINED"
    with pytest.raises(DataQualityError):
        service.load_bars("BTC", "D1")


def test_preprocessing_rejects_rows_without_publishing(tmp_path: Path):
    frame = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01"], utc=True),
        "open": [1], "high": [0.5], "low": [2], "close": [1.5], "volume": [1],
    })
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(frame)})
    manifest = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC))
    assert not manifest.quality_passed
    assert manifest.quality_status == "QUARANTINED"
    assert not list((tmp_path / "curated").rglob("bars.parquet"))
    assert list((tmp_path / "raw").rglob("*.parquet"))
    assert list((tmp_path / "quarantine").rglob("rows.parquet"))


# ---------------------------------------------------------------- storage

def test_write_frame_is_atomic_no_partial_files(tmp_path: Path):
    lake = DataLake(tmp_path)
    relative = Path("raw") / "test" / "broken.parquet"
    frame = pd.DataFrame({"timestamp": [pd.Timestamp("2024-01-01", tz="UTC")], "bad": [complex(1, 2)]})
    with pytest.raises(Exception):
        lake.write_frame(frame, relative)
    assert not (tmp_path / relative).exists()
    assert not list((tmp_path / relative).parent.glob("*.tmp"))


def test_parquet_uses_zstd_compression(tmp_path: Path):
    import pyarrow.parquet as pq

    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(daily_bars(periods=5))})
    service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC))
    current = service.lake.current_version("BTC", "D1")
    parquet_file = pq.ParquetFile(service.lake._resolve_root_relative(current["path"]))
    compression = parquet_file.metadata.row_group(0).column(0).compression
    assert str(compression) == "ZSTD"


def test_catalog_migration_preserves_existing_rows(tmp_path: Path):
    lake = DataLake(tmp_path)
    with lake._connect() as db:
        db.execute(
            "INSERT OR REPLACE INTO datasets(run_id, symbol, instrument_id, timeframe, source, "
            "manifest_path, status, version, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("old", "BTC", "BTCUSDT.BINANCE.SPOT", "D1", "binance", "p", "CURATED", "v1", "t"),
        )
    reopened = DataLake(tmp_path)
    with reopened._connect() as db:
        row = db.execute("SELECT run_id, asset_class, row_count FROM datasets WHERE run_id='old'").fetchone()
    assert row[0] == "old"
    assert row[1] == ""
    assert row[2] == 0
