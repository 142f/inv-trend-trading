from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time
from typing import Mapping
from uuid import uuid4

import pandas as pd

from .config import load_instruments
from .目录核验 import verify_catalog
from . import 成分股快照 as holdings_snapshots
from .成分股快照 import holding_instrument as _holding_instrument
from .models import (
    DataLineageError, DataQualityError, DataUnavailableError, DatasetManifest, DownloadRequest,
    HoldingsSnapshot, InstrumentConfig, Manifest, ProviderIdentityError, ProviderResult,
    SurvivorshipBiasError,
    path_text, utc_now,
)
from .integrity import sha256_file, stable_hash, strategy_frame_hash
from .lineage import _load_current_lineage, _load_update_lineage, load_current_lineage, load_versioned_lineage
from .processing import FRAME_DELTAS, RULE_VERSION, NormalizationResult, assess_quality, normalize_bars, resample_ohlcv_session
from .providers import BarsProvider, HoldingsCsvProvider, QqqHoldingsCsvProvider
from .registry import InstrumentRegistry
from .review import ReviewApprovalCoordinator
from .storage import open_data_lake

_DEFAULT_ROOT = Path("data")
_OVERLAPS = {"D1": 10, "H4": 20, "H1": 48}


class HistoricalDataService:
    def __init__(self, root: str | Path = _DEFAULT_ROOT, *,
                 instruments: Mapping[str, InstrumentConfig] | None = None,
                 providers: Mapping[str, BarsProvider] | None = None,
                 startup_check: bool = True) -> None:
        self.lake = open_data_lake(root, startup_check=startup_check)
        self.instruments = dict(instruments or load_instruments())
        if instruments is None:
            for symbol, instrument in self._snapshot_instruments().items():
                self.instruments.setdefault(symbol, instrument)
        self.registry = InstrumentRegistry(self.instruments)
        self.providers = dict(providers or {})
        self.review_approvals = ReviewApprovalCoordinator(self.lake, self.instrument)

    def instrument(self, symbol: str) -> InstrumentConfig:
        return self.registry.resolve(symbol)

    def ingest(self, symbol: str, timeframe: str, start: datetime, end: datetime, *, retries: int = 3) -> Manifest:
        instrument = self.instrument(symbol)
        timeframe = timeframe.upper()
        start, end = _bounded_range(instrument, start, end)
        result, errors = self._fetch(instrument, timeframe, start, end, retries)
        # Request identity is unique even when identical payloads are requested for
        # different ranges; content identity is recorded separately as raw snapshot.
        request_id = uuid4().hex[:24]
        raw_path = self.lake.write_raw_payload(
            result.raw_payload or result.frame.to_json(date_format="iso", orient="records").encode(),
            result.source, instrument.instrument_id, request_id, result.raw_payload_suffix,
        )
        raw_frame_path = self.lake.write_raw(result.frame, result.source, instrument.instrument_id, request_id)
        manifest = self._ingest_result(
            instrument, timeframe, start, end, result, request_id, raw_path, raw_frame_path, errors
        )
        if manifest.quality_passed or not instrument.fallback_sources or result.source != instrument.primary_source:
            return manifest
        quality_reason = (
            f"{instrument.primary_source}: quality gate failed "
            f"status={manifest.quality_status}, score={manifest.quality_score}"
        )
        try:
            fallback_result, fallback_errors = self._fetch(
                instrument, timeframe, start, end, retries,
                names=instrument.fallback_sources, primary_error=quality_reason,
            )
        except Exception as exc:
            self.lake.log_event({
                "run_id": manifest.run_id, "instrument_id": instrument.instrument_id,
                "timeframe": timeframe, "provider": instrument.primary_source,
                "severity": "WARNING", "error_type": type(exc).__name__,
                "message": str(exc), "attempt": 1, "action": "fallback_blocked",
                "fallback_provider": ",".join(instrument.fallback_sources),
            })
            return manifest
        fallback_request_id = uuid4().hex[:24]
        fallback_raw = self.lake.write_raw_payload(
            fallback_result.raw_payload or fallback_result.frame.to_json(
                date_format="iso", orient="records"
            ).encode(),
            fallback_result.source, instrument.instrument_id, fallback_request_id,
            fallback_result.raw_payload_suffix,
        )
        fallback_frame = self.lake.write_raw(
            fallback_result.frame, fallback_result.source,
            instrument.instrument_id, fallback_request_id,
        )
        return self._ingest_result(
            instrument, timeframe, start, end, fallback_result, fallback_request_id,
            fallback_raw, fallback_frame, [*errors, quality_reason, *fallback_errors],
        )

    def reprocess_raw(self, symbol: str, timeframe: str, run_id: str) -> Manifest:
        """Re-run preprocessing from an immutable raw frame without any provider call.

        The dataset version is content-addressed, so an identical raw snapshot
        always reproduces the same curated version.
        """
        instrument = self.instrument(symbol)
        timeframe = timeframe.upper()
        raw_path = self.lake.locate_raw(instrument.instrument_id, run_id)
        frame = pd.read_parquet(raw_path)
        time_column = next((name for name in ("timestamp", "time", "date", "open_time") if name in frame), None)
        if time_column is None or frame.empty:
            raise DataLineageError(f"raw frame for {run_id} has no usable timestamp column")
        frame["timestamp"] = pd.to_datetime(frame[time_column], utc=True)
        provider = raw_path.parent.parent.name.split("=", 1)[1]
        stored_metadata: dict[str, object] = {}
        stored_manifest = self.lake.root / "manifests" / f"{run_id}.json"
        if stored_manifest.exists():
            stored_metadata = json.loads(stored_manifest.read_text(encoding="utf-8")).get("provider_metadata", {})
        result = ProviderResult(frame, instrument.source_symbol, provider, instrument.license, dict(stored_metadata))
        start, end = _bounded_range(instrument, pd.Timestamp(frame["timestamp"].min()).to_pydatetime(), utc_now())
        request_id = uuid4().hex[:24]
        return self._ingest_result(instrument, timeframe, start, end, result, request_id, raw_path, raw_path, [])

    def _ingest_result(
        self, instrument: InstrumentConfig, timeframe: str, start: datetime, end: datetime,
        result: ProviderResult, request_id: str, raw_path: Path, raw_frame_path: Path,
        errors: list[str],
    ) -> Manifest:
        raw = result.frame.copy()
        raw_hash = sha256_file(raw_frame_path)
        existing_curated, current_pointer = self._load_update_base(instrument.symbol, timeframe)
        raw_snapshot_id = hashlib.sha256(raw_hash.encode()).hexdigest()[:24]
        normalized = normalize_bars(raw, instrument, timeframe, result.source)
        clean = normalized.clean.copy()
        clean["raw_file_hash"] = raw_hash
        clean["request_id"] = request_id
        clean["raw_snapshot_id"] = raw_snapshot_id
        clean["source_run_id"] = request_id
        outside_window_revisions = self._outside_overlap_revisions(
            instrument.symbol, instrument, timeframe, clean, existing=existing_curated
        )
        normalized_paths = self.lake.write_normalized(clean, instrument.asset_class, instrument.instrument_id, timeframe)
        if not normalized.quarantine.empty:
            self.lake.write_frame(normalized.quarantine, Path("quarantine") / f"run_id={request_id}" / "rows.parquet")
        combined, conflicts, rejected_incoming = self._merge_current(
            instrument.symbol, timeframe, clean, existing=existing_curated,
        )
        full_result = NormalizationResult(combined, pd.DataFrame(), normalized.audit, 0, 0,
                                          int(combined.get("quality_flags", pd.Series(dtype=str)).astype(str).str.contains("extreme_jump_review").sum()),
                                          len(conflicts))
        quality = assess_quality(full_result, instrument, timeframe, provider=result.source,
                                 requested_start=start, requested_end=end)
        quality.provider_available_start = str(result.metadata.get("provider_available_start", ""))
        inherited_research_only = bool(
            combined.get('quality_status', pd.Series(dtype=str)).eq('RESEARCH_ONLY').any()
        )
        status = "RESEARCH_ONLY" if result.metadata.get("research_only") or inherited_research_only else "CURATED"
        quality_ok = quality.quality_score >= 50 and quality.backtest_suitable
        if outside_window_revisions or conflicts:
            # A human decision is required; never silently overwrite published bars.
            status = "REVIEW_REQUIRED"
            publishable = False
        elif not quality_ok:
            status = "QUARANTINED"
            publishable = False
        else:
            publishable = True
        config_hash = stable_hash(asdict(instrument))
        metadata_hash = stable_hash(result.metadata)
        legacy_partial_in_current = (
            not existing_curated.empty
            and bool((~existing_curated["is_complete"].astype(bool)).any())
        )
        complete_mask = combined["is_complete"].astype(bool) if len(combined) else pd.Series(dtype=bool)
        complete_count = int(complete_mask.sum())
        # A rule upgrade (RULE_VERSION) must re-derive the content-addressed
        # version even when the bar content itself is unchanged.
        current_manifest = self._manifest_for_pointer(current_pointer)
        current_rule_matches = current_manifest is not None and current_manifest.cleaning_rule_version == RULE_VERSION
        unchanged_replay = (
            complete_count > 0
            and current_rule_matches
            and request_id not in set(combined.loc[complete_mask, "request_id"].astype(str))
            and not legacy_partial_in_current
        )
        dataset_version = (str(combined.loc[complete_mask, "dataset_version"].iloc[-1]) if unchanged_replay else stable_hash({
            "raw_snapshot_id": raw_snapshot_id, "rule_version": RULE_VERSION,
            "config_hash": config_hash, "metadata_hash": metadata_hash,
            "timeframe": timeframe, "bars": strategy_frame_hash(combined.loc[complete_mask] if complete_mask.any() else combined),
        })[:24])
        combined["dataset_version"] = dataset_version
        combined["curated_version"] = dataset_version
        combined["quality_score"] = quality.quality_score
        combined["quality_status"] = status
        quality.quality_status = status
        # P0: Curated must never fall back to publishing incomplete bars.  With
        # zero complete bars the dataset cannot be published at all.
        if complete_count == 0:
            publishable = False
            if status not in {"REVIEW_REQUIRED"}:
                status = "QUARANTINED"
                quality.quality_status = status
        curated_view = combined.loc[complete_mask] if complete_count else combined.iloc[0:0]
        parent_dataset_version = str(current_pointer["version"]) if current_pointer else None
        is_new_dataset = parent_dataset_version != dataset_version
        curated_path: Path | None = None
        dataset_manifest_path: Path | None = None
        if publishable and is_new_dataset:
            curated_path = self.lake.publish_curated(
                curated_view, symbol=instrument.symbol, instrument_id=instrument.instrument_id,
                asset_class=instrument.asset_class, timeframe=timeframe, version=dataset_version,
                run_id=request_id, activate=False,
            )
            dataset_quality = assess_quality(
                NormalizationResult(curated_view, pd.DataFrame(), [], 0, 0, 0, 0),
                instrument, timeframe, provider=result.source,
                requested_start=curated_view["timestamp"].min().to_pydatetime(),
                requested_end=(curated_view["timestamp"].max() + pd.Timedelta(FRAME_DELTAS[timeframe])).to_pydatetime(),
            )
            dataset_quality.provider_available_start = str(
                result.metadata.get("provider_available_start", "")
            )
            dataset_quality.quality_status = status
            dataset_quality_payload = dataset_quality.to_dict() | {
                "run_id": request_id,
                "publication_run_id": request_id,
                "dataset_version": dataset_version,
                "parent_dataset_version": parent_dataset_version,
                "instrument_id": instrument.instrument_id,
            }
            quality_relative = Path("quality_reports") / f"{dataset_version}.json"
            quality_path = self.lake.write_json(dataset_quality_payload, quality_relative)
            dataset_manifest = DatasetManifest(
                dataset_version=dataset_version,
                parent_dataset_version=parent_dataset_version,
                publication_run_id=request_id,
                symbol=instrument.symbol,
                instrument_id=instrument.instrument_id,
                timeframe=timeframe,
                full_actual_start=curated_view["timestamp"].min().isoformat(),
                full_actual_end=curated_view["timestamp"].max().isoformat(),
                row_count=len(curated_view),
                curated_path=path_text(curated_path, self.lake.root),
                curated_sha256=sha256_file(curated_path),
                quality_report_path=path_text(quality_path, self.lake.root),
                quality_report_sha256=sha256_file(quality_path),
                published_at=utc_now().isoformat(),
                cleaning_rule_version=RULE_VERSION,
            )
            dataset_manifest_path = self.lake.write_json(
                dataset_manifest.to_dict(), Path("dataset_manifests") / f"{dataset_version}.json"
            )
            self.lake.activate_curated(
                instrument.symbol, timeframe, dataset_version, request_id, curated_path,
                dataset_manifest_path=dataset_manifest_path,
                expected_current_version=parent_dataset_version,
            )
        elif publishable and current_pointer is not None:
            curated_path = self.lake._resolve_root_relative(str(current_pointer["path"]))
            manifest_ref = current_pointer.get("dataset_manifest_path")
            if manifest_ref:
                dataset_manifest_path = self.lake._resolve_root_relative(str(manifest_ref))
        elif status == "REVIEW_REQUIRED":
            # Review candidates preserve both sides of every conflict so a human
            # can approve either the published (base) or the incoming values.
            candidate_dir = Path("reviews") / f"candidate={request_id}"
            if not existing_curated.empty:
                self.lake.write_frame(existing_curated, candidate_dir / "base.parquet")
            if not rejected_incoming.empty:
                self.lake.write_frame(rejected_incoming, candidate_dir / "incoming.parquet")
            if conflicts:
                self.lake.write_json({"conflicts": conflicts, "run_id": request_id,
                                      "symbol": instrument.symbol, "timeframe": timeframe,
                                      "created_at": utc_now().isoformat()},
                                     candidate_dir / "conflicts.json")
            self.lake.write_frame(combined, candidate_dir / "bars.parquet")
            candidate_files = [self.lake.root / candidate_dir / name for name in
                               ('bars.parquet', 'base.parquet', 'incoming.parquet', 'conflicts.json')
                               if (self.lake.root / candidate_dir / name).exists()]
            self.lake.write_json({"run_id": request_id, "symbol": instrument.symbol,
                                  "timeframe": timeframe, "instrument_id": instrument.instrument_id,
                                  "dataset_version": dataset_version,
                                  "conflict_count": len(conflicts),
                                  "base_rows": len(existing_curated),
                                  "incoming_rows": len(rejected_incoming),
                                  "file_hashes": {path_text(path, self.lake.root): sha256_file(path)
                                                  for path in candidate_files},
                                  "created_at": utc_now().isoformat()},
                                 candidate_dir / "candidate_manifest.json")
        run_quality_payload = quality.to_dict() | {
            "run_id": request_id, "dataset_version": dataset_version, "instrument_id": instrument.instrument_id,
            "missing_intervals": quality.missing_intervals, "provider_failures": errors,
            "outside_overlap_revisions": outside_window_revisions,
            "conflicting_duplicates": conflicts,
        }
        run_quality_path = self.lake.write_json(
            run_quality_payload, Path("ingestion_reports") / f"{request_id}.json"
        )
        paths: list[Path] = []
        for candidate in (raw_path, raw_frame_path):
            if candidate not in paths:
                paths.append(candidate)
        paths += [*normalized_paths, run_quality_path]
        if status == 'REVIEW_REQUIRED':
            paths.append(self.lake.root / candidate_dir / 'candidate_manifest.json')
        root_path = self.lake.root
        manifest = Manifest(
            run_id=request_id, data_source=result.source, requested_symbol=instrument.symbol,
            actual_symbol=result.actual_symbol, requested_start=start.isoformat(), requested_end=end.isoformat(),
            actual_start=clean["timestamp"].min().isoformat() if len(clean) else None,
            actual_end=clean["timestamp"].max().isoformat() if len(clean) else None,
            timeframe=timeframe, row_count=len(clean),
            file_paths=[path_text(p, root_path) for p in paths],
            file_hashes={path_text(p, root_path): sha256_file(p) for p in paths},
            downloaded_at=utc_now().isoformat(), license=result.license,
            missing_intervals=[str(x) for x in quality.missing_intervals],
            anomaly_count=len(normalized.audit), cleaning_rule_version=RULE_VERSION,
            quality_passed=publishable, provider_metadata={
                **result.metadata, "provider_failures": errors,
                **({'inherited_research_only': True} if inherited_research_only else {}),
            },
            instrument_id=instrument.instrument_id, dataset_version=dataset_version,
            quality_score=quality.quality_score, quality_status=status, request_id=request_id,
            raw_snapshot_id=raw_snapshot_id, processing_run_id=request_id, curated_version=dataset_version,
            config_hash=config_hash, provider_metadata_hash=metadata_hash,
            listing_start=instrument.earliest_valid_date or "",
            # provider_available_start is only recorded when the provider
            # explicitly declares a verified earliest coverage; it is never
            # derived from actual_start.
            provider_available_start=result.metadata.get("provider_available_start", ""),
            requested_start_explicit=start.isoformat(), requested_end_explicit=end.isoformat(),
            actual_end_explicit=(pd.Timestamp(end) - pd.Timedelta(FRAME_DELTAS[timeframe])).isoformat(),
            session_timezone=instrument.session_timezone, bar_close_rule=instrument.bar_close_rule,
            parent_dataset_version=parent_dataset_version if is_new_dataset else None,
            publication_run_id=request_id if publishable and is_new_dataset else (
                str(current_pointer.get("run_id", "")) if current_pointer else ""
            ),
            dataset_manifest_path=path_text(dataset_manifest_path, root_path) if dataset_manifest_path else "",
        )
        manifest_path = self.lake.write_json(manifest.to_dict(), Path("manifests") / f"{request_id}.json")
        self.lake.catalog(request_id, instrument.symbol, timeframe, result.source, manifest_path,
                          instrument_id=instrument.instrument_id, status=status, version=dataset_version,
                          asset_class=instrument.asset_class,
                          start_time=manifest.actual_start or "", end_time=manifest.actual_end or "",
                          row_count=manifest.row_count, schema_version=RULE_VERSION,
                          checksum=sha256_file(manifest_path), raw_source=path_text(raw_frame_path, root_path))
        self.lake.log_event({"run_id": request_id, "instrument_id": instrument.instrument_id, "timeframe": timeframe,
                             "provider": result.source, "severity": "INFO" if publishable else "WARNING",
                             "error_type": "", "message": "published" if publishable else "not published",
                             "attempt": 1, "action": "publish" if publishable else "quarantine",
                             "fallback_provider": ""})
        return manifest

    def _outside_overlap_revisions(
        self, symbol: str, instrument: InstrumentConfig, timeframe: str, incoming: pd.DataFrame,
        *, existing: pd.DataFrame | None = None,
    ) -> list[dict[str, object]]:
        """Return completed-bar corrections preceding the automatic revision window."""
        if existing is None:
            existing, _ = self._load_update_base(symbol, timeframe)
        if existing.empty or incoming.empty:
            return []
        overlap = instrument.revision_overlap_bars or _OVERLAPS[timeframe]
        cutoff = existing["timestamp"].max() - overlap * pd.Timedelta(FRAME_DELTAS[timeframe])
        comparable = [column for column in ("open", "high", "low", "close", "adjusted_close", "volume")
                      if column in existing and column in incoming]
        old = existing.set_index("timestamp")
        revisions: list[dict[str, object]] = []
        for _, row in incoming.loc[incoming["timestamp"] < cutoff].iterrows():
            timestamp = row["timestamp"]
            if timestamp not in old.index:
                continue
            previous = old.loc[timestamp]
            if isinstance(previous, pd.DataFrame):
                previous = previous.iloc[-1]
            if not (bool(previous.get("is_complete", False)) and bool(row.get("is_complete", False))):
                continue
            changed = [
                column for column in comparable
                if not _same_bar_value(previous[column], row[column])
            ]
            if changed:
                revisions.append({"timestamp": timestamp.isoformat(), "changed_fields": changed})
        return revisions

    def _fetch(
        self, instrument: InstrumentConfig, timeframe: str, start: datetime, end: datetime,
        retries: int, *, names: tuple[str, ...] | None = None, primary_error: str = "",
    ):
        names = names or ((instrument.primary_source,) + instrument.fallback_sources)
        errors: list[str] = []
        identity_error: ProviderIdentityError | None = None
        for provider_index, name in enumerate(names):
            is_fallback = name != instrument.primary_source
            provider = self.providers.get(name)
            if provider is None:
                errors.append(f"{name}: provider not registered")
                if not is_fallback:
                    primary_error = errors[-1]
                continue
            for attempt in range(1, retries + 1):
                result: ProviderResult | None = None
                try:
                    requested_timeframe = "H1" if name == "dukascopy" and timeframe == "H4" else timeframe
                    result = provider.fetch(DownloadRequest(instrument, requested_timeframe, start, end))
                    if requested_timeframe != timeframe:
                        result.frame = resample_ohlcv_session(
                            result.frame, timeframe, session_timezone=instrument.session_timezone, close_hour=17
                        )
                        result.metadata = {**result.metadata, "derived_from_timeframe": requested_timeframe,
                                           "session_timezone": instrument.session_timezone, "bar_close_rule": instrument.bar_close_rule}
                    _validate_provider_identity(result, instrument, fallback=is_fallback, timeframe=timeframe)
                    if is_fallback:
                        result.metadata = {
                            **result.metadata,
                            "fallback_used": True,
                            "primary_provider": instrument.primary_source,
                            "fallback_provider": name,
                            "fallback_reason": primary_error or "primary provider failed",
                        }
                        self.lake.log_event({
                            "run_id": "", "instrument_id": instrument.instrument_id,
                            "timeframe": timeframe, "provider": instrument.primary_source,
                            "severity": "WARNING", "error_type": "ProviderFallback",
                            "message": primary_error or "primary provider failed",
                            "attempt": attempt, "action": "fallback_accepted",
                            "fallback_provider": name,
                        })
                    return result, errors
                except Exception as exc:  # provider boundary
                    errors.append(f"{name} attempt {attempt}: {exc}")
                    if not is_fallback:
                        primary_error = errors[-1]
                    elif isinstance(exc, ProviderIdentityError) and result is not None:
                        blocked_id = uuid4().hex[:24]
                        blocked_raw = self.lake.write_raw_payload(
                            result.raw_payload or result.frame.to_json(
                                date_format="iso", orient="records"
                            ).encode(),
                            result.source, instrument.instrument_id, blocked_id,
                            result.raw_payload_suffix,
                        )
                        self.lake.write_raw(
                            result.frame, result.source, instrument.instrument_id, blocked_id
                        )
                        self.lake.log_event({
                            "run_id": blocked_id, "instrument_id": instrument.instrument_id,
                            "timeframe": timeframe, "provider": result.source,
                            "severity": "WARNING", "error_type": type(exc).__name__,
                            "message": f"{exc}; raw={path_text(blocked_raw, self.lake.root)}",
                            "attempt": attempt, "action": "fallback_identity_blocked",
                            "fallback_provider": name,
                        })
                    self.lake.log_event({"run_id": "", "instrument_id": instrument.instrument_id, "timeframe": timeframe,
                                         "provider": name, "severity": "ERROR", "error_type": type(exc).__name__,
                                         "message": str(exc), "attempt": attempt,
                                         "action": "identity_blocked" if isinstance(exc, ProviderIdentityError) else "retry",
                                         "fallback_provider": name if is_fallback else ""})
                    if isinstance(exc, ProviderIdentityError):
                        identity_error = exc
                        break
                    if not is_fallback and not _fallback_eligible(exc):
                        raise
                    if attempt < retries:
                        time.sleep(min(2 ** (attempt - 1), 4))
        if identity_error is not None:
            raise ProviderIdentityError(
                "fallback identity blocked: " + str(identity_error)
            ) from identity_error
        raise RuntimeError("all configured providers failed: " + " | ".join(errors))

    def _merge_current(
        self,
        symbol: str,
        timeframe: str,
        incoming: pd.DataFrame,
        *, existing: pd.DataFrame | None = None,
    ) -> tuple[pd.DataFrame, list[dict[str, object]], pd.DataFrame]:
        """Merge bars while quarantining completed-bar OHLCV conflicts."""
        if existing is None:
            existing, _ = self._load_update_base(symbol, timeframe)
        conflicts: list[dict[str, object]] = []
        rejected: list[pd.Series] = []
        if not existing.empty and not incoming.empty:
            prior = existing.set_index("timestamp")
            value_columns = [
                column
                for column in (
                    "open", "high", "low", "close", "adjusted_close",
                    "volume", "is_complete",
                )
                if column in existing and column in incoming
            ]
            keep_incoming: list[bool] = []
            for _, row in incoming.iterrows():
                old = (
                    prior.loc[row["timestamp"]]
                    if row["timestamp"] in prior.index
                    else None
                )
                if isinstance(old, pd.DataFrame):
                    old = old.iloc[-1]
                if old is None or not bool(old.get("is_complete", False)):
                    keep_incoming.append(True)
                    continue
                changed = [
                    column
                    for column in value_columns
                    if not _same_bar_value(old[column], row[column])
                ]
                if not changed:
                    keep_incoming.append(False)
                    continue
                conflicts.append(
                    {
                        "timestamp": row["timestamp"].isoformat(),
                        "existing_value": {
                            column: None if pd.isna(old[column]) else old[column]
                            for column in value_columns
                        },
                        "incoming_value": {
                            column: None if pd.isna(row[column]) else row[column]
                            for column in value_columns
                        },
                        "changed_fields": changed,
                    }
                )
                rejected.append(row)
                keep_incoming.append(False)
            incoming = incoming.loc[keep_incoming]

        non_empty = [frame for frame in (existing, incoming) if not frame.empty]
        if not non_empty:
            combined = pd.DataFrame(columns=incoming.columns)
        elif len(non_empty) == 1:
            combined = non_empty[0].copy()
        else:
            combined = pd.concat(non_empty, ignore_index=True, sort=False)
        order = ["timestamp"] + (["ingested_at"] if "ingested_at" in combined else [])
        if not combined.empty:
            combined = (
                combined.sort_values(order)
                .drop_duplicates("timestamp", keep="last")
                .reset_index(drop=True)
            )
        rejected_frame = (
            pd.DataFrame(rejected, columns=incoming.columns)
            if rejected
            else pd.DataFrame()
        )
        return combined, conflicts, rejected_frame

    def missing_intervals(
        self, symbol: str, timeframe: str, *, end: datetime | None = None
    ) -> list[dict[str, object]]:
        """Compute exactly which ranges are missing for one instrument/timeframe.

        Interior gaps are only reported when the calendar is exact (24x7); for
        business-day calendars a persisted quality report is the authoritative
        gap source instead.
        """
        instrument = self.instrument(symbol)
        timeframe = timeframe.upper()
        end = pd.Timestamp(_aware_utc(end or utc_now()))
        delta = pd.Timedelta(FRAME_DELTAS[timeframe])
        overlap = instrument.revision_overlap_bars or _OVERLAPS[timeframe]
        existing, _ = self._load_update_base(instrument.symbol, timeframe)
        intervals: list[dict[str, object]] = []
        if existing.empty:
            if not instrument.earliest_valid_date:
                raise ValueError("cold download requires earliest_valid_date")
            # Reuse the most recent request range when a prior ingest was
            # quarantined; requesting all the way back to the listing date
            # would re-introduce head-truncation for instruments whose data
            # legitimately starts later than the verified listing.
            requested_start = self._last_requested_start(symbol, timeframe)
            earliest = pd.Timestamp(instrument.earliest_valid_date, tz="UTC")
            start_bound = max(requested_start, earliest) if requested_start is not None else earliest
            if start_bound <= end:
                intervals.append({"start": start_bound.to_pydatetime(), "end": end.to_pydatetime(),
                                  "classification": "full"})
            return intervals
        stamps = pd.to_datetime(existing["timestamp"], utc=True).drop_duplicates().sort_values()
        start, stop = stamps.iloc[0], stamps.iloc[-1]
        if instrument.session == "24x7":
            expected = pd.date_range(start, stop, freq=delta)
            absent = expected.difference(pd.DatetimeIndex(stamps))
            for group in _group_consecutive(absent, delta):
                intervals.append({
                    "start": max(group[0] - overlap * delta, start).to_pydatetime(),
                    "end": group[-1].to_pydatetime(), "classification": "provider_gap",
                })
        tail_start = stop + delta
        if tail_start <= end:
            intervals.append({"start": max(stop - overlap * delta, start).to_pydatetime(),
                              "end": end.to_pydatetime(), "classification": "tail"})
        return intervals

    def update_gaps(self, symbol: str, timeframe: str, *, end: datetime | None = None) -> list[Manifest]:
        """Fill interior data gaps without touching the current tail."""
        manifests: list[Manifest] = []
        for interval in self.missing_intervals(symbol, timeframe, end=end):
            if interval["classification"] != "provider_gap":
                continue
            manifests.append(self.ingest(symbol, timeframe, interval["start"], interval["end"]))
        return manifests

    def update(
        self, symbol: str, timeframe: str, *, end: datetime | None = None,
        overlap_bars: int | None = None, retries: int = 3,
    ) -> Manifest:
        instrument = self.instrument(symbol)
        timeframe = timeframe.upper()
        manifests: list[Manifest] = []
        for interval in self.missing_intervals(symbol, timeframe, end=end):
            start, end_point = interval["start"], interval["end"]
            if overlap_bars is not None and interval["classification"] == "tail":
                base, _ = self._load_update_base(instrument.symbol, timeframe)
                last = base["timestamp"].max().to_pydatetime()
                start = last - overlap_bars * pd.Timedelta(FRAME_DELTAS[timeframe]).to_pytimedelta()
            manifests.append(self.ingest(symbol, timeframe, start, end_point, retries=retries))
        if manifests:
            return manifests[-1]
        current = self._current_manifest(symbol, timeframe)
        if current is not None:
            return current
        raise RuntimeError(f"no missing intervals and no current manifest for {symbol}/{timeframe}")

    def _last_requested_start(self, symbol: str, timeframe: str) -> pd.Timestamp | None:
        """Latest requested_start recorded for this symbol/timeframe, else None."""
        with self.lake._connect() as db:
            rows = db.execute(
                "SELECT run_id FROM datasets WHERE symbol=? AND timeframe=? "
                "ORDER BY created_at DESC LIMIT 1",
                (symbol.upper(), timeframe.upper()),
            ).fetchall()
        for (run_id,) in rows:
            manifest_file = self.lake.root / "manifests" / f"{run_id}.json"
            if manifest_file.exists():
                payload = json.loads(manifest_file.read_text(encoding="utf-8"))
                if payload.get("requested_start"):
                    return pd.Timestamp(payload["requested_start"])
        return None

    def _current_manifest(self, symbol: str, timeframe: str) -> Manifest | None:
        current = self.lake.current_version(symbol.upper(), timeframe.upper())
        return self._manifest_for_pointer(current)

    def _manifest_for_pointer(self, current: dict | None) -> Manifest | None:
        if not current:
            return None
        run_id = current.get("run_id")
        if run_id:
            path = self.lake.root / "manifests" / f"{run_id}.json"
            if path.exists():
                return Manifest(**json.loads(path.read_text(encoding="utf-8")))
        return None

    def _load_update_base(self, symbol: str, timeframe: str) -> tuple[pd.DataFrame, dict | None]:
        """Pin and verify the exact base used by merge and activation's CAS.

        A concurrent head change during validation fails closed. A later head
        change is rejected by activate_curated(expected_current_version=...).
        Missing files under an existing pointer are corruption, not a cold start.
        """
        pointer = self.lake.current_version(symbol, timeframe)
        if pointer is None:
            if self.lake._current_catalog_row(symbol, timeframe) is not None:
                raise DataLineageError(f'pointer/catalog drift for {symbol}/{timeframe}')
            return pd.DataFrame(), None
        try:
            frame = self.lake.read_bars_version(symbol, timeframe, str(pointer.get('version') or ''))
        except FileNotFoundError as exc:
            raise DataLineageError(f'update base artifact is missing for {symbol}/{timeframe}') from exc
        lineage = _load_update_lineage(self.lake, symbol, timeframe, bars=frame)
        if lineage.pointer != pointer:
            raise DataLineageError(f'update base changed during validation for {symbol}/{timeframe}')
        if lineage.dataset_manifest.get('instrument_id') != self.instrument(symbol).instrument_id:
            raise DataLineageError('update base instrument identity differs from registered instrument')
        return frame, pointer

    def update_many(self, symbols: list[str], timeframe: str, *, end: datetime | None = None) -> dict[str, object]:
        """Best-effort batch update: one failure never prevents other instruments."""
        report: dict[str, object] = {"timeframe": timeframe.upper(), "succeeded": [], "failed": [], "skipped": []}
        for symbol in symbols:
            try:
                manifest = self.update(symbol, timeframe, end=end)
                report["succeeded"].append({"symbol": symbol.upper(), "version": manifest.dataset_version,
                                            "rows": manifest.row_count})
            except Exception as exc:  # batch boundary
                category = "skipped" if type(exc).__name__ == "InstrumentNotImplementedError" else "failed"
                report[category].append({"symbol": symbol.upper(), "error_type": type(exc).__name__, "message": str(exc)})
                self.lake.log_event({"run_id": "", "instrument_id": symbol.upper(), "timeframe": timeframe.upper(),
                                     "provider": "", "severity": "WARNING", "error_type": type(exc).__name__,
                                     "message": str(exc), "attempt": 1, "action": category, "fallback_provider": ""})
        return report

    def load_bars(self, symbol: str, timeframe: str, start: datetime | None = None, end: datetime | None = None,
                  adjusted: bool = True, *, completed_only: bool = True, allow_research: bool = False,
                  allow_legacy: bool = False, min_quality_score: float = 50.0) -> pd.DataFrame:
        try:
            instrument = self.instrument(symbol)
        except KeyError:
            if not allow_legacy:
                raise
            instrument = _legacy_instrument(symbol)
        using_legacy = False
        try:
            frame = self.lake.read_bars(symbol.upper(), timeframe.upper())
        except FileNotFoundError:
            if self.lake.current_version(symbol.upper(), timeframe.upper()) is not None:
                raise DataLineageError(f"published {symbol.upper()}/{timeframe.upper()} artifact is unavailable") from None
            if not allow_legacy:
                raise DataUnavailableError(f"no published {symbol.upper()}/{timeframe.upper()} bars meet the quality policy") from None
            frame = self.lake.read_legacy_bars(symbol.upper(), timeframe.upper())
            using_legacy = True
        lineage = None
        if not using_legacy:
            # Verify the unfiltered, strategy-visible dataset before applying a
            # caller's range or completion policy.  Otherwise a malformed
            # current pointer, Catalog row, or quality report could be masked
            # by a filtered subset.
            lineage = load_current_lineage(
                self.lake,
                symbol,
                timeframe,
                bars=frame,
            )
        quality_report = ""
        if lineage is not None:
            if lineage.dataset_manifest.get('instrument_id') != instrument.instrument_id:
                raise DataLineageError('dataset instrument identity differs from registered instrument')
            quality_report = str(lineage.quality_report_path)
        return self._strategy_visible_bars(
            frame,
            symbol=symbol,
            timeframe=timeframe,
            instrument=instrument,
            start=start,
            end=end,
            adjusted=adjusted,
            completed_only=completed_only,
            allow_research=allow_research,
            allow_legacy=allow_legacy,
            min_quality_score=min_quality_score,
            quality_report=quality_report,
            quality_contract=lineage.quality_report if lineage is not None else None,
        )

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
        allow_legacy: bool = False,
        min_quality_score: float = 50.0,
    ) -> pd.DataFrame:
        """Load one immutable curated version, independently of ``current``.

        ``dataset_version`` is the stage hand-off identity, not a hint.  The
        method verifies its versioned lineage before applying the caller's
        visibility filters, so an activated newer pointer cannot change a
        resumed D1 scan's inputs.
        """

        if allow_legacy:
            raise DataQualityError(
                "version-pinned reads do not support legacy-only datasets"
            )
        instrument = self.instrument(symbol)
        version = str(dataset_version or "")
        if not version:
            raise DataLineageError(
                f"dataset version is required for {symbol.upper()}/{timeframe.upper()}"
            )
        frame = self.lake.read_bars_version(symbol.upper(), timeframe.upper(), version)
        lineage = load_versioned_lineage(
            self.lake,
            symbol,
            timeframe,
            version,
            bars=frame,
        )
        if lineage.dataset_manifest.get('instrument_id') != instrument.instrument_id:
            raise DataLineageError('dataset instrument identity differs from registered instrument')
        return self._strategy_visible_bars(
            frame,
            symbol=symbol,
            timeframe=timeframe,
            instrument=instrument,
            start=start,
            end=end,
            adjusted=adjusted,
            completed_only=completed_only,
            allow_research=allow_research,
            allow_legacy=False,
            min_quality_score=min_quality_score,
            quality_report=str(lineage.quality_report_path),
            quality_contract=lineage.quality_report,
        )

    @staticmethod
    def _strategy_visible_bars(
        frame: pd.DataFrame,
        *,
        symbol: str,
        timeframe: str,
        instrument: InstrumentConfig,
        start: datetime | None,
        end: datetime | None,
        adjusted: bool,
        completed_only: bool,
        allow_research: bool,
        allow_legacy: bool,
        min_quality_score: float,
        quality_report: str,
        quality_contract: Mapping[str, object] | None = None,
    ) -> pd.DataFrame:
        """Apply the existing D1 visibility/adjustment policy to verified bars."""

        if completed_only:
            frame = frame.loc[frame["is_complete"].astype(bool)]
        allowed = {"CURATED"}
        if allow_research:
            allowed.add("RESEARCH_ONLY")
        if allow_legacy:
            allowed.add("LEGACY_ONLY")
        frame = frame.loc[frame["quality_status"].isin(allowed)]
        frame = frame.loc[
            pd.to_numeric(frame["quality_score"], errors="coerce") >= min_quality_score
        ]
        if frame.empty:
            raise DataQualityError(f"no {symbol}/{timeframe} bars meet requested quality policy")
        if start is not None:
            frame = frame.loc[frame["timestamp"] >= pd.Timestamp(_aware_utc(start))]
        if end is not None:
            frame = frame.loc[frame["timestamp"] <= pd.Timestamp(_aware_utc(end))]
        if adjusted and instrument.asset_class == "equity" and "adjusted_close" in frame:
            factor = frame["adjusted_close"] / frame["close"]
            for column in ("open", "high", "low", "close"):
                frame[f"unadjusted_{column}"] = frame[column]
                frame[column] = frame[column] * factor
        frame = (
            frame.sort_values("timestamp")
            .drop_duplicates("timestamp", keep="last")
            .reset_index(drop=True)
        )
        frame.attrs.update(
            {
                "symbol": symbol.upper(),
                "instrument_id": instrument.instrument_id,
                "timeframe": timeframe.upper(),
                "adjustment": instrument.adjustment_policy if adjusted else "none",
                "data_sources": sorted(frame["data_source"].dropna().unique().tolist()),
                "dataset_version": str(frame["dataset_version"].iloc[-1]),
                "quality_report": quality_report,
                "quality_contract": ({key: quality_contract.get(key) for key in
                    ('dataset_version', 'instrument_id', 'symbol', 'timeframe', 'quality_status',
                     'backtest_suitable', 'missing_intervals', 'actual_bars')}
                    if quality_contract is not None else None),
            }
        )
        return frame

    def status(self, symbol: str, timeframe: str) -> dict[str, object]:
        current = self.lake.current_version(symbol.upper(), timeframe.upper())
        if not current:
            return {"symbol": symbol.upper(), "timeframe": timeframe.upper(), "status": "MISSING"}
        bars = self.lake.read_bars(symbol.upper(), timeframe.upper())
        return _published_status(symbol, timeframe, current, bars)

    def verify_catalog(self) -> dict[str, object]:
        return verify_catalog(self.lake)


    def coverage(self, symbol: str, timeframe: str) -> dict[str, object]:
        """Return coverage only after verifying the complete current lineage."""
        current = self.lake.current_version(symbol.upper(), timeframe.upper())
        if not current:
            return {"symbol": symbol.upper(), "timeframe": timeframe.upper(), "status": "MISSING"}
        bars = self.lake.read_bars(symbol.upper(), timeframe.upper())
        result = _published_status(symbol, timeframe, current, bars)
        lineage = load_current_lineage(
            self.lake,
            symbol,
            timeframe,
            bars=bars,
        )
        return result | {
            "quality_report": str(lineage.quality_report_path),
            "missing_intervals": lineage.quality_report["missing_intervals"],
        }

    def diagnose_current(self, symbol: str, timeframe: str) -> dict[str, object]:
        """Read verified metadata, including explicit rejection, without serving bars.

        This diagnostic cannot grant strategy eligibility. Identity, hashes,
        pointer/catalog agreement and approval validation remain mandatory.
        """
        instrument = self.instrument(symbol)
        frame = timeframe.upper()
        if frame not in FRAME_DELTAS:
            raise ValueError('unsupported diagnostic timeframe')
        if self.lake.current_version(instrument.symbol, frame) is None:
            raise DataUnavailableError(f'formal dataset is missing for {instrument.symbol}/{frame}')
        lineage = _load_current_lineage(self.lake, instrument.symbol, frame,
                                        bars=None, require_certified=False)
        if lineage.dataset_manifest['instrument_id'] != instrument.instrument_id:
            raise DataLineageError('diagnostic manifest instrument does not match registry')
        report = lineage.quality_report
        certified = (report['backtest_suitable'] is True and report['quality_status'] == 'CURATED'
                     and report['missing_intervals'] == [])
        return {'diagnostic_only': True, 'dataset_version': lineage.dataset_version,
                'certified': certified, 'quality_report': report,
                'manifest': lineage.dataset_manifest, 'pointer': lineage.pointer}


    def repair_current_lineage(self, symbol: str, timeframe: str) -> dict[str, object]:
        """Materialize formal dataset lineage for a pre-separation current pointer."""
        symbol, timeframe = symbol.upper(), timeframe.upper()
        current = self.lake.current_version(symbol, timeframe)
        if not current:
            raise FileNotFoundError(f"no current dataset for {symbol}/{timeframe}")
        existing_ref = current.get("dataset_manifest_path")
        if existing_ref and self.lake._resolve_root_relative(str(existing_ref)).exists():
            return {"symbol": symbol, "timeframe": timeframe, "version": current["version"],
                    "action": "unchanged", "dataset_manifest_path": str(existing_ref)}
        frame = self.lake.read_bars(symbol, timeframe)
        instrument = self.instrument(symbol)
        version = str(current["version"])
        publication_run_id = str(current["run_id"])
        quality = assess_quality(
            NormalizationResult(frame, pd.DataFrame(), [], 0, 0, 0, 0),
            instrument, timeframe,
            provider=str(frame["data_source"].iloc[-1]) if "data_source" in frame else "",
            requested_start=frame["timestamp"].min().to_pydatetime(),
            requested_end=(frame["timestamp"].max() + pd.Timedelta(FRAME_DELTAS[timeframe])).to_pydatetime(),
        )
        quality.quality_status = "CURATED"
        quality_path = self.lake.write_json(
            quality.to_dict() | {
                "run_id": publication_run_id, "publication_run_id": publication_run_id,
                "dataset_version": version, "parent_dataset_version": None,
                "instrument_id": instrument.instrument_id,
            },
            Path("quality_reports") / f"{version}.dataset.json",
        )
        curated_path = self.lake._resolve_root_relative(str(current["path"]))
        dataset_manifest = DatasetManifest(
            dataset_version=version, parent_dataset_version=None,
            publication_run_id=publication_run_id, symbol=symbol,
            instrument_id=instrument.instrument_id, timeframe=timeframe,
            full_actual_start=frame["timestamp"].min().isoformat(),
            full_actual_end=frame["timestamp"].max().isoformat(), row_count=len(frame),
            curated_path=path_text(curated_path, self.lake.root), curated_sha256=sha256_file(curated_path),
            quality_report_path=path_text(quality_path, self.lake.root),
            quality_report_sha256=sha256_file(quality_path), published_at=utc_now().isoformat(),
            cleaning_rule_version=RULE_VERSION,
        )
        dataset_manifest_path = self.lake.write_json(
            dataset_manifest.to_dict(), Path("dataset_manifests") / f"{version}.json"
        )
        self.lake.activate_curated(
            symbol, timeframe, version, publication_run_id, curated_path,
            dataset_manifest_path=dataset_manifest_path,
        )
        return {"symbol": symbol, "timeframe": timeframe, "version": version, "action": "repaired",
                "dataset_manifest_path": path_text(dataset_manifest_path, self.lake.root),
                "quality_report_path": path_text(quality_path, self.lake.root)}

    def approve_review(
        self,
        run_id: str,
        reason: str = "",
        *,
        actor: str = "",
        decision: str = "approve_existing",
    ) -> str:
        """Publish a review candidate through the durable approval coordinator."""
        return self.review_approvals.approve(
            run_id,
            reason,
            actor=actor,
            decision=decision,
        )

    def reject_review(self, run_id: str, reason: str, *, actor: str = '') -> Path:
        return self.lake.review(run_id, 'rejected', reason, actor=actor)

    def reopen_review(self, run_id: str, reason: str, *, actor: str = '') -> str:
        return self.review_approvals.reopen(run_id, reason, actor=actor)

    def update_holdings(
        self, provider: HoldingsCsvProvider, snapshot_date: str | None = None, *, top: int = 50
    ) -> HoldingsSnapshot:
        return holdings_snapshots.update_holdings(self.lake, provider, snapshot_date, top=top)

    def load_holdings(
        self, funds: tuple[str, ...] = ("QQQ", "SPY"), as_of: str | datetime | None = None,
        *, top: int = 50,
    ) -> pd.DataFrame:
        return holdings_snapshots.load_holdings(self.lake, funds, as_of, top=top)

    def _snapshot_instruments(self) -> dict[str, InstrumentConfig]:
        try:
            holdings = self.load_holdings()
        except (FileNotFoundError, SurvivorshipBiasError):
            return {}
        return {str(row.symbol): _holding_instrument(str(row.symbol)) for row in holdings.itertuples()}

    def sync_holdings_universe(
        self, *, timeframe: str = "D1", start: datetime, end: datetime | None = None
    ) -> dict[str, list[dict[str, str]]]:
        holdings = self.load_holdings()
        end = end or utc_now()
        report: dict[str, list[dict[str, str]]] = {"succeeded": [], "failed": []}
        for symbol in holdings["symbol"].astype(str):
            try:
                if self.lake.current_version(symbol, timeframe):
                    manifest = self.update(symbol, timeframe, end=end)
                else:
                    manifest = self.ingest(symbol, timeframe, start, end)
                if self.lake.current_version(symbol, timeframe) is None:
                    raise DataQualityError(
                        f"{symbol}/{timeframe} was fetched but not published: "
                        f"status={manifest.quality_status}, score={manifest.quality_score}"
                    )
                report["succeeded"].append({"symbol": symbol, "version": manifest.dataset_version})
            except Exception as exc:
                report["failed"].append({"symbol": symbol, "error": str(exc), "error_type": type(exc).__name__})
        return report

    def update_qqq_holdings(self, provider: QqqHoldingsCsvProvider, snapshot_date: str | None = None) -> Path:
        return holdings_snapshots.update_qqq_holdings(self.lake, provider, snapshot_date)

    def load_qqq_holdings(self, as_of: str | datetime | None = None) -> pd.DataFrame:
        return holdings_snapshots.load_qqq_holdings(self.lake, as_of)


def load_bars(symbol: str, timeframe: str, start: datetime | None = None, end: datetime | None = None,
              adjusted: bool = True, completed_only: bool = True, *, root: str | Path = _DEFAULT_ROOT,
              allow_research: bool = False, allow_legacy: bool = False, min_quality_score: float = 50.0) -> pd.DataFrame:
    return HistoricalDataService(root).load_bars(symbol, timeframe, start, end, adjusted,
                                                  completed_only=completed_only, allow_research=allow_research,
                                                  allow_legacy=allow_legacy, min_quality_score=min_quality_score)


def _bounded_range(instrument: InstrumentConfig, start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start, end = _aware_utc(start), _aware_utc(end)
    if instrument.earliest_valid_date:
        start = max(start, pd.Timestamp(instrument.earliest_valid_date, tz="UTC").to_pydatetime())
    if start > end:
        raise ValueError("requested range ends before verified instrument history")
    return start, end


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime boundaries must be timezone-aware")
    return value.astimezone(timezone.utc)


def _same_bar_value(left: object, right: object) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    try:
        return bool(abs(float(left) - float(right)) <= 1e-12)
    except (TypeError, ValueError):
        return left == right


def _validate_provider_identity(
    result: ProviderResult, instrument: InstrumentConfig, *, fallback: bool, timeframe: str
) -> None:
    """Reject a fallback that explicitly reports a different economic series."""
    actual = str(result.actual_symbol).upper().replace("-", "").replace("_", "")
    expected = instrument.source_symbol.upper().replace("-", "").replace("_", "")
    if actual != expected:
        raise ProviderIdentityError(
            f"provider symbol {result.actual_symbol!r} does not match {instrument.source_symbol!r}"
        )
    # Validate explicit row identity before normalization overwrites canonical
    # columns. A provider's request echo cannot override contradictory inputs.
    expected_columns = {
        'symbol': instrument.symbol, 'instrument_id': instrument.instrument_id,
        'source_symbol': instrument.source_symbol, 'timeframe': timeframe,
        'asset_class': instrument.asset_class, 'market': instrument.market,
    }
    aliases = {'1D': 'D1', '4H': 'H4', '1H': 'H1'}
    for column, expected_value in expected_columns.items():
        if column not in result.frame:
            continue
        values = result.frame[column]
        if values.isna().any():
            raise ProviderIdentityError(f'provider row identity {column} is missing')
        def canonical_identity(value):
            token = str(value).strip()
            if column == 'source_symbol':
                return token.upper().replace('-', '').replace('_', '')
            if column == 'timeframe':
                return aliases.get(token.upper(), token.upper())
            return token.upper()
        expected_token = canonical_identity(expected_value)
        if any(canonical_identity(value) != expected_token for value in values):
            raise ProviderIdentityError(f'provider row identity {column} contradicts requested instrument/timeframe')
    if not fallback:
        return
    reported = result.metadata.get("instrument_identity", {})
    if not isinstance(reported, Mapping):
        raise ProviderIdentityError("fallback must report instrument_identity metadata")
    required = {
        "instrument_type": instrument.instrument_type,
        "venue": instrument.venue,
        "currency": instrument.currency,
        "price_basis": instrument.price_basis,
        "adjustment_method": instrument.adjustment_method,
        "session_timezone": instrument.session_timezone,
        "bar_close_rule": instrument.bar_close_rule,
        "ohlc_definition": "provider_native",
    }
    mismatches = {
        key: {"expected": expected_value, "actual": reported.get(key)}
        for key, expected_value in required.items()
        if reported.get(key) != expected_value
    }
    if mismatches:
        raise ProviderIdentityError(f"fallback instrument identity mismatch: {mismatches}")


def _fallback_eligible(exc: Exception) -> bool:
    """Only operational/provider-data failures may trigger a source switch."""
    return isinstance(
        exc,
        (ConnectionError, OSError, RuntimeError, TimeoutError, ValueError),
    )


def _legacy_instrument(symbol: str) -> InstrumentConfig:
    return InstrumentConfig(symbol=symbol.upper(), source_symbol=symbol.upper(), instrument_id=f"{symbol.upper()}.LEGACY.CSV",
                            asset_class="legacy", market="legacy", instrument_type="spot", quote_currency="USD",
                            timezone="UTC", session="24x7", primary_source="legacy_csv")




def _group_consecutive(stamps: pd.DatetimeIndex, delta: pd.Timedelta) -> list[list[pd.Timestamp]]:
    groups: list[list[pd.Timestamp]] = []
    for stamp in stamps:
        if groups and stamp - groups[-1][-1] == delta:
            groups[-1].append(stamp)
        else:
            groups.append([stamp])
    return groups


def _published_status(symbol, timeframe, current, bars):
    return {"symbol": symbol.upper(), "timeframe": timeframe.upper(), "status": "PUBLISHED", "version": current["version"],
                "actual_start": bars["timestamp"].min().isoformat(), "actual_end": bars["timestamp"].max().isoformat(),
                "latest_complete_bar": bars.loc[bars["is_complete"].astype(bool), "timestamp"].max().isoformat()}
