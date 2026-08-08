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
    DataLineageError, DataQualityError, DownloadRequest,
    InstrumentConfig, Manifest, ProviderResult, SurvivorshipBiasError,
    path_text, utc_now,
)
from .processing import FRAME_DELTAS, RULE_VERSION, NormalizationResult, assess_quality, normalize_bars, resample_ohlcv_session
from .providers import BarsProvider, QqqHoldingsCsvProvider
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
        return self._ingest_result(instrument, timeframe, start, end, result, request_id, raw_path, raw_frame_path, errors)

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
        combined, conflicts = self._merge_current(instrument.symbol, timeframe, clean)
        full_result = NormalizationResult(combined, pd.DataFrame(), normalized.audit, 0, 0,
                                          int(combined.get("quality_flags", pd.Series(dtype=str)).astype(str).str.contains("extreme_jump_review").sum()),
                                          len(conflicts))
        quality = assess_quality(full_result, instrument, timeframe, provider=result.source,
                                 requested_start=start, requested_end=end)
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
        unchanged_replay = (
            bool(complete_mask.any())
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
        curated_view = combined.loc[complete_mask] if complete_mask.any() else combined
        curated_path: Path | None = None
        if publishable:
            curated_path = self.lake.publish_curated(
                curated_view, symbol=instrument.symbol, instrument_id=instrument.instrument_id,
                asset_class=instrument.asset_class, timeframe=timeframe, version=dataset_version, run_id=request_id,
            )
        elif status == "REVIEW_REQUIRED":
            self.lake.write_frame(combined, Path("reviews") / f"candidate={request_id}" / "bars.parquet")
        quality_payload = quality.to_dict() | {
            "run_id": request_id, "dataset_version": dataset_version, "instrument_id": instrument.instrument_id,
            "missing_intervals": quality.missing_intervals, "provider_failures": errors,
            "outside_overlap_revisions": outside_window_revisions,
            "conflicting_duplicates": conflicts,
        }
        quality_relative = Path("quality_reports") / f"{dataset_version}.json"
        quality_path = self.lake.root / quality_relative
        if not quality_path.exists():
            quality_path = self.lake.write_json(quality_payload, quality_relative)
        paths: list[Path] = []
        for candidate in (raw_path, raw_frame_path):
            if candidate not in paths:
                paths.append(candidate)
        paths += [*normalized_paths, quality_path, *([curated_path] if curated_path else [])]
        manifest = Manifest(
            request_id, result.source, instrument.symbol, result.actual_symbol, start.isoformat(), end.isoformat(),
            curated_view["timestamp"].min().isoformat() if len(curated_view) else None,
            curated_view["timestamp"].max().isoformat() if len(curated_view) else None,
            timeframe, len(curated_view), [path_text(p) for p in paths], {path_text(p): sha256_file(p) for p in paths},
            utc_now().isoformat(), result.license, [str(x) for x in quality.missing_intervals],
            len(normalized.audit), RULE_VERSION, publishable, {**result.metadata, "provider_failures": errors},
            instrument.instrument_id, dataset_version, quality.quality_score, status,
            request_id, raw_snapshot_id, request_id, dataset_version, config_hash, metadata_hash,
            instrument.earliest_valid_date or "", curated_view["timestamp"].min().isoformat() if len(curated_view) else "",
            start.isoformat(), end.isoformat(),
            (pd.Timestamp(end) - pd.Timedelta(FRAME_DELTAS[timeframe])).isoformat(),
        )
        manifest_path = self.lake.write_json(manifest.to_dict(), Path("manifests") / f"{request_id}.json")
        self.lake.catalog(request_id, instrument.symbol, timeframe, result.source, manifest_path,
                          instrument_id=instrument.instrument_id, status=status, version=dataset_version,
                          asset_class=instrument.asset_class,
                          start_time=manifest.actual_start or "", end_time=manifest.actual_end or "",
                          row_count=manifest.row_count, schema_version=RULE_VERSION,
                          checksum=sha256_file(manifest_path), raw_source=path_text(raw_frame_path))
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

    def _fetch(self, instrument: InstrumentConfig, timeframe: str, start: datetime, end: datetime, retries: int):
        names = (instrument.primary_source,) + instrument.fallback_sources
        errors: list[str] = []
        for name in names:
            provider = self.providers.get(name)
            if provider is None:
                errors.append(f"{name}: provider not registered")
                continue
            for attempt in range(1, retries + 1):
                try:
                    requested_timeframe = "H1" if name == "dukascopy" and timeframe == "H4" else timeframe
                    result = provider.fetch(DownloadRequest(instrument, requested_timeframe, start, end))
                    if requested_timeframe != timeframe:
                        result.frame = resample_ohlcv_session(
                            result.frame, timeframe, session_timezone=instrument.session_timezone, close_hour=17
                        )
                        result.metadata = {**result.metadata, "derived_from_timeframe": requested_timeframe,
                                           "session_timezone": instrument.session_timezone, "bar_close_rule": instrument.bar_close_rule}
                    return result, errors
                except Exception as exc:  # provider boundary
                    errors.append(f"{name} attempt {attempt}: {exc}")
                    self.lake.log_event({"run_id": "", "instrument_id": instrument.instrument_id, "timeframe": timeframe,
                                         "provider": name, "severity": "ERROR", "error_type": type(exc).__name__,
                                         "message": str(exc), "attempt": attempt, "action": "retry",
                                         "fallback_provider": ""})
                    if attempt < retries:
                        time.sleep(min(2 ** (attempt - 1), 4))
        raise RuntimeError("all configured providers failed: " + " | ".join(errors))

    def _merge_current(self, symbol: str, timeframe: str, incoming: pd.DataFrame) -> tuple[pd.DataFrame, list[dict[str, object]]]:
        """Merge incoming bars with the published view; detect OHLCV conflicts.

        Exact duplicates are dropped silently with an audit note.  Conflicting
        duplicates (same timestamp, different OHLCV) are never silently
        overwritten: they are returned for REVIEW_REQUIRED routing.
        """
        try:
            existing = self.lake.read_bars(symbol, timeframe)
        except FileNotFoundError:
            existing = pd.DataFrame(columns=incoming.columns)
        conflicts: list[dict[str, object]] = []
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
                keep_incoming.append(False)
            incoming = incoming.loc[keep_incoming]
        combined = pd.concat([existing, incoming], ignore_index=True, sort=False)
        return combined.sort_values(["timestamp", "ingested_at"]).drop_duplicates("timestamp", keep="last").reset_index(drop=True), conflicts

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
            earliest = pd.Timestamp(instrument.earliest_valid_date, tz="UTC")
            if earliest <= end:
                intervals.append({"start": earliest.to_pydatetime(), "end": end.to_pydatetime(),
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

    def update(self, symbol: str, timeframe: str, *, end: datetime | None = None, overlap_bars: int | None = None) -> Manifest:
        instrument = self.instrument(symbol)
        timeframe = timeframe.upper()
        manifests: list[Manifest] = []
        for interval in self.missing_intervals(symbol, timeframe, end=end):
            start, end_point = interval["start"], interval["end"]
            if overlap_bars is not None and interval["classification"] == "tail":
                last = self.lake.read_bars(instrument.symbol, timeframe)["timestamp"].max().to_pydatetime()
                start = last - overlap_bars * pd.Timedelta(FRAME_DELTAS[timeframe]).to_pytimedelta()
            manifests.append(self.ingest(symbol, timeframe, start, end_point))
        if manifests:
            return manifests[-1]
        current = self._current_manifest(symbol, timeframe)
        if current is not None:
            return current
        raise RuntimeError(f"no missing intervals and no current manifest for {symbol}/{timeframe}")

    def _current_manifest(self, symbol: str, timeframe: str) -> Manifest | None:
        try:
            current = self.lake.current_version(symbol.upper(), timeframe.upper())
        except Exception:
            return None
        if not current:
            return None
        with self.lake._connect() as db:
            rows = db.execute(
                "SELECT run_id FROM datasets WHERE symbol=? AND timeframe=? AND version=? "
                "ORDER BY created_at DESC LIMIT 1",
                (symbol.upper(), timeframe.upper(), current["version"]),
            ).fetchall()
        for (run_id,) in rows:
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
        frame.attrs.update({"symbol": symbol.upper(), "instrument_id": instrument.instrument_id,
                            "timeframe": timeframe.upper(), "adjustment": instrument.adjustment_policy if adjusted else "none",
                            "data_sources": sorted(frame["data_source"].dropna().unique().tolist()),
                            "dataset_version": str(frame["dataset_version"].iloc[-1]),
                            "quality_report": str(self.lake.root / "quality_reports" / f"{frame['dataset_version'].iloc[-1]}.json")})
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
        """Audit every catalog row against its manifest, curated file and hashes.

        Reports valid records plus missing manifest / missing curated / dangling
        current pointers.  Legacy records are counted separately and never
        considered part of the formal universe.
        """
        result = {"valid_dataset_records": 0, "missing_manifest_records": [],
                  "missing_curated_records": [], "dangling_current_records": [],
                  "hash_mismatch_records": [], "legacy_records": 0, "total_records": 0,
                  "legacy_contaminated_current": []}
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
            curated_files = [Path(p) for p in manifest.get("file_paths", []) if "curated" in p and "bars.parquet" in p]
            if not curated_files or not curated_files[0].exists():
                result["missing_curated_records"].append({"run_id": run_id, "symbol": symbol, "timeframe": timeframe, "version": version})
                continue
            bad = [p for p in manifest.get("file_paths", []) if Path(p).exists() and sha256_file(Path(p)) != manifest["file_hashes"].get(p)]
            if bad:
                result["hash_mismatch_records"].append({"run_id": run_id, "symbol": symbol, "files": bad})
                continue
            result["valid_dataset_records"] += 1
        for pointer in (self.lake.root / "curated").glob("symbol=*/timeframe=*/*.json"):
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            raw_target = str(payload.get("path", ""))
            target = self.lake.root / raw_target
            if not target.exists():
                target = Path(raw_target)  # pointer paths are project-relative
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
        return result

    def coverage(self, symbol: str, timeframe: str) -> dict[str, object]:
        """Return current coverage plus the persisted gap-classification report."""
        result = self.status(symbol, timeframe)
        if result["status"] == "MISSING":
            return result
        bars = self.lake.read_bars(symbol.upper(), timeframe.upper())
        dataset_version = str(bars["dataset_version"].iloc[-1])
        report_path = self.lake.root / "quality_reports" / f"{dataset_version}.json"
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

    def approve_review(self, run_id: str, reason: str = "", *, actor: str = "") -> str:
        """Publish a new audited version from an immutable review candidate."""
        manifest_path = self.lake.root / "manifests" / f"{run_id}.json"
        candidate_path = self.lake.root / "reviews" / f"candidate={run_id}" / "bars.parquet"
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
        version = _stable_hash({"approved_from": run_id, "reason": reason, "bars": _frame_hash(frame)})[:24]
        # Approval is an audit decision.  Publication policy remains CURATED so
        # Repository callers can read the newly approved complete version.
        frame["quality_status"] = "CURATED"
        frame["dataset_version"] = version
        frame["curated_version"] = version
        candidate_report = self.lake.root / "quality_reports" / f"{manifest['dataset_version']}.json"
        if not candidate_report.exists():
            raise DataLineageError(f"review candidate has no quality report: {manifest['dataset_version']}")
        path = self.lake.publish_curated(frame, symbol=symbol, instrument_id=instrument.instrument_id,
                                         asset_class=instrument.asset_class, timeframe=str(manifest["timeframe"]),
                                         version=version, run_id=run_id, activate=False)
        source_quality = json.loads(candidate_report.read_text(encoding="utf-8"))
        report = source_quality | {"approved_from": run_id, "dataset_version": version, "reason": reason,
                                   "actor": actor, "published_path": str(path), "approved_at": utc_now().isoformat(),
                                   "quality_status": "CURATED", "approval_status": "APPROVED"}
        self.lake.write_json(report, Path("quality_reports") / f"{version}.json")
        approved_report_path = self.lake.root / "quality_reports" / f"{version}.json"
        approval_manifest = manifest | {"run_id": f"approval-{version}", "processing_run_id": f"approval-{version}",
                                        "dataset_version": version, "curated_version": version,
                                        "quality_status": "CURATED", "quality_passed": True,
                                        "approval_status": "APPROVED", "approved_from": run_id,
                                        "approval_reason": reason, "approved_at": utc_now().isoformat(),
                                        "file_paths": [str(path), str(approved_report_path)],
                                        "file_hashes": {str(path): sha256_file(path), str(approved_report_path): sha256_file(approved_report_path)},
                                        "row_count": len(frame)}
        approval_path = self.lake.write_json(approval_manifest, Path("manifests") / f"approval-{version}.json")
        self.lake.catalog(f"approval-{version}", symbol, str(manifest["timeframe"]), str(manifest["data_source"]), approval_path,
                          instrument_id=instrument.instrument_id, status="CURATED", version=version)
        self.lake.review(run_id, "approved", reason, actor=actor, published_version=version)
        # All immutable artifacts now exist and their hashes are recorded. Only
        # then may the strategy-visible current pointer advance.
        self.lake.activate_curated(symbol, str(manifest["timeframe"]), version, run_id, path)
        return version

    def update_qqq_holdings(self, provider: QqqHoldingsCsvProvider, snapshot_date: str | None = None) -> Path:
        frame = provider.fetch(snapshot_date)
        dates = frame["snapshot_date"].unique()
        if len(dates) != 1:
            raise ValueError("one holdings file must represent exactly one snapshot date")
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


def _group_consecutive(stamps: pd.DatetimeIndex, delta: pd.Timedelta) -> list[list[pd.Timestamp]]:
    groups: list[list[pd.Timestamp]] = []
    for stamp in stamps:
        if groups and stamp - groups[-1][-1] == delta:
            groups[-1].append(stamp)
        else:
            groups.append([stamp])
    return groups
