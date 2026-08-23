"""The only D1 stage allowed to mutate cursor, signals, or outbox state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from typing import Any, Mapping

from inv_trend.core.signals import CORRECTED_STRATEGY_VERSION, SignalEvent

from .ports import DailyWorkflowRuntimePort
from .projections import (
    attach_stage_results,
    data_gate_status,
)
from .stage_payloads import (
    data_update_result,
    json_safe,
    mapping,
    rows,
    strategy_screening_result,
    validate_commit_context,
    validate_commit_projection,
    trend_decision_result,
    validate_commit_evidence,
    validate_hash_chain,
    validate_result_hash,
    validate_stage_identity,
)
from .workspace import DailyStagingWorkspace, canonical_payload_hash


@dataclass(frozen=True)
class SignalCommitOutcome:
    """Receipt-ready result of one commit stage invocation."""

    snapshot: Mapping[str, Any]
    summary: Mapping[str, Any]


class SignalCommitService:
    """Commit only already-validated staged evidence through state ports."""

    def __init__(self, runtime: DailyWorkflowRuntimePort) -> None:
        self._runtime = runtime

    def commit(self, workspace: DailyStagingWorkspace) -> SignalCommitOutcome:
        context = workspace.read_context()
        options = mapping(context.get("configuration"))
        symbols = _context_symbols(context)
        metadata = mapping(context.get("symbol_metadata"))
        started_at = str(context.get("started_at"))
        strategy_version = str(options.get("strategy_version", CORRECTED_STRATEGY_VERSION))
        research_mode = bool(options.get("research_mode", False))
        repository = self._runtime.state_repository
        recovered = self._recover_completed_commit(
            repository,
            workspace=workspace,
            context=context,
            symbols=symbols,
            options=options,
        )
        if recovered is not None:
            return recovered

        # Every modular workflow repository must commit all instruments,
        # cursors, outbox rows, audit payloads, and terminal status in one
        # transaction.  Do every validation before entering that boundary so
        # a failed second symbol cannot leave the first symbol eligible for
        # delivery.  The historical per-symbol path remains only in the
        # legacy facade, never as a silent fallback for this workflow.
        atomic_commit = getattr(repository, "commit_daily_run", None)
        if not callable(atomic_commit):
            raise TypeError(
                "DailyStateRepositoryPort.commit_daily_run is required for the modular workflow"
            )
        records: list[dict[str, Any]] = []
        commits: list[dict[str, Any]] = []
        try:
            for symbol in symbols:
                row, persistence = self._prepare_symbol(
                    workspace,
                    symbol,
                    context=context,
                    metadata=mapping(metadata.get(symbol)),
                    strategy_version=strategy_version,
                    research_mode=research_mode,
                    started_at=started_at,
                )
                records.append(row)
                if persistence is not None:
                    commits.append({**persistence, "row": row})
            finished_at = _now_text(self._runtime)
            _all_new, run_summary = atomic_commit(
                run_id=workspace.run_id,
                started_at=started_at,
                finished_at=finished_at,
                commits=commits,
                records=records,
            )
        except KeyboardInterrupt:
            repository.interrupt_run(workspace.run_id, _now_text(self._runtime))
            raise
        except Exception:
            repository.interrupt_run(workspace.run_id, _now_text(self._runtime))
            raise
        snapshot = {
            "schema_version": "4",
            "report_schema_version": "3",
            "run_id": workspace.run_id,
            "report_date": workspace.report_date,
            "started_at": started_at,
            "finished_at": finished_at,
            "timezone": "Asia/Shanghai",
            "timeframe": "D1",
            "configuration": {**dict(options), "symbols": symbols},
            "symbols": records,
            "summary": run_summary,
        }
        return SignalCommitOutcome(snapshot=snapshot, summary=run_summary)

    @staticmethod
    def _recover_completed_commit(
        repository: Any,
        *,
        workspace: DailyStagingWorkspace,
        context: Mapping[str, Any],
        symbols: list[str],
        options: Mapping[str, Any],
    ) -> SignalCommitOutcome | None:
        """Rebuild a missing filesystem receipt from finalized SQLite rows."""

        loader = getattr(repository, "load_completed_run", None)
        if not callable(loader):
            return None
        persisted = loader(workspace.run_id)
        if persisted is None:
            return None
        persisted_mapping = mapping(persisted)
        instrument_rows = mapping(persisted_mapping.get("instruments"))
        if set(instrument_rows) != set(symbols):
            raise ValueError(
                "completed daily run does not contain exactly the staged instrument payloads"
            )
        records = [dict(mapping(instrument_rows[symbol])) for symbol in symbols]
        if any(not record for record in records):
            raise ValueError("completed daily run contains an invalid instrument payload")
        summary = dict(mapping(persisted_mapping.get("summary")))
        if not summary:
            raise ValueError("completed daily run does not contain a recoverable summary")
        snapshot = {
            "schema_version": "4",
            "report_schema_version": "3",
            "run_id": workspace.run_id,
            "report_date": workspace.report_date,
            "started_at": str(context.get("started_at") or persisted_mapping.get("started_at") or ""),
            "finished_at": str(persisted_mapping.get("finished_at") or ""),
            "timezone": "Asia/Shanghai",
            "timeframe": "D1",
            "configuration": {**dict(options), "symbols": symbols},
            "symbols": records,
            "summary": summary,
        }
        if not snapshot["started_at"] or not snapshot["finished_at"]:
            raise ValueError("completed daily run has invalid audit timestamps")
        return SignalCommitOutcome(snapshot=snapshot, summary=summary)

    def _prepare_symbol(
        self,
        workspace: DailyStagingWorkspace,
        symbol: str,
        *,
        context: Mapping[str, Any],
        metadata: Mapping[str, Any],
        strategy_version: str,
        research_mode: bool,
        started_at: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        del started_at  # Event audit times were fixed in the decision projection.
        data_payload = workspace.read_stage(symbol, "data_update")
        screen_payload = workspace.read_stage(symbol, "strategy_screening")
        decision_payload = workspace.read_stage(symbol, "trend_decision")
        data = data_update_result(data_payload)
        screening = strategy_screening_result(screen_payload)
        decision = trend_decision_result(decision_payload)
        validate_result_hash(data_payload, data, "data-update")
        validate_result_hash(screen_payload, screening, "strategy-screen")
        validate_result_hash(decision_payload, decision, "trend-decide")
        validate_hash_chain(data, screening, decision, symbol=symbol)
        configured_instrument_id = str(metadata.get("instrument_id") or "")
        if not configured_instrument_id:
            raise ValueError(f"staging context has no instrument identity for {symbol}")
        validate_stage_identity(
            data,
            screening,
            decision,
            symbol=symbol,
            instrument_id=configured_instrument_id,
            timeframe="D1",
        )

        row = dict(metadata)
        row.setdefault("symbol", symbol)
        row.setdefault("instrument", symbol)
        row.setdefault("instrument_id", data.instrument_id)
        row.setdefault("market", "")
        row.setdefault("timeframe", "D1")
        row["data_update_result"] = data_payload
        row["freshness"] = json_safe(data.freshness)
        row.setdefault(
            "data",
            {
                "dataset_version": data.dataset_version,
                "quality_statuses": list(data.quality.get("statuses", ())),
                "completed_bars": 0,
                "latest_complete_bar": data.latest_complete_d1,
            },
        )
        event_decisions = list(json_safe(rows(decision.event_decisions)))
        attach_stage_results(row, data_payload, screen_payload, decision_payload, event_decisions)

        # Observation-only evidence can be useful for research reports, but
        # it must never be promoted to cursor, signal, or outbox state just
        # because a caller later edits the run context.  Treat every stage's
        # explicit observation marker as a formal-state blocker.
        formal_eligible = (
            data.formal_ready
            and not research_mode
            and not data.observation_only
            and not screening.observation_only
            and not decision.observation_only
        )
        # Commit consumes the result JSON itself.  The audit sidecar is only a
        # duplicated inspection aid; if present it must match but can never
        # alter which signals/cursor transition this stage persists.
        runtime = mapping(screening.commit_evidence)
        validate_commit_evidence(screening, runtime, symbol)
        audit_runtime_path = workspace.symbol_root(symbol) / "04_audit" / "screening_runtime.json"
        if audit_runtime_path.exists():
            audit_runtime = workspace.read_runtime(symbol, "screening_runtime")
            if canonical_payload_hash(json_safe(audit_runtime)) != canonical_payload_hash(
                json_safe(runtime)
            ):
                raise ValueError(
                    f"strategy-screen commit evidence hash/audit mirror does not match canonical result for {symbol}"
                )
        validate_commit_context(
            runtime,
            context,
            symbol=symbol,
            metadata=metadata,
        )
        projection = validate_commit_projection(
            decision,
            context,
            screening,
            symbol=symbol,
        )
        persistence_plan = mapping(projection.get("persistence"))
        if str(persistence_plan.get("strategy_version") or "") != strategy_version:
            raise ValueError(
                f"trend-decision commit projection strategy version is invalid for {symbol}"
            )
        observable = screening.screening_status in {"READY", "OBSERVATION_ONLY"}
        if bool(persistence_plan.get("formal_eligible", False)) != formal_eligible:
            raise ValueError(
                f"trend-decision commit projection readiness is invalid for {symbol}"
            )
        row_projection = mapping(projection.get("row_projection"))
        detected = _projection_events(projection, symbol=symbol)
        if not observable and detected:
            raise ValueError(
                f"unavailable strategy-screen has formal events in commit projection for {symbol}"
            )
        row["scan"] = json_safe(mapping(row_projection.get("scan"))) or {
            "status": "not_run",
            "reason": "data_readiness_gate_blocked",
        }
        row["chart"] = json_safe(rows(row_projection.get("chart")))
        row["alert_policy"] = {
            "formal_eligible": formal_eligible,
            "research_mode": research_mode,
            "notification_source": "execution_decision_event",
            "suppressed_reason": (
                None
                if formal_eligible
                else "research_observation_only"
                if research_mode
                else ",".join(data.blocking_reasons) or "data_not_ready"
            ),
        }
        row["signals_detected"] = len(detected) if observable else 0
        row["signals"] = [event.to_dict() for event in detected]
        row["execution_decision_events"] = json_safe(
            rows(projection.get("execution_decision_events"))
        )
        report_bundle = json_safe(mapping(row_projection.get("report_bundle")))
        if not report_bundle:
            raise ValueError(
                f"trend-decision commit projection has no report bundle for {symbol}"
            )
        row["report_bundle"] = report_bundle
        row["result_id"] = str(report_bundle.get("result_id") or "")
        row["result_hash"] = str(report_bundle.get("result_hash") or "")
        if not row["result_id"] or not row["result_hash"]:
            raise ValueError(
                f"trend-decision commit projection report identity is invalid for {symbol}"
            )
        cursor_before = persistence_plan.get("cursor_before")
        if cursor_before is not None and not isinstance(cursor_before, str):
            raise ValueError(
                f"trend-decision commit projection cursor identity is invalid for {symbol}"
            )
        persistence: dict[str, Any] | None = None
        if formal_eligible and observable:
            latest_time = str(persistence_plan.get("last_signal_time") or "")
            if not latest_time:
                raise ValueError(f"trend-decision projection has no latest D1 timestamp for {symbol}")
            if str(persistence_plan.get("cursor_after") or "") != latest_time:
                raise ValueError(
                    f"trend-decision commit projection cursor transition is invalid for {symbol}"
                )
            expected_anchor = str(persistence_plan.get("session_anchor") or latest_time)
            existing_anchor = self._runtime.state_repository.load_session_anchor(
                data.instrument_id,
                "D1",
            )
            if existing_anchor is not None and existing_anchor != expected_anchor:
                raise ValueError(
                    f"D1 session anchor changed after strategy-screen for {symbol}; rerun strategy-screen"
                )
            known_events = {event.signal_id: event for event in detected}
            notification_ids, legacy_suppressed = _resolve_projection_notification_ids(
                projection, known_events, symbol=symbol
            )
            if legacy_suppressed:
                row["alert_policy"]["legacy_notification_ids_suppressed"] = list(
                    legacy_suppressed
                )
            row["signals_new"], row["signals_duplicate"] = 0, 0
            persistence = {
                "instrument_id": data.instrument_id,
                "timeframe": "D1",
                "strategy_version": strategy_version,
                # The repository compares this value while holding its
                # transaction.  A delayed staged run can therefore never
                # overwrite a cursor advanced by a newer committed run.
                "expected_cursor": cursor_before,
                "last_signal_time": latest_time,
                "session_anchor": expected_anchor,
                "events": detected,
                "enqueue_notifications": bool(persistence_plan.get("enqueue_notifications", True)),
                "notification_event_ids": notification_ids,
            }
        else:
            row["signals_new"], row["signals_duplicate"] = 0, 0
            if research_mode:
                row["research_signals"] = row["signals"]
        row["cursor"] = {
            "strategy_version": strategy_version,
            "before": cursor_before,
            "after": persistence_plan.get("cursor_after") if formal_eligible else cursor_before,
            "bars_replayed": int(persistence_plan.get("bars_replayed", 0)),
        }
        row["run_status"] = data_gate_status(
            data, mapping(row.get("update")), research_mode=research_mode
        )
        return row, persistence


def _resolve_projection_notification_ids(
    projection: Mapping[str, Any],
    known_events: Mapping[str, SignalEvent],
    *,
    symbol: str,
) -> tuple[set[str], tuple[str, ...]]:
    """Resolve a hash-bound projection without reviving legacy grade alerts."""

    uses_execution_projection = "notification_event_ids" in projection
    raw_notification_ids = (
        projection.get("notification_event_ids", ())
        if uses_execution_projection
        else projection.get("notification_signal_ids", ())
    )
    if raw_notification_ids is None:
        raw_notification_ids = ()
    if not isinstance(raw_notification_ids, (list, tuple, set, frozenset)):
        raise ValueError(
            f"trend-decision commit projection notification identity is invalid for {symbol}"
        )
    requested = {str(value) for value in raw_notification_ids}
    if not requested.issubset(known_events):
        raise ValueError(
            f"trend-decision commit projection notification identity is invalid for {symbol}"
        )
    confirmed = {
        event_id
        for event_id in requested
        if _is_execution_notification_event(known_events[event_id])
    }
    invalid = sorted(
        known_events[event_id].signal_type for event_id in requested - confirmed
    )
    if uses_execution_projection and invalid:
        raise ValueError(
            "formal notification outbox accepts only confirmed execution "
            f"decision events for {symbol}: {invalid}"
        )
    # Old schema-v1 projections selected A-grade evidence.  Preserve staged
    # recovery, but fail closed by suppressing those technical notification IDs.
    suppressed = tuple(sorted(requested - confirmed))
    return confirmed, suppressed


def _is_execution_notification_event(event: SignalEvent) -> bool:
    return (
        event.indicator_name == "execution_decision"
        and event.signal_type in {"ENTRY_DECISION_LONG", "ENTRY_DECISION_SHORT"}
    )


def _projection_events(
    projection: Mapping[str, Any], *, symbol: str
) -> list[SignalEvent]:
    """Deserialize the already-hashed operational event plan.

    No indicator, rating, turtle, or report logic is evaluated here.  The
    object conversion is solely the repository's established typed input
    contract and rejects malformed hand-off JSON before a transaction opens.
    """

    raw_events = projection.get("formal_signal_events", ())
    if not isinstance(raw_events, (list, tuple)):
        raise ValueError(f"trend-decision commit projection events are invalid for {symbol}")
    events: list[SignalEvent] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            raise ValueError(f"trend-decision commit projection event is invalid for {symbol}")
        try:
            # Stage result parsing freezes nested mappings for immutability.
            # Convert those mapping proxies back to ordinary value mappings at
            # the persistence boundary; ``SignalEvent.to_dict`` intentionally
            # uses dataclasses.asdict and therefore needs deepcopy-able data.
            values = dict(raw)
            values["indicator_parameters"] = dict(
                mapping(values.get("indicator_parameters"))
            )
            values["metadata"] = dict(mapping(values.get("metadata")))
            event = SignalEvent(**values)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"trend-decision commit projection event cannot be restored for {symbol}"
            ) from exc
        # The D1 workflow owns the cursor, while the established daily
        # analysis can retain D2/D5/D7 event evidence in the same completed
        # D1 scan.  Preserve that compatibility instead of silently dropping
        # those structured events at the commit boundary.
        if event.symbol != symbol or not event.timeframe or not event.detected_at:
            raise ValueError(
                "trend-decision commit projection event identity is invalid for "
                f"{symbol}: symbol={event.symbol!r}, timeframe={event.timeframe!r}, "
                f"detected_at={event.detected_at!r}"
            )
        events.append(event)
    return events


def _context_symbols(context: Mapping[str, Any]) -> list[str]:
    values = context.get("symbols")
    if not isinstance(values, list) or not values:
        raise ValueError("staging context has no selected symbols")
    return [str(value) for value in values]


def _now_text(runtime: DailyWorkflowRuntimePort) -> str:
    value = runtime.now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


__all__ = ["SignalCommitOutcome", "SignalCommitService"]
