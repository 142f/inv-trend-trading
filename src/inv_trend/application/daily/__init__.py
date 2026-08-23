"""D1 daily-workflow building blocks.

This package is the preferred import surface for the modular daily workflow.
The implementation still lives in the established modules while the migration
is in progress, so these imports intentionally preserve their public identity
and behaviour.
"""

from .artifact_publication import ArtifactPublicationService, DailyArtifactPublication
from .data_update import DataUpdateResult, DataUpdateStage, DailyDataUpdateService
from .notification_delivery import NotificationDeliveryService
from .ports import (
    ArtifactPublisherPort,
    DailyReportRendererPort,
    DailyStateRepositoryPort,
    DailyWorkflowRuntimePort,
    EligibilityPort,
    LineagePort,
    MarketDataPort,
    NotificationPort,
)
from .run_contract import DailyMarketScanResult
from .signal_commit import SignalCommitOutcome, SignalCommitService
from .strategy_screening import (
    ExecutionContext,
    StrategyScreeningResult,
    StrategyScreeningService,
    StrategyScreeningStage,
)
from .trend_decision import (
    TrendDecisionResult,
    TrendDecisionService,
    build_execution_decision_event,
)

__all__ = [
    "ArtifactPublisherPort",
    "ArtifactPublicationService",
    "DailyArtifactPublication",
    "DailyDataUpdateService",
    "DailyRunArtifactWriter",
    "DailyReportRendererPort",
    "DailyStagingWorkspace",
    "DailyWorkflow",
    "DailyWorkflowRuntimePort",
    "DailyStateRepositoryPort",
    "DailyMarketScanResult",
    "DataUpdateResult",
    "DataUpdateStage",
    "EligibilityPort",
    "ExecutionContext",
    "LineagePort",
    "LogNotifier",
    "MarketDataPort",
    "NotificationDeliveryService",
    "NotificationPort",
    "SignalNotifier",
    "SignalCommitOutcome",
    "SignalCommitService",
    "StrategyScreeningResult",
    "StrategyScreeningService",
    "StrategyScreeningStage",
    "TrendDecisionResult",
    "TrendDecisionService",
    "build_execution_decision_event",
]


def __getattr__(name: str):
    """Load workflow-facing modules lazily to keep legacy imports acyclic."""

    if name == "DailyWorkflow":
        from .workflow import DailyWorkflow

        return DailyWorkflow
    if name == "DailyStagingWorkspace":
        from .workspace import DailyStagingWorkspace

        return DailyStagingWorkspace
    if name == "DailyRunArtifactWriter":
        from .artifact_publication import DailyRunArtifactWriter

        return DailyRunArtifactWriter
    if name in {"LogNotifier", "SignalNotifier"}:
        from .notification_delivery import LogNotifier, SignalNotifier

        return {"LogNotifier": LogNotifier, "SignalNotifier": SignalNotifier}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
