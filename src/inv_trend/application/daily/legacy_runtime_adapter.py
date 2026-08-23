"""Adapter that lets the new workflow reuse the established D1 composition root.

The adapter is intentionally the only new-D1 module that imports the historical
``DailyMarketScanService``.  It gives the workflow named ports and keeps the
old class available as a backwards-compatible public facade during migration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any, Callable, Iterable, Mapping

from inv_trend.adapters.daily.composition import create_daily_run_artifact_writer
from inv_trend.adapters.daily.data_lineage import CurrentLineageAdapter
from inv_trend.adapters.detector.freshness import FreshnessPolicy
from inv_trend.adapters.detector.models import AssetConfig
from inv_trend.core.signals import SignalEvent
from inv_trend.data import HistoricalDataService, create_default_providers

from ..asset_config import load_detector_asset_configs
from ..daily_pipeline import DailyMarketScanService
from ..strategy_config import DailyChecksConfig, TrendDecisionConfig, load_resolved_run_config
from .artifact_publication import ArtifactPublicationService
from .data_update import DailyDataUpdateService


class ReadOnlyDailyStateAdapter:
    """Read cursor context without creating or migrating a SQLite database.

    Stand-alone data, screening, and decision stages may use an existing
    cursor/session anchor to reproduce a normal scan.  They must never turn a
    missing ``signals.sqlite3`` into state simply because an operator is
    preparing evidence.  This adapter opens the database in SQLite's strict
    read-only URI mode only when the file already exists; an absent or older
    store is treated as having no committed cursor/anchor.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load_cursor(
        self,
        instrument_id: str,
        timeframe: str,
        strategy_version: str,
    ) -> str | None:
        return self._value(
            """SELECT last_signal_time FROM scan_cursors
            WHERE instrument_id=? AND timeframe=? AND strategy_version=?""",
            (instrument_id, timeframe, strategy_version),
        )

    def load_session_anchor(self, instrument_id: str, base_timeframe: str) -> str | None:
        return self._value(
            """SELECT anchor_time FROM strategy_session_anchors
            WHERE instrument_id=? AND base_timeframe=?""",
            (instrument_id, base_timeframe),
        )

    def _value(self, query: str, parameters: tuple[str, ...]) -> str | None:
        # Avoid even asking SQLite to open a URI if there is no state file.
        # In particular, do not create ``state/`` as a side effect of a
        # read-only stage command.
        if not self.path.is_file():
            return None
        try:
            uri = f"{self.path.resolve().as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            try:
                connection.execute("PRAGMA query_only=ON")
                row = connection.execute(query, parameters).fetchone()
            finally:
                connection.close()
        except sqlite3.Error:
            # A pre-schema historical file is equivalent to no available
            # cursor context.  Commit remains the only stage that can create
            # or migrate the current state schema.
            return None
        return None if row is None else str(row[0])


class PreCommitDailyRuntimeAdapter:
    """Compose read-only daily stages without constructing SQLite state.

    This is intentionally separate from :class:`LegacyDailyRuntimeAdapter`.
    The legacy service initializes ``SQLiteDailySignalRepository`` in its
    constructor, which is correct for ``run``/``commit``/``deliver`` but not
    for independently executable evidence stages.
    """

    def __init__(
        self,
        *,
        data_root: str | Path = "data",
        output_dir: str | Path = "outputs/daily_market_scan",
        database_path: str | Path | None = None,
        signal_log_dir: str | Path = "logs/signals",
        assets: Mapping[str, AssetConfig] | None = None,
        historical_service: HistoricalDataService | None = None,
        freshness_policy: FreshnessPolicy | None = None,
        provider_timeout: int = 10,
        provider_retries: int = 1,
        refresh_data: bool = True,
        daily_checks_config: DailyChecksConfig | None = None,
        trend_decision_config: TrendDecisionConfig | None = None,
        execution_context: Any = None,
        strategy_config_path: str | Path | None = None,
        now: Callable[[], datetime] | None = None,
        # Accepted only so this read-only composition root can be built from
        # the complete legacy service argument set.  The values are purposely
        # not touched before the explicit commit/deliver boundary.
        signal_repository: Any = None,
        notifier: Any = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.output_dir = Path(output_dir)
        self.database_path = Path(database_path or self.output_dir / "state" / "signals.sqlite3")
        self.signal_log_dir = Path(signal_log_dir)
        self.assets = dict(assets or load_detector_asset_configs())
        self._historical_service = historical_service
        self._provider_timeout = provider_timeout
        self._provider_retries = provider_retries
        self._freshness_policy = freshness_policy or FreshnessPolicy()
        self._refresh_data = refresh_data
        resolved_config = load_resolved_run_config(strategy_config_path)
        self.daily_checks_config = daily_checks_config or resolved_config.daily_checks
        self.trend_decision_config = trend_decision_config or resolved_config.trend_decision
        self.execution_context = execution_context
        self._state_repository = ReadOnlyDailyStateAdapter(self.database_path)
        self._data_update_stage: DailyDataUpdateService | None = None
        self._artifact_publisher: ArtifactPublicationService | None = None
        self._now = now or (lambda: datetime.now(timezone.utc))

    @property
    def market_data(self) -> HistoricalDataService:
        if self._historical_service is None:
            self._historical_service = HistoricalDataService(
                self.data_root,
                providers=create_default_providers(
                    timeout=self._provider_timeout,
                    retries=self._provider_retries,
                ),
            )
        return self._historical_service

    @property
    def state_repository(self) -> ReadOnlyDailyStateAdapter:
        return self._state_repository

    @property
    def data_update_stage(self) -> DailyDataUpdateService:
        if self._data_update_stage is None:
            self._data_update_stage = DailyDataUpdateService(
                self.market_data,
                freshness_policy=self._freshness_policy,
                lineage_port=CurrentLineageAdapter(),
                provider_retries=self._provider_retries,
                refresh_data=self._refresh_data,
            )
        return self._data_update_stage

    @property
    def artifact_publisher(self) -> Any:
        # Publication is a filesystem-only derivative of an already committed
        # snapshot.  It must not construct SQLite merely to render JSON/HTML/
        # CSV, so it is intentionally available from the read-only runtime.
        if self._artifact_publisher is None:
            self._artifact_publisher = ArtifactPublicationService(
                create_daily_run_artifact_writer(
                    self.output_dir,
                    signal_log_root=self.signal_log_dir,
                )
            )
        return self._artifact_publisher

    @property
    def notification_port(self) -> Any:
        raise RuntimeError("notification delivery requires the explicit deliver stage runtime")

    def now(self) -> datetime:
        return self._now()

    def select_assets(self, symbols: Iterable[str] | None) -> tuple[AssetConfig, ...]:
        d1_assets = {
            name: asset for name, asset in self.assets.items() if "D1" in asset.timeframes
        }
        if symbols is None:
            return tuple(d1_assets[name] for name in sorted(d1_assets))
        wanted = tuple(dict.fromkeys(str(symbol).upper() for symbol in symbols))
        unknown = sorted(set(wanted) - set(d1_assets))
        if unknown:
            raise ValueError(f"symbols are not configured for D1: {unknown}")
        return tuple(d1_assets[symbol] for symbol in wanted)

    def signal_events(
        self,
        instrument_id: str,
        asset: AssetConfig,
        analysis: Mapping[str, Any],
        dataset_version: str,
        detected_at: str,
        refresh_failed: bool,
        strategy_version: str,
    ) -> list[SignalEvent]:
        # A pre-commit runtime is never valid for commit, but retaining this
        # method keeps the port structurally complete and makes accidental use
        # fail at the stage boundary rather than silently changing semantics.
        raise RuntimeError("signal persistence requires the explicit commit stage runtime")


class DeferredStateDailyRuntimeAdapter:
    """Use read-only ports until the workflow explicitly enters ``commit``.

    This adapter is the composition root for the recommended CLI flow.  It
    keeps data-update, strategy-screen, trend-decide, and publish free of
    SQLite creation/migration.  Once :meth:`activate_stateful` is called by
    the workflow's commit/deliver boundary, it composes the historical runtime
    using the exact same assets, market-data service, resolved configuration,
    and clock that produced the staged evidence.

    The legacy adapter remains available for direct in-process compatibility;
    this class is deliberately opt-in at the CLI boundary so no existing
    constructor semantics are silently changed.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._precommit = PreCommitDailyRuntimeAdapter(**kwargs)
        self._stateful_kwargs = dict(kwargs)
        self._stateful: LegacyDailyRuntimeAdapter | None = None
        self._delivery_repository: Any | None = None
        self._delivery_notifier: Any | None = None

    @property
    def output_dir(self) -> Path:
        return self._precommit.output_dir

    @property
    def database_path(self) -> Path:
        return self._precommit.database_path

    @property
    def signal_log_dir(self) -> Path:
        return self._precommit.signal_log_dir

    @property
    def assets(self) -> Mapping[str, Any]:
        return self._precommit.assets

    @property
    def market_data(self) -> HistoricalDataService:
        return self._precommit.market_data

    @property
    def state_repository(self) -> Any:
        if self._stateful is not None:
            return self._stateful.state_repository
        if self._delivery_repository is not None:
            return self._delivery_repository
        return self._precommit.state_repository

    @property
    def data_update_stage(self) -> DailyDataUpdateService:
        return self._precommit.data_update_stage

    @property
    def artifact_publisher(self) -> ArtifactPublicationService:
        return self._precommit.artifact_publisher

    @property
    def notification_port(self) -> Any:
        if self._delivery_notifier is not None:
            return self._delivery_notifier
        return self._require_stateful().notification_port

    @property
    def execution_context(self) -> Any:
        return self._precommit.execution_context

    @property
    def daily_checks_config(self) -> DailyChecksConfig:
        return self._precommit.daily_checks_config

    @property
    def trend_decision_config(self) -> TrendDecisionConfig:
        return self._precommit.trend_decision_config

    def now(self) -> datetime:
        return self._precommit.now()

    def select_assets(self, symbols: Iterable[str] | None) -> tuple[AssetConfig, ...]:
        return self._precommit.select_assets(symbols)

    def activate_stateful(self) -> None:
        """Create stateful adapters only at the explicit mutation boundary."""

        if self._stateful is not None:
            return
        # Ensure stateful legacy projection receives the exact dependencies
        # used by read stages.  In particular, a second current-data service
        # or a reloaded config cannot alter event IDs during commit.
        kwargs = dict(self._stateful_kwargs)
        if self._delivery_repository is not None:
            kwargs["signal_repository"] = self._delivery_repository
        if self._delivery_notifier is not None:
            kwargs["notifier"] = self._delivery_notifier
        kwargs.update(
            {
                "data_root": self._precommit.data_root,
                "output_dir": self._precommit.output_dir,
                "database_path": self._precommit.database_path,
                "signal_log_dir": self._precommit.signal_log_dir,
                "assets": self._precommit.assets,
                "historical_service": self._precommit.market_data,
                "freshness_policy": self._precommit._freshness_policy,
                "daily_checks_config": self._precommit.daily_checks_config,
                "trend_decision_config": self._precommit.trend_decision_config,
                "execution_context": self._precommit.execution_context,
                "now": self._precommit.now,
            }
        )
        self._stateful = LegacyDailyRuntimeAdapter(**kwargs)

    def activate_delivery(self) -> None:
        """Compose only the outbox/state and notifier ports for ``deliver``.

        Delivery does not read bars, resolve assets, or verify a mutable data
        lake.  Avoiding the historical service here keeps an explicit
        ``turtle-daily deliver`` command limited to the state/outbox scope.
        """

        if self._stateful is not None or self._delivery_repository is not None:
            return
        repository = self._stateful_kwargs.get("signal_repository")
        notifier = self._stateful_kwargs.get("notifier")
        if repository is None:
            from inv_trend.adapters.detector.storage.daily_signal_repository import (
                SQLiteDailySignalRepository,
            )

            repository = SQLiteDailySignalRepository(self.database_path)
        if notifier is None:
            from inv_trend.adapters.detector.alerts.daily_notifier import LogNotifier

            notifier = LogNotifier(self.signal_log_dir)
        self._delivery_repository = repository
        self._delivery_notifier = notifier

    def signal_events(
        self,
        instrument_id: str,
        asset: Any,
        analysis: Mapping[str, Any],
        dataset_version: str,
        detected_at: str,
        refresh_failed: bool,
        strategy_version: str,
    ) -> list[SignalEvent]:
        return self._require_stateful().signal_events(
            instrument_id,
            asset,
            analysis,
            dataset_version,
            detected_at,
            refresh_failed,
            strategy_version,
        )

    def _require_stateful(self) -> "LegacyDailyRuntimeAdapter":
        if self._stateful is None:
            raise RuntimeError(
                "stateful daily ports are available only during commit or deliver"
            )
        return self._stateful


class LegacyDailyRuntimeAdapter:
    """Adapt the existing service's constructed adapters to workflow ports."""

    def __init__(
        self,
        *,
        service: DailyMarketScanService | None = None,
        **kwargs: Any,
    ) -> None:
        if service is not None and kwargs:
            raise TypeError("service cannot be combined with legacy constructor arguments")
        self._service = service or DailyMarketScanService(**kwargs)
        self._artifact_publisher = ArtifactPublicationService(self._service.artifact_writer)

    @property
    def output_dir(self) -> Path:
        return self._service.output_dir

    @property
    def database_path(self) -> Path:
        return self._service.database_path

    @property
    def signal_log_dir(self) -> Path:
        return self._service.signal_log_dir

    @property
    def assets(self) -> Mapping[str, Any]:
        return self._service.assets

    @property
    def market_data(self) -> Any:
        return self._service.historical_service

    @property
    def state_repository(self) -> Any:
        return self._service.signal_repository

    @property
    def data_update_stage(self) -> Any:
        # ``--scan-only`` and the public ``refresh_data`` attribute remain
        # mutable legacy controls.  Copy their current value at the stage
        # boundary so a caller changing it between runs sees the same result
        # as before the workflow delegation.
        self._service.data_update_service.refresh_data = self._service.refresh_data
        return self._service.data_update_service

    @property
    def artifact_publisher(self) -> ArtifactPublicationService:
        return self._artifact_publisher

    @property
    def notification_port(self) -> Any:
        return self._service.notifier

    @property
    def execution_context(self) -> Any:
        return self._service.execution_context

    @property
    def daily_checks_config(self) -> Any:
        return self._service.daily_checks_config

    @property
    def trend_decision_config(self) -> Any:
        return self._service.trend_decision_config

    def now(self) -> datetime:
        return self._service._now()

    def select_assets(self, symbols: Iterable[str] | None) -> tuple[Any, ...]:
        return self._service._selected_assets(symbols)

    def signal_events(
        self,
        instrument_id: str,
        asset: Any,
        analysis: Mapping[str, Any],
        dataset_version: str,
        detected_at: str,
        refresh_failed: bool,
        strategy_version: str,
    ) -> list[SignalEvent]:
        return DailyMarketScanService._signal_events(
            instrument_id,
            asset,
            analysis,
            dataset_version,
            detected_at,
            refresh_failed,
            strategy_version,
        )


__all__ = [
    "DeferredStateDailyRuntimeAdapter",
    "LegacyDailyRuntimeAdapter",
    "PreCommitDailyRuntimeAdapter",
    "ReadOnlyDailyStateAdapter",
]
