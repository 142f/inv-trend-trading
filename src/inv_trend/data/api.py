from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Mapping

import pandas as pd

from .config import load_instruments
from .目录核验 import verify_catalog
from . import 成分股快照 as holdings_snapshots
from .成分股快照 import holding_instrument as _holding_instrument
from .models import (
    DataLineageError, DataQualityError, DataUnavailableError, DatasetManifest, HoldingsSnapshot, InstrumentConfig, Manifest, SurvivorshipBiasError,
    path_text, utc_now,
)
from inv_trend.infra.integrity import sha256_file
from .lineage import _load_current_lineage, _load_update_lineage, load_current_lineage
from .processing import FRAME_DELTAS, RULE_VERSION, NormalizationResult, assess_quality
from .providers import BarsProvider, HoldingsCsvProvider, QqqHoldingsCsvProvider
from .registry import InstrumentRegistry
from .review import ReviewApprovalCoordinator
from .storage import open_data_lake
from .pipeline import DatasetKey, MarketDataPipeline
from .pipeline.行情阶段_v1 import _OVERLAPS

_DEFAULT_ROOT = Path("data")


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
        # 标准四阶段流水线（P2 起 facade 只做薄委托）
        self.pipeline = MarketDataPipeline.for_kind(
            "bars",
            instruments=self.instruments,
            providers=self.providers,
            lake=self.lake,
            load_update_base=self._load_update_base,
            manifest_for_pointer=self._manifest_for_pointer,
        )
        # 测试仍会在构造之后替换 `_merge_current`。委托必须在调用时再取属性，
        # 不能在这里绑死原始方法。
        self._bars_core = self.pipeline.spec.acquire.core
        self._bars_core.merge_delegate = lambda *args, **kwargs: self._merge_current(*args, **kwargs)

    def instrument(self, symbol: str) -> InstrumentConfig:
        return self.registry.resolve(symbol)

    def ingest(self, symbol: str, timeframe: str, start: datetime, end: datetime, *, retries: int = 3) -> Manifest:
        """Thin facade: delegate the standard chain (acquire -> transform -> persist)."""
        return self.pipeline.ingest(DatasetKey(symbol, timeframe), start, end, retries=retries)

    def reprocess_raw(self, symbol: str, timeframe: str, run_id: str) -> Manifest:
        """Thin facade: reuse transform -> persist over an immutable raw frame."""
        return self.pipeline.reprocess_raw(DatasetKey(symbol, timeframe), run_id)

    def load_bars(self, symbol: str, timeframe: str, start: datetime | None = None, end: datetime | None = None,
                  adjusted: bool = True, *, completed_only: bool = True, allow_research: bool = False,
                  allow_legacy: bool = False, min_quality_score: float = 50.0) -> pd.DataFrame:
        return self.pipeline.read(
            DatasetKey(symbol, timeframe), start=start, end=end, adjusted=adjusted,
            completed_only=completed_only, allow_research=allow_research,
            allow_legacy=allow_legacy, min_quality_score=min_quality_score,
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
        return self.pipeline.read(
            DatasetKey(symbol, timeframe), version=dataset_version, start=start, end=end,
            adjusted=adjusted, completed_only=completed_only,
            allow_research=allow_research, allow_legacy=allow_legacy,
            min_quality_score=min_quality_score,
        )


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

    def _merge_current(
        self,
        symbol: str,
        timeframe: str,
        incoming: pd.DataFrame,
        *,
        existing: pd.DataFrame | None = None,
    ):
        """测试兼容入口。实现在流水线里；这里不经过 merge_delegate，避免递归。"""
        return self._bars_core.merge_current(symbol, timeframe, incoming, existing=existing)

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


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime boundaries must be timezone-aware")
    return value.astimezone(timezone.utc)


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
