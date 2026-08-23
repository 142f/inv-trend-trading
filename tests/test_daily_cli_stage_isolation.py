"""Focused CLI contracts for independently executable D1 stages."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any
from types import SimpleNamespace

import pandas as pd
import pytest

from inv_trend.application.daily.legacy_runtime_adapter import (
    DeferredStateDailyRuntimeAdapter,
    PreCommitDailyRuntimeAdapter,
    ReadOnlyDailyStateAdapter,
)
from inv_trend.application.daily.workflow import DailyWorkflow
from inv_trend.application.daily.workspace import DailyStagingWorkspace
from inv_trend.adapters.daily.stage_runtime_adapters import (
    CommitStageRuntimeAdapter,
    DeliveryStageRuntimeAdapter,
    PublishStageRuntimeAdapter,
)
from inv_trend.adapters.detector.models import AssetConfig, Market
from inv_trend.cli.daily import main as daily_main
from inv_trend.data.api import HistoricalDataService
from inv_trend.data.models import InstrumentConfig, ProviderResult


@pytest.mark.parametrize(
    "command",
    (
        "data-update",
        "strategy-screen",
        "trend-decide",
    ),
)
def test_precommit_stage_cli_does_not_initialize_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    command: str,
) -> None:
    """The independent evidence commands use the no-state composition root."""

    captured: dict[str, Any] = {}

    class FakeWorkflow:
        def __init__(self, **kwargs: Any) -> None:
            captured["runtime"] = kwargs["runtime"]

        def data_update(self, **kwargs: Any) -> dict[str, Any]:
            return {"stage": "data-update", "run_id": kwargs["run_id"]}

        def strategy_screen(self, **kwargs: Any) -> dict[str, Any]:
            return {"stage": "strategy-screen", "run_id": kwargs["run_id"]}

        def trend_decide(self, **kwargs: Any) -> dict[str, Any]:
            return {"stage": "trend-decide", "run_id": kwargs["run_id"]}

    monkeypatch.setattr("inv_trend.cli.daily.DailyWorkflow", FakeWorkflow)
    output_dir = tmp_path / "artifacts"
    argv = [
        command,
        "--output-dir",
        str(output_dir),
        "--run-id",
        "readonly-stage",
        "--report-date",
        "2024-04-19",
    ]
    if command == "data-update":
        argv.extend(("--symbol", "BTCUSDT"))

    assert daily_main(argv) == 0
    assert isinstance(captured["runtime"], PreCommitDailyRuntimeAdapter)
    assert json.loads(capsys.readouterr().out) == {
        "run_id": "readonly-stage",
        "stage": command,
    }
    assert not (output_dir / "state").exists()


def test_read_only_state_adapter_never_creates_missing_store(tmp_path: Path) -> None:
    path = tmp_path / "new-output" / "state" / "signals.sqlite3"
    state = ReadOnlyDailyStateAdapter(path)

    assert state.load_cursor("BTC.TEST", "D1", "corrected-v2") is None
    assert state.load_session_anchor("BTC.TEST", "D1") is None
    assert not path.parent.exists()


def test_deferred_runtime_creates_sqlite_only_after_explicit_activation(
    tmp_path: Path,
) -> None:
    """The CLI composition root keeps SQLite behind commit/deliver."""

    output_dir = tmp_path / "artifacts"
    runtime = DeferredStateDailyRuntimeAdapter(
        data_root=tmp_path / "data",
        output_dir=output_dir,
    )

    assert isinstance(runtime.state_repository, ReadOnlyDailyStateAdapter)
    assert not (output_dir / "state").exists()
    # Publication is available without turning the read-only workspace into
    # operational state.
    assert runtime.artifact_publisher is not None
    assert not (output_dir / "state").exists()

    runtime.activate_stateful()
    assert (output_dir / "state" / "signals.sqlite3").is_file()


def test_publish_stage_cli_uses_the_no_state_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Publish is a pure artifact derivation and must not initialize SQLite."""

    captured: dict[str, Any] = {}

    class FakeWorkflow:
        def __init__(self, **kwargs: Any) -> None:
            captured["runtime"] = kwargs["runtime"]

        def publish(self, **kwargs: Any) -> Any:
            root = tmp_path / "published"
            return SimpleNamespace(
                run_directory=root,
                compatibility_json=root / "report.json",
                compatibility_html=root / "report.html",
            )

    monkeypatch.setattr("inv_trend.cli.daily.DailyWorkflow", FakeWorkflow)
    output_dir = tmp_path / "artifacts"

    assert daily_main([
        "publish", "--output-dir", str(output_dir), "--run-id", "published-run",
        "--report-date", "2024-04-19",
    ]) == 0
    assert isinstance(captured["runtime"], PublishStageRuntimeAdapter)
    assert not (output_dir / "state").exists()
    assert json.loads(capsys.readouterr().out)["stage"] == "publish"


@pytest.mark.parametrize(
    ("command", "runtime_type"),
    (
        ("commit", CommitStageRuntimeAdapter),
        ("publish", PublishStageRuntimeAdapter),
        ("deliver", DeliveryStageRuntimeAdapter),
    ),
)
def test_resumed_stage_cli_uses_a_minimal_runtime(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
    runtime_type: type[Any],
) -> None:
    """Late stages must not fall back to the data/configuration composition root."""

    captured: dict[str, Any] = {}

    class FakeWorkflow:
        def __init__(self, **kwargs: Any) -> None:
            captured["runtime"] = kwargs["runtime"]

        def commit(self, **kwargs: Any) -> dict[str, Any]:
            return {"stage": "commit", "run_id": kwargs["run_id"]}

        def publish(self, **kwargs: Any) -> Any:
            root = Path("published")
            return SimpleNamespace(
                run_directory=root,
                compatibility_json=root / "report.json",
                compatibility_html=None,
            )

        def deliver(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "stage": "deliver",
                "run_id": kwargs["run_id"],
                "delivery": {"notified": 0, "errors": 0, "recovered": 0},
            }

    monkeypatch.setattr("inv_trend.cli.daily.DailyWorkflow", FakeWorkflow)

    assert daily_main(
        [command, "--run-id", "minimal-stage", "--report-date", "2024-04-19"]
    ) == 0
    assert isinstance(captured["runtime"], runtime_type)
    assert json.loads(capsys.readouterr().out)["stage"] == command


def test_legacy_no_subcommand_cli_uses_deferred_state_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The compatibility ``turtle-daily --...`` route does not pre-open SQLite."""

    captured: dict[str, Any] = {}

    class FakeWorkflow:
        def __init__(self, **kwargs: Any) -> None:
            captured["runtime"] = kwargs["runtime"]

        def run(self, **kwargs: Any) -> Any:
            output_dir = Path(captured["runtime"].output_dir)
            return SimpleNamespace(
                snapshot={"report_date": "2024-04-19", "symbols": [], "summary": {}},
                snapshot_path=output_dir / "2024-04-19.json",
                database_path=output_dir / "state" / "signals.sqlite3",
                signal_log_root=tmp_path / "logs",
                artifact_publication=None,
                failed_symbols=(),
                exit_code=0,
            )

    monkeypatch.setattr("inv_trend.cli.daily.DailyWorkflow", FakeWorkflow)
    output_dir = tmp_path / "artifacts"

    assert daily_main(["--output-dir", str(output_dir), "--no-color"]) == 0
    assert isinstance(captured["runtime"], DeferredStateDailyRuntimeAdapter)
    assert not (output_dir / "state").exists()


def test_read_only_state_adapter_reads_existing_cursor_without_schema_setup(tmp_path: Path) -> None:
    path = tmp_path / "state" / "signals.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE scan_cursors (
                instrument_id TEXT,
                timeframe TEXT,
                strategy_version TEXT,
                last_signal_time TEXT
            );
            CREATE TABLE strategy_session_anchors (
                instrument_id TEXT,
                base_timeframe TEXT,
                anchor_time TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO scan_cursors VALUES (?, ?, ?, ?)",
            ("BTC.TEST", "D1", "corrected-v2", "2024-04-18T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO strategy_session_anchors VALUES (?, ?, ?)",
            ("BTC.TEST", "D1", "2020-01-01T00:00:00+00:00"),
        )

    state = ReadOnlyDailyStateAdapter(path)
    assert state.load_cursor("BTC.TEST", "D1", "corrected-v2") == "2024-04-18T00:00:00+00:00"
    assert state.load_session_anchor("BTC.TEST", "D1") == "2020-01-01T00:00:00+00:00"


def test_actual_precommit_workflow_stages_leave_state_store_absent(tmp_path: Path) -> None:
    """Data, screening, and decision stages never initialize the state store."""

    class Provider:
        name = "fake"

        def fetch(self, request: Any) -> ProviderResult:
            frame = _curated_bars()
            timestamps = pd.to_datetime(frame["timestamp"], utc=True)
            return ProviderResult(
                frame.loc[
                    (timestamps >= pd.Timestamp(request.start))
                    & (timestamps <= pd.Timestamp(request.end))
                ].copy(),
                request.instrument.source_symbol,
                self.name,
                "test",
            )

    instrument = InstrumentConfig(
        symbol="AAA",
        source_symbol="AAA",
        instrument_id="AAA.TEST.SPOT",
        asset_class="crypto",
        market="test",
        venue="test",
        instrument_type="spot",
        quote_currency="USD",
        currency="USD",
        timezone="UTC",
        session_timezone="UTC",
        session="24x7",
        primary_source="fake",
        earliest_valid_date="2020-01-01",
        adjustment_policy="none",
        adjustment_method="none",
    )
    output_dir = tmp_path / "artifacts"
    runtime = PreCommitDailyRuntimeAdapter(
        data_root=tmp_path / "data",
        output_dir=output_dir,
        assets={
            "AAA": AssetConfig(
                symbol="AAA",
                instrument="AAA_TEST",
                market=Market.CRYPTO,
                data_source="fake",
                timeframes=("D1",),
            )
        },
        historical_service=HistoricalDataService(
            tmp_path / "data", instruments={"AAA": instrument}, providers={"fake": Provider()}
        ),
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=timezone.utc),
    )
    workflow = DailyWorkflow(runtime=runtime)

    workflow.data_update(
        run_id="isolated-workflow",
        report_date="2024-04-19",
        symbols=("AAA",),
        bootstrap_days=80,
    )
    workflow.strategy_screen(run_id="isolated-workflow", report_date="2024-04-19")
    workflow.trend_decide(run_id="isolated-workflow", report_date="2024-04-19")

    assert not (output_dir / "state").exists()


def _curated_bars() -> pd.DataFrame:
    closes = [100.0] * 79 + [111.0]
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-30", periods=80, freq="D", tz="UTC"),
            "open": closes,
            "high": [110.0] * 79 + [111.0],
            "low": [90.0] * 79 + [100.0],
            "close": closes,
            "volume": [1000.0] * 80,
            "is_complete": [True] * 80,
        }
    )
    frame["quality_status"] = "CURATED"
    return frame


def test_data_update_cli_returns_nonzero_for_formal_readiness_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output_dir, report_date, run_id = _blocked_data_workspace(tmp_path, observation_only=False)

    class FakeWorkflow:
        def __init__(self, **kwargs: Any) -> None:
            self.runtime = kwargs["runtime"]

        def data_update(self, **kwargs: Any) -> dict[str, Any]:
            return {"stage": "data-update", "run_id": kwargs["run_id"]}

    monkeypatch.setattr("inv_trend.cli.daily.DailyWorkflow", FakeWorkflow)

    assert daily_main([
        "data-update", "--output-dir", str(output_dir), "--run-id", run_id,
        "--report-date", report_date, "--symbol", "AAA",
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["gate_failures"] == [
        {"kind": "data_readiness", "reasons": ["stale_data"], "symbol": "AAA"}
    ]
    assert not (output_dir / "state").exists()


def test_research_observation_data_gate_is_not_a_process_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    output_dir, report_date, run_id = _blocked_data_workspace(tmp_path, observation_only=True)

    class FakeWorkflow:
        def __init__(self, **kwargs: Any) -> None:
            self.runtime = kwargs["runtime"]

        def data_update(self, **kwargs: Any) -> dict[str, Any]:
            return {"stage": "data-update", "run_id": kwargs["run_id"]}

    monkeypatch.setattr("inv_trend.cli.daily.DailyWorkflow", FakeWorkflow)

    assert daily_main([
        "data-update", "--output-dir", str(output_dir), "--run-id", run_id,
        "--report-date", report_date, "--symbol", "AAA", "--research-mode",
    ]) == 0
    assert "gate_failures" not in json.loads(capsys.readouterr().out)


def test_deliver_cli_returns_nonzero_when_outbox_delivery_has_errors(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeWorkflow:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def deliver(self, **kwargs: Any) -> dict[str, Any]:
            return {
                "stage": "deliver",
                "run_id": kwargs["run_id"],
                "delivery": {"notified": 0, "errors": 1, "recovered": 0},
            }

    monkeypatch.setattr("inv_trend.cli.daily.DailyWorkflow", FakeWorkflow)

    assert daily_main([
        "deliver", "--run-id", "delivery-error", "--report-date", "2024-04-19",
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["gate_failures"] == [{"errors": 1, "kind": "delivery"}]


@pytest.mark.parametrize(
    ("argument", "value"),
    (
        ("--run-id", "../outside"),
        ("--run-id", "\u8fd0\u884c-001"),
        ("--run-id", ".."),
        ("--report-date", "2024-04-31"),
        ("--report-date", "2024/04/19"),
    ),
)
def test_stage_cli_rejects_malformed_workspace_identifiers_at_parse_time(
    argument: str,
    value: str,
) -> None:
    """Invalid run locations must be argparse errors, never stage failures."""

    argv = ["commit", "--run-id", "valid-run", "--report-date", "2024-04-19"]
    option_index = argv.index(argument)
    argv[option_index + 1] = value

    with pytest.raises(SystemExit) as exc_info:
        daily_main(argv)

    assert exc_info.value.code == 2


def test_resumed_cli_stages_do_not_reload_unavailable_data_or_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Staged JSON/receipts remain executable after mutable inputs disappear."""

    output_dir, signal_log_dir, report_date, run_id = _staged_evidence(tmp_path)

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("resumed stage must not load current assets, config, or market data")

    # These are the eager compatibility runtime's mutable construction
    # points.  All must remain untouched by the three narrow CLI runtimes.
    monkeypatch.setattr(
        "inv_trend.application.daily.legacy_runtime_adapter.load_detector_asset_configs",
        unavailable,
    )
    monkeypatch.setattr(
        "inv_trend.application.daily.legacy_runtime_adapter.load_resolved_run_config",
        unavailable,
    )
    monkeypatch.setattr(
        "inv_trend.application.daily.legacy_runtime_adapter.HistoricalDataService",
        unavailable,
    )

    unavailable_data_root = tmp_path / "unavailable-data-root"
    unavailable_data_root.write_text("this is deliberately not a data lake", encoding="utf-8")
    common = [
        "--output-dir",
        str(output_dir),
        "--data-root",
        str(unavailable_data_root),
        "--signal-log-dir",
        str(signal_log_dir),
        "--run-id",
        run_id,
        "--report-date",
        report_date,
    ]

    assert daily_main(["commit", *common]) == 0
    workspace = DailyStagingWorkspace(output_dir, report_date, run_id)
    assert workspace.commit_receipt_path.exists()

    assert daily_main(["publish", *common, "--no-html"]) == 0
    assert (
        output_dir
        / "runs"
        / report_date
        / run_id
        / "AAA"
        / "01_canonical"
        / "complete_analysis_result.json"
    ).exists()

    assert daily_main(["deliver", *common]) == 0
    assert workspace.delivery_receipt_path.exists()


def _blocked_data_workspace(
    tmp_path: Path,
    *,
    observation_only: bool,
) -> tuple[Path, str, str]:
    output_dir = tmp_path / "artifacts"
    report_date = "2024-04-19"
    run_id = "blocked-data"
    workspace = DailyStagingWorkspace(output_dir, report_date, run_id)
    workspace.initialize(
        {
            "schema_version": "1",
            "run_id": run_id,
            "report_date": report_date,
            "started_at": "2024-04-19T00:00:00+00:00",
            "timezone": "Asia/Shanghai",
            "timeframe": "D1",
            "symbols": ["AAA"],
            "configuration": {},
            "symbol_metadata": {},
        }
    )
    workspace.write_stage(
        "AAA",
        "data_update",
        {
            "symbol": "AAA",
            "data_readiness": "BLOCKED",
            "blocking_reasons": ["stale_data"],
            "observation_only": observation_only,
        },
    )
    return output_dir, report_date, run_id


def _staged_evidence(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Create real hash-bound evidence before mutable dependencies disappear."""

    class Provider:
        name = "fake"

        def fetch(self, request: Any) -> ProviderResult:
            frame = _curated_bars()
            timestamps = pd.to_datetime(frame["timestamp"], utc=True)
            return ProviderResult(
                frame.loc[
                    (timestamps >= pd.Timestamp(request.start))
                    & (timestamps <= pd.Timestamp(request.end))
                ].copy(),
                request.instrument.source_symbol,
                self.name,
                "test",
            )

    instrument = InstrumentConfig(
        symbol="AAA",
        source_symbol="AAA",
        instrument_id="AAA.TEST.SPOT",
        asset_class="crypto",
        market="test",
        venue="test",
        instrument_type="spot",
        quote_currency="USD",
        currency="USD",
        timezone="UTC",
        session_timezone="UTC",
        session="24x7",
        primary_source="fake",
        earliest_valid_date="2020-01-01",
        adjustment_policy="none",
        adjustment_method="none",
    )
    output_dir = tmp_path / "artifacts"
    signal_log_dir = tmp_path / "signal-logs"
    runtime = PreCommitDailyRuntimeAdapter(
        data_root=tmp_path / "data",
        output_dir=output_dir,
        signal_log_dir=signal_log_dir,
        assets={
            "AAA": AssetConfig(
                symbol="AAA",
                instrument="AAA_TEST",
                market=Market.CRYPTO,
                data_source="fake",
                timeframes=("D1",),
            )
        },
        historical_service=HistoricalDataService(
            tmp_path / "data",
            instruments={"AAA": instrument},
            providers={"fake": Provider()},
        ),
        now=lambda: datetime(2024, 4, 19, 12, tzinfo=timezone.utc),
    )
    workflow = DailyWorkflow(runtime=runtime)
    report_date = "2024-04-19"
    run_id = "resumed-cli-001"
    workflow.data_update(
        run_id=run_id,
        report_date=report_date,
        symbols=("AAA",),
        bootstrap_days=80,
    )
    workflow.strategy_screen(run_id=run_id, report_date=report_date)
    workflow.trend_decide(run_id=run_id, report_date=report_date)
    return output_dir, signal_log_dir, report_date, run_id
