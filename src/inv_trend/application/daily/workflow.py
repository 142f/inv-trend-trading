"""Independent, resumable stages for the D1 daily market workflow.

The historical :mod:`inv_trend.application.daily_pipeline` remains the
compatibility implementation.  This module is the new orchestration surface:
each public stage exchanges immutable JSON through ``.staging`` and only the
commit/delivery stages mutate external state.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

import pandas as pd

from inv_trend.core.signals import CORRECTED_STRATEGY_VERSION
from inv_trend.core.strategy.daily.signals import normalize_completed_daily_bars

from ..daily_models import (
    DataUpdateResult,
    StrategyScreeningResult,
)
from ..strategy_config import DailyChecksConfig, TrendDecisionConfig
from .artifact_publication import ArtifactPublicationService
from .compatibility import create_legacy_runtime
from .notification_delivery import NotificationDeliveryService
from .ports import DailyWorkflowRuntimePort
from .run_contract import BEIJING, DEFAULT_BOOTSTRAP_DAYS, DailyMarketScanResult
from .signal_commit import SignalCommitService
from .stage_payloads import (
    commit_context_payload,
    data_update_result,
    json_safe as _json_safe,
    mapping as _mapping,
    strategy_screening_result,
    trend_decision_result,
    validate_commit_evidence,
    validate_hash_chain,
    validate_result_hash,
    validate_stage_identity,
)
from .strategy_screening import StrategyScreeningEvidenceBuilder
from .strategy_screening import StrategyScreeningService
from .trend_decision import TrendDecisionProjectionBuilder, TrendDecisionService
from .workspace import DailyStagingWorkspace, canonical_payload_hash


class DailyWorkflow:
    """Compose D1 stages without letting read-only stages mutate run state.

    ``DailyWorkflow`` deliberately accepts the same constructor arguments as
    :class:`DailyMarketScanService` by forwarding them to that compatibility
    service.  Existing callers can therefore adopt the staged API without a
    provider, signal-store, or configuration migration.
    """

    def __init__(
        self,
        *,
        runtime: DailyWorkflowRuntimePort | None = None,
        **kwargs: Any,
    ) -> None:
        if runtime is not None and kwargs:
            raise TypeError("runtime cannot be combined with legacy constructor arguments")
        # The normal CLI still uses the old composition root through this
        # adapter.  The workflow itself only sees dependency ports.
        self._runtime: DailyWorkflowRuntimePort = runtime or create_legacy_runtime(**kwargs)

    @property
    def output_dir(self) -> Path:
        return Path(self._runtime.output_dir)

    @property
    def database_path(self) -> Path:
        return Path(self._runtime.database_path)

    @property
    def signal_log_dir(self) -> Path:
        return Path(self._runtime.signal_log_dir)

    @property
    def assets(self) -> Mapping[str, Any]:
        return self._runtime.assets

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
        """Run all explicit stages in the canonical order."""

        started_at = _aware_utc(self._runtime.now())
        run_id = uuid4().hex
        report_date = started_at.astimezone(BEIJING).date().isoformat()
        self.data_update(
            run_id=run_id,
            report_date=report_date,
            symbols=symbols,
            started_at=started_at,
            bootstrap_days=bootstrap_days,
            research_mode=research_mode,
            strategy_version=strategy_version,
            backfill_signals=backfill_signals,
            chart_bars=chart_bars,
        )
        self.strategy_screen(run_id=run_id, report_date=report_date)
        self.trend_decide(run_id=run_id, report_date=report_date)
        self.commit(run_id=run_id, report_date=report_date)
        publication = self.publish(
            run_id=run_id, report_date=report_date, render_html=render_html
        )
        # Publication is immutable.  Delivery intentionally follows it and
        # writes a separate receipt, so retrying a notifier never rewrites a
        # canonical report or changes its decision evidence.
        self.deliver(run_id=run_id, report_date=report_date)
        # The return value mirrors the immutable compatibility JSON rather
        # than the later delivery receipt.  Delivery is an independent retry
        # stage and never mutates published business artifacts.
        from inv_trend.storage.制品 import read_daily
        snapshot = _compatibility_snapshot(_mapping(read_daily(publication.compatibility_json)))
        return DailyMarketScanResult(
            publication.compatibility_json,
            self.database_path,
            self.signal_log_dir,
            snapshot,
            publication,
        )

    def data_update(
        self,
        *,
        run_id: str,
        report_date: str,
        symbols: Iterable[str] | None = None,
        started_at: datetime | None = None,
        bootstrap_days: int = DEFAULT_BOOTSTRAP_DAYS,
        research_mode: bool = False,
        strategy_version: str = CORRECTED_STRATEGY_VERSION,
        backfill_signals: bool = False,
        chart_bars: int = 180,
    ) -> dict[str, Any]:
        """Refresh/read D1 inputs and persist only ``DataUpdateResult`` JSON."""

        _validate_run_options(bootstrap_days, strategy_version, chart_bars)
        workspace = self._workspace(report_date, run_id)
        existing = workspace.read_context() if workspace.context_path.exists() else None
        # Resume exactly the saved selection and clock unless a caller makes
        # the same request explicitly.  This lets independent stage commands
        # be retried without rewriting previously hashed data evidence.
        requested_symbols = symbols
        if existing is not None and requested_symbols is None:
            requested_symbols = _context_symbols(existing)
        selected = self._runtime.select_assets(requested_symbols)
        if existing is not None and started_at is None:
            started = _context_started_at(existing)
        else:
            started = _aware_utc(started_at or self._runtime.now())
        context = workspace.initialize(
            self._new_context(
                run_id=run_id,
                report_date=report_date,
                started_at=started,
                selected=selected,
                bootstrap_days=bootstrap_days,
                research_mode=research_mode,
                strategy_version=strategy_version,
                backfill_signals=backfill_signals,
                chart_bars=chart_bars,
            )
        )
        metadata = dict(_mapping(context.get("symbol_metadata")))
        missing_assets: list[Any] = []
        recovered_metadata = False
        for asset in selected:
            if not workspace.stage_path(asset.symbol, "data_update").exists():
                missing_assets.append(asset)
                continue
            # A data-update retry must not silently accept a changed stage
            # file merely because its compatibility metadata was written.
            existing_payload = workspace.read_stage(asset.symbol, "data_update")
            existing_result = data_update_result(existing_payload)
            validate_result_hash(existing_payload, existing_result, "data-update")
            self._validate_configured_identity(asset.symbol, existing_result)
            if asset.symbol not in metadata:
                # A crash between the first stage-file write and context
                # update is recoverable without refreshing/replacing a
                # pinned result.  The result itself now carries the full
                # update summary needed for a faithful compatibility row.
                payload = workspace.read_stage(asset.symbol, "data_update")
                recovered = data_update_result(payload)
                validate_result_hash(payload, recovered, "data-update")
                metadata[asset.symbol] = self._symbol_metadata(
                    asset,
                    recovered,
                    completed_bars=0,
                )
                recovered_metadata = True
        if not missing_assets:
            if recovered_metadata:
                context["symbol_metadata"] = metadata
                workspace.write_context(context)
            return {
                **workspace.summary(
                    stage="data-update", symbols=[asset.symbol for asset in selected]
                ),
                "idempotent": True,
            }
        for asset in missing_assets:
            stage = self._runtime.data_update_stage.run(
                asset,
                started_at=started,
                bootstrap_days=bootstrap_days,
                research_mode=research_mode,
            )
            self._validate_configured_identity(asset.symbol, stage.result)
            metadata[asset.symbol] = self._symbol_metadata(
                asset,
                stage.result,
                completed_bars=len(stage.bars) if stage.bars is not None else 0,
            )
            # Save the metadata before its paired immutable result.  A
            # process interruption can then only leave metadata-without-stage
            # (safe to recompute), never a stage file with lost metadata.
            context["symbol_metadata"] = metadata
            workspace.write_context(context)
            workspace.write_stage(asset.symbol, "data_update", stage.result.to_dict())
        return workspace.summary(stage="data-update", symbols=[asset.symbol for asset in selected])

    def strategy_screen(self, *, run_id: str, report_date: str) -> dict[str, Any]:
        """Read pinned D1 data and produce strategy evidence without state writes."""

        workspace = self._workspace(report_date, run_id)
        context = workspace.read_context()
        symbols = _context_symbols(context)
        wrote_stage = False
        for symbol in symbols:
            asset = self._asset(symbol)
            data_payload = workspace.read_stage(symbol, "data_update")
            data_result = data_update_result(data_payload)
            validate_result_hash(data_payload, data_result, "data-update")
            self._validate_configured_identity(symbol, data_result)
            screening_path = workspace.stage_path(symbol, "strategy_screening")
            if screening_path.exists():
                existing_payload = workspace.read_stage(symbol, "strategy_screening")
                existing = strategy_screening_result(existing_payload)
                validate_result_hash(existing_payload, existing, "strategy-screen")
                validate_hash_chain(data_result, existing, symbol=symbol)
                continue
            options = _mapping(context.get("configuration"))
            research_mode = bool(options.get("research_mode", False))
            screening_service, _ = self._pinned_stage_services(context)
            if not data_result.formal_ready and not research_mode:
                stage = screening_service.screen(data_result, asset, bars=None)
                self._write_screening_stage(
                    workspace,
                    symbol,
                    stage.result,
                    {
                        "analysis": {},
                        "positions": [],
                        "replay_analyses": {},
                        "report_bundle_seed": {},
                        "commit_context": commit_context_payload(
                            context,
                            symbol=symbol,
                            metadata=_mapping(
                                _mapping(context.get("symbol_metadata")).get(symbol)
                            ),
                        ),
                    },
                )
                wrote_stage = True
                continue

            bars = self._load_pinned_bars(asset, data_result)
            completed = normalize_completed_daily_bars(bars)
            if completed.empty:
                raise RuntimeError(f"no completed D1 bars remain for {symbol}")
            strategy_version = str(options.get("strategy_version", CORRECTED_STRATEGY_VERSION))
            cursor_before = self._runtime.state_repository.load_cursor(
                data_result.instrument_id, "D1", strategy_version
            )
            load_anchor = getattr(self._runtime.state_repository, "load_session_anchor", None)
            existing_anchor = (
                load_anchor(data_result.instrument_id, "D1") if callable(load_anchor) else None
            )
            session_anchor = existing_anchor or completed.index[0].isoformat()
            positions = _replay_positions(
                completed,
                cursor_before=cursor_before,
                backfill=bool(options.get("backfill_signals", False)),
            )
            stage = screening_service.screen(
                data_result,
                asset,
                bars=completed,
                # A screen stage must be read-only.  The commit stage records
                # the same anchor in SQLite once it has passed all validation.
                session_anchor=session_anchor,
                replay_positions=positions,
                execution_context=self._runtime.execution_context,
            )
            runtime = self._screening_runtime(
                asset=asset,
                data_result=data_result,
                stage=stage,
                positions=positions,
                cursor_before=cursor_before,
                context=context,
                session_anchor=str(session_anchor),
            )
            self._write_screening_stage(workspace, symbol, stage.result, runtime)
            wrote_stage = True
        summary = workspace.summary(stage="strategy-screen", symbols=symbols)
        if not wrote_stage:
            summary["idempotent"] = True
        return summary

    def trend_decide(self, *, run_id: str, report_date: str) -> dict[str, Any]:
        """Derive final trend decisions strictly from staged evidence JSON."""

        workspace = self._workspace(report_date, run_id)
        context = workspace.read_context()
        wrote_stage = False
        for symbol in _context_symbols(context):
            data_payload = workspace.read_stage(symbol, "data_update")
            screening_payload = workspace.read_stage(symbol, "strategy_screening")
            data_result = data_update_result(data_payload)
            screening_result = strategy_screening_result(screening_payload)
            validate_result_hash(data_payload, data_result, "data-update")
            validate_result_hash(screening_payload, screening_result, "strategy-screen")
            validate_hash_chain(data_result, screening_result, symbol=symbol)
            validate_commit_evidence(
                screening_result,
                _mapping(screening_result.commit_evidence),
                symbol,
            )
            # A stand-alone trend decision consumes only fixed staged JSON.
            # Its identity check must not lazily construct a market-data
            # adapter (which could create/read a mutable data lake merely to
            # render a decision from already verified screening evidence).
            self._validate_context_identity(
                context, symbol, data_result, screening_result
            )
            decision_path = workspace.stage_path(symbol, "trend_decision")
            if decision_path.exists():
                existing_payload = workspace.read_stage(symbol, "trend_decision")
                existing = trend_decision_result(existing_payload)
                validate_result_hash(existing_payload, existing, "trend-decide")
                validate_hash_chain(data_result, screening_result, existing, symbol=symbol)
                continue
            _, decision_service = self._pinned_stage_services(context)
            decision = decision_service.decide(data_result, screening_result)
            event_decisions: list[dict[str, Any]] = []
            for snapshot in screening_result.event_snapshots:
                event_decision = decision_service.decide(
                    data_result, screening_result, event_snapshot=snapshot
                ).to_dict()
                # Position is stage-runtime metadata, not part of the decision
                # business hash.  It lets commit bind one event-day decision to
                # the matching replay evidence without recalculating anything.
                event_decision["position"] = snapshot.get("position")
                event_decisions.append(event_decision)
            # Event-day decisions are immutable evidence too; keeping them in
            # the decision result prevents commit/report projections from
            # trusting an unhashed sidecar file.
            decision = replace(decision, event_decisions=tuple(event_decisions))
            projection = TrendDecisionProjectionBuilder.build(
                data=data_result,
                screening=screening_result,
                decision=decision,
                data_payload=data_payload,
                screening_payload=screening_payload,
                context=context,
                metadata=_mapping(_mapping(context.get("symbol_metadata")).get(symbol)),
            )
            decision = replace(
                decision,
                commit_projection=projection,
                commit_projection_hash=canonical_payload_hash(projection),
            )
            workspace.write_stage(symbol, "trend_decision", decision.to_dict())
            wrote_stage = True
        summary = workspace.summary(stage="trend-decide", symbols=_context_symbols(context))
        if not wrote_stage:
            summary["idempotent"] = True
        return summary

    def commit(self, *, run_id: str, report_date: str) -> dict[str, Any]:
        """Atomically persist cursor/events/outbox from already-screened evidence."""

        workspace = self._workspace(report_date, run_id)
        if workspace.commit_receipt_path.exists():
            receipt = workspace.read_commit_receipt()
            self._verify_commit_receipt(
                workspace, receipt, validate_configured_identity=False
            )
            return {**workspace.summary(stage="commit", symbols=_context_symbols(workspace.read_context())), "idempotent": True, "summary": receipt.get("summary", {})}
        self._activate_stateful_runtime()
        outcome = SignalCommitService(self._runtime).commit(workspace)
        context = workspace.read_context()
        receipt_integrity = self._commit_receipt_integrity(
            workspace,
            context=context,
            snapshot=outcome.snapshot,
        )
        workspace.write_commit_receipt(
            {
                "run_id": run_id,
                "committed_at": outcome.snapshot["finished_at"],
                "summary": outcome.summary,
                "snapshot": outcome.snapshot,
                "snapshot_hash": receipt_integrity["snapshot_hash"],
                "integrity": receipt_integrity,
                "integrity_hash": canonical_payload_hash(receipt_integrity),
            }
        )
        return {
            **workspace.summary(
                stage="commit", symbols=_context_symbols(workspace.read_context())
            ),
            "summary": outcome.summary,
        }

    def deliver(self, *, run_id: str, report_date: str) -> dict[str, Any]:
        """Deliver only outbox records; failed delivery remains retryable."""

        workspace = self._workspace(report_date, run_id)
        # Validate the immutable commit hand-off even when a prior delivery
        # receipt exists.  A delivery receipt is only operational state; it
        # must never become a way to bypass the canonical evidence checks.
        receipt = workspace.read_commit_receipt()
        self._verify_commit_receipt(
            workspace, receipt, validate_configured_identity=False
        )
        self._activate_delivery_runtime()
        if workspace.delivery_receipt_path.exists():
            # A receipt records an attempt, not necessarily a successful
            # delivery.  ``deliver`` also recovers failed rows from earlier
            # runs, so it can short-circuit only once *both* this run and the
            # shared retry queue are empty.
            pending = self._runtime.state_repository.pending_notifications(run_id)
            recovery_pending = self._runtime.state_repository.pending_notifications()
            if not pending and not recovery_pending:
                return {
                    **workspace.summary(
                        stage="deliver", symbols=_context_symbols(workspace.read_context())
                    ),
                    "idempotent": True,
                    "delivery": workspace.read_delivery_receipt().get("delivery", {}),
                }
        # Require a completed commit, but keep its receipt and the published
        # business snapshot immutable.  Delivery updates outbox records and
        # the operational run-accounting summary only; it never changes
        # evidence, decisions, cursors, or formal signal rows.
        delivery = NotificationDeliveryService(
            self._runtime.state_repository,
            self._runtime.notification_port,
            now=self._runtime.now,
        ).deliver(run_id)
        previous = (
            workspace.read_delivery_receipt()
            if workspace.delivery_receipt_path.exists()
            else {}
        )
        delivery_summary = dict(_mapping(_mapping(receipt.get("snapshot")).get("summary")))
        delivery_summary["signals_notified"] = int(delivery.get("notified", 0))
        delivery_summary["delivery_errors"] = int(delivery.get("errors", 0))
        delivery_summary["recovered_deliveries"] = int(delivery.get("recovered", 0))
        delivered_at = _utc_now_text(self._runtime)
        self._runtime.state_repository.finish_run(run_id, delivered_at, delivery_summary)
        workspace.write_delivery_receipt(
            {
                "run_id": run_id,
                "delivered_at": delivered_at,
                "delivery": delivery,
                "run_summary": delivery_summary,
                "attempt": int(_mapping(previous).get("attempt", 0)) + 1,
            }
        )
        return {**workspace.summary(stage="deliver", symbols=_context_symbols(workspace.read_context())), "delivery": delivery}

    def publish(
        self,
        *,
        run_id: str,
        report_date: str,
        render_html: bool = True,
    ) -> Any:
        """Verify staged input and atomically publish the existing artifact contract."""

        workspace = self._workspace(report_date, run_id)
        receipt = workspace.read_commit_receipt()
        self._verify_commit_receipt(
            workspace, receipt, validate_configured_identity=False
        )
        snapshot = _mapping(receipt.get("snapshot"))
        if not snapshot:
            raise ValueError("commit receipt does not contain a publishable snapshot")
        self._verify_staged_hash_chain(
            workspace,
            _context_symbols(workspace.read_context()),
            validate_configured_identity=False,
        )
        publisher = self._runtime.artifact_publisher
        publication = (
            publisher.publish(snapshot, render_html=render_html)
            if isinstance(publisher, ArtifactPublicationService)
            else ArtifactPublicationService(publisher).publish(snapshot, render_html=render_html)
        )
        updated = dict(receipt)
        updated["publication"] = {
            "run_directory": str(publication.run_directory),
            "compatibility_json": str(publication.compatibility_json),
            "compatibility_html": (
                None if publication.compatibility_html is None else str(publication.compatibility_html)
            ),
        }
        workspace.write_commit_receipt(updated)
        return publication

    def _screening_runtime(
        self,
        *,
        asset: Any,
        data_result: DataUpdateResult,
        stage: Any,
        positions: tuple[int, ...],
        cursor_before: str | None,
        context: Mapping[str, Any],
        session_anchor: str,
    ) -> dict[str, Any]:
        return StrategyScreeningEvidenceBuilder.build(
            # This runtime sidecar is hashed into the immutable screening
            # result.  Bind mutable per-symbol data-update metadata as well
            # as the already checked immutable configuration context before
            # commit can consume it.
            asset=asset,
            data_result=data_result,
            stage=stage,
            positions=positions,
            cursor_before=cursor_before,
            chart_bars=int(_mapping(context.get("configuration")).get("chart_bars", 180)),
            session_anchor=session_anchor,
        ) | {
            "commit_context": commit_context_payload(
                context,
                symbol=asset.symbol,
                metadata=_mapping(_mapping(context.get("symbol_metadata")).get(asset.symbol)),
            )
        }

    def _commit_receipt_integrity(
        self,
        workspace: DailyStagingWorkspace,
        *,
        context: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        validate_configured_identity: bool = True,
    ) -> dict[str, Any]:
        """Bind a commit receipt to the exact staged authority files.

        ``commit`` is the sole side-effecting transition in the workflow.  Its
        receipt therefore records a stable snapshot digest *and* the three
        validated result hashes for every selected symbol.  This is an audit
        integrity guard rather than a secret signature: it detects accidental
        changes or partial/manual edits before publish or delivery consumes a
        receipt.
        """

        symbols = _context_symbols(context)
        staged: dict[str, dict[str, Mapping[str, Any]]] = {}
        stage_hashes: dict[str, dict[str, str]] = {}
        for symbol in symbols:
            data_payload = workspace.read_stage(symbol, "data_update")
            screening_payload = workspace.read_stage(symbol, "strategy_screening")
            decision_payload = workspace.read_stage(symbol, "trend_decision")
            data = data_update_result(data_payload)
            screening = strategy_screening_result(screening_payload)
            decision = trend_decision_result(decision_payload)
            validate_result_hash(data_payload, data, "data-update")
            validate_result_hash(screening_payload, screening, "strategy-screen")
            validate_result_hash(decision_payload, decision, "trend-decide")
            validate_hash_chain(data, screening, decision, symbol=symbol)
            if validate_configured_identity:
                # Commit is a staged-evidence/state boundary.  The data
                # identity was verified while producing the context; using
                # that immutable per-run metadata here prevents a resumed
                # commit from opening a mutable market-data port merely to
                # rebuild its receipt.
                self._validate_context_identity(context, symbol, data, screening, decision)
            staged[symbol] = {
                "data_update": data_payload,
                "strategy_screening": screening_payload,
                "trend_decision": decision_payload,
            }
            stage_hashes[symbol] = {
                "data_update_result_hash": data.result_hash,
                "strategy_screening_result_hash": screening.result_hash,
                "trend_decision_result_hash": decision.result_hash,
            }

        self._validate_commit_snapshot(snapshot, context=context, staged=staged)
        return {
            "schema_version": "1",
            "run_id": workspace.run_id,
            "report_date": workspace.report_date,
            "immutable_context_hash": str(context.get("immutable_context_hash") or ""),
            "stage_result_hashes": stage_hashes,
            "snapshot_hash": canonical_payload_hash(snapshot),
        }

    def _verify_commit_receipt(
        self,
        workspace: DailyStagingWorkspace,
        receipt: Mapping[str, Any],
        *,
        validate_configured_identity: bool = True,
    ) -> None:
        """Reject a receipt that no longer represents the staged evidence."""

        context = workspace.read_context()
        snapshot = _mapping(receipt.get("snapshot"))
        if not snapshot:
            raise ValueError("commit receipt does not contain a publishable snapshot")
        expected = self._commit_receipt_integrity(
            workspace,
            context=context,
            snapshot=snapshot,
            validate_configured_identity=validate_configured_identity,
        )
        provided_integrity = _mapping(receipt.get("integrity"))
        if provided_integrity != expected:
            raise ValueError("commit receipt integrity does not match staged evidence")
        provided_integrity_hash = str(receipt.get("integrity_hash") or "")
        if provided_integrity_hash != canonical_payload_hash(provided_integrity):
            raise ValueError("commit receipt integrity hash is invalid")
        if str(receipt.get("snapshot_hash") or "") != expected["snapshot_hash"]:
            raise ValueError("commit receipt snapshot hash is invalid")
        if str(receipt.get("run_id") or "") != workspace.run_id:
            raise ValueError("commit receipt run_id does not match the staging workspace")
        if str(receipt.get("committed_at") or "") != str(snapshot.get("finished_at") or ""):
            raise ValueError("commit receipt committed_at does not match its snapshot")
        if _mapping(receipt.get("summary")) != _mapping(snapshot.get("summary")):
            raise ValueError("commit receipt summary does not match its snapshot")

    @staticmethod
    def _validate_commit_snapshot(
        snapshot: Mapping[str, Any],
        *,
        context: Mapping[str, Any],
        staged: Mapping[str, Mapping[str, Mapping[str, Any]]],
    ) -> None:
        """Ensure commit's compatibility snapshot is a projection of stages."""

        symbols = _context_symbols(context)
        if str(snapshot.get("run_id") or "") != str(context.get("run_id") or ""):
            raise ValueError("commit snapshot run_id does not match the staging context")
        if str(snapshot.get("report_date") or "") != str(context.get("report_date") or ""):
            raise ValueError("commit snapshot report_date does not match the staging context")
        if str(snapshot.get("started_at") or "") != str(context.get("started_at") or ""):
            raise ValueError("commit snapshot started_at does not match the staging context")
        if str(snapshot.get("timezone") or "") != str(context.get("timezone") or ""):
            raise ValueError("commit snapshot timezone does not match the staging context")
        if str(snapshot.get("timeframe") or "") != str(context.get("timeframe") or ""):
            raise ValueError("commit snapshot timeframe does not match the staging context")

        expected_configuration = dict(_mapping(context.get("configuration")))
        expected_configuration["symbols"] = symbols
        if canonical_payload_hash(_mapping(snapshot.get("configuration"))) != canonical_payload_hash(
            expected_configuration
        ):
            raise ValueError("commit snapshot configuration does not match the staging context")

        raw_rows = snapshot.get("symbols")
        if not isinstance(raw_rows, list):
            raise ValueError("commit snapshot symbols must be a JSON array")
        rows_by_symbol: dict[str, Mapping[str, Any]] = {}
        for raw_row in raw_rows:
            if not isinstance(raw_row, Mapping):
                raise ValueError("commit snapshot contains a non-object symbol row")
            symbol = str(raw_row.get("symbol") or "")
            if not symbol or symbol in rows_by_symbol:
                raise ValueError("commit snapshot symbol rows are missing or duplicated")
            rows_by_symbol[symbol] = raw_row
        if list(rows_by_symbol) != symbols:
            raise ValueError("commit snapshot symbols do not match the staging context")

        for symbol in symbols:
            row = rows_by_symbol[symbol]
            expected = staged[symbol]
            data_payload = expected["data_update"]
            if str(row.get("instrument_id") or "") != str(data_payload.get("instrument_id") or ""):
                raise ValueError(f"commit snapshot instrument identity is invalid for {symbol}")
            for stage_name in ("data_update", "strategy_screening", "trend_decision"):
                actual_payload = _mapping(row.get(f"{stage_name}_result"))
                if canonical_payload_hash(actual_payload) != canonical_payload_hash(expected[stage_name]):
                    raise ValueError(
                        f"commit snapshot {stage_name} result does not match staged evidence for {symbol}"
                    )

    def _verify_staged_hash_chain(
        self,
        workspace: DailyStagingWorkspace,
        symbols: Iterable[str],
        *,
        validate_configured_identity: bool = True,
    ) -> None:
        for symbol in symbols:
            data_payload = workspace.read_stage(symbol, "data_update")
            screening_payload = workspace.read_stage(symbol, "strategy_screening")
            decision_payload = workspace.read_stage(symbol, "trend_decision")
            data = data_update_result(data_payload)
            screening = strategy_screening_result(screening_payload)
            decision = trend_decision_result(decision_payload)
            validate_result_hash(data_payload, data, "data-update")
            validate_result_hash(screening_payload, screening, "strategy-screen")
            validate_result_hash(decision_payload, decision, "trend-decide")
            validate_hash_chain(data, screening, decision, symbol=symbol)
            if validate_configured_identity:
                self._validate_configured_identity(symbol, data, screening, decision)

    def _load_pinned_bars(self, asset: Any, data: DataUpdateResult) -> pd.DataFrame:
        expected_version = str(data.dataset_version or "")
        if not expected_version:
            raise ValueError(f"staged data-update result has no dataset version for {asset.symbol}")
        bars = self._runtime.market_data.load_bars_version(
            asset.symbol,
            "D1",
            expected_version,
            completed_only=True,
            allow_research=True,
        )
        actual = str(bars.attrs.get("dataset_version", data.dataset_version))
        if actual != expected_version:
            raise ValueError(f"loaded D1 bars do not match staged dataset version for {asset.symbol}")
        return bars

    def _workspace(self, report_date: str, run_id: str) -> DailyStagingWorkspace:
        return DailyStagingWorkspace(self.output_dir, report_date, run_id)

    def _activate_stateful_runtime(self) -> None:
        """Enter the sole stateful boundary when a runtime supports deferral.

        Normal compatibility runtimes expose no such hook.  The recommended
        CLI runtime uses it to guarantee that opening/migrating SQLite cannot
        happen while evidence is merely being prepared or published.
        """

        activate = getattr(self._runtime, "activate_stateful", None)
        if callable(activate):
            activate()

    def _activate_delivery_runtime(self) -> None:
        """Enter the outbox-only runtime when the composition root supports it."""

        activate = getattr(self._runtime, "activate_delivery", None)
        if callable(activate):
            activate()
            return
        self._activate_stateful_runtime()

    def _asset(self, symbol: str) -> Any:
        try:
            return self.assets[symbol]
        except KeyError as exc:
            raise ValueError(f"symbol is not configured for D1: {symbol}") from exc

    def _validate_configured_identity(self, symbol: str, *results: Any) -> None:
        """Check staged values against the data-port's configured instrument."""

        instrument = self._runtime.market_data.instrument(symbol)
        validate_stage_identity(
            *results,
            symbol=symbol,
            instrument_id=str(instrument.instrument_id),
            timeframe="D1",
        )

    @staticmethod
    def _validate_context_identity(
        context: Mapping[str, Any], symbol: str, *results: Any
    ) -> None:
        """Validate staged identity without reading mutable external ports."""

        metadata = _mapping(_mapping(context.get("symbol_metadata")).get(symbol))
        instrument_id = str(metadata.get("instrument_id") or "")
        if not instrument_id:
            raise ValueError(f"staging context has no instrument identity for {symbol}")
        validate_stage_identity(
            *results,
            symbol=symbol,
            instrument_id=instrument_id,
            timeframe="D1",
        )

    def _symbol_metadata(
        self,
        asset: Any,
        data_result: DataUpdateResult,
        *,
        completed_bars: int,
    ) -> dict[str, Any]:
        """Create the compatibility row metadata from hashed data evidence."""

        instrument = self._runtime.market_data.instrument(asset.symbol)
        return {
            "symbol": asset.symbol,
            "instrument": asset.instrument,
            "instrument_id": data_result.instrument_id,
            "market": asset.market.value,
            "timeframe": "D1",
            "provider": {
                "primary": instrument.primary_source,
                "fallbacks": list(instrument.fallback_sources),
            },
            "update": _json_safe(data_result.update),
            "data": {
                "dataset_version": data_result.dataset_version,
                "quality_statuses": list(data_result.quality.get("statuses", ())),
                "completed_bars": completed_bars,
                "latest_complete_bar": data_result.latest_complete_d1,
            },
            "freshness": _json_safe(data_result.freshness),
        }

    @staticmethod
    def _pinned_stage_services(
        context: Mapping[str, Any],
    ) -> tuple[StrategyScreeningService, TrendDecisionService]:
        """Reconstruct the exact config captured by ``data-update``.

        Stage commands may run hours apart.  Reading a newly edited YAML at
        screen/decision time would make one audit context describe a different
        strategy than the one that actually generated the evidence.
        """

        configuration = _mapping(context.get("configuration"))
        try:
            daily_values = dict(_mapping(configuration.get("daily_checks")))
            for name in ("sma_periods", "ema_periods", "macd_session_periods"):
                if isinstance(daily_values.get(name), list):
                    daily_values[name] = tuple(daily_values[name])
            daily_checks = DailyChecksConfig(**daily_values)
            trend_decision = TrendDecisionConfig(
                **dict(_mapping(configuration.get("trend_decision")))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("staging context has an invalid pinned strategy configuration") from exc
        return (
            StrategyScreeningService(daily_checks, trend_decision),
            TrendDecisionService(trend_decision),
        )

    @staticmethod
    def _write_screening_stage(
        workspace: DailyStagingWorkspace,
        symbol: str,
        result: StrategyScreeningResult,
        runtime: Mapping[str, Any],
    ) -> StrategyScreeningResult:
        """Bind all precomputed commit/report evidence to the stage hash."""

        canonical_runtime = _json_safe(runtime)
        bound = replace(
            result,
            commit_evidence=canonical_runtime,
            commit_evidence_hash=canonical_payload_hash(canonical_runtime),
        )
        # Retain a human-friendly audit mirror, but commit reads the immutable
        # result above.  On-disk audit edits are therefore detected rather
        # than becoming a second source of business evidence.
        workspace.write_runtime(symbol, "screening_runtime", runtime)
        workspace.write_stage(symbol, "strategy_screening", bound.to_dict())
        return bound

    def _new_context(
        self,
        *,
        run_id: str,
        report_date: str,
        started_at: datetime,
        selected: Iterable[Any],
        bootstrap_days: int,
        research_mode: bool,
        strategy_version: str,
        backfill_signals: bool,
        chart_bars: int,
    ) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "run_id": run_id,
            "report_date": report_date,
            "started_at": started_at.isoformat(),
            "timezone": "Asia/Shanghai",
            "timeframe": "D1",
            "symbols": [asset.symbol for asset in selected],
            "configuration": {
                "bootstrap_days": bootstrap_days,
                "research_mode": research_mode,
                "strategy_version": strategy_version,
                "backfill_signals": backfill_signals,
                "chart_bars": chart_bars,
                "formal_quality_statuses": ["CURATED"],
                "daily_checks": asdict(self._runtime.daily_checks_config),
                "trend_decision": asdict(self._runtime.trend_decision_config),
            },
            "symbol_metadata": {},
        }


def _replay_positions(
    completed: pd.DataFrame, *, cursor_before: str | None, backfill: bool
) -> tuple[int, ...]:
    if backfill:
        return tuple(range(len(completed)))
    if cursor_before is None:
        return (len(completed) - 1,)
    positions = tuple(
        index
        for index, timestamp in enumerate(completed.index)
        if timestamp.isoformat() > cursor_before
    )
    return positions or (len(completed) - 1,)


def _context_symbols(context: Mapping[str, Any]) -> list[str]:
    values = context.get("symbols")
    if not isinstance(values, list) or not values:
        raise ValueError("staging context has no selected symbols")
    return [str(value) for value in values]


def _context_started_at(context: Mapping[str, Any]) -> datetime:
    value = str(context.get("started_at") or "")
    if not value:
        raise ValueError("staging context has no started_at timestamp")
    try:
        return _aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise ValueError("staging context has an invalid started_at timestamp") from exc


def _compatibility_snapshot(snapshot: Mapping[str, Any]) -> Mapping[str, Any]:
    """Restore the small Python-level tuple convention of the old result DTO.

    Canonical JSON necessarily uses arrays.  Existing in-process callers of
    ``DailyMarketScanService`` historically received dataclass-derived tuples
    for period settings, so retain that harmless representation detail only in
    the returned compatibility object—not in authority files.
    """

    result = dict(snapshot)
    configuration = dict(_mapping(result.get("configuration")))
    daily_checks = dict(_mapping(configuration.get("daily_checks")))
    for name in ("sma_periods", "ema_periods", "macd_session_periods"):
        if isinstance(daily_checks.get(name), list):
            daily_checks[name] = tuple(daily_checks[name])
    if daily_checks:
        configuration["daily_checks"] = daily_checks
    if configuration:
        result["configuration"] = configuration
    return result


def _validate_run_options(bootstrap_days: int, strategy_version: str, chart_bars: int) -> None:
    if bootstrap_days < 1:
        raise ValueError("bootstrap_days must be positive")
    if strategy_version != CORRECTED_STRATEGY_VERSION:
        raise ValueError("only the executable strategy version 'corrected-v2' is supported")
    if chart_bars < 1:
        raise ValueError("chart_bars must be positive")


def _utc_now_text(runtime: DailyWorkflowRuntimePort) -> str:
    return _aware_utc(runtime.now()).isoformat()


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


__all__ = ["DailyWorkflow"]
