"""Application orchestration for the D1 daily market scan.

This module owns the use case and returns a result object.  Report rendering is
deliberately left to ``inv_trend.observability`` at the CLI boundary.
"""

from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from uuid import uuid4

from inv_trend.data import HistoricalDataService, create_default_providers
from inv_trend.core.signals import CORRECTED_STRATEGY_VERSION, SignalEvent
from inv_trend.core.strategy.daily.signals import build_daily_signal_events

from inv_trend.adapters.detector.alerts.daily_notifier import LogNotifier, SignalNotifier
from inv_trend.adapters.daily.data_lineage import CurrentLineageAdapter
from inv_trend.adapters.detector.freshness import FreshnessPolicy
from inv_trend.adapters.detector.indicators.daily_signals import (
    normalize_completed_daily_bars,
)
from inv_trend.adapters.detector.models import AssetConfig
from inv_trend.adapters.detector.storage.daily_signal_repository import SQLiteDailySignalRepository
from inv_trend.adapters.daily.composition import create_daily_run_artifact_writer
from .daily_analysis import (
    build_breakout_assessments,
    build_instrument_report_bundle,
)
from .daily.run_contract import BEIJING, DEFAULT_BOOTSTRAP_DAYS, DailyMarketScanResult
from .daily.trend_decision import build_execution_decision_event
from .daily_models import BreakoutAssessment, MarketAssessment
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
        self.signal_repository = signal_repository or SQLiteDailySignalRepository(self.database_path)
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
        publication, then outbox delivery.  The retained ``_run_legacy`` is
        deliberately private during the evidence-backed migration window.
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

    def _run_legacy(
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
        if bootstrap_days < 1:
            raise ValueError("bootstrap_days must be positive")
        if strategy_version != CORRECTED_STRATEGY_VERSION:
            raise ValueError("only the executable strategy version 'corrected-v2' is supported")
        if chart_bars < 1:
            raise ValueError("chart_bars must be positive")
        selected = self._selected_assets(symbols)
        started_at = _aware_utc(self._now())
        run_id = uuid4().hex
        self.signal_repository.start_run(run_id, started_at.isoformat())
        rows: list[dict[str, Any]] = []
        all_new: list[SignalEvent] = []
        delivery = {"notified": 0, "errors": 0, "recovered": 0}
        try:
            for asset in selected:
                row, new_events = self._run_symbol(
                    asset, run_id=run_id, started_at=started_at, bootstrap_days=bootstrap_days,
                    research_mode=research_mode, strategy_version=strategy_version,
                    backfill_signals=backfill_signals, chart_bars=chart_bars,
                )
                rows.append(row)
                all_new.extend(new_events)
                self.signal_repository.record_instrument(run_id, row)
            delivery = self._deliver_pending(run_id)
        except KeyboardInterrupt:
            self.signal_repository.interrupt_run(run_id, _aware_utc(self._now()).isoformat())
            raise
        finished_at = _aware_utc(self._now())
        summary = _summary(rows, len(all_new), delivery)
        self.signal_repository.finish_run(run_id, finished_at.isoformat(), summary)
        report_date = started_at.astimezone(BEIJING).date().isoformat()
        snapshot = {
            "schema_version": "4", "report_schema_version": "4",
            "run_id": run_id, "report_date": report_date,
            "started_at": started_at.isoformat(), "finished_at": finished_at.isoformat(),
            "timezone": "Asia/Shanghai", "timeframe": "D1",
            "configuration": {
                "bootstrap_days": bootstrap_days, "research_mode": research_mode,
                "strategy_version": strategy_version, "backfill_signals": backfill_signals,
                "chart_bars": chart_bars, "formal_quality_statuses": ["CURATED"],
                "symbols": [asset.symbol for asset in selected],
                "daily_checks": asdict(self.daily_checks_config),
                "trend_decision": asdict(self.trend_decision_config),
            },
            "symbols": rows, "summary": summary,
        }
        publication = self.artifact_writer.publish(snapshot, render_html=render_html)
        return DailyMarketScanResult(
            publication.compatibility_json,
            self.database_path,
            self.signal_log_dir,
            snapshot,
            publication,
        )

    def _selected_assets(self, symbols: Iterable[str] | None) -> tuple[AssetConfig, ...]:
        d1_assets = {name: asset for name, asset in self.assets.items() if "D1" in asset.timeframes}
        if symbols is None:
            return tuple(d1_assets[name] for name in sorted(d1_assets))
        wanted = tuple(dict.fromkeys(str(symbol).upper() for symbol in symbols))
        unknown = sorted(set(wanted) - set(d1_assets))
        if unknown:
            raise ValueError(f"symbols are not configured for D1: {unknown}")
        return tuple(d1_assets[symbol] for symbol in wanted)

    def _run_symbol(
        self, asset: AssetConfig, *, run_id: str, started_at: datetime, bootstrap_days: int,
        research_mode: bool, strategy_version: str, backfill_signals: bool, chart_bars: int,
    ) -> tuple[dict[str, Any], list[SignalEvent]]:
        instrument = self.historical_service.instrument(asset.symbol)
        row: dict[str, Any] = {
            "symbol": asset.symbol, "instrument": asset.instrument, "instrument_id": instrument.instrument_id,
            "market": asset.market.value, "timeframe": "D1",
            "provider": {"primary": instrument.primary_source, "fallbacks": list(instrument.fallback_sources)},
        }
        self.data_update_service.refresh_data = self.refresh_data
        data_stage = self.data_update_service.run(
            asset,
            started_at=started_at,
            bootstrap_days=bootstrap_days,
            research_mode=research_mode,
        )
        data_result = data_stage.result
        row["update"] = dict(data_stage.update)
        row["data"] = {
            "dataset_version": data_result.dataset_version,
            "quality_statuses": list(data_result.quality.get("statuses", ())),
            "completed_bars": len(data_stage.bars) if data_stage.bars is not None else 0,
            "latest_complete_bar": data_result.latest_complete_d1,
        }
        row["freshness"] = dict(data_result.freshness)
        row["data_update_result"] = data_result.to_dict()

        formal_eligible = data_result.formal_ready and not research_mode
        if not formal_eligible and not research_mode:
            screening_stage = self.strategy_screening_service.screen(
                data_result, asset, bars=None
            )
            decision = self.trend_decision_service.decide(data_result, screening_stage.result)
            _attach_stage_results(row, data_result.to_dict(), screening_stage.result.to_dict(), decision.to_dict(), ())
            row["scan"] = {"status": "not_run", "reason": "data_readiness_gate_blocked"}
            row["alert_policy"] = {
                "formal_eligible": False,
                "research_mode": False,
                "suppressed_reason": ",".join(data_result.blocking_reasons) or "data_not_ready",
            }
            row["signals_detected"] = 0
            row["signals"] = []
            row["signals_new"] = 0
            row["signals_duplicate"] = 0
            row["cursor"] = {
                "strategy_version": strategy_version,
                "before": self.signal_repository.load_cursor(instrument.instrument_id, "D1", strategy_version),
                "after": self.signal_repository.load_cursor(instrument.instrument_id, "D1", strategy_version),
                "bars_replayed": 0,
            }
            row["run_status"] = _data_gate_status(data_stage, research_mode=False)
            return row, []

        try:
            if data_stage.bars is None:
                raise RuntimeError("data stage supplied no readable completed D1 bars")
            completed = normalize_completed_daily_bars(data_stage.bars)
            if not len(completed):
                raise RuntimeError("data stage supplied an empty completed D1 frame")
            session_anchor = self.signal_repository.get_or_create_session_anchor(
                instrument.instrument_id,
                "D1",
                completed.index[0].isoformat(),
            )
            cursor_before = self.signal_repository.load_cursor(
                instrument.instrument_id, "D1", strategy_version
            )
            if backfill_signals:
                positions = tuple(range(len(completed)))
            elif cursor_before is None:
                positions = (len(completed) - 1,)
            else:
                positions = tuple(
                    index
                    for index, timestamp in enumerate(completed.index)
                    if timestamp.isoformat() > cursor_before
                ) or (len(completed) - 1,)
            screening_stage = self.strategy_screening_service.screen(
                data_stage,
                asset,
                session_anchor=session_anchor,
                replay_positions=positions,
                execution_context=self.execution_context,
            )
            screening = screening_stage.result
            decision = self.trend_decision_service.decide(data_result, screening)
            event_decisions = tuple(
                self.trend_decision_service.decide(
                    data_result, screening, event_snapshot=snapshot
                )
                for snapshot in screening.event_snapshots
            )
            prepared = screening_stage.prepared
            if prepared is None:
                raise RuntimeError("strategy screening did not prepare D1 features")
            row["scan"] = dict(screening_stage.analysis)
            row["chart"] = _chart_payload(prepared.base.tail(chart_bars))
            row["alert_policy"] = {
                "formal_eligible": formal_eligible,
                "research_mode": research_mode,
                "suppressed_reason": None if formal_eligible else "research_observation_only",
            }

            detected: list[SignalEvent] = []
            execution_decision_events: list[dict[str, Any]] = []
            breakout_assessments: list[BreakoutAssessment] = []
            decisions_by_position = {
                int(snapshot.get("position", -1)): event_decision
                for snapshot, event_decision in zip(screening.event_snapshots, event_decisions)
            }
            for position in positions:
                replay = screening_stage.replay_analyses.get(position)
                if replay is None:
                    continue
                replay_events = self._signal_events(
                    instrument.instrument_id,
                    asset,
                    replay,
                    data_result.dataset_version or "unknown",
                    started_at.isoformat(),
                    data_stage.refresh_error is not None,
                    strategy_version,
                )
                event_decision = decisions_by_position.get(position)
                if event_decision is not None:
                    execution_event = build_execution_decision_event(
                        event_decision,
                        trigger_events=replay_events,
                        dataset_version=data_result.dataset_version or "unknown",
                        detected_at=started_at.isoformat(),
                        strategy_version=strategy_version,
                        market=asset.market.value,
                    )
                    if execution_event is not None:
                        execution_decision_events.append(execution_event.to_dict())
                        replay_events.append(execution_event.to_signal_event())
                detected.extend(replay_events)
                signal_ids = {
                    (event.indicator_name, event.signal_time, event.direction.lower()): event.signal_id
                    for event in replay_events
                }
                assessments = build_breakout_assessments(
                    prepared,
                    replay,
                    position=position,
                    signal_ids=signal_ids,
                )
                breakout_assessments.extend(
                    _decision_backed_assessment(item, event_decision)
                    for item in assessments
                )

            row["signals_detected"] = len(detected)
            row["signals"] = [event.to_dict() for event in detected]
            event_decision_payloads = tuple(item.to_dict() for item in event_decisions)
            report_bundle = build_instrument_report_bundle(
                prepared,
                symbol=asset.symbol,
                instrument_id=instrument.instrument_id,
                generated_at=started_at.isoformat(),
                analysis=screening_stage.analysis,
                signals=row["signals"],
                breakout_assessments=breakout_assessments,
                market_assessment=_market_assessment_from_decision(decision),
                data_update_result=data_result.to_dict(),
                strategy_screening_result=screening.to_dict(),
                trend_decision_result=decision.to_dict(),
                event_decisions=event_decision_payloads,
                chart_bars=chart_bars,
            )
            report_payload = {
                **report_bundle.to_dict(),
                "execution_decision_events": execution_decision_events,
            }
            report_payload.pop("result_id", None)
            report_payload.pop("result_hash", None)
            report_digest = hashlib.sha256(
                json.dumps(
                    report_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            report_payload["result_id"] = report_digest[:24]
            report_payload["result_hash"] = report_digest
            row["report_bundle"] = report_payload
            row["execution_decision_events"] = execution_decision_events
            row["result_id"] = report_payload["result_id"]
            row["result_hash"] = report_payload["result_hash"]
            _attach_stage_results(
                row,
                data_result.to_dict(),
                screening.to_dict(),
                decision.to_dict(),
                event_decision_payloads,
            )
            if formal_eligible:
                latest_time = prepared.base.index[-1].isoformat()
                notification_event_ids = {
                    event.signal_id
                    for event in detected
                    if event.signal_type in {
                        "ENTRY_DECISION_LONG",
                        "ENTRY_DECISION_SHORT",
                    }
                }
                new, duplicates = self.signal_repository.commit_events_and_cursor(
                    detected,
                    run_id=run_id,
                    instrument_id=instrument.instrument_id,
                    timeframe="D1",
                    strategy_version=strategy_version,
                    last_signal_time=latest_time,
                    enqueue_notifications=not backfill_signals,
                    notification_event_ids=(
                        notification_event_ids if not backfill_signals else ()
                    ),
                )
                row["signals_new"], row["signals_duplicate"] = len(new), duplicates
            else:
                new = []
                row["signals_new"], row["signals_duplicate"] = 0, 0
                row["research_signals"] = row["signals"]
            row["cursor"] = {
                "strategy_version": strategy_version,
                "before": cursor_before,
                "after": prepared.base.index[-1].isoformat() if formal_eligible else cursor_before,
                "bars_replayed": len(positions),
            }
            row["run_status"] = _data_gate_status(data_stage, research_mode=research_mode)
            return row, new
        except Exception as exc:
            row["run_status"] = "failed"
            row["scan"] = {"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}
            screening_stage = self.strategy_screening_service.screen(data_result, asset, bars=None)
            decision = self.trend_decision_service.decide(data_result, screening_stage.result)
            _attach_stage_results(row, data_result.to_dict(), screening_stage.result.to_dict(), decision.to_dict(), ())
            row["signals_detected"] = 0
            row["signals"] = []
            row["signals_new"] = 0
            row["signals_duplicate"] = 0
            return row, []

    @staticmethod
    def _signal_events(instrument_id: str, asset: AssetConfig, analysis: Mapping[str, Any], dataset_version: str, detected_at: str, refresh_failed: bool, strategy_version: str) -> list[SignalEvent]:
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

    def _deliver_pending(self, run_id: str) -> dict[str, int]:
        result = {"notified": 0, "errors": 0, "recovered": 0}
        current = self.signal_repository.pending_notifications(run_id)
        for event in current:
            try:
                self.notifier.notify(event)
                self.signal_repository.mark_notified(event.signal_id, _aware_utc(self._now()).isoformat())
                result["notified"] += 1
            except Exception as exc:
                self.signal_repository.mark_delivery_error(event.signal_id, str(exc))
                result["errors"] += 1
        for event in self.signal_repository.pending_notifications():
            if event in current:
                continue
            try:
                self.notifier.notify(event)
                self.signal_repository.mark_notified(event.signal_id, _aware_utc(self._now()).isoformat())
                result["recovered"] += 1
            except Exception as exc:
                self.signal_repository.mark_delivery_error(event.signal_id, str(exc))
        return result


def _attach_stage_results(
    row: dict[str, Any],
    data_update: Mapping[str, Any],
    strategy_screening: Mapping[str, Any],
    trend_decision: Mapping[str, Any],
    event_decisions: Iterable[Mapping[str, Any]],
) -> None:
    """Keep full stage payloads in both snapshot and SQLite instrument JSON."""

    row["data_update_result"] = dict(data_update)
    row["strategy_screening_result"] = dict(strategy_screening)
    row["trend_decision_result"] = dict(trend_decision)
    row["event_decisions"] = [dict(item) for item in event_decisions]
    row["stages"] = {
        "data_update": row["data_update_result"],
        "strategy_screening": row["strategy_screening_result"],
        "trend_decision": row["trend_decision_result"],
    }


def _data_gate_status(data_stage: Any, *, research_mode: bool) -> str:
    """Preserve legacy run statuses while refusing formal gate bypasses."""

    result = data_stage.result
    freshness = result.freshness.get("status")
    update = data_stage.update
    if freshness == "STALE_DATA":
        return "stale"
    if not result.formal_ready:
        if research_mode and data_stage.readable:
            return str(update.get("status", "unchanged"))
        if update.get("status") == "failed" and not data_stage.readable:
            return "blocked" if update.get("error_type") == "ProviderIdentityError" else "failed"
        return "blocked"
    if update.get("status") == "failed":
        return "blocked" if update.get("error_type") == "ProviderIdentityError" else "failed"
    return str(update.get("status", "unchanged"))


def _market_assessment_from_decision(decision: Any) -> MarketAssessment:
    direction = str(decision.trend_direction)
    trend = {"LONG": "上升趋势", "SHORT": "下降趋势"}.get(direction, "趋势不明确")
    entry = {
        "ENTER_LONG": "做多",
        "ENTER_SHORT": "做空",
        "ENTRY_CANDIDATE_LONG": "做多候选",
        "ENTRY_CANDIDATE_SHORT": "做空候选",
    }.get(str(decision.execution_state), "不入场")
    confidence = decision.confidence
    evidence = (
        decision.long_evidence if direction == "LONG"
        else decision.short_evidence if direction == "SHORT"
        else decision.reverse_evidence
    )
    return MarketAssessment(
        as_of=decision.as_of,
        market_status="观察性结果" if decision.observation_only else "已使用结构化三阶段结果",
        trend=trend,
        trend_basis=tuple(evidence),
        entry_direction=entry,
        entry_reason=decision.conclusion,
        rating_grade=confidence.get("rating_grade"),
        rating_score=confidence.get("rating_score"),
    )


def _decision_backed_assessment(
    assessment: BreakoutAssessment,
    decision: Any | None,
) -> BreakoutAssessment:
    if decision is None:
        return assessment
    trend = {"LONG": "上升趋势", "SHORT": "下降趋势"}.get(
        str(decision.trend_direction), "趋势不明确"
    )
    entry = {
        "ENTER_LONG": "做多",
        "ENTER_SHORT": "做空",
    }.get(str(decision.execution_state), "不入场")
    confidence = decision.confidence
    strength = f"{confidence.get('rating_grade') or '无'}级 / {confidence.get('rating_score', '不可用')} 分"
    direction_evidence = (
        decision.long_evidence if decision.trend_direction == "LONG"
        else decision.short_evidence if decision.trend_direction == "SHORT"
        else ()
    )
    return replace(
        assessment,
        assessment_id=decision.result_id,
        trend=trend,
        trend_basis=tuple(direction_evidence),
        entry_direction=entry,
        signal_strength=strength,
        triggered_conditions=tuple(direction_evidence),
        unmet_conditions=tuple(decision.reverse_evidence) + tuple(decision.risk_blocks),
        conclusion=decision.conclusion,
    )


def _data_metadata(bars: Any) -> dict[str, Any]:
    values = bars.get("quality_status")
    qualities = sorted({str(value) for value in values.dropna().tolist()}) if values is not None else []
    return {"dataset_version": str(bars.attrs.get("dataset_version", "unknown")), "quality_statuses": qualities, "completed_bars": len(bars), "latest_complete_bar": _timestamp_text(bars["timestamp"].iloc[-1]) if len(bars) else None}


def _chart_payload(frame: Any) -> list[dict[str, Any]]:
    columns = ("open", "high", "low", "close", "volume", "channel_high_20", "channel_low_20", "channel_high_55", "channel_low_55", "sma_10", "sma_20", "dif", "dea", "histogram")
    rows: list[dict[str, Any]] = []
    for timestamp, values in frame.iterrows():
        row: dict[str, Any] = {"timestamp": timestamp.isoformat()}
        for column in columns:
            try:
                value = float(values.get(column))
            except (TypeError, ValueError):
                value = float("nan")
            row[column] = value if value == value and abs(value) != float("inf") else None
        rows.append(row)
    return rows


def _summary(rows: list[Mapping[str, Any]], signals_new: int, delivery: Mapping[str, int]) -> dict[str, int]:
    statuses = [str(row.get("run_status", "failed")) for row in rows]
    return {"selected": len(rows), "updated": statuses.count("updated"), "unchanged": statuses.count("unchanged"), "blocked": statuses.count("blocked"), "stale": statuses.count("stale"), "failed": statuses.count("failed"), "scanned": sum(1 for row in rows if isinstance(row.get("scan", {}).get("status"), Mapping)), "signals_detected": sum(int(row.get("signals_detected", 0)) for row in rows), "signals_new": signals_new, "signals_duplicate": sum(int(row.get("signals_duplicate", 0)) for row in rows), "signals_notified": delivery.get("notified", 0), "delivery_errors": delivery.get("errors", 0), "recovered_deliveries": delivery.get("recovered", 0), "error_count": sum(1 for row in rows if row.get("run_status") in {"failed", "blocked", "stale"})}


def _timestamp_text(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _daily_refresh_end(value: datetime) -> datetime:
    return _aware_utc(value).replace(hour=0, minute=0, second=0, microsecond=0)


def _earliest_utc(value: str) -> datetime:
    return _aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False, default=str)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


__all__ = ["DEFAULT_BOOTSTRAP_DAYS", "DailyMarketScanResult", "DailyMarketScanService"]
