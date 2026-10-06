"""Transactional orchestration for review-candidate publication.

The coordinator prepares every immutable audit artifact before advancing the
strategy-visible current pointer.  Re-running the same approval request resumes
an interrupted publication; changing any immutable decision field fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .integrity import (
    json_bytes,
    sha256_bytes,
    sha256_file,
    stable_hash,
    strategy_frame_hash,
)
from .models import (
    DataLineageError,
    DatasetManifest,
    InstrumentConfig,
    path_text,
    utc_now,
)
from .processing import RULE_VERSION, FRAME_DELTAS, NormalizationResult, assess_quality
from .storage import DataLake


_APPROVAL_DECISIONS = frozenset({"approve_existing", "approve_incoming"})


@dataclass(frozen=True)
class ReviewCandidate:
    run_id: str
    symbol: str
    timeframe: str
    instrument: InstrumentConfig
    manifest_path: Path
    manifest: dict[str, Any]
    candidate_dir: Path
    candidate_path: Path
    candidate_manifest_path: Path
    quality_report_path: Path
    base_path: Path | None
    incoming_path: Path | None
    approved_frame: pd.DataFrame
    parent_dataset_version: str | None
    input_hashes: dict[str, str]


@dataclass(frozen=True)
class ApprovalPlan:
    run_id: str
    version: str
    publication_run_id: str
    symbol: str
    timeframe: str
    reason: str
    actor: str
    decision: str
    parent_dataset_version: str | None
    approved_frame_hash: str
    approved_at: str
    input_hashes: dict[str, str]
    plan_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "version": self.version,
            "publication_run_id": self.publication_run_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "reason": self.reason,
            "actor": self.actor,
            "decision": self.decision,
            "parent_dataset_version": self.parent_dataset_version,
            "approved_frame_hash": self.approved_frame_hash,
            "approved_at": self.approved_at,
            "input_hashes": self.input_hashes,
        }


@dataclass(frozen=True)
class ApprovalArtifacts:
    curated_path: Path
    quality_report_path: Path
    dataset_manifest_path: Path
    approval_manifest_path: Path
    review_record_path: Path


class ReviewApprovalCoordinator:
    """Prepare, verify and atomically activate one review approval."""

    def __init__(
        self,
        lake: DataLake,
        instrument_resolver: Callable[[str], InstrumentConfig],
    ) -> None:
        self.lake = lake
        self._instrument_resolver = instrument_resolver

    def approve(
        self,
        run_id: str,
        reason: str = "",
        *,
        actor: str = "",
        decision: str = "approve_existing",
    ) -> str:
        if decision not in _APPROVAL_DECISIONS:
            raise ValueError(
                "decision must be approve_existing or approve_incoming"
            )
        actor = _review_actor(actor)
        with self.lake.lock(f"review:{run_id}", timeout=60.0):
            prior = self._read_existing_review(run_id)
            if prior is not None:
                self._validate_existing_review(
                    prior,
                    run_id=run_id,
                    reason=reason,
                    actor=actor,
                    decision=decision,
                )
                receipt_path = self._receipt_path(run_id)
                if receipt_path.exists():
                    receipt = _read_json(receipt_path, 'publication receipt')
                    version = str(prior.get('published_version') or '')
                    if receipt.get('version') != version or receipt.get('run_id') != run_id:
                        raise DataLineageError('publication receipt conflicts with review')
                    self._verify_artifacts(self.lake.root / 'manifests' / f'approval-{version}.json')
                    return version

            candidate = self._load_candidate(run_id, decision)
            plan = self._load_or_create_plan(
                candidate,
                reason=reason,
                actor=actor,
                decision=decision,
            )
            if prior is not None:
                prior_version = str(prior.get("published_version") or "")
                if prior_version != plan.version:
                    raise DataLineageError(
                        "approved review version differs from immutable approval plan"
                    )

            artifacts = self._prepare_artifacts(candidate, plan)
            self._catalog(candidate, plan, artifacts)
            self._verify_artifacts(artifacts.approval_manifest_path, pending_review=plan)
            self._activate(candidate, plan, artifacts)
            self._verify_artifacts(artifacts.approval_manifest_path)
            return plan.version

    def _receipt_path(self, run_id: str) -> Path:
        return self.lake.root / 'reviews' / f'candidate={run_id}' / 'publication_receipt.json'

    def reopen(self, run_id: str, reason: str, *, actor: str = '') -> str:
        """Create a linked PENDING attempt; preserve the rejected decision."""
        if not reason.strip():
            raise ValueError('reopening a review requires a reason')
        actor = _review_actor(actor)
        with self.lake.lock(f'review:{run_id}', timeout=60):
            prior = self._read_existing_review(run_id)
            if prior is None or prior.get('decision') != 'rejected':
                raise DataLineageError('only a rejected review can be reopened')
            candidate = self._load_candidate(run_id, 'approve_existing')
            new_id = stable_hash({'reopened_from': run_id, 'reason': reason, 'actor': actor})[:24]
            marker = candidate.candidate_dir / f'reopen-{new_id}.json'
            if marker.exists():
                return str(_read_json(marker, 'reopen receipt')['new_run_id'])
            relative = Path('reviews') / f'candidate={new_id}'
            hashes = {}
            for name in ('bars.parquet', 'base.parquet', 'incoming.parquet', 'conflicts.json'):
                source = candidate.candidate_dir / name
                if not source.exists():
                    continue
                if name.endswith('.parquet'):
                    target = self.lake.write_frame(pd.read_parquet(source), relative / name)
                else:
                    payload = _read_json(source, 'candidate conflicts') | {'run_id': new_id}
                    target = self.lake.write_json(payload, relative / name, idempotent=True)
                hashes[path_text(target, self.lake.root)] = sha256_file(target)
            candidate_payload = _read_json(candidate.candidate_manifest_path, 'candidate manifest') | {
                'run_id': new_id, 'file_hashes': hashes, 'reopened_from_run_id': run_id,
                'reopen_reason': reason, 'reopen_actor': actor,
            }
            cm = self.lake.write_json(candidate_payload, relative / 'candidate_manifest.json', idempotent=True)
            quality_payload = _read_json(candidate.quality_report_path, 'candidate quality report') | {'run_id': new_id}
            quality = self.lake.write_json(quality_payload, Path('ingestion_reports') / f'{new_id}.json', idempotent=True)
            old_hashes = candidate.manifest['file_hashes']
            new_hashes = {key: value for key, value in old_hashes.items()
                          if key not in {path_text(candidate.quality_report_path, self.lake.root),
                                         path_text(candidate.candidate_manifest_path, self.lake.root)}}
            new_hashes.update({path_text(cm, self.lake.root): sha256_file(cm),
                               path_text(quality, self.lake.root): sha256_file(quality)})
            payload = candidate.manifest | {'run_id': new_id, 'processing_run_id': new_id,
                'request_id': new_id, 'reopened_from_run_id': run_id,
                'file_paths': list(new_hashes), 'file_hashes': new_hashes}
            manifest = self.lake.write_json(payload, Path('manifests') / f'{new_id}.json', idempotent=True)
            self.lake.catalog(new_id, candidate.symbol, candidate.timeframe,
                              str(payload.get('data_source') or 'review'), manifest,
                              instrument_id=candidate.instrument.instrument_id, status='REVIEW_REQUIRED',
                              version=payload['dataset_version'], checksum=sha256_file(manifest))
            self.lake.write_json({'new_run_id': new_id, 'reopened_from_run_id': run_id,
                                 'reason': reason, 'actor': actor}, marker.relative_to(self.lake.root), idempotent=True)
            return new_id

    def _load_candidate(
        self, run_id: str, decision: str
    ) -> ReviewCandidate:
        manifest_path = self.lake.root / "manifests" / f"{run_id}.json"
        candidate_dir = self.lake.root / "reviews" / f"candidate={run_id}"
        candidate_path = candidate_dir / "bars.parquet"
        candidate_manifest_path = candidate_dir / "candidate_manifest.json"
        quality_report_path = (
            self.lake.root / "ingestion_reports" / f"{run_id}.json"
        )
        required = {
            "ingestion manifest": manifest_path,
            "candidate bars": candidate_path,
            "candidate manifest": candidate_manifest_path,
            "candidate quality report": quality_report_path,
        }
        missing = [f"{label}: {path}" for label, path in required.items() if not path.exists()]
        if missing:
            raise FileNotFoundError(
                "review candidate is incomplete: " + "; ".join(missing)
            )

        manifest = _read_json(manifest_path, "ingestion manifest")
        candidate_manifest = _read_json(
            candidate_manifest_path, "candidate manifest"
        )
        declared = manifest.get('file_hashes')
        candidate_hashes = candidate_manifest.get('file_hashes')
        if not isinstance(declared, dict) or not isinstance(candidate_hashes, dict) or not candidate_hashes:
            raise DataLineageError('candidate lacks original artifact checksums; reingest required')
        for reference, expected in {**declared, **candidate_hashes}.items():
            artifact = self.lake._resolve_root_relative(reference)
            if not artifact.is_file() or sha256_file(artifact) != expected:
                raise DataLineageError(f'candidate original hash mismatch: {reference}')
        if candidate_hashes.get(path_text(candidate_path, self.lake.root)) != sha256_file(candidate_path):
            raise DataLineageError('candidate bars hash is missing or inconsistent')
        if str(manifest.get("run_id")) != run_id:
            raise DataLineageError("ingestion manifest run_id mismatch")
        if str(candidate_manifest.get("run_id")) != run_id:
            raise DataLineageError("candidate manifest run_id mismatch")
        if str(manifest.get("quality_status")) != "REVIEW_REQUIRED":
            raise DataLineageError(
                f"review candidate {run_id} is not REVIEW_REQUIRED"
            )

        symbol = str(manifest.get("requested_symbol") or "").upper()
        timeframe = str(manifest.get("timeframe") or "").upper()
        if not symbol or not timeframe:
            raise DataLineageError("candidate manifest lacks symbol/timeframe")
        if str(candidate_manifest.get("symbol", "")).upper() != symbol:
            raise DataLineageError("candidate manifest symbol mismatch")
        if str(candidate_manifest.get("timeframe", "")).upper() != timeframe:
            raise DataLineageError("candidate manifest timeframe mismatch")
        instrument = self._instrument_resolver(symbol)

        base_path = candidate_dir / "base.parquet"
        incoming_path = candidate_dir / "incoming.parquet"
        frame = pd.read_parquet(candidate_path)
        if decision == "approve_incoming":
            if not incoming_path.exists():
                raise DataLineageError(
                    "approve_incoming requires an immutable incoming.parquet"
                )
            incoming = pd.read_parquet(incoming_path)
            frame = _merge_incoming(frame, incoming)
        frame = _prepare_approved_frame(frame)

        parent = _parent_version(
            base_path if base_path.exists() else None,
            manifest.get("parent_dataset_version"),
        )
        input_paths = [
            manifest_path,
            candidate_path,
            candidate_manifest_path,
            quality_report_path,
        ]
        if base_path.exists():
            input_paths.append(base_path)
        if incoming_path.exists():
            input_paths.append(incoming_path)
        input_hashes = {
            path_text(path, self.lake.root): sha256_file(path)
            for path in input_paths
        }
        return ReviewCandidate(
            run_id=run_id,
            symbol=symbol,
            timeframe=timeframe,
            instrument=instrument,
            manifest_path=manifest_path,
            manifest=manifest,
            candidate_dir=candidate_dir,
            candidate_path=candidate_path,
            candidate_manifest_path=candidate_manifest_path,
            quality_report_path=quality_report_path,
            base_path=base_path if base_path.exists() else None,
            incoming_path=incoming_path if incoming_path.exists() else None,
            approved_frame=frame,
            parent_dataset_version=parent,
            input_hashes=input_hashes,
        )

    def _load_or_create_plan(
        self,
        candidate: ReviewCandidate,
        *,
        reason: str,
        actor: str,
        decision: str,
    ) -> ApprovalPlan:
        plan_path = candidate.candidate_dir / "approval_plan.json"
        frame_hash = strategy_frame_hash(candidate.approved_frame)
        version = stable_hash(
            {
                "approved_from": candidate.run_id,
                "reason": reason,
                "decision": decision,
                "bars": frame_hash,
            }
        )[:24]
        publication_run_id = f"approval-{version}"

        if plan_path.exists():
            payload = _read_json(plan_path, "approval plan")
            expected = {
                "run_id": candidate.run_id,
                "version": version,
                "publication_run_id": publication_run_id,
                "symbol": candidate.symbol,
                "timeframe": candidate.timeframe,
                "reason": reason,
                "actor": actor,
                "decision": decision,
                "parent_dataset_version": candidate.parent_dataset_version,
                "approved_frame_hash": frame_hash,
                "input_hashes": candidate.input_hashes,
            }
            for key, value in expected.items():
                if payload.get(key) != value:
                    raise DataLineageError(
                        f"approval plan conflicts on immutable field {key}"
                    )
            approved_at = str(payload.get("approved_at") or "")
            if not approved_at:
                raise DataLineageError("approval plan lacks approved_at")
            return ApprovalPlan(
                run_id=candidate.run_id,
                version=version,
                publication_run_id=publication_run_id,
                symbol=candidate.symbol,
                timeframe=candidate.timeframe,
                reason=reason,
                actor=actor,
                decision=decision,
                parent_dataset_version=candidate.parent_dataset_version,
                approved_frame_hash=frame_hash,
                approved_at=approved_at,
                input_hashes=candidate.input_hashes,
                plan_path=plan_path,
            )

        plan = ApprovalPlan(
            run_id=candidate.run_id,
            version=version,
            publication_run_id=publication_run_id,
            symbol=candidate.symbol,
            timeframe=candidate.timeframe,
            reason=reason,
            actor=actor,
            decision=decision,
            parent_dataset_version=candidate.parent_dataset_version,
            approved_frame_hash=frame_hash,
            approved_at=utc_now().isoformat(),
            input_hashes=candidate.input_hashes,
            plan_path=plan_path,
        )
        self.lake.write_json(
            plan.to_dict(),
            plan_path.relative_to(self.lake.root),
            idempotent=True,
        )
        return plan

    def _prepare_artifacts(
        self, candidate: ReviewCandidate, plan: ApprovalPlan
    ) -> ApprovalArtifacts:
        frame = candidate.approved_frame.copy()
        frame["quality_status"] = "CURATED"
        timestamps = pd.to_datetime(frame['timestamp'], utc=True)
        approved_quality = assess_quality(
            NormalizationResult(frame, pd.DataFrame(), [], 0, 0, 0, 0), candidate.instrument,
            candidate.timeframe, provider=str(candidate.manifest.get('data_source') or 'review'),
            requested_start=timestamps.min().to_pydatetime(),
            requested_end=(timestamps.max() + pd.Timedelta(FRAME_DELTAS[candidate.timeframe])).to_pydatetime(),
        )
        if approved_quality.quality_score < 50 or not approved_quality.backtest_suitable:
            raise DataLineageError('approved frame does not meet the unchanged quality policy')
        frame['quality_score'] = approved_quality.quality_score
        frame["dataset_version"] = plan.version
        frame["curated_version"] = plan.version

        curated_path = self.lake.publish_curated(
            frame,
            symbol=candidate.symbol,
            instrument_id=candidate.instrument.instrument_id,
            asset_class=candidate.instrument.asset_class,
            timeframe=candidate.timeframe,
            version=plan.version,
            run_id=plan.publication_run_id,
            activate=False,
        )

        source_quality = _read_json(
            candidate.quality_report_path, "candidate quality report"
        )
        timestamps = pd.to_datetime(frame["timestamp"], utc=True)
        quality_payload = source_quality | approved_quality.to_dict() | {
            "run_id": plan.publication_run_id,
            "publication_run_id": plan.publication_run_id,
            "dataset_version": plan.version,
            "parent_dataset_version": plan.parent_dataset_version,
            "instrument_id": candidate.instrument.instrument_id,
            "approved_from": candidate.run_id,
            "approved_from_dataset_version": candidate.manifest.get(
                "dataset_version"
            ),
            "reason": plan.reason,
            "decision": plan.decision,
            "actor": plan.actor,
            "published_path": path_text(curated_path, self.lake.root),
            "approved_at": plan.approved_at,
            "quality_status": "CURATED",
            "approval_status": "APPROVED",
            "actual_start": timestamps.min().isoformat(),
            "actual_end": timestamps.max().isoformat(),
            "actual_bars": len(frame),
            "stored_row_count": len(frame),
            "complete_row_count": len(frame),
            "incomplete_row_count": 0,
        }
        quality_path = self.lake.write_json(
            quality_payload,
            Path("quality_reports") / f"{plan.version}.json",
            idempotent=True,
        )

        dataset_manifest = DatasetManifest(
            dataset_version=plan.version,
            parent_dataset_version=plan.parent_dataset_version,
            publication_run_id=plan.publication_run_id,
            symbol=candidate.symbol,
            instrument_id=candidate.instrument.instrument_id,
            timeframe=candidate.timeframe,
            full_actual_start=timestamps.min().isoformat(),
            full_actual_end=timestamps.max().isoformat(),
            row_count=len(frame),
            curated_path=path_text(curated_path, self.lake.root),
            curated_sha256=sha256_file(curated_path),
            quality_report_path=path_text(quality_path, self.lake.root),
            quality_report_sha256=sha256_file(quality_path),
            published_at=plan.approved_at,
            cleaning_rule_version=RULE_VERSION,
        )
        dataset_manifest_path = self.lake.write_json(
            dataset_manifest.to_dict(),
            Path("dataset_manifests") / f"{plan.version}.json",
            idempotent=True,
        )

        review_record_path = self.lake.root / "reviews" / f"{plan.run_id}.json"
        review_payload = _review_payload(plan)
        review_hash = sha256_bytes(json_bytes(review_payload))
        intent_path = self.lake.write_json(
            review_payload, candidate.candidate_dir.relative_to(self.lake.root) / 'review_intent.json',
            idempotent=True,
        )

        file_hashes = dict(candidate.input_hashes)
        for artifact in (curated_path, quality_path, dataset_manifest_path):
            file_hashes[path_text(artifact, self.lake.root)] = sha256_file(artifact)
        file_hashes[path_text(plan.plan_path, self.lake.root)] = sha256_file(
            plan.plan_path
        )
        file_hashes[path_text(review_record_path, self.lake.root)] = review_hash
        file_hashes[path_text(intent_path, self.lake.root)] = sha256_file(intent_path)
        receipt_path = self._receipt_path(plan.run_id)
        receipt_hash = sha256_bytes(json_bytes(_receipt_payload(plan)))
        file_hashes[path_text(receipt_path, self.lake.root)] = receipt_hash

        approval_payload = candidate.manifest | {
            "manifest_type": "approval",
            "run_id": plan.publication_run_id,
            "processing_run_id": plan.publication_run_id,
            "publication_run_id": plan.publication_run_id,
            "dataset_version": plan.version,
            "curated_version": plan.version,
            "parent_dataset_version": plan.parent_dataset_version,
            "quality_status": "CURATED",
            "quality_passed": True,
            "approval_status": "APPROVED",
            "approved_from": plan.run_id,
            "approved_from_run_id": plan.run_id,
            "approved_from_dataset_version": candidate.manifest.get(
                "dataset_version"
            ),
            "approval_decision": plan.decision,
            "approval_reason": plan.reason,
            "approval_actor": plan.actor,
            "approved_at": plan.approved_at,
            "approved_curated_path": path_text(curated_path, self.lake.root),
            "approved_curated_sha256": sha256_file(curated_path),
            "approved_quality_report_path": path_text(
                quality_path, self.lake.root
            ),
            "approved_quality_report_sha256": sha256_file(quality_path),
            "dataset_manifest_path": path_text(
                dataset_manifest_path, self.lake.root
            ),
            "dataset_manifest_sha256": sha256_file(dataset_manifest_path),
            "candidate_data_path": path_text(
                candidate.candidate_path, self.lake.root
            ),
            "candidate_data_sha256": sha256_file(candidate.candidate_path),
            "candidate_manifest_path": path_text(
                candidate.candidate_manifest_path, self.lake.root
            ),
            "candidate_manifest_sha256": sha256_file(
                candidate.candidate_manifest_path
            ),
            "ingestion_manifest_path": path_text(
                candidate.manifest_path, self.lake.root
            ),
            "ingestion_manifest_sha256": sha256_file(candidate.manifest_path),
            "review_record_path": path_text(review_record_path, self.lake.root),
            "review_record_sha256": review_hash,
            "review_intent_path": path_text(intent_path, self.lake.root),
            "review_intent_sha256": sha256_file(intent_path),
            "publication_receipt_path": path_text(receipt_path, self.lake.root),
            "publication_receipt_sha256": receipt_hash,
            "approval_plan_path": path_text(plan.plan_path, self.lake.root),
            "approval_plan_sha256": sha256_file(plan.plan_path),
            "file_paths": list(file_hashes),
            "file_hashes": file_hashes,
            "row_count": len(frame),
        }
        approval_manifest_path = self.lake.write_json(
            approval_payload,
            Path("manifests") / f"{plan.publication_run_id}.json",
            idempotent=True,
        )
        return ApprovalArtifacts(
            curated_path=curated_path,
            quality_report_path=quality_path,
            dataset_manifest_path=dataset_manifest_path,
            approval_manifest_path=approval_manifest_path,
            review_record_path=review_record_path,
        )

    def _catalog(
        self,
        candidate: ReviewCandidate,
        plan: ApprovalPlan,
        artifacts: ApprovalArtifacts,
    ) -> None:
        frame = candidate.approved_frame
        timestamps = pd.to_datetime(frame["timestamp"], utc=True)
        self.lake.catalog(
            plan.publication_run_id,
            candidate.symbol,
            candidate.timeframe,
            str(candidate.manifest.get("data_source") or "review"),
            artifacts.approval_manifest_path,
            instrument_id=candidate.instrument.instrument_id,
            status="CURATED",
            version=plan.version,
            asset_class=candidate.instrument.asset_class,
            start_time=timestamps.min().isoformat(),
            end_time=timestamps.max().isoformat(),
            row_count=len(frame),
            schema_version=RULE_VERSION,
            checksum=sha256_file(artifacts.approval_manifest_path),
            raw_source=path_text(candidate.candidate_path, self.lake.root),
        )

    def _write_review_record(self, plan: ApprovalPlan, db=None) -> Path:
        if db is not None:
            # v3 commits the final decision, receipt and current head in the
            # same SQLite transaction. Immutable preparation remains separate.
            return self.lake.store.put_document(
                Path('reviews') / f'{plan.run_id}.json', json_bytes(_review_payload(plan)), db=db,
            )
        path = self.lake.review(
            plan.run_id,
            "approved",
            plan.reason,
            actor=plan.actor,
            published_version=plan.version,
            approval_decision=plan.decision,
            reviewed_at=plan.approved_at,
            _locked=True,
        )
        expected = sha256_bytes(json_bytes(_review_payload(plan)))
        actual = sha256_file(path)
        if actual != expected:
            raise DataLineageError(
                "review record bytes differ from the approval manifest hash"
            )
        return path

    def _verify_artifacts(self, approval_manifest_path: Path, *, pending_review: ApprovalPlan | None = None) -> None:
        pending = None
        if pending_review is not None:
            pending = {
                f'reviews/{pending_review.run_id}.json': json_bytes(_review_payload(pending_review)),
                path_text(self._receipt_path(pending_review.run_id), self.lake.root): json_bytes(_receipt_payload(pending_review)),
            }
        verify_approval_manifest(self.lake, approval_manifest_path, _pending=pending)

    def _activate(
        self,
        candidate: ReviewCandidate,
        plan: ApprovalPlan,
        artifacts: ApprovalArtifacts,
    ) -> None:
        current = self.lake.current_version(candidate.symbol, candidate.timeframe)
        current_version = str(current.get("version")) if current else None
        if current_version not in {plan.parent_dataset_version, plan.version}:
            raise DataLineageError(
                f"stale review approval: candidate parent is "
                f"{plan.parent_dataset_version}, current is {current_version}"
            )
        expected = (
            plan.version
            if current_version == plan.version
            else plan.parent_dataset_version
        )
        receipt = _receipt_payload(plan)
        review_artifacts = {
            f'reviews/{plan.run_id}.json': json_bytes(_review_payload(plan)),
            path_text(self._receipt_path(plan.run_id), self.lake.root): json_bytes(receipt),
        }

        def commit_review(db):
            self._write_review_record(plan, db=db)
            if db is not None:
                self.lake.store.put_document(self._receipt_path(plan.run_id), json_bytes(receipt), db=db)
            else:
                self.lake.write_json(receipt, self._receipt_path(plan.run_id).relative_to(self.lake.root), idempotent=True)

        self.lake.activate_curated(
            candidate.symbol,
            candidate.timeframe,
            plan.version,
            plan.publication_run_id,
            artifacts.curated_path,
            dataset_manifest_path=artifacts.dataset_manifest_path,
            expected_current_version=expected,
            _review_commit=commit_review,
            _review_artifacts=review_artifacts,
        )

    def _read_existing_review(self, run_id: str) -> dict[str, Any] | None:
        path = self.lake.root / "reviews" / f"{run_id}.json"
        if not path.exists():
            return None
        return _read_json(path, "review record")

    @staticmethod
    def _validate_existing_review(
        prior: dict[str, Any],
        *,
        run_id: str,
        reason: str,
        actor: str,
        decision: str,
    ) -> None:
        prior_decision = str(prior.get("decision") or "")
        if prior_decision == "rejected":
            raise DataLineageError(
                f"review {run_id} is rejected; use an explicit reopen workflow"
            )
        if prior_decision != "approved":
            raise DataLineageError(
                f"review {run_id} has unsupported decision {prior_decision!r}"
            )
        expected = {
            "run_id": run_id,
            "reason": reason,
            "actor": actor,
            "approval_decision": decision,
        }
        for key, value in expected.items():
            if str(prior.get(key, "")) != str(value):
                raise DataLineageError(
                    f"review is already approved with a different {key}"
                )


def verify_approval_manifest(lake: DataLake, path: Path, *, _pending: dict[str, bytes] | None = None) -> dict[str, Any]:
    """Verify required artifact roles and every checksum, including final commit."""
    from inv_trend.storage.结构化存储_v3 import StoreError
    approval = _read_json(path, 'approval manifest')
    if approval.get('manifest_type') != 'approval' or approval.get('approval_status') != 'APPROVED':
        raise DataLineageError('invalid approval manifest type or status')
    paths, hashes = approval.get('file_paths'), approval.get('file_hashes')
    if (not isinstance(paths, list) or not isinstance(hashes, dict) or not hashes
            or len(paths) != len(set(paths)) or set(paths) != set(hashes)):
        raise DataLineageError('approval manifest file_paths and file_hashes are inconsistent')
    for role in ('approved_curated', 'approved_quality_report', 'candidate_data', 'candidate_manifest',
                 'review_record', 'ingestion_manifest', 'dataset_manifest', 'approval_plan',
                 'review_intent', 'publication_receipt'):
        reference, expected = approval.get(role + '_path'), approval.get(role + '_sha256')
        if not reference or not expected or hashes.get(reference) != expected:
            raise DataLineageError(f'approval manifest lacks complete {role} binding')
    for raw_path, expected in hashes.items():
        artifact = lake._resolve_root_relative(str(raw_path))
        if not artifact.exists() and _pending is not None and raw_path in _pending:
            actual = sha256_bytes(_pending[raw_path])
        elif not artifact.is_file():
            raise DataLineageError(f'approval manifest artifact is missing: {artifact}')
        else:
            try:
                actual = sha256_file(artifact)
            except (OSError, StoreError) as exc:
                raise DataLineageError(f'approval artifact cannot be verified: {artifact}') from exc
        if actual != str(expected):
            raise DataLineageError(f'approval manifest hash mismatch for {artifact}')
    if _pending is None:
        review = _read_json(lake._resolve_root_relative(approval['review_record_path']), 'review record')
        receipt = _read_json(lake._resolve_root_relative(approval['publication_receipt_path']), 'publication receipt')
        if (review.get('published_version') != approval.get('dataset_version')
                or review.get('run_id') != approval.get('approved_from_run_id')
                or receipt.get('version') != approval.get('dataset_version')
                or receipt.get('run_id') != review.get('run_id')):
            raise DataLineageError('approval review/receipt identity mismatch')
    return approval


def _receipt_payload(plan: ApprovalPlan) -> dict[str, Any]:
    return {'schema_version': 1, 'run_id': plan.run_id, 'version': plan.version,
            'publication_run_id': plan.publication_run_id, 'committed_at': plan.approved_at}


def _prepare_approved_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        raise DataLineageError("review candidate contains no bars")
    result = frame.copy()
    if "timestamp" not in result:
        raise DataLineageError("review candidate has no timestamp column")
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True)
    if "is_complete" in result:
        result = result.loc[result["is_complete"].astype(bool)].copy()
    if result.empty:
        raise DataLineageError("review candidate contains no complete bars")
    order = ["timestamp"]
    if "ingested_at" in result:
        order.append("ingested_at")
    return (
        result.sort_values(order)
        .drop_duplicates("timestamp", keep="last")
        .reset_index(drop=True)
    )


def _merge_incoming(base: pd.DataFrame, incoming: pd.DataFrame) -> pd.DataFrame:
    if incoming.empty:
        return base.copy()
    frames = [part for part in (base, incoming) if not part.empty]
    combined = pd.concat(frames, ignore_index=True, sort=False)
    combined["timestamp"] = pd.to_datetime(combined["timestamp"], utc=True)
    order = ["timestamp"]
    if "ingested_at" in combined:
        order.append("ingested_at")
    return combined.sort_values(order).drop_duplicates("timestamp", keep="last")


def _parent_version(base_path: Path | None, fallback: object) -> str | None:
    if base_path is not None:
        base = pd.read_parquet(base_path, columns=["dataset_version"])
        versions = {
            str(value)
            for value in base["dataset_version"].dropna().astype(str)
            if str(value)
        }
        if len(versions) > 1:
            raise DataLineageError(
                "review candidate base contains multiple dataset versions"
            )
        if versions:
            return next(iter(versions))
    value = str(fallback or "")
    return value or None


def _review_actor(actor: str) -> str:
    return actor or os.getenv("USERNAME") or os.getenv("USER") or "unknown"


def _review_payload(plan: ApprovalPlan) -> dict[str, Any]:
    return {
        "run_id": plan.run_id,
        "decision": "approved",
        "reason": plan.reason,
        "actor": plan.actor,
        "published_version": plan.version,
        "approval_decision": plan.decision,
        "reviewed_at": plan.approved_at,
    }


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DataLineageError(f"{label} is missing: {path}") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise DataLineageError(f"invalid {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise DataLineageError(f"invalid {label} payload: {path}")
    return payload
