"""K 线（bars）链路的四阶段实现。

方法体自 ``inv_trend.data.api`` 逐行搬移：
- ``BarsCore.fetch``                     ← ``HistoricalDataService._fetch``
- ``BarsCore.outside_overlap_revisions`` ← ``_outside_overlap_revisions``
- ``BarsCore.merge_current``             ← ``_merge_current``
- ``BarsCore.transform``                 ← ``_ingest_result`` 的计算半（不写盘）
- ``BarsCore.persist``                   ← ``_ingest_result`` 的落盘半
- ``BarsCore.read``                      ← ``load_bars`` / ``load_bars_version`` / ``_strategy_visible_bars``
- ``BarsCore.reprocess_raw``             ← ``reprocess_raw``
- 纯函数 ``_bounded_range`` / ``_completed_bar_changes`` / ``_same_bar_value`` /
  ``_validate_provider_identity`` / ``_fallback_eligible`` / ``_group_consecutive``

搬移铁律：只改 import 与接收参数（``self.xxx`` → 显式注入），
不改任何算术、字段顺序、dtype、排序。stage 2 只算不写，stage 3 只写不算。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping
from uuid import uuid4

import pandas as pd

from inv_trend.infra.integrity import sha256_file, stable_hash, strategy_frame_hash
from ..registry import InstrumentRegistry
from ..lineage import load_current_lineage, load_versioned_lineage
from ..models import (
    DataLineageError,
    DataQualityError,
    DataUnavailableError,
    DatasetManifest,
    DownloadRequest,
    InstrumentConfig,
    Manifest,
    ProviderIdentityError,
    ProviderResult,
    path_text,
    utc_now,
)
from ..processing import (
    FRAME_DELTAS,
    RULE_VERSION,
    NormalizationResult,
    assess_quality,
    audit_published_frame,
    normalize_bars,
    resample_ohlcv_session,
)
from ..storage import DataLake
from .契约_v1 import (
    AcquireOutcome,
    DatasetKey,
    PipelineContext,
    PublicationOutcome,
    TransformOutcome,
)

if TYPE_CHECKING:
    from ..providers import BarsProvider

_OVERLAPS = {"D1": 10, "H4": 20, "H1": 48}


# ---------------------------------------------------------------------------
# 纯函数（自 api.py 原样搬移）
# ---------------------------------------------------------------------------


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime boundaries must be timezone-aware")
    return value.astimezone(timezone.utc)


def _bounded_range(instrument: InstrumentConfig, start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start, end = _aware_utc(start), _aware_utc(end)
    if instrument.earliest_valid_date:
        start = max(start, pd.Timestamp(instrument.earliest_valid_date, tz="UTC").to_pydatetime())
    if start > end:
        raise ValueError("requested range ends before verified instrument history")
    return start, end


def _timestamp_positions(frame: pd.DataFrame) -> tuple[dict[object, int], int | None]:
    """Last row of each timestamp. NaT is not equal to itself, so it is stored apart."""

    positions: dict[object, int] = {}
    missing: int | None = None
    for position, timestamp in enumerate(frame["timestamp"].tolist()):
        if pd.isna(timestamp):
            missing = position
        else:
            positions[timestamp] = position
    return positions, missing


def _locate_timestamp(
    timestamp: object, positions: dict[object, int], missing: int | None
) -> int | None:
    if pd.isna(timestamp):
        return missing
    return positions.get(timestamp)


def _flag(frame: pd.DataFrame, column: str, position: int) -> bool:
    if column not in frame.columns:
        return False
    return bool(frame[column].iloc[position])


def _completed_bar_changes(
    existing: pd.DataFrame,
    incoming: pd.DataFrame,
    columns: list[str],
    selected: pd.Series | None = None,
):
    """Pair incoming rows with the last existing row and list changed columns."""

    positions, missing = _timestamp_positions(existing)
    existing_values = {column: existing[column].tolist() for column in columns}
    incoming_values = {column: incoming[column].tolist() for column in columns}
    timestamps = incoming["timestamp"].tolist()
    chosen = selected.tolist() if selected is not None else None
    for incoming_position, timestamp in enumerate(timestamps):
        if chosen is not None and not chosen[incoming_position]:
            continue
        existing_position = _locate_timestamp(timestamp, positions, missing)
        if existing_position is None or not _flag(existing, "is_complete", existing_position):
            changed: list[str] = []
        else:
            changed = [
                column
                for column in columns
                if not _same_bar_value(
                    existing_values[column][existing_position],
                    incoming_values[column][incoming_position],
                )
            ]
        yield timestamp, existing_position, incoming_position, changed


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



# ---------------------------------------------------------------------------
# 核心实现（供四个阶段共享）
# ---------------------------------------------------------------------------


@dataclass
class BarsCore:
    """K 线链路的实现主体；四个阶段委托到这里。

    ``load_update_base`` / ``manifest_for_pointer`` 由 facade 注入（facade 仍需
    用它们服务 ``missing_intervals`` / ``update*`` 等方法），避免反向依赖。
    """

    lake: DataLake
    instruments: Mapping[str, InstrumentConfig]
    providers: Mapping[str, "BarsProvider"]
    load_update_base: Callable[[str, str], tuple[pd.DataFrame, dict | None]]
    manifest_for_pointer: Callable[[dict | None], Manifest | None]
    # 由 facade 在构造后挂上。调用时再解析，这样测试替换 service._merge_current 仍然生效。
    merge_delegate: Callable[..., tuple] | None = None

    def instrument(self, symbol: str) -> InstrumentConfig:
        return InstrumentRegistry(self.instruments).resolve(symbol)

    # --- 阶段 1：获取 -------------------------------------------------------
    def fetch(
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

    # --- 冲突检测（读取既有的公开条带） --------------------------------------
    def outside_overlap_revisions(
        self, symbol: str, instrument: InstrumentConfig, timeframe: str, incoming: pd.DataFrame,
        *, existing: pd.DataFrame | None = None,
    ) -> list[dict[str, object]]:
        """Return completed-bar corrections preceding the automatic revision window."""
        if existing is None:
            existing, _ = self.load_update_base(symbol, timeframe)
        if existing.empty or incoming.empty:
            return []
        overlap = instrument.revision_overlap_bars or _OVERLAPS[timeframe]
        cutoff = existing["timestamp"].max() - overlap * pd.Timedelta(FRAME_DELTAS[timeframe])
        comparable = [column for column in ("open", "high", "low", "close", "adjusted_close", "volume")
                      if column in existing and column in incoming]
        revisions: list[dict[str, object]] = []
        for timestamp, existing_position, incoming_position, changed in _completed_bar_changes(
            existing, incoming, comparable, incoming["timestamp"] < cutoff
        ):
            if not _flag(incoming, "is_complete", incoming_position):
                continue
            if changed:
                revisions.append({"timestamp": timestamp.isoformat(), "changed_fields": changed})
        return revisions

    def merge_current(
        self,
        symbol: str,
        timeframe: str,
        incoming: pd.DataFrame,
        *, existing: pd.DataFrame | None = None,
    ) -> tuple[pd.DataFrame, list[dict[str, object]], pd.DataFrame]:
        """Merge bars while quarantining completed-bar OHLCV conflicts."""
        if existing is None:
            existing, _ = self.load_update_base(symbol, timeframe)
        conflicts: list[dict[str, object]] = []
        rejected: list[pd.Series] = []
        if not existing.empty and not incoming.empty:
            value_columns = [
                column
                for column in (
                    "open", "high", "low", "close", "adjusted_close",
                    "volume", "is_complete",
                )
                if column in existing and column in incoming
            ]
            keep_incoming: list[bool] = []
            rejected_positions: list[int] = []
            for timestamp, existing_position, incoming_position, changed in _completed_bar_changes(
                existing, incoming, value_columns
            ):
                if existing_position is None or not _flag(existing, "is_complete", existing_position):
                    keep_incoming.append(True)
                    continue
                if not changed:
                    keep_incoming.append(False)
                    continue
                old = existing.iloc[existing_position]
                row = incoming.iloc[incoming_position]
                conflicts.append(
                    {
                        "timestamp": timestamp.isoformat(),
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
                rejected_positions.append(incoming_position)
                keep_incoming.append(False)
            if rejected_positions:
                rejected.extend(incoming.iloc[position] for position in rejected_positions)
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

    # --- 阶段 2：转换（只算不写） -------------------------------------------
    def transform(self, acquired: AcquireOutcome, context: PipelineContext) -> TransformOutcome:
        """``_ingest_result`` 的计算半：不含任何写盘。"""

        instrument = self.instrument(context.key.symbol)
        timeframe = context.key.timeframe.upper()
        start, end = _bounded_range(instrument, context.start, context.end)
        result = acquired.provider_result
        if result is None:
            raise ValueError("BarsTransform requires an AcquireOutcome carrying provider_result")
        raw_path, raw_frame_path = acquired.raw_path, acquired.raw_frame_path
        if raw_path is None or raw_frame_path is None:
            raise ValueError("BarsTransform requires raw paths from AcquireOutcome")
        request_id = context.run_id
        errors = list(acquired.errors)

        raw = result.frame.copy()
        raw_hash = sha256_file(raw_frame_path)
        existing_curated, current_pointer = self.load_update_base(instrument.symbol, timeframe)
        raw_snapshot_id = hashlib.sha256(raw_hash.encode()).hexdigest()[:24]
        normalized = normalize_bars(raw, instrument, timeframe, result.source)
        clean = normalized.clean.copy()
        clean["raw_file_hash"] = raw_hash
        clean["request_id"] = request_id
        clean["raw_snapshot_id"] = raw_snapshot_id
        clean["source_run_id"] = request_id
        outside_window_revisions = self.outside_overlap_revisions(
            instrument.symbol, instrument, timeframe, clean, existing=existing_curated
        )
        merge = self.merge_delegate or self.merge_current
        combined, conflicts, rejected_incoming = merge(
            instrument.symbol, timeframe, clean, existing=existing_curated,
        )
        # Carry the counters produced while cleaning this run instead of asserting
        # zero: hard-coded zeros made every report score >= 85 and marked every
        # dataset backtest-suitable even when duplicates or bad OHLCV were dropped.
        full_result = NormalizationResult(
            combined, normalized.quarantine, normalized.audit,
            normalized.duplicate_count, normalized.ohlc_anomaly_count,
            int(combined.get("quality_flags", pd.Series(dtype=str)).astype(str).str.contains("extreme_jump_review").sum()),
            len(conflicts),
        )
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
        current_manifest = self.manifest_for_pointer(current_pointer)
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

        return TransformOutcome(
            normalized=normalized,
            clean=clean,
            merged=combined,
            conflicts=tuple(conflicts),
            rejected_incoming=rejected_incoming,
            quality=quality,
            dataset_version=dataset_version,
            request_id=request_id,
            raw_snapshot_id=raw_snapshot_id,
            outside_window_revisions=tuple(outside_window_revisions),
            provider_result=result,
            raw_path=raw_path,
            raw_frame_path=raw_frame_path,
            errors=tuple(errors),
            status=status,
            publishable=publishable,
            current_pointer=current_pointer,
            parent_dataset_version=parent_dataset_version,
            is_new_dataset=is_new_dataset,
            curated_view=curated_view,
            complete_count=complete_count,
            inherited_research_only=inherited_research_only,
            config_hash=config_hash,
            metadata_hash=metadata_hash,
        )

    # --- 阶段 3：结构化保存（只写不算） -------------------------------------
    def persist(self, transformed: TransformOutcome, context: PipelineContext) -> PublicationOutcome:
        """``_ingest_result`` 的落盘半：只做写盘与发布决策的落地。"""

        instrument = self.instrument(context.key.symbol)
        timeframe = context.key.timeframe.upper()
        start, end = _bounded_range(instrument, context.start, context.end)
        result = transformed.provider_result
        if result is None:
            raise ValueError("BarsPersist requires provider_result")
        request_id = transformed.request_id
        normalized = transformed.normalized
        clean = transformed.clean
        combined = transformed.merged
        conflicts = list(transformed.conflicts)
        rejected_incoming = transformed.rejected_incoming
        quality = transformed.quality
        dataset_version = transformed.dataset_version
        raw_snapshot_id = transformed.raw_snapshot_id
        outside_window_revisions = list(transformed.outside_window_revisions)
        status = transformed.status
        publishable = transformed.publishable

        normalized_paths = self.lake.write_normalized(clean, instrument.asset_class, instrument.instrument_id, timeframe)
        if not normalized.quarantine.empty:
            self.lake.write_frame(normalized.quarantine, Path("quarantine") / f"run_id={request_id}" / "rows.parquet")

        current_pointer = transformed.current_pointer
        parent_dataset_version = transformed.parent_dataset_version
        is_new_dataset = transformed.is_new_dataset
        curated_view = transformed.curated_view
        curated_path: Path | None = None
        dataset_manifest_path: Path | None = None
        if publishable and is_new_dataset:
            curated_path = self.lake.publish_curated(
                curated_view, symbol=instrument.symbol, instrument_id=instrument.instrument_id,
                asset_class=instrument.asset_class, timeframe=timeframe, version=dataset_version,
                run_id=request_id, activate=False,
            )
            # The dataset-level report is what certification reads later, so the
            # expected window must come from what was requested, never from the
            # data that actually arrived: using the actual head makes every
            # truncated dataset certify itself.
            dataset_window_start = min(pd.Timestamp(start), curated_view["timestamp"].min())
            dataset_quality = assess_quality(
                audit_published_frame(curated_view),
                instrument, timeframe, provider=result.source,
                requested_start=dataset_window_start.to_pydatetime(),
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
            # Read the published base once: each call re-reads the Parquet file,
            # re-hashes it and globs the curated tree.
            candidate_dir = Path("reviews") / f"candidate={request_id}"
            base_frame = self._existing_curated(instrument, timeframe)
            if not base_frame.empty:
                self.lake.write_frame(base_frame, candidate_dir / "base.parquet")
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
                                  "base_rows": len(base_frame),
                                  "incoming_rows": len(rejected_incoming),
                                  "file_hashes": {path_text(path, self.lake.root): sha256_file(path)
                                                  for path in candidate_files},
                                  "created_at": utc_now().isoformat()},
                                 candidate_dir / "candidate_manifest.json")
            candidate_manifest_path = self.lake.root / candidate_dir / 'candidate_manifest.json'

        run_quality_payload = quality.to_dict() | {
            "run_id": request_id, "dataset_version": dataset_version, "instrument_id": instrument.instrument_id,
            "missing_intervals": quality.missing_intervals, "provider_failures": list(transformed.errors),
            "outside_overlap_revisions": outside_window_revisions,
            "conflicting_duplicates": conflicts,
        }
        run_quality_path = self.lake.write_json(
            run_quality_payload, Path("ingestion_reports") / f"{request_id}.json"
        )
        paths: list[Path] = []
        for candidate in (transformed.raw_path, transformed.raw_frame_path):
            if candidate not in paths:
                paths.append(candidate)
        paths += [*normalized_paths, run_quality_path]
        if status == 'REVIEW_REQUIRED':
            paths.append(candidate_manifest_path)
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
                **result.metadata, "provider_failures": list(transformed.errors),
                **({'inherited_research_only': True} if transformed.inherited_research_only else {}),
            },
            instrument_id=instrument.instrument_id, dataset_version=dataset_version,
            quality_score=quality.quality_score, quality_status=status, request_id=request_id,
            raw_snapshot_id=raw_snapshot_id, processing_run_id=request_id, curated_version=dataset_version,
            config_hash=transformed.config_hash, provider_metadata_hash=transformed.metadata_hash,
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
                          checksum=sha256_file(manifest_path), raw_source=path_text(transformed.raw_frame_path, root_path))
        self.lake.log_event({"run_id": request_id, "instrument_id": instrument.instrument_id, "timeframe": timeframe,
                             "provider": result.source, "severity": "INFO" if publishable else "WARNING",
                             "error_type": "", "message": "published" if publishable else "not published",
                             "attempt": 1, "action": "publish" if publishable else "quarantine",
                             "fallback_provider": ""})
        return PublicationOutcome(
            manifest=manifest,
            status=status,
            publishable=publishable,
            dataset_manifest_path=dataset_manifest_path,
        )

    def _existing_curated(self, instrument: InstrumentConfig, timeframe: str) -> pd.DataFrame:
        existing, _ = self.load_update_base(instrument.symbol, timeframe)
        return existing

    # --- 重新处理 -----------------------------------------------------------
    def reprocess_raw(self, instrument: InstrumentConfig, timeframe: str, run_id: str) -> Manifest:
        """Re-run preprocessing from an immutable raw frame without any provider call."""
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
        context = PipelineContext(
            kind="bars", key=DatasetKey(instrument.symbol, timeframe), start=start, end=end,
            run_id=request_id,
        )
        acquired = AcquireOutcome(
            provider_result=result, raw_path=raw_path, raw_frame_path=raw_path,
        )
        return self.persist(self.transform(acquired, context), context).manifest  # type: ignore[return-value]

    # --- 阶段 4：读取 -------------------------------------------------------
    def read(
        self,
        key: DatasetKey,
        *,
        version: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        adjusted: bool = True,
        completed_only: bool = True,
        allow_research: bool = False,
        allow_legacy: bool = False,
        min_quality_score: float = 50.0,
    ) -> pd.DataFrame:
        symbol, timeframe = key.symbol, key.timeframe
        if version:
            return self.load_bars_version(
                symbol, timeframe, version, start, end, adjusted,
                completed_only=completed_only, allow_research=allow_research,
                allow_legacy=allow_legacy, min_quality_score=min_quality_score,
            )
        return self.load_bars(
            symbol, timeframe, start, end, adjusted,
            completed_only=completed_only, allow_research=allow_research,
            allow_legacy=allow_legacy, min_quality_score=min_quality_score,
        )

    def load_bars(
        self, symbol: str, timeframe: str, start: datetime | None = None, end: datetime | None = None,
        adjusted: bool = True, *, completed_only: bool = True, allow_research: bool = False,
        allow_legacy: bool = False, min_quality_score: float = 50.0,
    ) -> pd.DataFrame:
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
        return self.strategy_visible_bars(
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
        """Load one immutable curated version, independently of ``current``."""

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
        return self.strategy_visible_bars(
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
    def strategy_visible_bars(
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


# ---------------------------------------------------------------------------
# 四阶段适配器
# ---------------------------------------------------------------------------


@dataclass
class BarsAcquire:
    """阶段 1：获取 + raw 落盘。"""

    core: BarsCore
    kind: str = "bars"

    def acquire(self, context: PipelineContext) -> AcquireOutcome:
        return self._acquire_with(context, instrument=None)

    def _acquire_with(self, context: PipelineContext, *, instrument) -> AcquireOutcome:
        instrument = instrument or self.core.instrument(context.key.symbol)
        timeframe = context.key.timeframe.upper()
        start, end = _bounded_range(instrument, context.start, context.end)
        result, errors = self.core.fetch(instrument, timeframe, start, end, context.retries)
        request_id = context.run_id
        raw_path = self.core.lake.write_raw_payload(
            result.raw_payload or result.frame.to_json(date_format="iso", orient="records").encode(),
            result.source, instrument.instrument_id, request_id, result.raw_payload_suffix,
        )
        raw_frame_path = self.core.lake.write_raw(
            result.frame, result.source, instrument.instrument_id, request_id
        )
        return AcquireOutcome(
            provider_result=result,
            raw_path=raw_path,
            raw_frame_path=raw_frame_path,
            errors=tuple(errors),
        )

    def retry_fallback(
        self, context: PipelineContext, acquired: AcquireOutcome, published: PublicationOutcome
    ) -> tuple[PipelineContext, "AcquireOutcome"] | None:
        """原 ``ingest`` 的质量门禁回退：主源未通过时切换到 fallback_sources。"""

        instrument = self.core.instrument(context.key.symbol)
        timeframe = context.key.timeframe.upper()
        start, end = _bounded_range(instrument, context.start, context.end)
        manifest = published.manifest
        result = acquired.provider_result
        if manifest is None or result is None:
            return None
        if manifest.quality_passed or not instrument.fallback_sources or result.source != instrument.primary_source:
            return None
        quality_reason = (
            f"{instrument.primary_source}: quality gate failed "
            f"status={manifest.quality_status}, score={manifest.quality_score}"
        )
        try:
            fallback_result, fallback_errors = self.core.fetch(
                instrument, timeframe, start, end, context.retries,
                names=instrument.fallback_sources, primary_error=quality_reason,
            )
        except Exception as exc:
            self.core.lake.log_event({
                "run_id": manifest.run_id, "instrument_id": instrument.instrument_id,
                "timeframe": timeframe, "provider": instrument.primary_source,
                "severity": "WARNING", "error_type": type(exc).__name__,
                "message": str(exc), "attempt": 1, "action": "fallback_blocked",
                "fallback_provider": ",".join(instrument.fallback_sources),
            })
            return None
        fallback_context = replace(context, run_id=uuid4().hex[:24])
        fallback_raw = self.core.lake.write_raw_payload(
            fallback_result.raw_payload or fallback_result.frame.to_json(
                date_format="iso", orient="records"
            ).encode(),
            fallback_result.source, instrument.instrument_id, fallback_context.run_id,
            fallback_result.raw_payload_suffix,
        )
        fallback_frame = self.core.lake.write_raw(
            fallback_result.frame, fallback_result.source,
            instrument.instrument_id, fallback_context.run_id,
        )
        return fallback_context, AcquireOutcome(
            provider_result=fallback_result,
            raw_path=fallback_raw,
            raw_frame_path=fallback_frame,
            errors=tuple([*acquired.errors, quality_reason, *fallback_errors]),
        )


@dataclass
class BarsTransform:
    """阶段 2：转换（只算不写）。"""

    core: BarsCore
    kind: str = "bars"

    def transform(self, acquired: AcquireOutcome, context: PipelineContext) -> TransformOutcome:
        return self.core.transform(acquired, context)


@dataclass
class BarsPersist:
    """阶段 3：结构化保存（只写不算）。"""

    core: BarsCore
    kind: str = "bars"

    def persist(self, transformed: TransformOutcome, context: PipelineContext) -> PublicationOutcome:
        return self.core.persist(transformed, context)


@dataclass
class BarsRead:
    """阶段 4：读取。"""

    core: BarsCore
    kind: str = "bars"

    def read(self, key: DatasetKey, **filters) -> pd.DataFrame:
        return self.core.read(key, **filters)

    def load_bars(self, symbol: str, timeframe: str, start=None, end=None, adjusted: bool = True, **kwargs):
        return self.core.load_bars(symbol, timeframe, start, end, adjusted, **kwargs)

    def load_bars_version(self, symbol: str, timeframe: str, dataset_version: str,
                          start=None, end=None, adjusted: bool = True, **kwargs):
        return self.core.load_bars_version(symbol, timeframe, dataset_version, start, end, adjusted, **kwargs)


def build_bars_core(
    lake: DataLake,
    instruments: Mapping[str, InstrumentConfig],
    providers: Mapping[str, "BarsProvider"],
    load_update_base: Callable[[str, str], tuple[pd.DataFrame, dict | None]],
    manifest_for_pointer: Callable[[dict | None], Manifest | None],
) -> BarsCore:
    return BarsCore(
        lake=lake,
        instruments=instruments,
        providers=providers,
        load_update_base=load_update_base,
        manifest_for_pointer=manifest_for_pointer,
    )


__all__ = [
    "BarsAcquire",
    "BarsCore",
    "BarsPersist",
    "BarsRead",
    "BarsTransform",
    "build_bars_core",
]
