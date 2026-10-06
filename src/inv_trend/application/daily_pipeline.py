"""Application orchestration for the D1 daily market scan.

This module owns the use case and returns a result object.  Report rendering is
deliberately left to ``inv_trend.observability`` at the CLI boundary.
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from inv_trend.data import HistoricalDataService, create_default_providers
from inv_trend.core.signals import CORRECTED_STRATEGY_VERSION, SignalEvent
from inv_trend.core.strategy.daily.signals import (
    build_daily_signal_events,
)

from inv_trend.adapters.detector.alerts.daily_notifier import LogNotifier, SignalNotifier
from inv_trend.adapters.daily.data_lineage import CurrentLineageAdapter
from inv_trend.adapters.detector.freshness import FreshnessPolicy
from inv_trend.adapters.detector.models import AssetConfig
from inv_trend.adapters.detector.storage.daily_signal_repository import SQLiteDailySignalRepository
from inv_trend.adapters.daily.composition import create_daily_run_artifact_writer
from .asset_config import select_detector_assets
from .daily.run_contract import DEFAULT_BOOTSTRAP_DAYS, DailyMarketScanResult
from .daily_stages import (
    DailyDataUpdateService,
    ExecutionContext,
    StrategyScreeningService,
    TrendDecisionService,
)
from .strategy_config import (
    DailyChecksConfig,
    TrendDecisionConfig,
    load_resolved_run_config,
)


class DailyMarketScanService:
    def __init__(
        self,
        *,
        data_root: str | Path = "data",
        output_dir: str | Path = "outputs/daily_market_scan",
        database_path: str | Path | None = None,
        signal_log_dir: str | Path = "logs/signals",
        assets: Mapping[str, AssetConfig] | None = None,
        historical_service: HistoricalDataService | None = None,
        signal_repository: SQLiteDailySignalRepository | None = None,
        notifier: SignalNotifier | None = None,
        freshness_policy: FreshnessPolicy | None = None,
        provider_timeout: int = 10,
        provider_retries: int = 1,
        refresh_data: bool = True,
        daily_checks_config: DailyChecksConfig | None = None,
        trend_decision_config: TrendDecisionConfig | None = None,
        execution_context: ExecutionContext | Mapping[str, Any] | None = None,
        strategy_config_path: str | Path | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.output_dir = Path(output_dir)
        self.database_path = Path(database_path or self.output_dir / "state" / "signals.sqlite3")
        self.signal_log_dir = Path(signal_log_dir)
        from .asset_config import load_detector_asset_configs

        self.assets = dict(assets or load_detector_asset_configs())
        self.historical_service = historical_service or HistoricalDataService(
            self.data_root,
            providers=create_default_providers(timeout=provider_timeout, retries=provider_retries),
        )
        self.signal_repository = signal_repository or SQLiteDailySignalRepository(
            self.database_path
        )
        self.notifier = notifier or LogNotifier(self.signal_log_dir)
        self.freshness_policy = freshness_policy or FreshnessPolicy()
        self.provider_retries = provider_retries
        self.refresh_data = refresh_data
        # Omitted configuration now resolves to the versioned canonical
        # strategy document.  An explicit historical application YAML remains
        # accepted by ``load_resolved_run_config`` for compatibility.
        resolved_config = load_resolved_run_config(strategy_config_path)
        self.daily_checks_config = daily_checks_config or resolved_config.daily_checks
        self.trend_decision_config = trend_decision_config or resolved_config.trend_decision
        self.execution_context = execution_context
        self.data_update_service = DailyDataUpdateService(
            self.historical_service,
            freshness_policy=self.freshness_policy,
            lineage_port=CurrentLineageAdapter(),
            provider_retries=self.provider_retries,
            refresh_data=self.refresh_data,
        )
        self.strategy_screening_service = StrategyScreeningService(
            self.daily_checks_config,
            self.trend_decision_config,
        )
        self.trend_decision_service = TrendDecisionService(self.trend_decision_config)
        self.artifact_writer = create_daily_run_artifact_writer(
            self.output_dir,
            signal_log_root=self.signal_log_dir,
        )
        self._now = now or (lambda: datetime.now(timezone.utc))

    def run(
        self,
        *,
        symbols: Iterable[str] | None = None,
        bootstrap_days: int = DEFAULT_BOOTSTRAP_DAYS,
        research_mode: bool = False,
        strategy_version: str = CORRECTED_STRATEGY_VERSION,
        backfill_signals: bool = False,
        chart_bars: int = 180,
        render_html: bool = True,
    ) -> DailyMarketScanResult:
        """Run the staged D1 workflow through this legacy public facade.

        Existing callers keep their ``DailyMarketScanService`` construction
        and return type.  The actual chain is now the one authoritative
        sequence used by the CLI: data update, screening, decision, commit,
        publication, then outbox delivery.
        """

        # Imports are local because this class remains the composition root
        # used by ``LegacyDailyRuntimeAdapter`` itself.
        from .daily.legacy_runtime_adapter import LegacyDailyRuntimeAdapter
        from .daily.workflow import DailyWorkflow

        return DailyWorkflow(runtime=LegacyDailyRuntimeAdapter(service=self)).run(
            symbols=symbols,
            bootstrap_days=bootstrap_days,
            research_mode=research_mode,
            strategy_version=strategy_version,
            backfill_signals=backfill_signals,
            chart_bars=chart_bars,
            render_html=render_html,
        )

    def _selected_assets(self, symbols: Iterable[str] | None) -> tuple[AssetConfig, ...]:
        return select_detector_assets(self.assets, symbols)

    @staticmethod
    def _signal_events(
        instrument_id: str,
        asset: AssetConfig,
        analysis: Mapping[str, Any],
        dataset_version: str,
        detected_at: str,
        refresh_failed: bool,
        strategy_version: str,
    ) -> list[SignalEvent]:
        return build_daily_signal_events(
            analysis,
            instrument_id=instrument_id,
            symbol=asset.symbol,
            market=asset.market.value,
            dataset_version=dataset_version,
            detected_at=detected_at,
            refresh_failed=refresh_failed,
            strategy_version=strategy_version,
        )


__all__ = ["DEFAULT_BOOTSTRAP_DAYS", "DailyMarketScanResult", "DailyMarketScanService"]
