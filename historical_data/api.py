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
from .models import (
    DataLineageError, DataQualityError, DatasetManifest, DownloadRequest,
    HoldingsSnapshot, InstrumentConfig, Manifest, ProviderIdentityError, ProviderResult,
    SurvivorshipBiasError,
    path_text, utc_now,
)
from .processing import FRAME_DELTAS, RULE_VERSION, NormalizationResult, assess_quality, normalize_bars, resample_ohlcv_session
from .providers import BarsProvider, HoldingsCsvProvider, QqqHoldingsCsvProvider
from .registry import InstrumentRegistry
from .storage import DataLake, sha256_file

_DEFAULT_ROOT = Path("data")
_OVERLAPS = {"D1": 10, "H4": 20, "H1": 48}


class HistoricalDataService:
    def __init__(self, root: str | Path = _DEFAULT_ROOT, *,
                 instruments: Mapping[str, InstrumentConfig] | None = None,
                 providers: Mapping[str, BarsProvider] | None = None) -> None:
        self.lake = DataLake(root)
        self.instruments = dict(instruments or load_instruments())
        if instruments is None:
            for symbol, instrument in self._snapshot_instruments().items():
                self.instruments.setdefault(symbol, instrument)
        self.registry = InstrumentRegistry(self.instruments)
        self.providers = dict(providers or {})

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
        raw_snapshot_id = hashlib.sha256(raw_hash.encode()).hexdigest()[:24]
        normalized = normalize_bars(raw, instrument, timeframe, result.source)
        clean = normalized.clean.copy()
        clean["raw_file_hash"] = raw_hash
        clean["request_id"] = request_id
        clean["raw_snapshot_id"] = raw_snapshot_id
        clean["source_run_id"] = request_id
        outside_window_revisions = self._outside_overlap_revisions(
            instrument.symbol, instrument, timeframe, clean
        )
        normalized_paths = self.lake.write_normalized(clean, instrument.asset_class, instrument.instrument_id, timeframe)
        if not normalized.quarantine.empty:
            self.lake.write_frame(normalized.quarantine, Path("quarantine") / f"run_id={request_id}" / "rows.parquet")
        combined, conflicts, rejected_incoming = self._merge_current(instrument.symbol, timeframe, clean)
        full_result = NormalizationResult(combined, pd.DataFrame(), normalized.audit, 0, 0,
                                          int(combined.get("quality_flags", pd.Series(dtype=str)).astype(str).str.contains("extreme_jump_review").sum()),
                                          len(conflicts))
        quality = assess_quality(full_result, instrument, timeframe, provider=result.source,
                                 requested_start=start, requested_end=end)
        quality.provider_available_start = str(result.metadata.get("provider_available_start", ""))
        status = "RESEARCH_ONLY" if result.metadata.get("research_only") else "CURATED"
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
        config_hash = _stable_hash(asdict(instrument))
        metadata_hash = _stable_hash(result.metadata)
        try:
            existing_curated = self.lake.read_bars(instrument.symbol, timeframe)
        except FileNotFoundError:
            existing_curated = pd.DataFrame()
        legacy_partial_in_current = (
            not existing_curated.empty
            and bool((~existing_curated["is_complete"].astype(bool)).any())
        )
        complete_mask = combined["is_complete"].astype(bool) if len(combined) else pd.Series(dtype=bool)
        complete_count = int(complete_mask.sum())
        # A rule upgrade (RULE_VERSION) must re-derive the content-addressed
        # version even when the bar content itself is unchanged.
        current_manifest = self._current_manifest(instrument.symbol, timeframe)
        current_rule_matches = current_manifest is not None and current_manifest.cleaning_rule_version == RULE_VERSION
        unchanged_replay = (
            complete_count > 0
            and current_rule_matches
            and request_id not in set(combined.loc[complete_mask, "request_id"].astype(str))
            and not legacy_partial_in_current
        )
        dataset_version = (str(combined.loc[complete_mask, "dataset_version"].iloc[-1]) if unchanged_replay else _stable_hash({
            "raw_snapshot_id": raw_snapshot_id, "rule_version": RULE_VERSION,
            "config_hash": config_hash, "metadata_hash": metadata_hash,
            "timeframe": timeframe, "bars": _frame_hash(combined.loc[complete_mask] if complete_mask.any() else combined),
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
        current_pointer = self.lake.current_version(instrument.symbol, timeframe)
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
            self.lake.write_json({"run_id": request_id, "symbol": instrument.symbol,
                                  "timeframe": timeframe, "instrument_id": instrument.instrument_id,
                                  "dataset_version": dataset_version,
                                  "conflict_count": len(conflicts),
                                  "base_rows": len(existing_curated),
                                  "incoming_rows": len(rejected_incoming),
                                  "created_at": utc_now().isoformat()},
                                 candidate_dir / "candidate_manifest.json")
            self.lake.write_frame(combined, candidate_dir / "bars.parquet")
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
            quality_passed=publishable, provider_metadata={**result.metadata, "provider_failures": errors},
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
    ) -> list[dict[str, object]]:
        """Return completed-bar corrections preceding the automatic revision window."""
        try:
            existing = self.lake.read_bars(symbol, timeframe)
        except FileNotFoundError:
            return []
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
                    _validate_provider_identity(result, instrument, fallback=is_fallback)
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

    def _merge_current(self, symbol: str, timeframe: str, incoming: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, object]], pd.DataFrame]:
        """Merge incoming bars with the published view; detect OHLCV conflicts.

        Exact duplicates are dropped silently with an audit note.  Conflicting
        duplicates (same timestamp, different OHLCV) are never silently
        overwritten: the incoming rows are returned separately so a review
        candidate can preserve both sides of the conflict.
        """
        try:
            existing = self.lake.read_bars(symbol, timeframe)
        except FileNotFoundError:
            existing = pd.DataFrame(columns=incoming.columns)
        conflicts: list[dict[str, object]] = []
        rejected: list[pd.DataFrame] = []
        if not existing.empty and not incoming.empty:
            prior = existing.set_index("timestamp")
            value_columns = [c for c in ("open", "high", "low", "close", "adjusted_close", "volume", "is_complete")
                             if c in existing and c in incoming]
            keep_incoming: list[bool] = []
            for _, row in incoming.iterrows():
                old = prior.loc[row["timestamp"]] if row["timestamp"] in prior.index else None
                if isinstance(old, pd.DataFrame):
                    old = old.iloc[-1]
                if old is None:
                    keep_incoming.append(True)
                    continue
                if not bool(old.get("is_complete", False)):
                    # The published bar was not closed yet; its values may still
                    # evolve.  The incoming (newer) bar supersedes it.
                    keep_incoming.append(True)
                    continue
                if all(_same_bar_value(old[c], row[c]) for c in value_columns):
                    keep_incoming.append(False)  # exact duplicate of published bar
                    continue
                # Same timestamp, different OHLCV: never silently keep=last.
                changed = [c for c in value_columns if not _same_bar_value(old[c], row[c])]
                conflicts.append({"timestamp": row["timestamp"].isoformat(),
                                  "existing_value": {c: (None if pd.isna(old[c]) else old[c]) for c in value_columns},
                                  "incoming_value": {c: (None if pd.isna(row[c]) else row[c]) for c in value_columns},
                                  "changed_fields": changed})
                rejected.append(row)
                keep_incoming.append(False)
            incoming = incoming.loc[keep_incoming]
        combined = pd.concat([existing, incoming], ignore_index=True, sort=False)
        rejected_frame = pd.DataFrame(rejected, columns=incoming.columns) if rejected else pd.DataFrame()
        return (combined.sort_values(["timestamp", "ingested_at"]).drop_duplicates("timestamp", keep="last").reset_index(drop=True),
                conflicts, rejected_frame)

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
        try:
            existing = self.lake.read_bars(instrument.symbol, timeframe)
        except FileNotFoundError:
            existing = pd.DataFrame()
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
                last = self.lake.read_bars(instrument.symbol, timeframe)["timestamp"].max().to_pydatetime()
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
        try:
            with self.lake._connect() as db:
                rows = db.execute(
                    "SELECT run_id FROM datasets WHERE symbol=? AND timeframe=? "
                    "ORDER BY created_at DESC LIMIT 1",
                    (symbol.upper(), timeframe.upper()),
                ).fetchall()
        except Exception:
            rows = []
        for (run_id,) in rows:
            manifest_file = self.lake.root / "manifests" / f"{run_id}.json"
            if manifest_file.exists():
                payload = json.loads(manifest_file.read_text(encoding="utf-8"))
                if payload.get("requested_start"):
                    return pd.Timestamp(payload["requested_start"])
        return None

    def _current_manifest(self, symbol: str, timeframe: str) -> Manifest | None:
        try:
            current = self.lake.current_version(symbol.upper(), timeframe.upper())
        except Exception:
            return None
        if not current:
            return None
        run_id = current.get("run_id")
        if run_id:
            path = self.lake.root / "manifests" / f"{run_id}.json"
            if path.exists():
                return Manifest(**json.loads(path.read_text(encoding="utf-8")))
        return None

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
        try:
            frame = self.lake.read_bars(symbol.upper(), timeframe.upper())
        except FileNotFoundError:
            if not allow_legacy:
                raise DataQualityError(f"no published {symbol.upper()}/{timeframe.upper()} bars meet the quality policy") from None
            frame = self.lake.read_legacy_bars(symbol.upper(), timeframe.upper())
        if completed_only:
            frame = frame.loc[frame["is_complete"].astype(bool)]
        allowed = {"CURATED"}
        if allow_research:
            allowed.add("RESEARCH_ONLY")
        if allow_legacy:
            allowed.add("LEGACY_ONLY")
        frame = frame.loc[frame["quality_status"].isin(allowed)]
        frame = frame.loc[pd.to_numeric(frame["quality_score"], errors="coerce") >= min_quality_score]
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
        frame = frame.sort_values("timestamp").drop_duplicates("timestamp", keep="last").reset_index(drop=True)
        quality_report = ""
        if str(frame["quality_status"].iloc[-1]) != "LEGACY_ONLY":
            quality_report = self._current_quality_report_path(symbol, timeframe)
        frame.attrs.update({"symbol": symbol.upper(), "instrument_id": instrument.instrument_id,
                            "timeframe": timeframe.upper(), "adjustment": instrument.adjustment_policy if adjusted else "none",
                            "data_sources": sorted(frame["data_source"].dropna().unique().tolist()),
                            "dataset_version": str(frame["dataset_version"].iloc[-1]),
                            "quality_report": quality_report})
        return frame

    def status(self, symbol: str, timeframe: str) -> dict[str, object]:
        current = self.lake.current_version(symbol.upper(), timeframe.upper())
        if not current:
            return {"symbol": symbol.upper(), "timeframe": timeframe.upper(), "status": "MISSING"}
        bars = self.lake.read_bars(symbol.upper(), timeframe.upper())
        return {"symbol": symbol.upper(), "timeframe": timeframe.upper(), "status": "PUBLISHED", "version": current["version"],
                "actual_start": bars["timestamp"].min().isoformat(), "actual_end": bars["timestamp"].max().isoformat(),
                "latest_complete_bar": bars.loc[bars["is_complete"].astype(bool), "timestamp"].max().isoformat()}

    def verify_catalog(self) -> dict[str, object]:
        """Audit every catalog row against its manifest and every declared artifact.

        Reports valid records plus separately missing raw / normalized /
        quality / curated artifacts, hash mismatches, dangling or
        legacy-contaminated current pointers, and pointer-vs-catalog drift.
        A missing file is never hidden behind a "0 hash mismatch" number.
        """
        result = {"valid_dataset_records": 0, "missing_manifest_records": [],
                  "missing_raw_records": [], "missing_normalized_records": [],
                  "missing_quality_report_records": [], "missing_curated_records": [],
                  "missing_dataset_manifest_records": [],
                  "dangling_current_records": [], "hash_mismatch_records": [],
                  "legacy_records": 0, "total_records": 0,
                  "legacy_contaminated_current": [], "pointer_catalog_mismatch": [],
                  "valid_current_records": 0}
        with self.lake._connect() as db:
            rows = db.execute(
                "SELECT run_id, symbol, timeframe, status, version, manifest_path, checksum "
                "FROM datasets ORDER BY symbol, timeframe"
            ).fetchall()
        for run_id, symbol, timeframe, status, version, manifest_path, checksum in rows:
            result["total_records"] += 1
            if status == "LEGACY_ONLY":
                result["legacy_records"] += 1
                continue
            manifest_file = self.lake.root / "manifests" / f"{run_id}.json"
            if not manifest_file.exists():
                result["missing_manifest_records"].append({"run_id": run_id, "symbol": symbol, "timeframe": timeframe})
                continue
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
            bucket = {"raw": [], "normalized": [], "quality": [], "curated": []}
            for raw_path in manifest.get("file_paths", []):
                candidate = self._resolve_lake_path(raw_path)
                portable = raw_path.replace("\\", "/")
                if portable.startswith("raw/") or "/raw/" in portable:
                    bucket["raw"].append((raw_path, candidate))
                elif portable.startswith("normalized/") or "/normalized/" in portable:
                    bucket["normalized"].append((raw_path, candidate))
                elif portable.startswith("ingestion_reports/"):
                    bucket["quality"].append((raw_path, candidate))
                elif (portable.startswith("curated/") or "/curated/" in portable) and raw_path.endswith("bars.parquet"):
                    bucket["curated"].append((raw_path, candidate))
            for kind, key in (("raw", "missing_raw_records"), ("normalized", "missing_normalized_records"),
                              ("quality", "missing_quality_report_records"), ("curated", "missing_curated_records")):
                missing = [p for p, resolved in bucket[kind] if not resolved.exists()]
                if missing and kind == "curated" and status in {"QUARANTINED", "REVIEW_REQUIRED"}:
                    continue  # quarantined/review rows legitimately have no curated artifact
                if missing:
                    result[key].append({"run_id": run_id, "symbol": symbol, "timeframe": timeframe,
                                        "missing_files": missing, "version": version})
            bad = []
            for raw_path in manifest.get("file_paths", []):
                candidate = self._resolve_lake_path(raw_path)
                if candidate.exists() and sha256_file(candidate) != manifest["file_hashes"].get(raw_path):
                    bad.append(raw_path)
            if bad:
                result["hash_mismatch_records"].append({"run_id": run_id, "symbol": symbol, "files": bad})
                continue
            # A record is valid only when every declared artifact exists and hashes match.
            all_declared_exist = all(
                self._resolve_lake_path(p).exists() for p in manifest.get("file_paths", [])
            )
            if all_declared_exist:
                result["valid_dataset_records"] += 1
        for pointer in (self.lake.root / "curated").glob("symbol=*/timeframe=*/*.json"):
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            raw_target = str(payload.get("path", ""))
            target = self._resolve_lake_path(raw_target)
            if not target.exists():
                result["dangling_current_records"].append({
                    "pointer": str(pointer), "version": payload.get("version"),
                    "expected_path": raw_target,
                })
            elif "/asset_class=legacy/" in raw_target.replace("\\", "/"):
                result["legacy_contaminated_current"].append({
                    "pointer": str(pointer), "version": payload.get("version"),
                    "path": raw_target,
                })
        # Pointer vs catalog drift for the formal channel.
        with self.lake._connect() as db:
            catalog_current = {
                (row[0].upper(), row[1].upper()): (row[2], row[3])
                for row in db.execute("SELECT symbol, timeframe, version, run_id FROM current_versions").fetchall()
            }
        for symbol_dir in (self.lake.root / "curated").glob("symbol=*"):
            symbol = symbol_dir.name.split("=", 1)[1].upper()
            for timeframe_dir in symbol_dir.glob("timeframe=*"):
                timeframe = timeframe_dir.name.split("=", 1)[1].upper()
                pointer = self.lake.current_version(symbol, timeframe)
                catalog_entry = catalog_current.get((symbol, timeframe))
                catalog_version = catalog_entry[0] if catalog_entry else None
                catalog_run_id = catalog_entry[1] if catalog_entry else None
                if pointer is not None and (
                    pointer.get("version") != catalog_version or pointer.get("run_id") != catalog_run_id
                ):
                    result["pointer_catalog_mismatch"].append({
                        "symbol": symbol, "timeframe": timeframe,
                        "pointer_version": pointer.get("version"), "catalog_version": catalog_version,
                        "pointer_run_id": pointer.get("run_id"), "catalog_run_id": catalog_run_id,
                    })
                if pointer is None:
                    continue
                dataset_manifest_ref = pointer.get("dataset_manifest_path")
                dataset_manifest_path = (
                    self._resolve_lake_path(str(dataset_manifest_ref)) if dataset_manifest_ref
                    else self.lake.root / "dataset_manifests" / f"{pointer.get('version')}.json"
                )
                if not dataset_manifest_path.exists():
                    result["missing_dataset_manifest_records"].append({
                        "symbol": symbol, "timeframe": timeframe,
                        "version": pointer.get("version"), "path": str(dataset_manifest_path),
                    })
                    continue
                dataset_manifest = json.loads(dataset_manifest_path.read_text(encoding="utf-8"))
                curated = self._resolve_lake_path(str(dataset_manifest.get("curated_path", "")))
                quality = self._resolve_lake_path(str(dataset_manifest.get("quality_report_path", "")))
                dataset_ok = bool(
                    dataset_manifest.get("dataset_version") == pointer.get("version")
                    and dataset_manifest.get("publication_run_id") == pointer.get("run_id")
                    and curated.exists() and quality.exists()
                    and sha256_file(curated) == dataset_manifest.get("curated_sha256")
                    and sha256_file(quality) == dataset_manifest.get("quality_report_sha256")
                )
                if dataset_ok:
                    report = json.loads(quality.read_text(encoding="utf-8"))
                    frame = pd.read_parquet(curated, columns=["dataset_version"])
                    dataset_ok = bool(
                        report.get("dataset_version") == pointer.get("version")
                        and not frame.empty
                        and set(frame["dataset_version"].astype(str)) == {str(pointer.get("version"))}
                        and len(frame) == int(dataset_manifest.get("row_count", -1))
                    )
                if dataset_ok and pointer.get("version") == catalog_version:
                    result["valid_current_records"] += 1
                elif not dataset_ok:
                    result["hash_mismatch_records"].append({
                        "run_id": pointer.get("run_id"), "symbol": symbol,
                        "files": [str(dataset_manifest_path)], "kind": "dataset_lineage",
                    })
        return result

    def _resolve_lake_path(self, raw_path: str) -> Path:
        """Resolve a manifest/pointer path against the data root (portable paths).

        Accepts root-relative paths (``curated/...``), project-relative legacy
        paths (``data\\curated\\...``) and absolute paths for backwards compat.
        """
        candidate = Path(raw_path)
        if candidate.is_absolute():
            return candidate
        root_relative = self.lake.root / candidate
        if root_relative.exists():
            return root_relative
        if candidate.exists():
            return candidate
        return root_relative

    def coverage(self, symbol: str, timeframe: str) -> dict[str, object]:
        """Return current coverage plus the persisted gap-classification report."""
        result = self.status(symbol, timeframe)
        if result["status"] == "MISSING":
            return result
        bars = self.lake.read_bars(symbol.upper(), timeframe.upper())
        dataset_version = str(bars["dataset_version"].iloc[-1])
        report_path = Path(self._current_quality_report_path(symbol, timeframe))
        if not report_path.exists():
            raise DataLineageError(f"missing quality report for current dataset version: {dataset_version}")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        recorded = report.get("dataset_version") or report.get("run_id")
        if recorded != dataset_version:
            raise DataLineageError(f"quality report version mismatch: expected {dataset_version}")
        return result | {
            "quality_report": str(report_path),
            "missing_intervals": report.get("missing_intervals", []),
        }

    def _current_quality_report_path(self, symbol: str, timeframe: str) -> str:
        current = self.lake.current_version(symbol.upper(), timeframe.upper())
        if not current or not current.get("dataset_manifest_path"):
            raise DataLineageError(f"current dataset manifest is missing for {symbol.upper()}/{timeframe.upper()}")
        dataset_manifest_path = self.lake._resolve_root_relative(str(current["dataset_manifest_path"]))
        if not dataset_manifest_path.exists():
            raise DataLineageError(f"current dataset manifest is missing: {dataset_manifest_path}")
        dataset_manifest = json.loads(dataset_manifest_path.read_text(encoding="utf-8"))
        return str(self.lake._resolve_root_relative(str(dataset_manifest["quality_report_path"])))

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

    def approve_review(self, run_id: str, reason: str = "", *, actor: str = "",
                       decision: str = "approve_existing") -> str:
        """Publish a new audited version from an immutable review candidate.

        ``decision`` selects which side of a conflict wins:
        - ``approve_existing``: keep the published (base) values;
        - ``approve_incoming``: replace conflicting bars with incoming values.
        Reject is handled through ``lake.review`` and cannot be approved later.
        """
        if decision not in {"approve_existing", "approve_incoming"}:
            raise ValueError("decision must be approve_existing or approve_incoming")
        manifest_path = self.lake.root / "manifests" / f"{run_id}.json"
        candidate_dir = self.lake.root / "reviews" / f"candidate={run_id}"
        candidate_path = candidate_dir / "bars.parquet"
        review_path = self.lake.root / "reviews" / f"{run_id}.json"
        if review_path.exists():
            prior = json.loads(review_path.read_text(encoding="utf-8"))
            if prior.get("decision") == "approved":
                if prior.get("reason", "") != reason:
                    raise DataLineageError("review is already approved with a different reason")
                return str(prior["published_version"])
            raise DataLineageError(f"review is already {prior.get('decision')}; it cannot be approved")
        if not manifest_path.exists() or not candidate_path.exists():
            raise FileNotFoundError(f"review candidate not found: {run_id}")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        frame = pd.read_parquet(candidate_path)
        symbol = str(manifest["requested_symbol"])
        instrument = self.instrument(symbol)
        incoming_path = candidate_dir / "incoming.parquet"
        if decision == "approve_incoming" and incoming_path.exists():
            incoming = pd.read_parquet(incoming_path)
            if not incoming.empty:
                combined = pd.concat([frame, incoming], ignore_index=True, sort=False)
                combined = combined.sort_values(["timestamp", "ingested_at"]).drop_duplicates("timestamp", keep="last").reset_index(drop=True)
                # Incoming rows carry their own metadata columns; normalize the
                # audited version's quality fields for the whole frame.
                combined["quality_status"] = "CURATED"
                combined["quality_score"] = pd.to_numeric(combined.get("quality_score"), errors="coerce").fillna(100.0)
                frame = combined
        version = _stable_hash({"approved_from": run_id, "reason": reason, "decision": decision,
                                "bars": _frame_hash(frame)})[:24]
        # Approval is an audit decision.  Publication policy remains CURATED so
        # Repository callers can read the newly approved complete version.
        frame["quality_status"] = "CURATED"
        frame["dataset_version"] = version
        frame["curated_version"] = version
        candidate_report = self.lake.root / "ingestion_reports" / f"{run_id}.json"
        if not candidate_report.exists():
            raise DataLineageError(f"review candidate has no quality report: {manifest['dataset_version']}")
        path = self.lake.publish_curated(frame, symbol=symbol, instrument_id=instrument.instrument_id,
                                         asset_class=instrument.asset_class, timeframe=str(manifest["timeframe"]),
                                         version=version, run_id=run_id, activate=False)
        source_quality = json.loads(candidate_report.read_text(encoding="utf-8"))
        report = source_quality | {"approved_from": run_id, "dataset_version": version, "reason": reason,
                                   "decision": decision, "actor": actor,
                                   "published_path": path_text(path, self.lake.root),
                                   "approved_at": utc_now().isoformat(),
                                   "quality_status": "CURATED", "approval_status": "APPROVED"}
        self.lake.write_json(report, Path("quality_reports") / f"{version}.json")
        approved_report_path = self.lake.root / "quality_reports" / f"{version}.json"
        review_record_path = self.lake.review(run_id, "approved", reason, actor=actor, published_version=version)
        approval_manifest = manifest | {"run_id": f"approval-{version}", "processing_run_id": f"approval-{version}",
                                        "dataset_version": version, "curated_version": version,
                                        "quality_status": "CURATED", "quality_passed": True,
                                        "approval_status": "APPROVED", "approved_from": run_id,
                                        "approval_decision": decision,
                                        "approved_from_dataset_version": manifest.get("dataset_version"),
                                        "approval_reason": reason, "approved_at": utc_now().isoformat(),
                                        "approved_curated_path": path_text(path, self.lake.root),
                                        "approved_curated_sha256": sha256_file(path),
                                        "approved_quality_report_path": path_text(approved_report_path, self.lake.root),
                                        "approved_quality_report_sha256": sha256_file(approved_report_path),
                                        "candidate_data_path": path_text(candidate_path, self.lake.root),
                                        "candidate_data_sha256": sha256_file(candidate_path),
                                        "candidate_manifest_path": path_text(manifest_path, self.lake.root),
                                        "candidate_manifest_sha256": sha256_file(manifest_path),
                                        "review_record_path": path_text(review_record_path, self.lake.root),
                                        "review_record_sha256": sha256_file(review_record_path),
                                        "file_paths": [path_text(path, self.lake.root), path_text(approved_report_path, self.lake.root)],
                                        "file_hashes": {path_text(path, self.lake.root): sha256_file(path),
                                                        path_text(approved_report_path, self.lake.root): sha256_file(approved_report_path)},
                                        "row_count": len(frame)}
        approval_path = self.lake.write_json(approval_manifest, Path("manifests") / f"approval-{version}.json")
        self.lake.catalog(f"approval-{version}", symbol, str(manifest["timeframe"]), str(manifest["data_source"]), approval_path,
                          instrument_id=instrument.instrument_id, status="CURATED", version=version)
        # All immutable artifacts now exist and their hashes are recorded. Only
        # then may the strategy-visible current pointer advance.
        parent = self.lake.current_version(symbol, str(manifest["timeframe"]))
        dataset_manifest = DatasetManifest(
            dataset_version=version,
            parent_dataset_version=str(parent["version"]) if parent else None,
            publication_run_id=f"approval-{version}", symbol=symbol,
            instrument_id=instrument.instrument_id, timeframe=str(manifest["timeframe"]),
            full_actual_start=pd.to_datetime(frame["timestamp"], utc=True).min().isoformat(),
            full_actual_end=pd.to_datetime(frame["timestamp"], utc=True).max().isoformat(),
            row_count=len(frame), curated_path=path_text(path, self.lake.root),
            curated_sha256=sha256_file(path),
            quality_report_path=path_text(approved_report_path, self.lake.root),
            quality_report_sha256=sha256_file(approved_report_path),
            published_at=utc_now().isoformat(), cleaning_rule_version=RULE_VERSION,
        )
        dataset_manifest_path = self.lake.write_json(
            dataset_manifest.to_dict(), Path("dataset_manifests") / f"{version}.json"
        )
        self.lake.activate_curated(
            symbol, str(manifest["timeframe"]), version, f"approval-{version}", path,
            dataset_manifest_path=dataset_manifest_path,
        )
        return version

    def update_holdings(
        self, provider: HoldingsCsvProvider, snapshot_date: str | None = None, *, top: int = 50
    ) -> HoldingsSnapshot:
        frame = provider.fetch(snapshot_date, top=top)
        if len(frame) < top:
            raise ValueError(f"holdings snapshot requires at least {top} unique rows, got {len(frame)}")
        dates = frame["snapshot_date"].unique()
        if len(dates) != 1:
            raise ValueError("one holdings file must represent exactly one snapshot date")
        fund = str(frame["fund"].iloc[0]).upper()
        relative = Path("reference") / "universes" / "etf_holdings" / f"fund={fund}" / f"snapshot_date={dates[0]}" / f"top{top}.parquet"
        path = self.lake.write_frame(frame, relative)
        return HoldingsSnapshot(fund, str(dates[0]), top, path_text(path, self.lake.root), len(frame))

    def load_holdings(
        self, funds: tuple[str, ...] = ("QQQ", "SPY"), as_of: str | datetime | None = None,
        *, top: int = 50,
    ) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for requested in funds:
            fund = requested.upper()
            base = self.lake.root / "reference" / "universes" / "etf_holdings" / f"fund={fund}"
            paths = list(base.glob(f"snapshot_date=*/top{top}.parquet"))
            if fund == "QQQ" and not paths and top == 20:
                paths = list((self.lake.root / "reference" / "universes" / "qqq_holdings").glob("snapshot_date=*/top20.parquet"))
            snapshots = sorted((p.parent.name.split("=", 1)[1], p) for p in paths)
            target = snapshots[-1] if snapshots and as_of is None else next(
                (item for item in reversed(snapshots) if item[0] <= pd.Timestamp(as_of).date().isoformat()), None
            )
            if target is None:
                raise SurvivorshipBiasError(f"no {fund} holdings snapshot on or before requested date")
            frame = pd.read_parquet(target[1]).head(top).copy()
            if "fund" not in frame:
                frame["fund"] = fund
            if "rank" not in frame:
                frame["rank"] = range(1, len(frame) + 1)
            frames.append(frame)
        combined = pd.concat(frames, ignore_index=True)
        rows = []
        for symbol, group in combined.groupby("symbol", sort=False):
            rows.append({
                "symbol": symbol,
                "funds": ",".join(group["fund"].astype(str)),
                "ranks": ",".join(f"{f}:{int(r)}" for f, r in zip(group["fund"], group["rank"])),
                "weights": ",".join(f"{f}:{float(w):.6g}" for f, w in zip(group["fund"], group["weight"])),
                "snapshot_dates": ",".join(f"{f}:{d}" for f, d in zip(group["fund"], group["snapshot_date"])),
            })
        result = pd.DataFrame(rows)
        result.attrs["funds"] = tuple(f.upper() for f in funds)
        return result

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
        frame = provider.fetch(snapshot_date, top=20)
        dates = frame["snapshot_date"].unique()
        path = self.lake.write_frame(frame, Path("reference") / "universes" / "qqq_holdings" / f"snapshot_date={dates[0]}" / "top20.parquet")
        return path

    def load_qqq_holdings(self, as_of: str | datetime | None = None) -> pd.DataFrame:
        paths = list((self.lake.root / "reference" / "universes" / "qqq_holdings").glob("snapshot_date=*/top20.parquet"))
        if not paths:
            raise FileNotFoundError("no QQQ holdings snapshots available")
        snapshots = sorted((p.parent.name.split("=", 1)[1], p) for p in paths)
        target = snapshots[-1] if as_of is None else next((item for item in reversed(snapshots) if item[0] <= pd.Timestamp(as_of).date().isoformat()), None)
        if target is None:
            raise SurvivorshipBiasError("no holdings snapshot on or before requested date")
        frame = pd.read_parquet(target[1])
        frame.attrs.update({"snapshot_date": target[0], "survivorship_bias_safe": True})
        return frame


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
    result: ProviderResult, instrument: InstrumentConfig, *, fallback: bool
) -> None:
    """Reject a fallback that explicitly reports a different economic series."""
    actual = str(result.actual_symbol).upper().replace("-", "").replace("_", "")
    expected = instrument.source_symbol.upper().replace("-", "").replace("_", "")
    if actual != expected:
        raise ProviderIdentityError(
            f"provider symbol {result.actual_symbol!r} does not match {instrument.source_symbol!r}"
        )
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


def _stable_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _frame_hash(frame: pd.DataFrame) -> str:
    lineage = {"ingested_at", "request_id", "raw_snapshot_id", "source_run_id", "dataset_version", "curated_version",
               "raw_file_hash", "quality_score", "quality_status", "quality_flags"}
    cols = [col for col in frame.columns if col not in lineage]
    return hashlib.sha256(pd.util.hash_pandas_object(frame[cols], index=False).values.tobytes()).hexdigest()


def _legacy_instrument(symbol: str) -> InstrumentConfig:
    return InstrumentConfig(symbol=symbol.upper(), source_symbol=symbol.upper(), instrument_id=f"{symbol.upper()}.LEGACY.CSV",
                            asset_class="legacy", market="legacy", instrument_type="spot", quote_currency="USD",
                            timezone="UTC", session="24x7", primary_source="legacy_csv")


def _holding_instrument(symbol: str) -> InstrumentConfig:
    symbol = symbol.upper()
    return InstrumentConfig(
        symbol=symbol, source_symbol=symbol.replace(".", "-"), instrument_id=f"{symbol}.US.EQUITY",
        asset_class="equity", market="xnas", venue="xnas",
        instrument_type="equity", quote_currency="USD", currency="USD",
        timezone="America/New_York", session_timezone="America/New_York",
        session="regular", primary_source="yahoo_chart", fallback_sources=("alpha_vantage",),
        earliest_valid_date="1970-01-01", adjustment_policy="provider_adjusted_close",
        adjustment_method="provider_adjusted_close", bar_close_rule="provider_native",
        license="Yahoo Finance terms", revision_overlap_bars=10,
    )


def _group_consecutive(stamps: pd.DatetimeIndex, delta: pd.Timedelta) -> list[list[pd.Timestamp]]:
    groups: list[list[pd.Timestamp]] = []
    for stamp in stamps:
        if groups and stamp - groups[-1][-1] == delta:
            groups[-1].append(stamp)
        else:
            groups.append([stamp])
    return groups
