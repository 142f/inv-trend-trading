"""Commit-evidence verification for the daily workflow.

Single responsibility: prove that a commit receipt, its snapshot and the staged
per-symbol evidence still agree, so publish/deliver never consume a stale or
hand-edited commit.  Methods are instance methods of the workflow (they use its
identity validators) and are mixed into DailyWorkflow.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .stage_payloads import (
    context_symbols as _context_symbols,
    data_update_result,
    mapping as _mapping,
    strategy_screening_result,
    trend_decision_result,
    validate_hash_chain,
    validate_result_hash,
)
from .workspace import DailyStagingWorkspace, canonical_payload_hash


class CommitEvidenceGuard:
    """Verify commit receipts against the staged authority files."""

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


__all__ = ["CommitEvidenceGuard"]
