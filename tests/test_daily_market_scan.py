from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from inv_trend.data.api import HistoricalDataService
from inv_trend.data.models import InstrumentConfig, ProviderResult
from inv_trend.adapters.detector.alerts.daily_notifier import LogNotifier
from inv_trend.cli.daily import main as daily_main
from inv_trend.core.signals import SignalEvent
from inv_trend.application import DailyMarketScanService
from inv_trend.adapters.detector.models import AssetConfig, Market
from inv_trend.adapters.detector.storage.daily_signal_repository import SQLiteDailySignalRepository


UTC = timezone.utc


def _instrument(symbol: str, *, status: str = "CURATED") -> InstrumentConfig:
    return InstrumentConfig(
        symbol=symbol, source_symbol=symbol, instrument_id=f"{symbol}.TEST.SPOT",
        asset_class="crypto", market="test", venue="test", instrument_type="spot",
        quote_currency="USD", currency="USD", timezone="UTC", session_timezone="UTC",
        session="24x7", primary_source="fake", earliest_valid_date="2020-01-01",
        adjustment_policy="none", adjustment_method="none",
    )


def _asset(symbol: str) -> AssetConfig:
    return AssetConfig(
        symbol=symbol, instrument=f"{symbol}_TEST", market=Market.CRYPTO,
        data_source="fake", timeframes=("D1",), price_type="spot", adjustment="none",
    )


def _bars(*, quality: str = "CURATED") -> pd.DataFrame:
    closes = [100.0] * 79 + [111.0]
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2024-01-30", periods=80, freq="D", tz="UTC"),
        "open": closes, "high": [110.0] * 79 + [111.0],
        "low": [90.0] * 79 + [100.0], "close": closes,
        "volume": [1000.0] * 80, "is_complete": [True] * 80,
    })
    frame["quality_status"] = quality
    return frame


class FakeProvider:
    name = "fake"

    def __init__(self, *, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.requests = []

    def fetch(self, request):
        self.requests.append(request)
        if request.instrument.symbol in self.fail:
            raise RuntimeError("provider timeout")
        source = _bars()
        stamps = pd.to_datetime(source["timestamp"], utc=True)
        frame = source.loc[(stamps >= pd.Timestamp(request.start)) & (stamps <= pd.Timestamp(request.end))].copy()
        return ProviderResult(frame, request.instrument.source_symbol, self.name, "test")


def _service(tmp_path: Path, symbols=("AAA",), *, fail=None, now_day=19):
    instruments = {symbol: _instrument(symbol) for symbol in symbols}
    provider = FakeProvider(fail=fail)
    historical = HistoricalDataService(
        tmp_path / "data", instruments=instruments, providers={"fake": provider}
    )
    scanner = DailyMarketScanService(
        data_root=tmp_path / "data", output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs", assets={s: _asset(s) for s in symbols},
        historical_service=historical,
        now=lambda: datetime(2024, 4, now_day, 12, tzinfo=UTC),
    )
    return scanner, provider


def test_bootstrap_is_idempotent_in_sqlite_and_log(tmp_path: Path) -> None:
    scanner, provider = _service(tmp_path)
    first = scanner.run(bootstrap_days=80)
    assert first.exit_code == 0
    assert first.snapshot["schema_version"] == "4"
    assert first.snapshot["report_schema_version"] == "3"
    assert first.snapshot["configuration"]["daily_checks"]["sma_periods"] == (5, 10, 20, 55, 120)
    assert "strategy_checks" in first.snapshot["symbols"][0]["scan"]
    assert first.snapshot["summary"]["signals_new"] >= 4
    assert SQLiteDailySignalRepository(first.database_path).signal_count() == first.snapshot["summary"]["signals_new"]
    log = tmp_path / "logs" / "2024-04-19.jsonl"
    assert not log.exists()

    second = scanner.run(bootstrap_days=80)
    assert second.snapshot["summary"]["signals_new"] == 0
    assert second.snapshot["summary"]["signals_duplicate"] == first.snapshot["summary"]["signals_new"]
    assert SQLiteDailySignalRepository(second.database_path).signal_count() == first.snapshot["summary"]["signals_new"]
    assert not log.exists()
    # The second run intentionally re-fetches the bounded revision overlap;
    # content addressing keeps the dataset and signals unchanged.
    assert len(provider.requests) == 2


def test_run_artifacts_are_authoritative_and_keep_flat_compatibility(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)
    result = scanner.run(bootstrap_days=80)
    root = tmp_path / "out"
    run_root = root / "runs" / "2024-04-19" / result.snapshot["run_id"] / "AAA"
    canonical = run_root / "01_canonical"
    exports = run_root / "03_exports"
    audit = run_root / "04_audit"

    for path in (
        canonical / "data_update_result.json",
        canonical / "strategy_screening_result.json",
        canonical / "trend_decision_result.json",
        canonical / "complete_analysis_result.json",
        run_root / "02_report" / "trend_analysis_report.html",
        exports / "strategy_conditions.csv",
        exports / "signals.csv",
        exports / "anomalies.csv",
        exports / "state_transitions.csv",
        audit / "run_manifest.json",
        audit / "configuration_snapshot.json",
        audit / "data_lineage.json",
        audit / "artifact_hashes.json",
    ):
        assert path.exists(), path

    complete_path = canonical / "complete_analysis_result.json"
    complete = json.loads(complete_path.read_text(encoding="utf-8"))
    assert set(("metadata", "data_update", "strategy_screening", "trend_decision", "signals", "anomalies", "state_transitions", "report_bundle", "hashes")) <= set(complete)
    assert complete["hashes"]["hash_chain_valid"] is True
    assert complete["trend_decision"]["input_screening_hash"] == complete["strategy_screening"]["result_hash"]
    assert complete["strategy_screening"]["input_data_hash"] == complete["data_update"]["result_hash"]
    assert result.snapshot_path == root / "2024-04-19.json"
    assert result.snapshot_path.with_suffix(".html").exists()
    assert root / "state" / "signals.sqlite3" == result.database_path
    assert not any(path.name == "signals.sqlite3" for path in run_root.rglob("*"))

    artifact_hashes = json.loads((audit / "artifact_hashes.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(complete_path.read_bytes()).hexdigest()
    assert artifact_hashes["complete_analysis_result.json"] == f"sha256:{digest}"
    configuration = json.loads((audit / "configuration_snapshot.json").read_text(encoding="utf-8"))
    assert configuration["strategy_config_hash"] == complete["strategy_screening"]["configuration_hash"]
    assert len(configuration["data_config_hash"]) == 64
    assert len(configuration["decision_config_hash"]) == 64
    assert (root / "latest" / "AAA" / "complete_analysis_result.json").read_bytes() == complete_path.read_bytes()
    assert (root / "latest" / "AAA" / "trend_analysis_report.html").exists()

    second = scanner.run(bootstrap_days=80)
    dated_runs = list((root / "runs" / "2024-04-19").iterdir())
    assert len(dated_runs) == 2
    assert second.snapshot["run_id"] != result.snapshot["run_id"]


def test_no_html_keeps_authoritative_json_and_skips_derived_html(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)
    result = scanner.run(bootstrap_days=80, render_html=False)
    root = tmp_path / "out"
    run_root = root / "runs" / "2024-04-19" / result.snapshot["run_id"] / "AAA"
    assert (run_root / "01_canonical" / "complete_analysis_result.json").exists()
    assert not (run_root / "02_report" / "trend_analysis_report.html").exists()
    assert not result.snapshot_path.with_suffix(".html").exists()
    assert not (root / "latest" / "AAA" / "trend_analysis_report.html").exists()


def test_refresh_failure_uses_fresh_current_but_returns_nonzero(tmp_path: Path) -> None:
    scanner, provider = _service(tmp_path, symbols=("AAA", "BBB"))
    initial = scanner.run(bootstrap_days=80)
    assert initial.exit_code == 0
    provider.fail.add("BBB")
    result = scanner.run(bootstrap_days=80)
    rows = {row["symbol"]: row for row in result.snapshot["symbols"]}
    assert result.exit_code == 1
    assert rows["BBB"]["update"]["previous_current_preserved"] is True
    assert rows["BBB"]["freshness"]["status"] == "FRESH"
    assert rows["BBB"]["scan"]["status"]["state"] == "ready"
    assert rows["BBB"]["run_status"] == "failed"


def test_stale_current_suppresses_formal_signal(tmp_path: Path) -> None:
    scanner, provider = _service(tmp_path, now_day=19)
    scanner.run(bootstrap_days=80)
    provider.fail.add("AAA")
    scanner._now = lambda: datetime(2024, 4, 22, 12, tzinfo=UTC)
    result = scanner.run(bootstrap_days=80)
    row = result.snapshot["symbols"][0]
    assert row["run_status"] == "stale"
    assert row["freshness"]["status"] == "STALE_DATA"
    assert row["signals_new"] == 0


def test_scan_only_never_contacts_provider_and_keeps_freshness_gate(tmp_path: Path) -> None:
    scanner, provider = _service(tmp_path)
    scanner.run(bootstrap_days=80)
    request_count = len(provider.requests)
    scanner.refresh_data = False
    result = scanner.run(bootstrap_days=80)
    row = result.snapshot["symbols"][0]
    assert len(provider.requests) == request_count
    assert row["update"]["mode"] == "scan_only"
    assert row["freshness"]["status"] == "FRESH"


def test_keyboard_interrupt_marks_daily_run_interrupted(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    scanner._run_symbol = interrupt
    with pytest.raises(KeyboardInterrupt):
        scanner.run()
    with sqlite3.connect(scanner.database_path) as db:
        statuses = [row[0] for row in db.execute("SELECT status FROM daily_runs")]
    assert statuses == ["INTERRUPTED"]


def test_research_data_is_computed_but_never_formally_persisted(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)
    bars = _bars(quality="RESEARCH_ONLY")
    bars.attrs["dataset_version"] = "research-v1"

    class Manifest:
        quality_passed = True
        quality_status = "RESEARCH_ONLY"
        quality_score = 75.0
        dataset_version = "research-v1"
        row_count = 80
        data_source = "fake"

    scanner.historical_service.lake.current_version = lambda *args: {"version": "research-v1"}
    scanner.historical_service.update = lambda *args, **kwargs: Manifest()
    scanner.historical_service.load_bars = lambda *args, **kwargs: bars.copy()
    result = scanner.run(research_mode=True)
    row = result.snapshot["symbols"][0]
    assert row["scan"]["status"]["state"] == "ready"
    assert row["alert_policy"]["formal_eligible"] is False
    assert len(row["research_signals"]) >= 4
    assert SQLiteDailySignalRepository(result.database_path).signal_count() == 0


def test_notifier_retries_without_duplicate_log_lines(tmp_path: Path) -> None:
    repo = SQLiteDailySignalRepository(tmp_path / "signals.sqlite3")
    event = SignalEvent.create(
        instrument_id="AAA.TEST", symbol="AAA", timeframe="D1",
        signal_type="MACD_GOLDEN_CROSS", direction="LONG",
        signal_time="2024-01-01T00:00:00+00:00", detected_at="2024-01-02T00:00:00+00:00",
        trigger_price=100, reference_value=0, dataset_version="v1",
        indicator_name="macd_12_26_9", indicator_parameters={"fast": 12},
    )
    assert repo.insert_signal(event)
    notifier = LogNotifier(tmp_path / "logs")
    notifier.notify(event)
    notifier.notify(event)
    lines = (tmp_path / "logs" / "2024-01-02.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1


def test_cli_returns_nonzero_for_bad_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeService:
        def __init__(self, **kwargs): pass
        def run(self, **kwargs):
            class Result:
                exit_code = 1
                failed_symbols = ("AAA",)
                snapshot_path = tmp_path / "report.json"
                database_path = tmp_path / "signals.sqlite3"
                signal_log_root = tmp_path / "logs"
                snapshot = {"report_date": "2024-01-01", "symbols": [], "summary": {"failed": 1}}
            return Result()
    monkeypatch.setattr("inv_trend.cli.daily.DailyMarketScanService", FakeService)
    assert daily_main(["--symbol", "AAA", "--no-color"]) == 1


def test_cli_returns_130_without_traceback_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InterruptedService:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self, **kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr("inv_trend.cli.daily.DailyMarketScanService", InterruptedService)
    assert daily_main(["--provider-timeout", "5", "--provider-retries", "1"]) == 130
