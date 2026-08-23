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
from inv_trend.application.strategy_config import DailyChecksConfig, TrendDecisionConfig
from inv_trend.application.daily.workflow import DailyWorkflow
from inv_trend.application.daily.strategy_screening import StrategyScreeningService
from inv_trend.application.daily.legacy_runtime_adapter import (
    DeferredStateDailyRuntimeAdapter,
    PreCommitDailyRuntimeAdapter,
)
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
    # The date directory permanently contains its private staging parent;
    # only named children are published immutable run histories.
    dated_runs = [
        path
        for path in (root / "runs" / "2024-04-19").iterdir()
        if path.name != ".staging"
    ]
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


def test_staged_daily_workflow_keeps_read_stages_side_effect_free(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "stage001"
    report_date = "2024-04-19"

    workflow.data_update(
        run_id=run_id,
        report_date=report_date,
        symbols=("AAA",),
        bootstrap_days=80,
    )
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    workflow.trend_decide(run_id=run_id, report_date=report_date)
    staging = tmp_path / "out" / "runs" / report_date / ".staging" / run_id / "AAA"
    assert (staging / "01_canonical" / "data_update_result.json").exists()
    assert (staging / "01_canonical" / "strategy_screening_result.json").exists()
    assert (staging / "01_canonical" / "trend_decision_result.json").exists()
    assert SQLiteDailySignalRepository(workflow.database_path).signal_count() == 0

    committed = workflow.commit(run_id=run_id, report_date=report_date)
    assert committed["summary"]["signals_new"] >= 1
    assert SQLiteDailySignalRepository(workflow.database_path).signal_count() >= 1
    publication = workflow.publish(run_id=run_id, report_date=report_date)
    assert (publication.run_directory / "AAA" / "01_canonical" / "complete_analysis_result.json").exists()
    delivery = workflow.deliver(run_id=run_id, report_date=report_date)
    assert set(delivery["delivery"]) == {"notified", "errors", "recovered"}
    retried_publication = workflow.publish(run_id=run_id, report_date=report_date)
    assert retried_publication.run_directory == publication.run_directory

    chained = workflow.run(symbols=("AAA",), bootstrap_days=80)
    assert chained.snapshot["run_id"] != run_id
    assert chained.artifact_publication is not None


def test_staged_screen_reads_the_pinned_version_after_current_moves(tmp_path: Path) -> None:
    """A resumed screen must not silently switch to a newly activated D1 set."""

    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    report_date, run_id = "2024-04-19", "pinned-version"
    workflow.data_update(
        run_id=run_id,
        report_date=report_date,
        symbols=("AAA",),
        bootstrap_days=80,
    )
    staging = tmp_path / "out" / "runs" / report_date / ".staging" / run_id / "AAA"
    first = json.loads(
        (staging / "01_canonical" / "data_update_result.json").read_text(encoding="utf-8")
    )
    first_version = first["dataset_version"]

    updated = _bars()
    new_bar = updated.iloc[-1].copy()
    new_bar["timestamp"] = pd.Timestamp("2024-04-19", tz="UTC")
    new_bar["open"] = 111.0
    new_bar["high"] = 113.0
    new_bar["low"] = 100.0
    new_bar["close"] = 112.0
    updated = pd.concat([updated, pd.DataFrame([new_bar])], ignore_index=True)

    class VersionTwoProvider:
        name = "fake"

        def fetch(self, request):
            stamps = pd.to_datetime(updated["timestamp"], utc=True)
            frame = updated.loc[
                (stamps >= pd.Timestamp(request.start)) & (stamps <= pd.Timestamp(request.end))
            ].copy()
            return ProviderResult(frame, request.instrument.source_symbol, self.name, "test")

    scanner.historical_service.providers["fake"] = VersionTwoProvider()
    second = scanner.historical_service.ingest(
        "AAA",
        "D1",
        datetime(2024, 1, 30, tzinfo=UTC),
        datetime(2024, 4, 20, tzinfo=UTC),
    )
    assert second.dataset_version != first_version
    assert scanner.historical_service.lake.current_version("AAA", "D1")["version"] == second.dataset_version

    loaded_versions: list[str] = []
    load_pinned = scanner.historical_service.load_bars_version

    def recording_load(symbol, timeframe, dataset_version, *args, **kwargs):
        loaded_versions.append(dataset_version)
        return load_pinned(symbol, timeframe, dataset_version, *args, **kwargs)

    scanner.historical_service.load_bars_version = recording_load
    workflow.strategy_screen(run_id=run_id, report_date=report_date)

    screening = json.loads(
        (staging / "01_canonical" / "strategy_screening_result.json").read_text(encoding="utf-8")
    )
    assert loaded_versions == [first_version]
    assert screening["dataset_version"] == first_version
    assert screening["as_of"] == first["latest_complete_d1"]


def test_staged_screen_rejects_a_port_that_returns_the_wrong_version(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    report_date, run_id = "2024-04-19", "version-mismatch"
    workflow.data_update(
        run_id=run_id,
        report_date=report_date,
        symbols=("AAA",),
        bootstrap_days=80,
    )
    staging = tmp_path / "out" / "runs" / report_date / ".staging" / run_id / "AAA"
    data = json.loads(
        (staging / "01_canonical" / "data_update_result.json").read_text(encoding="utf-8")
    )
    expected_version = data["dataset_version"]
    wrong = scanner.historical_service.load_bars_version("AAA", "D1", expected_version)
    wrong.attrs["dataset_version"] = "another-version"

    scanner.historical_service.load_bars_version = lambda *args, **kwargs: wrong.copy()
    with pytest.raises(ValueError, match="do not match staged dataset version"):
        workflow.strategy_screen(run_id=run_id, report_date=report_date)


def test_staged_research_mode_keeps_observation_evidence_without_state_mutation(
    tmp_path: Path,
) -> None:
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
    scanner.historical_service.load_bars_version = lambda *args, **kwargs: bars.copy()
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "research001"
    report_date = "2024-04-19"
    workflow.data_update(
        run_id=run_id,
        report_date=report_date,
        symbols=("AAA",),
        bootstrap_days=80,
        research_mode=True,
    )
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    workflow.trend_decide(run_id=run_id, report_date=report_date)
    committed = workflow.commit(run_id=run_id, report_date=report_date)

    row = committed["summary"]
    assert row["signals_new"] == 0
    receipt = json.loads(
        (tmp_path / "out" / "runs" / report_date / ".staging" / run_id / "commit_receipt.json").read_text(
            encoding="utf-8"
        )
    )
    instrument = receipt["snapshot"]["symbols"][0]
    assert instrument["alert_policy"]["research_mode"] is True
    assert len(instrument["research_signals"]) >= 1
    assert instrument["report_bundle"]["trend_decision_result"]["observation_only"] is True
    assert SQLiteDailySignalRepository(workflow.database_path).signal_count() == 0


def test_staged_commit_rejects_tampered_screening_runtime(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "tamper001"
    report_date = "2024-04-19"
    workflow.data_update(run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80)
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    workflow.trend_decide(run_id=run_id, report_date=report_date)
    runtime_path = (
        tmp_path
        / "out"
        / "runs"
        / report_date
        / ".staging"
        / run_id
        / "AAA"
        / "04_audit"
        / "screening_runtime.json"
    )
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    runtime["positions"] = []
    runtime_path.write_text(json.dumps(runtime, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="commit evidence hash"):
        workflow.commit(run_id=run_id, report_date=report_date)
    assert SQLiteDailySignalRepository(workflow.database_path).signal_count() == 0


def test_staged_commit_validates_all_symbols_before_any_sqlite_mutation(
    tmp_path: Path,
) -> None:
    """A later bad symbol must roll back/prevent every earlier formal write."""

    scanner, _ = _service(tmp_path, symbols=("AAA", "BBB"))
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "multi-symbol-atomic-001"
    report_date = "2024-04-19"
    workflow.data_update(
        run_id=run_id,
        report_date=report_date,
        symbols=("AAA", "BBB"),
        bootstrap_days=80,
    )
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    workflow.trend_decide(run_id=run_id, report_date=report_date)

    # Deliberately invalidate only the second symbol after every stage has
    # been prepared.  The old per-symbol sequence could have committed AAA
    # before discovering this failure; the run-level transaction must leave
    # both cursors, signals, and outbox rows absent.
    second_decision = (
        tmp_path
        / "out"
        / "runs"
        / report_date
        / ".staging"
        / run_id
        / "BBB"
        / "01_canonical"
        / "trend_decision_result.json"
    )
    payload = json.loads(second_decision.read_text(encoding="utf-8"))
    payload["result_hash"] = "tampered"
    second_decision.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="trend-decide result hash"):
        workflow.commit(run_id=run_id, report_date=report_date)

    repository = SQLiteDailySignalRepository(workflow.database_path)
    assert repository.signal_count() == 0
    assert repository.load_cursor("AAA.TEST.SPOT", "D1", "corrected-v2") is None
    assert repository.load_cursor("BBB.TEST.SPOT", "D1", "corrected-v2") is None
    assert repository.pending_notifications() == []


def test_staged_commit_rolls_back_a_late_sqlite_failure_for_every_symbol(
    tmp_path: Path,
) -> None:
    """A transaction failure after AAA is written leaves no partial D1 state."""

    scanner, _ = _service(tmp_path, symbols=("AAA", "BBB"))
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "multi-symbol-rollback-001"
    report_date = "2024-04-19"
    workflow.data_update(
        run_id=run_id,
        report_date=report_date,
        symbols=("AAA", "BBB"),
        bootstrap_days=80,
    )
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    workflow.trend_decide(run_id=run_id, report_date=report_date)

    # The first symbol reaches signal/outbox/cursor persistence before BBB's
    # cursor.  This injects a failure inside the same SQLite transaction,
    # rather than during pre-validation above.
    with sqlite3.connect(workflow.database_path) as db:
        db.execute(
            """CREATE TRIGGER fail_bbb_d1_cursor
            BEFORE INSERT ON scan_cursors
            WHEN NEW.instrument_id='BBB.TEST.SPOT'
            BEGIN
                SELECT RAISE(ABORT, 'injected BBB cursor failure');
            END"""
        )

    with pytest.raises(sqlite3.DatabaseError, match="injected BBB cursor failure"):
        workflow.commit(run_id=run_id, report_date=report_date)

    repository = SQLiteDailySignalRepository(workflow.database_path)
    assert repository.signal_count() == 0
    assert repository.load_cursor("AAA.TEST.SPOT", "D1", "corrected-v2") is None
    assert repository.load_cursor("BBB.TEST.SPOT", "D1", "corrected-v2") is None
    assert repository.pending_notifications() == []
    with sqlite3.connect(workflow.database_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM daily_runs WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM daily_run_instruments WHERE run_id=?", (run_id,)
        ).fetchone()[0] == 0


def test_staged_commit_rejects_a_cursor_advanced_by_a_newer_run(tmp_path: Path) -> None:
    """A delayed run must not move a D1 cursor backwards after re-screening."""

    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    report_date = "2024-04-19"
    older_run, newer_run = "cursor-cas-old", "cursor-cas-new"
    for run_id in (older_run, newer_run):
        workflow.data_update(
            run_id=run_id,
            report_date=report_date,
            symbols=("AAA",),
            bootstrap_days=80,
        )
        workflow.strategy_screen(run_id=run_id, report_date=report_date)
        workflow.trend_decide(run_id=run_id, report_date=report_date)

    workflow.commit(run_id=newer_run, report_date=report_date)
    repository = SQLiteDailySignalRepository(workflow.database_path)
    committed_cursor = repository.load_cursor("AAA.TEST.SPOT", "D1", "corrected-v2")
    assert committed_cursor is not None

    with pytest.raises(ValueError, match="cursor changed after strategy-screen"):
        workflow.commit(run_id=older_run, report_date=report_date)

    assert repository.load_cursor("AAA.TEST.SPOT", "D1", "corrected-v2") == committed_cursor
    with sqlite3.connect(workflow.database_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM daily_runs WHERE run_id=?", (older_run,)
        ).fetchone()[0] == 0


def test_staged_commit_deserializes_decision_projection_without_signal_generator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Commit cannot recalculate or regenerate events from a runtime adapter."""

    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "projection-only-commit-001"
    report_date = "2024-04-19"
    workflow.data_update(run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80)
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    workflow.trend_decide(run_id=run_id, report_date=report_date)

    def forbidden_signal_generator(*args, **kwargs):
        raise AssertionError("commit must consume the signed decision projection")

    monkeypatch.setattr(workflow._runtime, "signal_events", forbidden_signal_generator)
    committed = workflow.commit(run_id=run_id, report_date=report_date)
    assert committed["summary"]["signals_new"] >= 1


def test_staged_commit_does_not_read_market_data_after_decision(tmp_path: Path) -> None:
    """The state stage can resume from JSON with no market-data access."""

    scanner, _ = _service(tmp_path)
    first = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "commit-no-data-001"
    report_date = "2024-04-19"
    first.data_update(run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80)
    first.strategy_screen(run_id=run_id, report_date=report_date)
    first.trend_decide(run_id=run_id, report_date=report_date)

    class NoDataCommitRuntime(DeferredStateDailyRuntimeAdapter):
        @property
        def market_data(self):  # type: ignore[override]
            raise AssertionError("commit must not access market data")

    resumed = DailyWorkflow(
        runtime=NoDataCommitRuntime(
            data_root=tmp_path / "must-not-be-read-by-commit",
            output_dir=tmp_path / "out",
            signal_log_dir=tmp_path / "logs",
            assets=scanner.assets,
            now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
        )
    )
    committed = resumed.commit(run_id=run_id, report_date=report_date)
    assert committed["summary"]["signals_new"] >= 1


def test_staged_commit_rejects_tampered_decision_projection(tmp_path: Path) -> None:
    """Operational commit data is separately hash-bound to the decision JSON."""

    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "tamper-projection-001"
    report_date = "2024-04-19"
    workflow.data_update(run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80)
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    workflow.trend_decide(run_id=run_id, report_date=report_date)
    decision_path = (
        tmp_path / "out" / "runs" / report_date / ".staging" / run_id / "AAA"
        / "01_canonical" / "trend_decision_result.json"
    )
    payload = json.loads(decision_path.read_text(encoding="utf-8"))
    payload["commit_projection"]["persistence"]["strategy_version"] = "tampered"
    decision_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="commit projection hash"):
        workflow.commit(run_id=run_id, report_date=report_date)
    assert SQLiteDailySignalRepository(workflow.database_path).signal_count() == 0


def test_publish_and_deliver_do_not_read_market_data_after_commit(tmp_path: Path) -> None:
    """The post-commit stages validate staged JSON, not a mutable data lake."""

    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "post-commit-no-data-001"
    report_date = "2024-04-19"
    workflow.data_update(run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80)
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    workflow.trend_decide(run_id=run_id, report_date=report_date)
    workflow.commit(run_id=run_id, report_date=report_date)

    class NoDataPreCommitRuntime(PreCommitDailyRuntimeAdapter):
        @property
        def market_data(self):  # type: ignore[override]
            raise AssertionError("publish must not access market data")

    class NoDataDeferredRuntime(DeferredStateDailyRuntimeAdapter):
        @property
        def market_data(self):  # type: ignore[override]
            raise AssertionError("deliver must not access market data")

    publication = DailyWorkflow(
        runtime=NoDataPreCommitRuntime(
            data_root=tmp_path / "must-not-be-read-by-publish",
            output_dir=tmp_path / "out",
            signal_log_dir=tmp_path / "logs",
            assets=scanner.assets,
            now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
        )
    ).publish(run_id=run_id, report_date=report_date)
    assert publication.compatibility_json.exists()

    delivery = DailyWorkflow(
        runtime=NoDataDeferredRuntime(
            data_root=tmp_path / "must-not-be-read-by-deliver",
            output_dir=tmp_path / "out",
            signal_log_dir=tmp_path / "logs",
            assets=scanner.assets,
            now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
        )
    ).deliver(run_id=run_id, report_date=report_date)
    assert set(delivery["delivery"]) == {"notified", "errors", "recovered"}


def test_trend_decide_consumes_staged_json_without_opening_market_data(tmp_path: Path) -> None:
    """An independently resumed decision is a pure staged-evidence operation."""

    scanner, _ = _service(tmp_path)
    first = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "trend-no-data-001"
    report_date = "2024-04-19"
    first.data_update(run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80)
    first.strategy_screen(run_id=run_id, report_date=report_date)

    class NoDataRuntime(PreCommitDailyRuntimeAdapter):
        @property
        def market_data(self):  # type: ignore[override]
            raise AssertionError("trend-decide must not access market data")

    DailyWorkflow(
        runtime=NoDataRuntime(
            data_root=tmp_path / "must-not-be-read-by-trend",
            output_dir=tmp_path / "out",
            signal_log_dir=tmp_path / "logs",
            assets=scanner.assets,
            now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
        )
    ).trend_decide(run_id=run_id, report_date=report_date)


def test_staged_publish_rejects_tampered_canonical_result(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "publishhash001"
    report_date = "2024-04-19"
    workflow.data_update(run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80)
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    workflow.trend_decide(run_id=run_id, report_date=report_date)
    workflow.commit(run_id=run_id, report_date=report_date)
    decision_path = (
        tmp_path / "out" / "runs" / report_date / ".staging" / run_id / "AAA"
        / "01_canonical" / "trend_decision_result.json"
    )
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    decision["conclusion"] = "tampered after commit"
    decision_path.write_text(json.dumps(decision, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="trend-decide result hash"):
        workflow.publish(run_id=run_id, report_date=report_date)


def test_staged_publish_rejects_tampered_commit_snapshot(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "receipt-tamper-001"
    report_date = "2024-04-19"
    workflow.data_update(run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80)
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    workflow.trend_decide(run_id=run_id, report_date=report_date)
    workflow.commit(run_id=run_id, report_date=report_date)
    receipt_path = tmp_path / "out" / "runs" / report_date / ".staging" / run_id / "commit_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["snapshot"]["symbols"][0]["trend_decision_result"]["conclusion"] = "tampered receipt"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="commit snapshot"):
        workflow.publish(run_id=run_id, report_date=report_date)


def test_commit_recovers_a_missing_receipt_without_reapplying_sqlite_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "receipt-recovery-001"
    report_date = "2024-04-19"
    workflow.data_update(run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80)
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    workflow.trend_decide(run_id=run_id, report_date=report_date)
    original_integrity = workflow._commit_receipt_integrity

    def interrupted_after_sqlite(*args, **kwargs):
        raise RuntimeError("simulated receipt write interruption")

    monkeypatch.setattr(workflow, "_commit_receipt_integrity", interrupted_after_sqlite)
    with pytest.raises(RuntimeError, match="receipt write interruption"):
        workflow.commit(run_id=run_id, report_date=report_date)
    repository = SQLiteDailySignalRepository(workflow.database_path)
    signal_count = repository.signal_count()
    receipt_path = tmp_path / "out" / "runs" / report_date / ".staging" / run_id / "commit_receipt.json"
    assert not receipt_path.exists()

    monkeypatch.setattr(workflow, "_commit_receipt_integrity", original_integrity)
    recovered = workflow.commit(run_id=run_id, report_date=report_date)

    assert recovered["summary"]["signals_new"] >= 1
    assert repository.signal_count() == signal_count
    assert receipt_path.exists()


def test_staging_operational_context_rejects_copied_run_identity(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "context-identity-001"
    report_date = "2024-04-19"
    workflow.data_update(run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80)
    context_path = tmp_path / "out" / "runs" / report_date / ".staging" / run_id / "run_context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["started_at"] = "2024-04-20T00:00:00+00:00"
    context_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(ValueError, match="operational hash"):
        workflow.strategy_screen(run_id=run_id, report_date=report_date)


def test_data_update_retry_is_idempotent_and_rejects_context_drift(tmp_path: Path) -> None:
    scanner, provider = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "resume-data-001"
    report_date = "2024-04-19"
    first = workflow.data_update(
        run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80
    )
    request_count = len(provider.requests)
    retry = workflow.data_update(
        run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80
    )

    assert first["stage"] == "data-update"
    assert retry["idempotent"] is True
    assert len(provider.requests) == request_count
    with pytest.raises(ValueError, match="configuration does not match"):
        workflow.data_update(
            run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=81
        )


def test_staged_screen_uses_configuration_pinned_by_data_update(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "pinned-config-001"
    report_date = "2024-04-19"
    workflow.data_update(run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80)
    context_path = tmp_path / "out" / "runs" / report_date / ".staging" / run_id / "run_context.json"
    context = json.loads(context_path.read_text(encoding="utf-8"))
    expected = context["configuration"]["daily_checks"]
    # Simulate a YAML reload/edit between independently invoked stages.  The
    # screen must use the stored context, not this mutable runtime setting.
    runtime_service = workflow._runtime._service  # type: ignore[attr-defined]
    runtime_service.daily_checks_config = runtime_service.daily_checks_config.__class__(
        **{**runtime_service.daily_checks_config.__dict__, "sma_periods": (3, 6, 9)}
    )
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    screening_path = (
        tmp_path / "out" / "runs" / report_date / ".staging" / run_id / "AAA"
        / "01_canonical" / "strategy_screening_result.json"
    )
    screening = json.loads(screening_path.read_text(encoding="utf-8"))
    stored_checks = dict(expected)
    for name in ("sma_periods", "ema_periods", "macd_session_periods"):
        stored_checks[name] = tuple(stored_checks[name])
    expected_hash = StrategyScreeningService(
        DailyChecksConfig(**stored_checks),
        TrendDecisionConfig(**context["configuration"]["trend_decision"]),
    ).configuration_hash
    assert screening["configuration_hash"] == expected_hash


def test_stage_hashes_exclude_run_id_and_wall_clock_time(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)
    # Seed one immutable D1 version, then have two independent staged runs
    # read exactly that version at different wall-clock times.
    scanner.run(bootstrap_days=80, render_html=False)

    hashes: list[tuple[str, str]] = []
    for name, hour in (("hash-a", 12), ("hash-b", 18)):
        workflow = DailyWorkflow(
            data_root=tmp_path / "data",
            output_dir=tmp_path / name,
            signal_log_dir=tmp_path / "logs",
            assets=scanner.assets,
            historical_service=scanner.historical_service,
            refresh_data=False,
            now=lambda hour=hour: datetime(2024, 4, 19, hour, tzinfo=UTC),
        )
        workflow.data_update(
            run_id=name, report_date="2024-04-19", symbols=("AAA",), bootstrap_days=80
        )
        workflow.strategy_screen(run_id=name, report_date="2024-04-19")
        root = tmp_path / name / "runs" / "2024-04-19" / ".staging" / name / "AAA" / "01_canonical"
        data = json.loads((root / "data_update_result.json").read_text(encoding="utf-8"))
        screen = json.loads((root / "strategy_screening_result.json").read_text(encoding="utf-8"))
        hashes.append((data["result_hash"], screen["result_hash"]))

    assert hashes[0] == hashes[1]


def test_delivery_retry_only_consumes_outbox_and_keeps_publication_immutable(
    tmp_path: Path,
) -> None:
    class SwitchableNotifier:
        def __init__(self) -> None:
            self.available = False
            self.delivered: list[str] = []

        def notify(self, event: SignalEvent) -> None:
            if not self.available:
                raise RuntimeError("notification transport unavailable")
            self.delivered.append(event.signal_id)

    scanner, _ = _service(tmp_path)
    notifier = SwitchableNotifier()
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        notifier=notifier,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "delivery-retry-001"
    report_date = "2024-04-19"
    workflow.data_update(run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80)
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    workflow.trend_decide(run_id=run_id, report_date=report_date)
    workflow.commit(run_id=run_id, report_date=report_date)
    repository = SQLiteDailySignalRepository(workflow.database_path)
    grade_a = SignalEvent.create(
        instrument_id="AAA.TEST.SPOT",
        symbol="AAA",
        timeframe="D1",
        signal_type="STRATEGY_GRADE_A_LONG",
        direction="LONG",
        signal_time="2024-04-19T00:00:00+00:00",
        detected_at="2024-04-19T12:00:00+00:00",
        trigger_price=111.0,
        reference_value=110.0,
        dataset_version="test-v1",
        indicator_name="strategy_grade",
    )
    repository.commit_events_and_cursor(
        [grade_a],
        run_id=run_id,
        instrument_id=grade_a.instrument_id,
        timeframe="D1",
        strategy_version="corrected-v2",
        last_signal_time=grade_a.signal_time,
        notification_signal_ids={grade_a.signal_id},
    )
    assert repository.pending_notifications(run_id)
    publication = workflow.publish(run_id=run_id, report_date=report_date, render_html=False)
    canonical_before = publication.compatibility_json.read_bytes()

    first = workflow.deliver(run_id=run_id, report_date=report_date)
    assert first["delivery"]["errors"] >= 1
    notifier.available = True
    second = workflow.deliver(run_id=run_id, report_date=report_date)

    assert second["delivery"]["notified"] >= 1
    assert publication.compatibility_json.read_bytes() == canonical_before
    receipt_path = (
        tmp_path / "out" / "runs" / report_date / ".staging" / run_id / "delivery_receipt.json"
    )
    assert json.loads(receipt_path.read_text(encoding="utf-8"))["attempt"] == 2


def test_staged_commit_resumes_open_run_and_preserves_screen_anchor(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)
    workflow = DailyWorkflow(
        data_root=tmp_path / "data",
        output_dir=tmp_path / "out",
        signal_log_dir=tmp_path / "logs",
        assets=scanner.assets,
        historical_service=scanner.historical_service,
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=UTC),
    )
    run_id = "resume001"
    report_date = "2024-04-19"
    workflow.data_update(run_id=run_id, report_date=report_date, symbols=("AAA",), bootstrap_days=80)
    data_path = (
        tmp_path
        / "out"
        / "runs"
        / report_date
        / ".staging"
        / run_id
        / "AAA"
        / "01_canonical"
        / "data_update_result.json"
    )
    instrument_id = json.loads(data_path.read_text(encoding="utf-8"))["instrument_id"]
    repository = SQLiteDailySignalRepository(workflow.database_path)
    anchored = "2024-02-01T00:00:00+00:00"
    assert repository.get_or_create_session_anchor(instrument_id, "D1", anchored) == anchored
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    runtime_path = data_path.parent.parent / "04_audit" / "screening_runtime.json"
    assert json.loads(runtime_path.read_text(encoding="utf-8"))["session_anchor"] == anchored
    workflow.trend_decide(run_id=run_id, report_date=report_date)
    context = json.loads((runtime_path.parents[2] / "run_context.json").read_text(encoding="utf-8"))
    repository.start_run(run_id, context["started_at"])

    committed = workflow.commit(run_id=run_id, report_date=report_date)
    assert committed["summary"]["signals_new"] >= 1


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


def test_keyboard_interrupt_before_commit_keeps_state_unmodified(tmp_path: Path) -> None:
    scanner, _ = _service(tmp_path)

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    scanner.data_update_service.run = interrupt
    with pytest.raises(KeyboardInterrupt):
        scanner.run()
    with sqlite3.connect(scanner.database_path) as db:
        statuses = [row[0] for row in db.execute("SELECT status FROM daily_runs")]
    # The data-update stage is deliberately read-only.  A Ctrl+C before
    # commit therefore cannot create an interrupted signal/cursor transaction.
    assert statuses == []


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
    scanner.historical_service.load_bars_version = lambda *args, **kwargs: bars.copy()
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
    class FakeWorkflow:
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
    monkeypatch.setattr("inv_trend.cli.daily.DailyWorkflow", FakeWorkflow)
    assert daily_main(["--symbol", "AAA", "--no-color"]) == 1


def test_cli_stage_command_emits_machine_readable_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured = {}

    class FakeWorkflow:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def data_update(self, **kwargs):
            captured["stage"] = kwargs
            return {"stage": "data-update", "run_id": kwargs["run_id"], "symbols": ["AAA"]}

    monkeypatch.setattr("inv_trend.cli.daily.DailyWorkflow", FakeWorkflow)
    assert daily_main([
        "data-update", "--run-id", "run001", "--report-date", "2024-04-19", "--symbol", "AAA",
    ]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output == {"run_id": "run001", "stage": "data-update", "symbols": ["AAA"]}
    assert captured["stage"]["symbols"] == ["AAA"]


def test_cli_returns_130_without_traceback_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InterruptedWorkflow:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self, **kwargs):
            raise KeyboardInterrupt

    monkeypatch.setattr("inv_trend.cli.daily.DailyWorkflow", InterruptedWorkflow)
    assert daily_main(["--provider-timeout", "5", "--provider-retries", "1"]) == 130
