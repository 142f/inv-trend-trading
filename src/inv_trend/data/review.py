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
from .processing import RULE_VERSION
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
            self._write_review_record(plan)
            self._verify_artifacts(artifacts.approval_manifest_path)
            self._activate(candidate, plan, artifacts)
            return plan.version

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
        if "quality_score" in frame:
            frame["quality_score"] = pd.to_numeric(
                frame["quality_score"], errors="coerce"
            ).fillna(100.0)
        else:
            frame["quality_score"] = 100.0
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
        quality_payload = source_quality | {
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

        file_hashes = dict(candidate.input_hashes)
        for artifact in (curated_path, quality_path, dataset_manifest_path):
            file_hashes[path_text(artifact, self.lake.root)] = sha256_file(artifact)
        file_hashes[path_text(plan.plan_path, self.lake.root)] = sha256_file(
            plan.plan_path
        )
        file_hashes[path_text(review_record_path, self.lake.root)] = review_hash

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

    def _write_review_record(self, plan: ApprovalPlan) -> Path:
        path = self.lake.review(
            plan.run_id,
            "approved",
            plan.reason,
            actor=plan.actor,
            published_version=plan.version,
            approval_decision=plan.decision,
            reviewed_at=plan.approved_at,
        )
        expected = sha256_bytes(json_bytes(_review_payload(plan)))
        actual = sha256_file(path)
        if actual != expected:
            raise DataLineageError(
                "review record bytes differ from the approval manifest hash"
            )
        return path

    def _verify_artifacts(self, approval_manifest_path: Path) -> None:
        approval = _read_json(approval_manifest_path, "approval manifest")
        paths = list(approval.get("file_paths") or [])
        hashes = dict(approval.get("file_hashes") or {})
        if set(paths) != set(hashes):
            raise DataLineageError(
                "approval manifest file_paths and file_hashes are inconsistent"
            )
        for raw_path, expected in hashes.items():
            artifact = self.lake._resolve_root_relative(str(raw_path))
            if not artifact.exists():
                raise DataLineageError(
                    f"approval manifest artifact is missing: {artifact}"
                )
            actual = sha256_file(artifact)
            if actual != str(expected):
                raise DataLineageError(
                    f"approval manifest hash mismatch for {artifact}"
                )

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
        self.lake.activate_curated(
            candidate.symbol,
            candidate.timeframe,
            plan.version,
            plan.publication_run_id,
            artifacts.curated_path,
            dataset_manifest_path=artifacts.dataset_manifest_path,
            expected_current_version=expected,
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
