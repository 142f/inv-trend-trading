"""Dependency contracts for the modular D1 workflow.

The protocols describe what the application needs without importing a
particular data-lake, SQLite, notification, or reporting adapter.  They are
kept intentionally small and structural so existing adapters can participate
during the compatibility migration.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from inv_trend.core.signals import SignalEvent

if TYPE_CHECKING:
    from .artifact_publication import DailyArtifactPublication


@runtime_checkable
class MarketDataPort(Protocol):
    """Read and refresh a versioned market-data series."""

    lake: Any

    def instrument(self, symbol: str) -> Any:
        """Resolve a configured instrument."""

    def ingest(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        *,
        retries: int = 3,
    ) -> Any:
        """Ingest and publish a new dataset version."""

    def update(
        self,
        symbol: str,
        timeframe: str,
        *,
        end: datetime | None = None,
        retries: int = 3,
    ) -> Any:
        """Refresh the current dataset version."""

    def load_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        adjusted: bool = True,
        *,
        completed_only: bool = True,
        allow_research: bool = False,
    ) -> Any:
        """Load the completed, strategy-visible bars for a dataset version."""

    def load_bars_version(
        self,
        symbol: str,
        timeframe: str,
        dataset_version: str,
        start: datetime | None = None,
        end: datetime | None = None,
        adjusted: bool = True,
        *,
        completed_only: bool = True,
        allow_research: bool = False,
    ) -> Any:
        """Load one immutable, explicitly recorded dataset version.

        Implementations must not replace ``dataset_version`` with the mutable
        current pointer.  A mismatch is a failed stage hand-off.
        """


@runtime_checkable
class LineagePort(Protocol):
    """Verify that an input frame belongs to the published data version."""

    def verify_current(
        self,
        market_data: MarketDataPort,
        symbol: str,
        timeframe: str,
        *,
        bars: Any,
    ) -> Any:
        """Return the verified lineage summary for a current dataset."""


@runtime_checkable
class FreshnessPort(Protocol):
    """Evaluate whether an instrument's latest completed D1 bar is current."""

    def evaluate(self, instrument: Any, latest_complete_bar: Any, *, now: datetime) -> Any:
        """Return a serializable freshness verdict."""

    def expected_date(self, instrument: Any, *, now: datetime) -> Any:
        """Return the date that a fresh completed D1 bar should cover."""


@runtime_checkable
class DailyStateRepositoryPort(Protocol):
    """Transactional signal, cursor, run-accounting, and outbox state."""

    def start_run(self, run_id: str, started_at: str) -> None:
        """Open an auditable daily run."""

    def ensure_run(self, run_id: str, started_at: str) -> bool:
        """Open a run if absent and return whether it was newly created."""

    def get_or_create_session_anchor(
        self,
        instrument_id: str,
        base_timeframe: str,
        first_complete_time: str,
    ) -> str:
        """Return a stable D1 session anchor."""

    def load_session_anchor(self, instrument_id: str, base_timeframe: str) -> str | None:
        """Read an existing stable grouping anchor without mutating state."""

    def load_cursor(
        self,
        instrument_id: str,
        timeframe: str,
        strategy_version: str,
    ) -> str | None:
        """Return the latest committed cursor position."""

    def commit_events_and_cursor(
        self,
        events: list[SignalEvent],
        *,
        run_id: str,
        instrument_id: str,
        timeframe: str,
        strategy_version: str,
        last_signal_time: str,
        enqueue_notifications: bool = True,
        notification_event_ids: Collection[str] | None = None,
        notification_signal_ids: Collection[str] | None = None,
    ) -> tuple[list[SignalEvent], int]:
        """Persist events/cursor; enqueue selected execution-decision IDs.

        ``notification_signal_ids`` is a deprecated compatibility alias.
        """

    def commit_daily_run(
        self,
        *,
        run_id: str,
        started_at: str,
        finished_at: str,
        commits: list[Mapping[str, Any]],
        records: list[dict[str, Any]],
    ) -> tuple[list[SignalEvent], Mapping[str, int]]:
        """Atomically persist every instrument in one completed daily run."""

    def record_instrument(self, run_id: str, row: Mapping[str, Any]) -> None:
        """Store the complete instrument audit payload for a run."""

    def finish_run(
        self,
        run_id: str,
        completed_at: str,
        summary: Mapping[str, Any],
    ) -> None:
        """Close the run with its aggregate outcome."""

    def interrupt_run(self, run_id: str, completed_at: str) -> None:
        """Close an interrupted run."""

    def pending_notifications(self, run_id: str | None = None) -> list[SignalEvent]:
        """Return pending or failed outbox events in delivery order."""

    def mark_notified(self, signal_id: str, notified_at: str | None = None) -> None:
        """Mark an outbox event delivered."""

    def mark_delivery_error(self, signal_id: str, error: str) -> None:
        """Record a delivery failure for later retry."""


@runtime_checkable
class EligibilityPort(Protocol):
    """Optional execution-risk gate for a candidate signal."""

    def evaluate(
        self,
        *,
        signal: Any,
        bars: Any,
        asset: Any,
        account: Any = None,
        backtest_validated: bool = False,
        execution_ready: bool = False,
        require_account: bool = False,
    ) -> Any:
        """Evaluate whether a candidate may be executed."""


@runtime_checkable
class ArtifactPublisherPort(Protocol):
    """Publish canonical daily JSON and its derived presentation artifacts."""

    def publish(
        self,
        snapshot: Mapping[str, Any],
        *,
        render_html: bool = True,
    ) -> "DailyArtifactPublication":
        """Atomically publish a complete daily run artifact set."""


@runtime_checkable
class DailyReportRendererPort(Protocol):
    """Render canonical complete-analysis JSON into presentation-only HTML.

    The filesystem publisher owns atomic writes and artifact layout, while a
    renderer owns only the transformation from already-authoritative JSON to
    HTML.  Keeping the two ports separate prevents a storage adapter from
    reaching back into the observability package.
    """

    def render_complete_analysis(self, complete_result: Mapping[str, Any]) -> str:
        """Render one canonical ``complete_analysis_result.json`` payload."""

    def render_complete_analyses(
        self,
        complete_results: Mapping[str, Mapping[str, Any]],
    ) -> str:
        """Render the flat compatibility report from canonical payloads."""


@runtime_checkable
class NotificationPort(Protocol):
    """Deliver a committed formal execution-decision event."""

    def notify(self, signal: SignalEvent) -> None:
        """Deliver one deduplicated execution event projected as SignalEvent."""


@runtime_checkable
class DailyWorkflowRuntimePort(Protocol):
    """Composition-root dependencies consumed by :class:`DailyWorkflow`.

    It deliberately exposes named ports rather than a legacy service object.
    The normal CLI builds an adapter around the pre-existing implementation;
    tests and future deployments can provide a native runtime without making
    the workflow know about SQLite, a data lake, or report renderer classes.
    """

    output_dir: Any
    database_path: Any
    signal_log_dir: Any
    assets: Mapping[str, Any]
    market_data: MarketDataPort
    state_repository: DailyStateRepositoryPort
    data_update_stage: Any
    artifact_publisher: ArtifactPublisherPort
    notification_port: NotificationPort
    execution_context: Any
    daily_checks_config: Any
    trend_decision_config: Any

    def now(self) -> datetime:
        """Return the workflow clock value."""

    def select_assets(self, symbols: Iterable[str] | None) -> tuple[Any, ...]:
        """Return configured D1 assets in stable order."""


__all__ = [
    "ArtifactPublisherPort",
    "DailyReportRendererPort",
    "DailyStateRepositoryPort",
    "EligibilityPort",
    "FreshnessPort",
    "LineagePort",
    "MarketDataPort",
    "NotificationPort",
    "DailyWorkflowRuntimePort",
]
