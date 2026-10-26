"""K 线（bars）链路的四阶段适配器。

P1 阶段为**转发 shim**：``ingest`` / ``read`` / ``reprocess`` 整体委托既有的
``HistoricalDataService`` 实现，因此行为等价由构造保证；P2 再把
``_fetch`` / ``_ingest_result`` 的真正算法逐行搬进来，用同一组
``AcquireOutcome`` / ``TransformOutcome`` / ``PublicationOutcome`` 承载结果。

为避免与 facade 形成循环导入，这里**不导入** ``inv_trend.data.api``，
而是只依赖一个结构化协议 ``BarsBackend``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import pandas as pd

    from ..models import Manifest

from .契约_v1 import (
    AcquireOutcome,
    DatasetKey,
    PipelineContext,
    PublicationOutcome,
    TransformOutcome,
)


@runtime_checkable
class BarsBackend(Protocol):
    """facade 需要暴露的最小能力面（P1 由 HistoricalDataService 满足）。"""

    def ingest(
        self, symbol: str, timeframe: str, start: datetime, end: datetime, *, retries: int = 3
    ) -> "Manifest": ...

    def reprocess_raw(self, symbol: str, timeframe: str, run_id: str) -> "Manifest": ...

    def load_bars(self, symbol: str, timeframe: str, start=None, end=None, **kwargs) -> "pd.DataFrame": ...

    def load_bars_version(self, symbol: str, timeframe: str, dataset_version: str,
                          start=None, end=None, **kwargs) -> "pd.DataFrame": ...


def _first_of(manifest: "Manifest", suffix: str) -> Path | None:
    for name in manifest.file_paths:
        if name.endswith(suffix) and "/raw/" in f"/{name}":
            return Path(name)
    for name in manifest.file_paths:
        if name.endswith(suffix):
            return Path(name)
    return None


@dataclass
class BarsAcquire:
    """阶段 1：获取。P1 由 ``backend.ingest`` 整体完成（含 raw 落盘）。"""

    backend: BarsBackend
    kind: str = "bars"
    _cache: dict[str, "Manifest"] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self._cache is None:
            self._cache = {}

    def acquire(self, context: PipelineContext) -> AcquireOutcome:
        manifest = self.backend.ingest(
            context.key.symbol,
            context.key.timeframe,
            context.start,
            context.end,
            retries=context.retries,
        )
        self._cache[context.run_id] = manifest
        return AcquireOutcome(
            raw_path=_first_of(manifest, ".csv"),
            raw_frame_path=_first_of(manifest, ".parquet"),
            manifest=manifest,
        )


@dataclass
class BarsTransform:
    """阶段 2：转换。P1 的结果已由 ``ingest`` 内部算好，这里只做身份提取。"""

    kind: str = "bars"

    def transform(self, acquired: AcquireOutcome, context: PipelineContext) -> TransformOutcome:
        manifest = acquired.manifest
        if manifest is None:
            raise ValueError("BarsTransform requires an AcquireOutcome carrying a manifest")
        return TransformOutcome(
            dataset_version=manifest.dataset_version,
            request_id=manifest.request_id,
            raw_snapshot_id=manifest.raw_snapshot_id,
            manifest=manifest,
        )


@dataclass
class BarsPersist:
    """阶段 3：结构化保存。P1 的落盘已由 ``ingest`` 完成，这里只做发布态提取。"""

    kind: str = "bars"

    def persist(self, transformed: TransformOutcome, context: PipelineContext) -> PublicationOutcome:
        manifest = transformed.manifest
        if manifest is None:
            raise ValueError("BarsPersist requires a TransformOutcome carrying a manifest")
        return PublicationOutcome(
            manifest=manifest,
            status=manifest.quality_status,
            publishable=bool(manifest.quality_passed),
            dataset_manifest_path=(
                Path(manifest.dataset_manifest_path) if manifest.dataset_manifest_path else None
            ),
        )


@dataclass
class BarsRead:
    """阶段 4：读取。始终委托 facade，签名与默认值保持一致。"""

    backend: BarsBackend
    kind: str = "bars"

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
    ) -> "pd.DataFrame":
        kwargs: dict[str, Any] = {
            "adjusted": adjusted,
            "completed_only": completed_only,
            "allow_research": allow_research,
            "min_quality_score": min_quality_score,
        }
        if allow_legacy:
            kwargs["allow_legacy"] = True
        if version:
            return self.backend.load_bars_version(
                key.symbol, key.timeframe, version, start, end, **kwargs
            )
        return self.backend.load_bars(key.symbol, key.timeframe, start, end, **kwargs)


__all__ = ["BarsAcquire", "BarsBackend", "BarsPersist", "BarsRead", "BarsTransform"]
