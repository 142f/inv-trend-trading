from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from historical_data.api import HistoricalDataService
from historical_data.audit import audit_dataset
from historical_data.calendar import is_nyse_trading_day
from historical_data.config import load_instruments
from historical_data.models import (
    DataLineageError, DataQualityError, DatasetManifest, InstrumentNotImplementedError,
    ProviderResult, SurvivorshipBiasError, path_text, utc_now,
)
from historical_data.providers import CsvBarsProvider
from historical_data.processing import (
    apply_equity_adjustments,
    build_back_adjusted_continuous,
    normalize_bars,
    resample_ohlcv_session,
)
from historical_data.providers import QqqHoldingsCsvProvider
from historical_data.legacy import migrate_legacy_csv
from historical_data.storage import sha256_file
from historical_data.storage import DataLake

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


def equity_bars(start: str, sessions: int) -> pd.DataFrame:
    candidates = pd.date_range(start, periods=sessions * 2, freq="D", tz="UTC")
    stamps = pd.DatetimeIndex([stamp for stamp in candidates if is_nyse_trading_day(stamp)])[:sessions]
    values = pd.Series(range(100, 100 + sessions), dtype=float)
    return pd.DataFrame({
        "timestamp": stamps, "open": values, "high": values + 2, "low": values - 1,
        "close": values + 1, "adjusted_close": values + 1, "volume": [1000] * sessions,
        "is_complete": [True] * sessions,
    })


def test_ingest_manifest_parquet_hash_and_detector_compatible(tmp_path: Path):
    provider = FakeProvider(bars())
    service = HistoricalDataService(tmp_path, providers={"binance": provider})
    manifest = service.ingest(
        "BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC)
    )
    assert manifest.actual_symbol == "BTCUSDT"
    assert all(sha256_file(service.lake._resolve_root_relative(path)) == digest for path, digest in manifest.file_hashes.items())
    loaded = service.load_bars("BTC", "D1")
    assert loaded["timestamp"].is_monotonic_increasing
    assert loaded["timestamp"].is_unique
    assert len(loaded) == 2
    assert {"open", "high", "low", "close", "volume"}.issubset(loaded)
    assert loaded.attrs["data_sources"] == ["binance"]


def test_requested_start_is_clamped_to_listing_for_provider_call(tmp_path: Path):
    """The provider request is clamped to earliest_valid_date, never before it."""
    provider = FakeProvider(bars())
    service = HistoricalDataService(tmp_path, providers={"binance": provider})
    service.ingest("BTC", "D1", datetime(2000, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    assert provider.calls[0].start.year == 2017
    # Requesting before verified history with data only from 2024 is a head
    # truncation: it must fail closed instead of being graded 100.
    deficient = service.ingest("BTC", "D1", datetime(2000, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    assert deficient.quality_status == "QUARANTINED"


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
    # Same timestamp with a different volume is a conflict, never a silent overwrite.
    assert "conflicting_duplicate" in reasons


def test_exact_duplicate_bars_are_deduped_with_audit():
    frame = bars().iloc[:2].copy()
    exact = frame.copy()
    result = normalize_bars(pd.concat([frame, exact]), load_instruments()["BTC"], "D1", "x")
    assert result.clean["timestamp"].nunique() == len(result.clean)
    reasons = ";".join(result.quarantine["quarantine_reason"])
    assert "duplicate_superseded" in reasons
    assert "conflicting_duplicate" not in reasons


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


def test_short_overlap_run_keeps_dataset_manifest_and_full_coverage(tmp_path: Path):
    stamps = pd.date_range("2024-01-01", periods=100, freq="D", tz="UTC")
    full = pd.DataFrame({
        "timestamp": stamps, "open": range(10, 110), "high": range(12, 112),
        "low": range(9, 109), "close": range(11, 111), "volume": [100] * 100,
        "is_complete": [True] * 100,
    })
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(full)})
    first = service.ingest(
        "BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 4, 10, tzinfo=UTC)
    )
    current_before = service.lake.current_version("BTC", "D1")
    dataset_manifest_path = service.lake._resolve_root_relative(current_before["dataset_manifest_path"])
    dataset_manifest_before = dataset_manifest_path.read_bytes()
    run_count = len(list((tmp_path / "manifests").glob("*.json")))

    service.providers["binance"] = FakeProvider(full.tail(11).reset_index(drop=True))
    second = service.ingest(
        "BTC", "D1", datetime(2024, 3, 30, tzinfo=UTC), datetime(2024, 4, 10, tzinfo=UTC)
    )

    assert second.dataset_version == first.dataset_version
    assert service.lake.current_version("BTC", "D1") == current_before
    assert dataset_manifest_path.read_bytes() == dataset_manifest_before
    assert len(list((tmp_path / "manifests").glob("*.json"))) == run_count + 1
    audit = audit_dataset(tmp_path, "BTC", "D1")
    assert (audit["stored_rows"], audit["expected_rows"], audit["missing_rows"]) == (100, 100, 0)
    assert audit["coverage_ratio"] == 1.0


def test_equity_d1_provider_gap_is_zero_tolerance(tmp_path: Path):
    full = equity_bars("2024-01-02", 100)
    deficient = full.drop(index=50).reset_index(drop=True)
    service = HistoricalDataService(tmp_path, providers={"yahoo_chart": FakeProvider(deficient)})
    manifest = service.ingest(
        "AAPL", "D1", full["timestamp"].min().to_pydatetime(),
        (full["timestamp"].max() + pd.Timedelta(days=1)).to_pydatetime(),
    )
    report = json.loads(
        (tmp_path / "ingestion_reports" / f"{manifest.run_id}.json").read_text(encoding="utf-8")
    )
    assert report["expected_rows"] == 100
    assert report["actual_bars"] == 99
    assert report["gap_provider"] == 1
    assert report["gap_unknown"] == 0
    assert report["coverage_ratio"] == 0.99
    assert report["quality_score"] < 100
    assert report["quality_status"] == "QUARANTINED"
    assert report["backtest_suitable"] is False
    assert service.status("AAPL", "D1")["status"] == "MISSING"


def test_equity_d1_weekends_holidays_and_pre_listing_are_not_gaps(tmp_path: Path):
    full = equity_bars("2024-01-02", 100)
    service = HistoricalDataService(tmp_path / "calendar", providers={"yahoo_chart": FakeProvider(full)})
    manifest = service.ingest(
        "AAPL", "D1", full["timestamp"].min().to_pydatetime(),
        (full["timestamp"].max() + pd.Timedelta(days=1)).to_pydatetime(),
    )
    report = json.loads(
        (tmp_path / "calendar" / "ingestion_reports" / f"{manifest.run_id}.json").read_text(encoding="utf-8")
    )
    assert report["gap_weekend"] > 0
    assert report["gap_holiday"] > 0
    assert report["gap_provider"] == 0
    assert report["quality_status"] == "CURATED"
    assert report["backtest_suitable"] is True

    listed = equity_bars("2012-05-18", 20)
    listing_service = HistoricalDataService(
        tmp_path / "listing", providers={"yahoo_chart": FakeProvider(listed)}
    )
    listing_manifest = listing_service.ingest(
        "META", "D1", datetime(2006, 1, 1, tzinfo=UTC),
        (listed["timestamp"].max() + pd.Timedelta(days=1)).to_pydatetime(),
    )
    listing_report = json.loads(
        (tmp_path / "listing" / "ingestion_reports" / f"{listing_manifest.run_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert listing_report["gap_provider"] == 0
    assert listing_report["quality_status"] == "CURATED"


def test_equity_d1_failed_increment_keeps_old_current(tmp_path: Path):
    full = equity_bars("2024-01-02", 110)
    service = HistoricalDataService(tmp_path, providers={"yahoo_chart": FakeProvider(full.iloc[:100])})
    first = service.ingest(
        "AAPL", "D1", full["timestamp"].iloc[0].to_pydatetime(),
        (full["timestamp"].iloc[99] + pd.Timedelta(days=1)).to_pydatetime(),
    )
    current_before = service.lake.current_version("AAPL", "D1")
    incoming = full.iloc[90:].drop(index=105).reset_index(drop=True)
    service.providers["yahoo_chart"] = FakeProvider(incoming)
    failed = service.ingest(
        "AAPL", "D1", full["timestamp"].iloc[90].to_pydatetime(),
        (full["timestamp"].iloc[109] + pd.Timedelta(days=1)).to_pydatetime(),
    )
    report = json.loads(
        (tmp_path / "ingestion_reports" / f"{failed.run_id}.json").read_text(encoding="utf-8")
    )
    assert failed.quality_status == "QUARANTINED"
    assert report["gap_provider"] == 1
    assert report["backtest_suitable"] is False
    assert service.lake.current_version("AAPL", "D1") == current_before
    assert service.status("AAPL", "D1")["version"] == first.dataset_version


def test_equity_gap_audit_independently_matches_quality_report(tmp_path: Path):
    full = equity_bars("2024-01-02", 100)
    deficient = full.drop(index=50).reset_index(drop=True)
    service = HistoricalDataService(tmp_path, providers={"yahoo_chart": FakeProvider(deficient)})
    run = service.ingest(
        "AAPL", "D1", full["timestamp"].min().to_pydatetime(),
        (full["timestamp"].max() + pd.Timedelta(days=1)).to_pydatetime(),
    )
    run_report = json.loads(
        (tmp_path / "ingestion_reports" / f"{run.run_id}.json").read_text(encoding="utf-8")
    )
    normalized_path = next(
        service.lake._resolve_root_relative(path)
        for path in run.file_paths if path.startswith("normalized/")
    )
    frame = pd.read_parquet(normalized_path)
    version = "audit-gap-candidate"
    frame["dataset_version"] = version
    frame["curated_version"] = version
    curated = service.lake.publish_curated(
        frame, symbol="AAPL", instrument_id=service.instruments["AAPL"].instrument_id,
        asset_class="equity", timeframe="D1", version=version, run_id=run.run_id,
        activate=False,
    )
    quality = service.lake.write_json(
        run_report | {"dataset_version": version},
        Path("quality_reports") / f"{version}.json",
    )
    dataset_manifest = DatasetManifest(
        dataset_version=version, parent_dataset_version=None, publication_run_id=run.run_id,
        symbol="AAPL", instrument_id=service.instruments["AAPL"].instrument_id,
        timeframe="D1", full_actual_start=frame["timestamp"].min().isoformat(),
        full_actual_end=frame["timestamp"].max().isoformat(), row_count=len(frame),
        curated_path=path_text(curated, tmp_path), curated_sha256=sha256_file(curated),
        quality_report_path=path_text(quality, tmp_path), quality_report_sha256=sha256_file(quality),
        published_at=utc_now().isoformat(), cleaning_rule_version="1.1.2",
    )
    dataset_manifest_path = service.lake.write_json(
        dataset_manifest.to_dict(), Path("dataset_manifests") / f"{version}.json"
    )
    service.lake.activate_curated(
        "AAPL", "D1", version, run.run_id, curated,
        dataset_manifest_path=dataset_manifest_path,
    )

    audit = audit_dataset(tmp_path, "AAPL", "D1")
    assert run_report["gap_provider"] == audit["gap_provider"] == 1
    assert run_report["gap_unknown"] == audit["gap_unknown"] == 0
    assert audit["lineage"]["report_gap_provider"] == audit["lineage"]["recomputed_gap_provider"]
    assert audit["backtest_suitable"] is False


def test_outside_overlap_revision_requires_review_and_keeps_current_version(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(bars())})
    config = service.instruments["BTC"]
    service.instruments["BTC"] = config.__class__(**{**config.__dict__, "revision_overlap_bars": 1})
    first = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    revised = bars()
    revised.loc[0, "close"] = 10
    service.providers["binance"] = FakeProvider(revised)
    second = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    assert second.quality_status == "REVIEW_REQUIRED"
    assert service.status("BTC", "D1")["version"] == first.dataset_version


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


def test_research_data_requires_explicit_opt_in(tmp_path: Path):
    class ResearchProvider(FakeProvider):
        name = "yahoo_chart"

        def fetch(self, request):
            result = super().fetch(request)
            result.source = self.name
            result.metadata = {"research_only": True}
            return result

    service = HistoricalDataService(tmp_path, providers={"yahoo_chart": ResearchProvider(bars())})
    service.ingest("QQQ", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    with pytest.raises(DataQualityError):
        service.load_bars("QQQ", "D1")
    assert len(service.load_bars("QQQ", "D1", allow_research=True)) == 2


def test_csv_provider_filters_requested_range(tmp_path: Path):
    csv = tmp_path / "xau.csv"
    pd.DataFrame({
        "timestamp": ["2024-01-01", "2024-01-02", "2024-01-03"],
        "open": [1, 2, 3], "high": [2, 3, 4], "low": [0.5, 1.5, 2.5], "close": [1.5, 2.5, 3.5],
    }).to_csv(csv, index=False)
    service = HistoricalDataService(tmp_path / "lake", providers={"licensed_csv": CsvBarsProvider(csv)})
    base = service.instruments["XAU"]
    service.instruments["XAU"] = base.__class__(**{**base.__dict__, "primary_source": "licensed_csv"})
    manifest = service.ingest("XAU", "D1", datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC))
    assert manifest.row_count == 1


def test_reserved_instrument_raises_explicit_error(tmp_path: Path):
    with pytest.raises(InstrumentNotImplementedError):
        HistoricalDataService(tmp_path).load_bars("600519", "D1")


def test_h4_resample_uses_new_york_1700_boundary():
    frame = pd.DataFrame({
        # 22:00 UTC is 17:00 in New York during standard time: an OTC session boundary.
        "timestamp": pd.date_range("2024-01-01 22:00", periods=6, freq="h", tz="UTC"),
        "open": [1, 2, 3, 4, 5, 6], "high": [2, 3, 4, 5, 6, 7],
        "low": [0, 1, 2, 3, 4, 5], "close": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5], "volume": [1] * 6,
    })
    out = resample_ohlcv_session(frame, "H4", session_timezone="America/New_York")
    assert len(out) == 2
    assert out.iloc[0]["open"] == 1
    assert out.iloc[0]["close"] == 4.5


def test_coverage_uses_dataset_version_quality_report(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(bars())})
    manifest = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    coverage = service.coverage("BTC", "D1")
    assert coverage["quality_report"].endswith(f"{manifest.dataset_version}.json")


def test_review_approval_publishes_readable_curated_version(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(bars())})
    config = service.instruments["BTC"]
    service.instruments["BTC"] = config.__class__(**{**config.__dict__, "revision_overlap_bars": 1})
    service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    revised = bars()
    revised.loc[0, "close"] = 10
    service.providers["binance"] = FakeProvider(revised)
    review = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    version = service.approve_review(review.run_id, "verified", actor="test")
    assert service.status("BTC", "D1")["version"] == version
    assert len(service.load_bars("BTC", "D1")) == 2
    assert any(item["decision"] == "approved" for item in service.lake.list_reviews())
    assert service.approve_review(review.run_id, "verified", actor="test") == version
    with pytest.raises(DataLineageError):
        service.approve_review(review.run_id, "different reason", actor="test")
    approval = json.loads((tmp_path / "manifests" / f"approval-{version}.json").read_text(encoding="utf-8"))
    assert set(approval["file_paths"]) == set(approval["file_hashes"])
    assert all(sha256_file(service.lake._resolve_root_relative(path)) == digest for path, digest in approval["file_hashes"].items())
    assert approval["approved_curated_path"]
    assert approval["approved_curated_sha256"]
    assert approval["approved_quality_report_path"]
    assert approval["candidate_data_path"]
    assert approval["candidate_manifest_path"]
    assert approval["review_record_path"]
    assert approval["review_record_sha256"]


def test_coverage_fails_closed_when_quality_report_is_missing(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(bars())})
    manifest = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    (tmp_path / "quality_reports" / f"{manifest.dataset_version}.json").unlink()
    with pytest.raises(DataLineageError):
        service.coverage("BTC", "D1")


def test_catalog_backend_conflict_fails_closed(tmp_path: Path):
    DataLake(tmp_path)
    active = next(tmp_path.glob("catalog.*"))
    alternate = tmp_path / ("catalog.duckdb" if active.name == "catalog.sqlite3" else "catalog.sqlite3")
    alternate.write_bytes(b"conflict")
    with pytest.raises(DataLineageError):
        DataLake(tmp_path)


def test_legacy_migration_is_idempotent(tmp_path: Path):
    source = tmp_path / "processed" / "cleaned"
    source.mkdir(parents=True)
    pd.DataFrame({"symbol": ["OLD"], "timeframe": ["D1"], "timestamp": ["2024-01-01"],
                  "open": [1], "high": [2], "low": [0.5], "close": [1.5]}).to_csv(source / "old.csv", index=False)
    first = migrate_legacy_csv(tmp_path / "processed", tmp_path / "lake")
    second = migrate_legacy_csv(tmp_path / "processed", tmp_path / "lake")
    assert first["migrated"][0]["status"] == "migrated"
    assert second["migrated"][0]["status"] == "already_migrated"
    assert len(HistoricalDataService(tmp_path / "lake").load_bars("OLD", "D1", allow_legacy=True, min_quality_score=10)) == 1
