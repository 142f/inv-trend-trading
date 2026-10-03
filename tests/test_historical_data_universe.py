"""Universe, identity, calendar and conflict boundary tests for the D1 layer."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import pandas as pd
import pytest

from inv_trend.data.api import HistoricalDataService
from inv_trend.data.calendar import is_nyse_trading_day
from inv_trend.data.config import load_instruments
from inv_trend.data.models import DataConflictError, DataQualityError, ProviderResult
from inv_trend.data.integrity import sha256_file
from inv_trend.data.storage import DataLake

pytest.importorskip("pyarrow")
UTC = timezone.utc

STOCK_BASKET = ("NVDA", "MSFT", "GOOGL", "AMZN", "META", "AVGO", "TSM", "AMD", "AAPL", "ORCL")


class FakeProvider:
    name = "binance"

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame
        self.calls = []

    def fetch(self, request):
        self.calls.append(request)
        return ProviderResult(self.frame.copy(), request.instrument.source_symbol, self.name, "test license")


def stock_bars(start: str = "2024-01-01", periods: int = 30) -> pd.DataFrame:
    """Business-day bars for equity fixtures (weekdays only, no holidays)."""
    stamps = pd.bdate_range(start, periods=periods, tz="UTC")
    frame = pd.DataFrame({
        "timestamp": stamps, "open": range(100, 100 + periods), "high": range(102, 102 + periods),
        "low": range(99, 99 + periods), "close": range(101, 101 + periods),
        "volume": [1000] * periods, "is_complete": [True] * periods,
        "adjusted_close": range(101, 101 + periods),
    })
    for c in ("open", "high", "low", "close", "adjusted_close"):
        frame[c] = frame[c].astype(float)
    return frame


def bars() -> pd.DataFrame:
    frame = pd.DataFrame({
        "timestamp": pd.to_datetime(["2024-01-01", "2024-01-02", "2024-01-03"], utc=True),
        "open": [10, 11, 12], "high": [12, 13, 14], "low": [9, 10, 11],
        "close": [11, 12, 13], "volume": [100, 100, 100],
        "is_complete": [True, True, False],
    })
    for c in ("open", "high", "low", "close"):
        frame[c] = frame[c].astype(float)
    return frame


# ---------------------------------------------------------------- identity

def test_stock_basket_instrument_identity():
    instruments = load_instruments()
    expected = {
        "NVDA": ("NVDA.XNAS.EQUITY", "xnas"), "MSFT": ("MSFT.XNAS.EQUITY", "xnas"),
        "GOOGL": ("GOOGL.XNAS.EQUITY", "xnas"), "AMZN": ("AMZN.XNAS.EQUITY", "xnas"),
        "META": ("META.XNAS.EQUITY", "xnas"), "AVGO": ("AVGO.XNAS.EQUITY", "xnas"),
        "TSM": ("TSM.XNYS.EQUITY", "xnys"), "AMD": ("AMD.XNAS.EQUITY", "xnas"),
        "AAPL": ("AAPL.XNAS.EQUITY", "xnas"), "ORCL": ("ORCL.XNYS.EQUITY", "xnys"),
    }
    for symbol, (instrument_id, venue) in expected.items():
        config = instruments[symbol]
        assert config.instrument_id == instrument_id, f"{symbol} instrument identity"
        assert config.market == venue and config.venue == venue
        assert config.instrument_type == "equity"
        assert config.base_asset == symbol and config.quote_asset == "USD"
        assert config.earliest_valid_date, f"{symbol} requires earliest_valid_date"


def test_qqq_is_etf_and_never_in_stock_basket():
    instruments = load_instruments()
    assert instruments["QQQ"].instrument_id == "QQQ.XNAS.ETF"
    assert instruments["QQQ"].instrument_type == "etf"
    assert "QQQ" not in STOCK_BASKET


def test_xau_xag_identity_is_dukascopy_bid_cfd():
    instruments = load_instruments()
    xau = instruments["XAU"]
    assert xau.instrument_id == "XAUUSD.DUKAS.BID.CFD"
    assert xau.instrument_type == "cfd"
    assert xau.price_basis == "bid"
    assert xau.market == "dukascopy_otc" and xau.venue == "dukascopy_otc"
    xag = instruments["XAG"]
    assert xag.instrument_id == "XAGUSD.DUKAS.BID.CFD"
    assert xag.price_basis == "bid"


def test_xau_session_definition_is_provider_native_utc():
    """Registry session must match the Dukascopy dayStartTime=UTC request.

    Option A: Dukascopy provider-native D1 (UTC day start).  The registry
    declares session_timezone=UTC and bar_close_rule=provider_native_utc;
    a ny_1700 declaration would contradict the actual provider semantics.
    """
    from inv_trend.data.providers import DukascopyBarsProvider
    instruments = load_instruments()
    for symbol in ("XAU", "XAG"):
        config = instruments[symbol]
        assert config.session_timezone == "UTC"
        assert config.bar_close_rule == "provider_native_utc"
    # The provider request itself is always dayStartTime=UTC.
    calls = {}

    def fake_get(url, *, timeout, retries=3, headers=None):
        calls["url"] = url
        return json.dumps({"data": [{"time": 1704067200000, "open": 1, "high": 2, "low": 0.5,
                                     "close": 1.5, "volume": 10}]}).encode()

    from inv_trend.data import providers as providers_module
    from inv_trend.data.models import DownloadRequest

    provider = DukascopyBarsProvider(instrument_ids={"XAUUSD_DUKAS": 1})
    original = providers_module.get_bytes_with_retry
    providers_module.get_bytes_with_retry = fake_get
    try:
        request = DownloadRequest(instruments["XAU"], "D1",
                                  pd.Timestamp("2024-01-01", tz="UTC").to_pydatetime(),
                                  pd.Timestamp("2024-01-02", tz="UTC").to_pydatetime())
        provider.fetch(request)
    finally:
        providers_module.get_bytes_with_retry = original
    assert "dayStartTime=UTC" in calls["url"]


def test_crypto_identity():
    instruments = load_instruments()
    btc = instruments["BTC"]
    assert btc.instrument_id == "BTCUSDT.BINANCE.SPOT"
    assert btc.venue == "binance" and btc.base_asset == "BTC" and btc.quote_asset == "USDT"
    eth = instruments["ETH"]
    assert eth.instrument_id == "ETHUSDT.BINANCE.SPOT"


# ---------------------------------------------------------------- calendar

def test_nyse_holiday_rules():
    assert not is_nyse_trading_day(pd.Timestamp("2024-01-01"))
    assert not is_nyse_trading_day(pd.Timestamp("2024-01-15"))   # MLK
    assert not is_nyse_trading_day(pd.Timestamp("2024-03-29"))   # Good Friday
    assert not is_nyse_trading_day(pd.Timestamp("2024-07-04"))
    assert not is_nyse_trading_day(pd.Timestamp("2024-11-28"))   # Thanksgiving
    assert not is_nyse_trading_day(pd.Timestamp("2024-12-25"))
    assert is_nyse_trading_day(pd.Timestamp("2024-07-03"))
    assert not is_nyse_trading_day(pd.Timestamp("2024-01-06"))   # Saturday
    assert not is_nyse_trading_day(pd.Timestamp("2024-01-07"))   # Sunday


def test_weekend_and_holiday_are_not_provider_gaps(tmp_path: Path):
    """Missing weekend/holiday stamps must not count as missing bars."""
    service = HistoricalDataService(tmp_path, providers={"yahoo_chart": FakeProvider(stock_bars())})
    config = service.instruments["NVDA"]
    service.instruments["NVDA"] = config.__class__(**{**config.__dict__, "primary_source": "yahoo_chart"})
    manifest = service.ingest("NVDA", "D1", datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 2, 2, tzinfo=UTC))
    assert manifest.quality_status == "CURATED"
    assert manifest.quality_score == 100.0
    assert service.coverage("NVDA", "D1")["missing_intervals"] == []


def test_real_trading_day_missing_is_provider_gap(tmp_path: Path):
    """A missing mid-week NYSE trading day must be classified as a provider gap."""
    full = stock_bars("2024-01-02", periods=20)
    deficient = full.drop(full.index[5]).reset_index(drop=True)  # drop a Wednesday
    service = HistoricalDataService(tmp_path, providers={"yahoo_chart": FakeProvider(deficient)})
    config = service.instruments["NVDA"]
    service.instruments["NVDA"] = config.__class__(**{**config.__dict__, "primary_source": "yahoo_chart"})
    manifest = service.ingest("NVDA", "D1", datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 1, 31, tzinfo=UTC))
    assert not manifest.quality_passed
    assert manifest.quality_status == "QUARANTINED"
    assert manifest.quality_score < 100  # a real trading day is missing


def test_pre_listing_history_is_not_a_gap(tmp_path: Path):
    """META requested before its 2012 listing must not create missing bars."""
    listed = stock_bars("2012-05-18", periods=10)  # provider only has post-listing data
    service = HistoricalDataService(tmp_path, providers={"yahoo_chart": FakeProvider(listed)})
    config = service.instruments["META"]
    service.instruments["META"] = config.__class__(**{**config.__dict__, "primary_source": "yahoo_chart"})
    manifest = service.ingest("META", "D1", datetime(2006, 1, 1, tzinfo=UTC), datetime(2012, 6, 1, tzinfo=UTC))
    assert manifest.quality_status == "CURATED"
    assert manifest.quality_score >= 50
    report = manifest.to_dict()
    # The request was clamped to the verified listing date (2012-05-18):
    # requesting pre-listing history must never count those days as missing.
    assert report["requested_start"].startswith("2012-05-18")
    assert report["actual_start"].startswith("2012-05-18")


def test_head_truncation_20y_request_10y_data_fails_closed(tmp_path: Path):
    """Case 1: 20-year request, provider returns only 10 years.

    expected_start = max(requested=2006, listing=AAPL 1980) = 2006;
    the 2006->2016 trading days are real missing bars, so the dataset
    must never be CURATED.
    """
    listed = stock_bars("2016-08-08", periods=40)
    service = HistoricalDataService(tmp_path, providers={"yahoo_chart": FakeProvider(listed)})
    config = service.instruments["AAPL"]
    service.instruments["AAPL"] = config.__class__(**{**config.__dict__, "primary_source": "yahoo_chart"})
    manifest = service.ingest("AAPL", "D1", datetime(2006, 8, 8, tzinfo=UTC), datetime(2026, 8, 8, tzinfo=UTC))
    assert not manifest.quality_passed
    assert manifest.quality_status == "QUARANTINED"
    assert manifest.quality_score < 100
    report = manifest.to_dict()
    assert report["requested_start"].startswith("2006-08-08")
    assert report["actual_start"].startswith("2016-08-08")
    # Missing rows must be counted (quality report records the gap).
    q = json.loads((tmp_path / "ingestion_reports" / f"{manifest.run_id}.json").read_text(encoding="utf-8"))
    assert q["missing_count"] > 0
    assert q["backtest_suitable"] is False


def test_head_truncation_listing_too_recent_is_not_a_gap(tmp_path: Path):
    """Case 2: META listed 2012, requested 2006; pre-listing time is not missing."""
    listed = stock_bars("2012-05-18", periods=10)
    service = HistoricalDataService(tmp_path, providers={"yahoo_chart": FakeProvider(listed)})
    config = service.instruments["META"]
    service.instruments["META"] = config.__class__(**{**config.__dict__, "primary_source": "yahoo_chart"})
    manifest = service.ingest("META", "D1", datetime(2006, 1, 1, tzinfo=UTC), datetime(2012, 6, 1, tzinfo=UTC))
    assert manifest.quality_status == "CURATED"
    assert manifest.quality_score == 100.0


def test_head_truncation_missing_years_after_listing_is_provider_gap(tmp_path: Path):
    """Case 3: listing 2012, actual data starts 2014; 2012->2014 is a provider gap."""
    listed = stock_bars("2014-01-02", periods=20)
    service = HistoricalDataService(tmp_path, providers={"yahoo_chart": FakeProvider(listed)})
    config = service.instruments["META"]
    service.instruments["META"] = config.__class__(**{**config.__dict__, "primary_source": "yahoo_chart"})
    manifest = service.ingest("META", "D1", datetime(2006, 1, 1, tzinfo=UTC), datetime(2014, 2, 1, tzinfo=UTC))
    assert not manifest.quality_passed
    assert manifest.quality_status == "QUARANTINED"
    report = manifest.to_dict()
    assert report["actual_start"].startswith("2014-01-02")
    q = json.loads((tmp_path / "ingestion_reports" / f"{manifest.run_id}.json").read_text(encoding="utf-8"))
    assert q["missing_count"] > 0
    classifications = {interval["classification"] for interval in q["missing_intervals"]}
    assert classifications == {"provider_gap"}


# ---------------------------------------------------------------- crypto strictness

def test_partial_d1_bar_is_excluded_from_curated(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(bars())})
    manifest = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    assert manifest.quality_status == "CURATED"
    current = service.lake.current_version("BTC", "D1")
    stored = pd.read_parquet(service.lake._resolve_root_relative(current["path"]))
    assert (stored["is_complete"].astype(bool)).all(), "curated must only contain complete bars"
    loaded = service.load_bars("BTC", "D1")
    assert len(loaded) == 2
    assert loaded["timestamp"].max() < pd.Timestamp("2024-01-03", tz="UTC")
    quality = service.coverage("BTC", "D1")
    report_path = Path(quality["quality_report"])
    import json
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["actual_bars"] == 2
    assert report["stored_row_count"] == 2
    assert report["complete_row_count"] == 2
    assert report["incomplete_row_count"] == 0
    assert report["actual_end"].startswith("2024-01-02")
    assert report["latest_complete_bar"].startswith("2024-01-02")
    assert report["coverage_ratio"] == 1.0
    assert report["backtest_suitable"] is True


def test_crypto_continuous_24x7_required(tmp_path: Path):
    complete = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC"),
        "open": range(10, 15), "high": range(12, 17), "low": range(9, 14),
        "close": range(11, 16), "volume": [100] * 5, "is_complete": [True] * 5,
    })
    deficient = complete.drop(complete.index[2]).reset_index(drop=True)
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(deficient)})
    manifest = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC))
    assert not manifest.quality_passed
    assert manifest.quality_status == "QUARANTINED"


def test_all_partial_bars_never_publish_curated(tmp_path: Path):
    """When every bar is incomplete the dataset must not fall back to publishing."""
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-01", periods=3, freq="D", tz="UTC"),
        "open": [10, 11, 12], "high": [12, 13, 14], "low": [9, 10, 11],
        "close": [11, 12, 13], "volume": [100] * 3, "is_complete": [False, False, False],
    })
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(frame)})
    manifest = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    assert not manifest.quality_passed
    assert manifest.quality_status == "QUARANTINED"
    assert service.status("BTC", "D1")["status"] == "MISSING"
    assert not list((tmp_path / "curated").rglob("bars.parquet"))


def test_approve_incoming_resolves_conflict_with_incoming_values(tmp_path: Path):
    frame = bars().iloc[:2].copy()
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(frame.copy())})
    service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))
    conflicting = frame.copy()
    conflicting.loc[0, "close"] = 11.5
    service.providers["binance"] = FakeProvider(conflicting)
    review = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))
    assert review.quality_status == "REVIEW_REQUIRED"
    version = service.approve_review(review.run_id, "incoming wins", actor="test", decision="approve_incoming")
    loaded = service.load_bars("BTC", "D1")
    assert float(loaded.loc[0, "close"]) == 11.5
    assert service.status("BTC", "D1")["version"] == version


# ---------------------------------------------------------------- duplicates

def test_exact_duplicate_bars_dedup_conflicting_review(tmp_path: Path):
    frame = bars().iloc[:2].copy()
    exact = frame.copy()
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(exact)})
    first = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))
    conflicting = frame.copy()
    conflicting.loc[0, "close"] = 11.5  # valid OHLC but differs from published close=11
    service.providers["binance"] = FakeProvider(conflicting)
    second = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))
    assert second.quality_status == "REVIEW_REQUIRED"
    assert not second.quality_passed
    # The previous published version stays current: fail-safe, never silently overwritten.
    assert service.status("BTC", "D1")["version"] == first.dataset_version
    loaded = service.load_bars("BTC", "D1")
    assert loaded.loc[0, "close"] == 11.0
    candidate_dirs = list((tmp_path / "reviews").glob("candidate=*/bars.parquet"))
    assert candidate_dirs
    # The review candidate must preserve the incoming conflicting value too.
    incoming = pd.read_parquet(candidate_dirs[0].parent / "incoming.parquet")
    assert not incoming.empty
    assert float(incoming.iloc[0]["close"]) == 11.5
    conflicts_json = (candidate_dirs[0].parent / "conflicts.json").read_text(encoding="utf-8")
    assert "2024-01-01" in conflicts_json
    assert (candidate_dirs[0].parent / "candidate_manifest.json").exists()


def test_repository_raises_on_conflicting_duplicate_in_curated(tmp_path: Path):
    lake = DataLake(tmp_path)
    frame = bars().iloc[:2].copy()
    frame["symbol"] = "BTC"
    frame["dataset_version"] = "v-conflict"
    frame["quality_status"] = "CURATED"
    frame["quality_score"] = 100.0
    dup = frame.copy()
    dup.loc[0, "close"] = 99.0
    frame = pd.concat([frame, dup], ignore_index=True)
    lake.publish_curated(frame, symbol="BTC", instrument_id="BTCUSDT.BINANCE.SPOT",
                         asset_class="crypto", timeframe="D1", version="v-conflict", run_id="r1")
    service = HistoricalDataService(tmp_path)
    with pytest.raises(DataConflictError):
        service.load_bars("BTC", "D1")


# ---------------------------------------------------------------- lineage

def test_full_lineage_hash_chain_for_current(tmp_path: Path):
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(bars())})
    manifest = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC))
    payload = manifest.to_dict()
    assert all(service.lake._resolve_root_relative(path).exists() for path in payload["file_paths"])
    assert all(sha256_file(service.lake._resolve_root_relative(path)) == digest for path, digest in payload["file_hashes"].items())
    raw = next(p for p in payload["file_paths"] if "/raw/" in p or p.startswith("raw/"))
    normalized = [p for p in payload["file_paths"] if "/normalized/" in p or p.startswith("normalized/")]
    assert raw and normalized
    current = service.lake.current_version("BTC", "D1")
    assert current["version"] == manifest.dataset_version
    dataset_manifest_path = service.lake._resolve_root_relative(current["dataset_manifest_path"])
    dataset_manifest = json.loads(dataset_manifest_path.read_text(encoding="utf-8"))
    assert dataset_manifest["dataset_version"] == manifest.dataset_version
    assert dataset_manifest["publication_run_id"] == manifest.run_id
    assert dataset_manifest["row_count"] == 2
    assert sha256_file(service.lake._resolve_root_relative(dataset_manifest["curated_path"])) == dataset_manifest["curated_sha256"]
    assert sha256_file(service.lake._resolve_root_relative(dataset_manifest["quality_report_path"])) == dataset_manifest["quality_report_sha256"]
    catalog = service.verify_catalog()
    assert catalog["valid_dataset_records"] == 1
    assert catalog["missing_manifest_records"] == []
    assert catalog["missing_raw_records"] == []
    assert catalog["missing_normalized_records"] == []
    assert catalog["missing_quality_report_records"] == []
    assert catalog["missing_curated_records"] == []
    assert catalog["dangling_current_records"] == []
    assert catalog["hash_mismatch_records"] == []
    assert catalog["pointer_catalog_mismatch"] == []
    assert catalog["valid_current_records"] == 1


# ---------------------------------------------------------------- repository policy

def test_repository_rejects_research_only_by_default(tmp_path: Path):
    class ResearchProvider(FakeProvider):
        name = "yahoo_chart"

        def fetch(self, request):
            result = super().fetch(request)
            result.source = self.name
            result.metadata = {"research_only": True}
            return result

    service = HistoricalDataService(tmp_path, providers={"yahoo_chart": ResearchProvider(stock_bars())})
    service.ingest("NVDA", "D1", datetime(2024, 1, 2, tzinfo=UTC), datetime(2024, 2, 2, tzinfo=UTC))
    with pytest.raises(DataQualityError):
        service.load_bars("NVDA", "D1")
    assert len(service.load_bars("NVDA", "D1", allow_research=True)) > 0


def test_repository_rejects_legacy_only_by_default(tmp_path: Path):
    from inv_trend.data.legacy import migrate_legacy_csv
    source = tmp_path / "processed" / "cleaned"
    source.mkdir(parents=True)
    pd.DataFrame({"symbol": ["OLD"], "timeframe": ["D1"], "timestamp": ["2024-01-01"],
                  "open": [1], "high": [2], "low": [0.5], "close": [1.5]}).to_csv(source / "old.csv", index=False)
    migrate_legacy_csv(tmp_path / "processed", tmp_path / "lake")
    service = HistoricalDataService(tmp_path / "lake")
    # Not registered in the formal universe: default Repository access must reject.
    with pytest.raises((KeyError, DataQualityError)):
        service.load_bars("OLD", "D1")
    assert len(service.load_bars("OLD", "D1", allow_legacy=True, min_quality_score=10)) == 1


def test_repository_rejects_low_quality_score(tmp_path: Path):
    frame = bars().iloc[:2].copy()
    frame["is_complete"] = True
    service = HistoricalDataService(tmp_path, providers={"binance": FakeProvider(frame)})
    manifest = service.ingest("BTC", "D1", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 3, tzinfo=UTC))
    assert manifest.quality_status == "CURATED"
    assert len(service.load_bars("BTC", "D1")) == 2
    with pytest.raises(DataQualityError):
        service.load_bars("BTC", "D1", min_quality_score=100.5)
    assert len(service.load_bars("BTC", "D1", min_quality_score=100.0)) == 2
