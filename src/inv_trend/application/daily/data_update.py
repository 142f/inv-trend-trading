"""D1 data-update stage and its data-readiness evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

import pandas as pd

from ..daily_models import DataUpdateResult
from .ports import FreshnessPort, LineagePort, MarketDataPort


@dataclass(frozen=True)
class DataUpdateStage:
    """Readiness evidence together with the in-process completed D1 frame."""

    result: DataUpdateResult
    bars: pd.DataFrame | None
    update: Mapping[str, Any]
    refresh_error: Exception | None = None

    @property
    def readable(self) -> bool:
        return self.bars is not None and not self.bars.empty


class DailyDataUpdateService:
    """Refresh/read D1 data and produce the formal readiness gate."""

    def __init__(
        self,
        historical_service: MarketDataPort,
        *,
        freshness_policy: FreshnessPort,
        lineage_port: LineagePort,
        provider_retries: int = 1,
        refresh_data: bool = True,
    ) -> None:
        self.historical_service = historical_service
        self.freshness_policy = freshness_policy
        self.lineage_port = lineage_port
        self.provider_retries = provider_retries
        self.refresh_data = refresh_data

    def run(
        self,
        asset: Any,
        *,
        started_at: datetime,
        bootstrap_days: int,
        research_mode: bool = False,
    ) -> DataUpdateStage:
        instrument = self.historical_service.instrument(asset.symbol)
        before = self.historical_service.lake.current_version(asset.symbol, "D1")
        update: dict[str, Any]
        refresh_error: Exception | None = None
        manifest: Any = None
        try:
            if not self.refresh_data:
                if before is None:
                    raise FileNotFoundError(f"no current dataset for {asset.symbol}/D1")
                update = {
                    "status": "unchanged",
                    "mode": "scan_only",
                    "dataset_version": str(before["version"]),
                    "refresh_failed": False,
                    "skipped": True,
                }
            else:
                manifest = self._refresh(asset.symbol, started_at, bootstrap_days)
                after = self.historical_service.lake.current_version(asset.symbol, "D1")
                if not manifest.quality_passed or after is None:
                    raise RuntimeError(
                        "refresh not published: "
                        f"status={manifest.quality_status}, score={manifest.quality_score}"
                    )
                operation = (
                    "updated"
                    if before is None or before.get("version") != after.get("version")
                    else "unchanged"
                )
                update = {
                    "status": operation,
                    "mode": "bootstrap" if before is None else "incremental",
                    "provider": manifest.data_source,
                    "dataset_version": str(after["version"]),
                    "quality_status": manifest.quality_status,
                    "quality_score": manifest.quality_score,
                    "row_count": manifest.row_count,
                    "refresh_failed": False,
                }
        except Exception as exc:  # A preserved current version may still be usable.
            refresh_error = exc
            update = {
                "status": "failed",
                "error_type": type(exc).__name__,
                "error": str(exc),
                "refresh_failed": True,
                "previous_current_preserved": before is not None,
            }

        bars: pd.DataFrame | None = None
        load_error: Exception | None = None
        try:
            bars = self.historical_service.load_bars(
                asset.symbol, "D1", completed_only=True, allow_research=True
            )
        except Exception as exc:
            load_error = exc

        metadata = _data_metadata(bars)
        freshness: dict[str, Any]
        if bars is not None:
            freshness = self.freshness_policy.evaluate(
                instrument, metadata["latest_complete_bar"], now=started_at
            ).to_dict()
        else:
            freshness = {
                "status": "UNAVAILABLE",
                "expected_date": self.freshness_policy.expected_date(
                    instrument, now=started_at
                ).isoformat(),
                "actual_date": None,
                "reason": "no_readable_completed_d1",
            }

        lineage: dict[str, Any] = {"verified": False}
        lineage_error: Exception | None = None
        if bars is not None:
            try:
                # ``load_bars`` verifies this too.  Repeat it here to retain a
                # standalone, auditable lineage summary in this stage result.
                # Use the exact frame just read, rather than rereading the
                # mutable ``current`` pointer.  If another updater promotes a
                # version between these operations, ``load_current_lineage``
                # fails closed instead of accidentally validating the newer
                # frame while this result records the older version.
                verified = self.lineage_port.verify_current(
                    self.historical_service,
                    asset.symbol,
                    "D1",
                    bars=bars,
                )
                if str(verified.dataset_version) != str(metadata["dataset_version"]):
                    raise RuntimeError(
                        "lineage version does not match the readable D1 dataset version"
                    )
                manifest_payload = verified.dataset_manifest
                lineage = {
                    "verified": True,
                    "dataset_version": verified.dataset_version,
                    "publication_run_id": manifest_payload.get("publication_run_id"),
                    "curated_sha256": manifest_payload.get("curated_sha256"),
                    "quality_report_sha256": manifest_payload.get("quality_report_sha256"),
                    "quality_status": verified.quality_report.get("quality_status"),
                    "quality_score": verified.quality_report.get("quality_score"),
                }
            except Exception as exc:
                lineage_error = exc
                lineage = {
                    "verified": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }

        quality = {
            "statuses": metadata["quality_statuses"],
            "formal_status": "CURATED",
            "passed": metadata["quality_statuses"] == ["CURATED"],
            "manifest_status": getattr(manifest, "quality_status", None),
            "manifest_score": getattr(manifest, "quality_score", None),
        }
        blocking_reasons: list[str] = []
        if bars is None or bars.empty:
            blocking_reasons.append("no_readable_completed_d1")
        if not quality["passed"]:
            blocking_reasons.append("quality_not_curated")
        if freshness.get("status") != "FRESH":
            blocking_reasons.append("stale_data")
        if not lineage.get("verified"):
            blocking_reasons.append("lineage_verification_failed")
        if load_error is not None and bars is None:
            blocking_reasons.append("data_load_failed")
        # A refresh failure does not invalidate an independently verified,
        # current curated version.  It remains explicit in ``update_status``.
        if refresh_error is not None and before is None and bars is None:
            blocking_reasons.append("data_update_failed")

        result = DataUpdateResult(
            symbol=asset.symbol,
            instrument_id=instrument.instrument_id,
            timeframe="D1",
            update_status=str(update["status"]),
            dataset_version=metadata["dataset_version"],
            latest_complete_d1=metadata["latest_complete_bar"],
            update=update,
            quality=quality,
            freshness=freshness,
            lineage=lineage,
            data_readiness="READY" if not blocking_reasons else "BLOCKED",
            blocking_reasons=tuple(dict.fromkeys(blocking_reasons)),
            observation_only=research_mode,
        )
        return DataUpdateStage(result, bars, update, refresh_error or load_error or lineage_error)

    # A semantic alias is convenient for callers that use the stage directly.
    update = run

    def _refresh(self, symbol: str, now: datetime, bootstrap_days: int) -> Any:
        daily_end = _daily_refresh_end(now)
        if self.historical_service.lake.current_version(symbol, "D1") is not None:
            return self.historical_service.update(
                symbol, "D1", end=daily_end, retries=self.provider_retries
            )
        instrument = self.historical_service.instrument(symbol)
        start = daily_end - timedelta(days=bootstrap_days)
        if instrument.earliest_valid_date:
            start = max(start, _earliest_utc(instrument.earliest_valid_date))
        return self.historical_service.ingest(
            symbol, "D1", start, daily_end, retries=self.provider_retries
        )


def _data_metadata(bars: pd.DataFrame | None) -> dict[str, Any]:
    if bars is None or bars.empty:
        return {"dataset_version": None, "quality_statuses": [], "latest_complete_bar": None}
    statuses = sorted(
        {str(item) for item in bars.get("quality_status", pd.Series(dtype=object)).dropna()}
    )
    timestamp = bars["timestamp"].iloc[-1]
    return {
        "dataset_version": str(
            bars.attrs.get("dataset_version")
            or bars.get("dataset_version", pd.Series([None])).iloc[-1]
            or "unknown"
        ),
        "quality_statuses": statuses,
        "latest_complete_bar": _timestamp(timestamp),
    }


def _timestamp(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _daily_refresh_end(now: datetime) -> datetime:
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _earliest_utc(value: str) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC").to_pydatetime()


__all__ = ["DataUpdateResult", "DataUpdateStage", "DailyDataUpdateService"]
