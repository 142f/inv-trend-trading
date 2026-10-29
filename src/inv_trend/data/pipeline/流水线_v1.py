"""标准链路编排：acquire → transform → persist；read 为独立入口。dataset-agnostic。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from .契约_v1 import (
    AcquireOutcome,
    DatasetKey,
    PipelineContext,
    PipelineSpec,
    PublicationOutcome,
    TransformOutcome,
)
from .注册表_v1 import PipelineBuilder, get_pipeline


class MarketDataPipeline:
    """把 ``PipelineSpec`` 的四个阶段串成标准链路。

    ``ingest`` 严格实现为 ``acquire → transform → persist``；
    ``read`` 是独立入口。编排本身不含任何行情算法。
    """

    def __init__(self, spec: PipelineSpec) -> None:
        self.spec = spec

    # --- 构造 ---------------------------------------------------------------
    @classmethod
    def for_kind(cls, kind: str, **kwargs: Any) -> "MarketDataPipeline":
        builder: PipelineBuilder = get_pipeline(kind)
        return cls(builder(**kwargs))

    # --- 上下文 -------------------------------------------------------------
    def _context(
        self,
        key: DatasetKey,
        start: datetime,
        end: datetime,
        *,
        retries: int = 3,
        run_id: str | None = None,
    ) -> PipelineContext:
        return PipelineContext(
            kind=self.spec.kind,
            key=key,
            start=start,
            end=end,
            run_id=run_id or uuid4().hex[:24],
            retries=retries,
        )

    # --- 单一阶段 -----------------------------------------------------------
    def acquire(
        self, key: DatasetKey, start: datetime, end: datetime, *, retries: int = 3,
        run_id: str | None = None,
    ) -> AcquireOutcome:
        return self.spec.acquire.acquire(
            self._context(key, start, end, retries=retries, run_id=run_id)
        )

    def transform(self, acquired: AcquireOutcome, context: PipelineContext) -> TransformOutcome:
        return self.spec.transform.transform(acquired, context)

    def persist(self, transformed: TransformOutcome, context: PipelineContext) -> PublicationOutcome:
        return self.spec.persist.persist(transformed, context)

    # --- 组合链路 -----------------------------------------------------------
    def ingest(
        self, key: DatasetKey, start: datetime, end: datetime, *, retries: int = 3
    ):
        """= acquire → transform → persist；返回发布后的 Manifest。

        质量门禁回退（主源未通过时切换 fallback_sources）由 acquire 端口的
        可选 ``retry_fallback`` 提供：命中时用**新的 run_id** 再走一遍
        transform → persist，语义与搬移前完全一致。
        """

        context = self._context(key, start, end, retries=retries)
        acquired = self.spec.acquire.acquire(context)
        transformed = self.spec.transform.transform(acquired, context)
        published = self.spec.persist.persist(transformed, context)

        retry = getattr(self.spec.acquire, "retry_fallback", None)
        if retry is not None:
            retry_result = retry(context, acquired, published)
            if retry_result is not None:
                fallback_context, fallback_acquired = retry_result
                fallback_transformed = self.spec.transform.transform(
                    fallback_acquired, fallback_context
                )
                fallback_published = self.spec.persist.persist(
                    fallback_transformed, fallback_context
                )
                if fallback_published.manifest is not None:
                    return fallback_published.manifest

        if published.manifest is None:
            raise RuntimeError("pipeline persist produced no manifest")
        return published.manifest

    def reprocess_raw(self, key: DatasetKey, run_id: str):
        """复用 transform → persist 的重新处理入口（不调用任何 provider）。"""

        core = getattr(self.spec.acquire, "core", None)
        if core is None:
            raise NotImplementedError(
                f"pipeline kind {self.spec.kind!r} does not support reprocess_raw"
            )
        instrument = core.instrument(key.symbol)
        return core.reprocess_raw(instrument, key.timeframe.upper(), run_id)

    # --- 读取 ---------------------------------------------------------------
    def read(self, key: DatasetKey, **filters: Any):
        return self.spec.read.read(key, **filters)


__all__ = ["MarketDataPipeline"]
