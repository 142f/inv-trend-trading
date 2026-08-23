"""Minimal composition roots for resumed, side-effect-scoped D1 stages.

The evidence stages deliberately use the richer read-only runtime because they
need configured assets, the pinned strategy configuration, and the data lake.
Once those results have been staged, however, commit, publication, and
delivery must not reconstruct that mutable world.  These adapters expose only
the ports each later stage is allowed to consume.

All concrete I/O implementations are imported lazily.  In particular, merely
constructing a resumed stage runtime never loads the asset YAML, strategy YAML,
or :class:`~inv_trend.data.HistoricalDataService`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


class _MinimalStageRuntimeAdapter:
    """Common paths, clock, and fail-closed unsupported workflow ports."""

    _stage_name = "resumed"

    def __init__(
        self,
        *,
        output_dir: str | Path = "outputs/daily_market_scan",
        database_path: str | Path | None = None,
        signal_log_dir: str | Path = "logs/signals",
        now: Callable[[], datetime] | None = None,
        **_unused: Any,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.database_path = Path(
            database_path or self.output_dir / "state" / "signals.sqlite3"
        )
        self.signal_log_dir = Path(signal_log_dir)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def now(self) -> datetime:
        """Return a UTC-capable clock for commit/delivery audit timestamps."""

        return self._now()

    @property
    def assets(self) -> Mapping[str, Any]:
        return self._unsupported("configured assets")

    @property
    def market_data(self) -> Any:
        return self._unsupported("market data")

    @property
    def data_update_stage(self) -> Any:
        return self._unsupported("data-update service")

    @property
    def execution_context(self) -> Any:
        return self._unsupported("execution context")

    @property
    def daily_checks_config(self) -> Any:
        return self._unsupported("daily screening configuration")

    @property
    def trend_decision_config(self) -> Any:
        return self._unsupported("trend-decision configuration")

    def select_assets(self, _symbols: Any = None) -> tuple[Any, ...]:
        return self._unsupported("configured assets")

    def _unsupported(self, port: str) -> Any:
        raise RuntimeError(
            f"{self._stage_name} stage runtime does not provide {port}; "
            "rerun an evidence stage to use mutable data/configuration"
        )


class CommitStageRuntimeAdapter(_MinimalStageRuntimeAdapter):
    """Compose only SQLite state for the explicit ``commit`` stage."""

    _stage_name = "commit"

    def __init__(self, *, state_repository: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._state_repository = state_repository

    @property
    def state_repository(self) -> Any:
        if self._state_repository is None:
            # Keep the only mutation-capable adapter behind the explicit
            # commit boundary.  This import has no dependency on the data
            # lake, asset configuration, or strategy configuration.
            from inv_trend.adapters.detector.storage.daily_signal_repository import (
                SQLiteDailySignalRepository,
            )

            self._state_repository = SQLiteDailySignalRepository(self.database_path)
        return self._state_repository

    @property
    def artifact_publisher(self) -> Any:
        return self._unsupported("artifact publisher")

    @property
    def notification_port(self) -> Any:
        return self._unsupported("notification port")

    def activate_stateful(self) -> None:
        """Mark the workflow boundary without constructing unrelated ports.

        The state repository remains lazy so a malformed staged hand-off is
        rejected before SQLite is opened or migrated.
        """


class PublishStageRuntimeAdapter(_MinimalStageRuntimeAdapter):
    """Compose only the filesystem/HTML publication port for ``publish``."""

    _stage_name = "publish"

    def __init__(self, *, artifact_publisher: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._artifact_publisher = artifact_publisher

    @property
    def artifact_publisher(self) -> Any:
        if self._artifact_publisher is None:
            # The outer composition helper pairs a canonical-JSON renderer
            # with the filesystem writer.  It deliberately needs neither a
            # state store nor current market/configuration inputs.
            from inv_trend.adapters.daily.composition import create_daily_run_artifact_writer
            from inv_trend.application.daily.artifact_publication import (
                ArtifactPublicationService,
            )

            self._artifact_publisher = ArtifactPublicationService(
                create_daily_run_artifact_writer(
                    self.output_dir,
                    signal_log_root=self.signal_log_dir,
                )
            )
        return self._artifact_publisher

    @property
    def state_repository(self) -> Any:
        return self._unsupported("state repository")

    @property
    def notification_port(self) -> Any:
        return self._unsupported("notification port")


class DeliveryStageRuntimeAdapter(_MinimalStageRuntimeAdapter):
    """Compose only SQLite outbox state and JSONL notification delivery."""

    _stage_name = "deliver"

    def __init__(
        self,
        *,
        state_repository: Any = None,
        notification_port: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._state_repository = state_repository
        self._notification_port = notification_port

    @property
    def state_repository(self) -> Any:
        if self._state_repository is None:
            from inv_trend.adapters.detector.storage.daily_signal_repository import (
                SQLiteDailySignalRepository,
            )

            self._state_repository = SQLiteDailySignalRepository(self.database_path)
        return self._state_repository

    @property
    def notification_port(self) -> Any:
        if self._notification_port is None:
            from inv_trend.adapters.detector.alerts.daily_notifier import LogNotifier

            self._notification_port = LogNotifier(self.signal_log_dir)
        return self._notification_port

    @property
    def artifact_publisher(self) -> Any:
        return self._unsupported("artifact publisher")

    def activate_delivery(self) -> None:
        """Keep delivery composition lazy until a verified receipt exists."""


__all__ = [
    "CommitStageRuntimeAdapter",
    "DeliveryStageRuntimeAdapter",
    "PublishStageRuntimeAdapter",
]
