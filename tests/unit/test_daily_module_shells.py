from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from inv_trend.adapters.detector.alerts.daily_notifier import LogNotifier as LegacyLogNotifier
from inv_trend.adapters.daily.artifact_publisher import DailyRunArtifactWriter as AdapterArtifactWriter
from inv_trend.application.daily import (
    ArtifactPublicationService,
    DailyDataUpdateService,
    DailyRunArtifactWriter,
    NotificationDeliveryService,
    StrategyScreeningService,
    TrendDecisionService,
)
from inv_trend.application.daily_artifacts import DailyRunArtifactWriter as LegacyArtifactWriter
from inv_trend.application.daily.artifact_publication import (
    DailyArtifactPublication,
    DailyRunArtifactWriter as PublicationModuleWriter,
)
from inv_trend.application.daily_stages import (
    DailyDataUpdateService as LegacyDataUpdateService,
    StrategyScreeningService as LegacyScreeningService,
    TrendDecisionService as LegacyDecisionService,
)
from inv_trend.application.daily_analysis import prepare_daily_analysis as legacy_prepare_daily_analysis
from inv_trend.application.daily_models import StrategyScreeningResult
from inv_trend.application.daily.strategy_screening import _screening_snapshot
from inv_trend.core.signals import SignalEvent
from inv_trend.core.strategy.daily import prepare_daily_analysis


def _event(symbol: str) -> SignalEvent:
    return SignalEvent.create(
        instrument_id=f"{symbol}-spot",
        symbol=symbol,
        timeframe="D1",
        signal_type="SYSTEM1_BREAKOUT",
        direction="LONG",
        signal_time="2026-08-20T00:00:00+00:00",
        detected_at="2026-08-20T01:00:00+00:00",
        trigger_price=100.0,
        reference_value=99.0,
        dataset_version="dataset-v1",
        indicator_name="turtle_20",
    )


class _Repository:
    def __init__(self, current: list[SignalEvent], recoverable: list[SignalEvent]) -> None:
        self.current = current
        self.recoverable = recoverable
        self.notified: list[tuple[str, str | None]] = []
        self.errors: list[tuple[str, str]] = []

    def pending_notifications(self, run_id: str | None = None) -> list[SignalEvent]:
        return list(self.current if run_id is not None else [*self.current, *self.recoverable])

    def mark_notified(self, signal_id: str, notified_at: str | None = None) -> None:
        self.notified.append((signal_id, notified_at))

    def mark_delivery_error(self, signal_id: str, error: str) -> None:
        self.errors.append((signal_id, error))


class _Notifier:
    def __init__(self) -> None:
        self.signal_ids: list[str] = []

    def notify(self, signal: SignalEvent) -> None:
        self.signal_ids.append(signal.signal_id)


def test_daily_package_reexports_existing_stage_and_artifact_implementations() -> None:
    assert DailyDataUpdateService is LegacyDataUpdateService
    assert StrategyScreeningService is LegacyScreeningService
    assert TrendDecisionService is LegacyDecisionService
    assert DailyRunArtifactWriter is LegacyArtifactWriter
    assert DailyRunArtifactWriter is AdapterArtifactWriter
    assert DailyRunArtifactWriter is PublicationModuleWriter
    assert LegacyLogNotifier.__name__ == "LogNotifier"
    assert DailyDataUpdateService.__module__ == "inv_trend.application.daily.data_update"
    assert StrategyScreeningService.__module__ == "inv_trend.application.daily.strategy_screening"
    assert TrendDecisionService.__module__ == "inv_trend.application.daily.trend_decision"
    assert DailyRunArtifactWriter.__module__ == "inv_trend.adapters.daily.artifact_publisher"


def test_core_daily_facade_resolves_the_existing_analysis_function() -> None:
    assert prepare_daily_analysis is legacy_prepare_daily_analysis


def test_event_snapshot_keeps_latest_bar_as_canonical_screening_evidence() -> None:
    snapshot = _screening_snapshot(
        {
            "latest_bar": {"timestamp": "2026-08-20T00:00:00+00:00", "close": 101.5},
            "strategy_checks": {},
            "rule_evaluations": [],
            "signals": [],
        },
        (),
        {"status": "NOT_APPLICABLE"},
        7,
    )
    result = StrategyScreeningResult(
        symbol="BTC",
        instrument_id="BTC.TEST",
        timeframe="D1",
        input_data_hash="data-hash",
        configuration_hash="config-hash",
        dataset_version="v1",
        as_of="2026-08-20T00:00:00+00:00",
        event_snapshots=(snapshot,),
    )

    event = result.to_dict()["event_snapshots"][0]
    assert event["latest_bar"] == {
        "timestamp": "2026-08-20T00:00:00+00:00",
        "close": 101.5,
    }


def test_notification_delivery_reuses_outbox_and_recovery_semantics() -> None:
    current = _event("BTC")
    recovered = _event("ETH")
    repository = _Repository([current], [recovered])
    notifier = _Notifier()
    delivery = NotificationDeliveryService(
        repository,  # type: ignore[arg-type]
        notifier,
        now=lambda: datetime(2026, 8, 20, 2, tzinfo=timezone.utc),
    )

    result = delivery.deliver("run-1")

    assert result == {"notified": 1, "errors": 0, "recovered": 1}
    assert notifier.signal_ids == [current.signal_id, recovered.signal_id]
    assert repository.notified == [
        (current.signal_id, "2026-08-20T02:00:00+00:00"),
        (recovered.signal_id, "2026-08-20T02:00:00+00:00"),
    ]
    assert repository.errors == []


def test_daily_ports_do_not_depend_on_concrete_adapters() -> None:
    source = (
        Path(__file__).parents[2]
        / "src"
        / "inv_trend"
        / "application"
        / "daily"
        / "ports.py"
    ).read_text(encoding="utf-8")

    assert "inv_trend.adapters" not in source
    assert "inv_trend.data" not in source


class _ArtifactPublisher:
    def __init__(self, publication: DailyArtifactPublication) -> None:
        self.publication = publication
        self.calls: list[tuple[dict[str, object], bool]] = []

    def publish(self, snapshot, *, render_html: bool = True) -> DailyArtifactPublication:
        self.calls.append((dict(snapshot), render_html))
        return self.publication


def test_artifact_publication_service_delegates_idempotent_retry_to_port() -> None:
    publication = DailyArtifactPublication(
        run_directory=Path("artifacts/runs/2026-08-20/run-1"),
        compatibility_json=Path("artifacts/2026-08-20.json"),
        compatibility_html=Path("artifacts/2026-08-20.html"),
        complete_results={},
    )
    publisher = _ArtifactPublisher(publication)
    service = ArtifactPublicationService(publisher)  # type: ignore[arg-type]
    snapshot = {"report_date": "2026-08-20", "run_id": "run-1"}

    assert service.publish(snapshot) is publication
    assert service.resume(snapshot, render_html=False) is publication
    assert publisher.calls == [(snapshot, True), (snapshot, False)]
