"""数据管理流水线的标准链路契约：获取(acquire) → 转换(transform) → 结构化保存(persist) → 读取(read)。

本模块只定义**契约**（阶段常量、DTO、端口协议、``PipelineSpec``），不含任何行情算法，
也不依赖 ``inv_trend.data.api``（避免与 facade 形成循环导入）。

P2 之后：四阶段由 ``行情阶段_v1.py`` 的 ``BarsCore`` 实现，分析性字段
（``normalized`` / ``clean`` / ``merged`` / ``conflicts`` 等）已逐字段填充；
本模块仍只定义契约，不含任何行情算法。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:  # 仅类型检查期导入，运行期保持零依赖
    import pandas as pd

    from ..models import Manifest, ProviderResult

PIPELINE_STAGES: tuple[str, ...] = ("acquire", "transform", "persist", "read")


@dataclass(frozen=True)
class DatasetKey:
    """数据集身份：品种 + 周期。"""

    symbol: str
    timeframe: str


@dataclass(frozen=True)
class PipelineContext:
    """一次链路的上下文（不含任何行情算法）。"""

    kind: str
    key: DatasetKey
    start: datetime
    end: datetime
    run_id: str
    retries: int = 3


@dataclass(frozen=True)
class AcquireOutcome:
    """阶段 1 输出：原始载荷与原始帧的落盘位置。

    ``manifest`` 是 P1 过渡字段：整体委托旧实现时直接携带其 Manifest，
    P2 拆分后仍保留（供 persist 复用已算好的身份）。
    """

    provider_result: "ProviderResult | None" = None
    raw_path: "Path | None" = None
    raw_frame_path: "Path | None" = None
    errors: tuple[str, ...] = ()
    manifest: "Manifest | None" = None


@dataclass(frozen=True)
class TransformOutcome:
    """阶段 2 输出：纯计算结果（不落盘）。

    除方案列出的分析性字段外，另带 P2 拆分所需的**传递字段**（``provider_result``、
    ``raw_path``、``status``、``curated_view`` 等），使 stage 3 无需回读即可落盘。
    这些字段为新增，不改变既有字段的名称与语义。
    """

    normalized: Any = None
    clean: "pd.DataFrame | None" = None
    merged: "pd.DataFrame | None" = None
    conflicts: tuple[dict[str, object], ...] = ()
    rejected_incoming: "pd.DataFrame | None" = None
    quality: Any = None
    dataset_version: str = ""
    request_id: str = ""
    raw_snapshot_id: str = ""
    outside_window_revisions: tuple[dict[str, object], ...] = ()
    # --- 阶段 2 → 阶段 3 传递字段（P2 新增） ---
    provider_result: "ProviderResult | None" = None
    raw_path: "Path | None" = None
    raw_frame_path: "Path | None" = None
    errors: tuple[str, ...] = ()
    status: str = ""
    publishable: bool = False
    current_pointer: dict | None = None
    parent_dataset_version: str | None = None
    is_new_dataset: bool = False
    curated_view: "pd.DataFrame | None" = None
    complete_count: int = 0
    inherited_research_only: bool = False
    config_hash: str = ""
    metadata_hash: str = ""


@dataclass(frozen=True)
class PublicationOutcome:
    """阶段 3 输出：发布结果。"""

    manifest: "Manifest | None" = None
    status: str = ""
    publishable: bool = False
    dataset_manifest_path: Path | None = None


@runtime_checkable
class DataAcquirePort(Protocol):
    kind: str

    def acquire(self, context: PipelineContext) -> AcquireOutcome: ...


@runtime_checkable
class DataTransformPort(Protocol):
    def transform(self, acquired: AcquireOutcome, context: PipelineContext) -> TransformOutcome: ...


@runtime_checkable
class DataPersistPort(Protocol):
    def persist(self, transformed: TransformOutcome, context: PipelineContext) -> PublicationOutcome: ...


@runtime_checkable
class DataReadPort(Protocol):
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
    ) -> "pd.DataFrame": ...


@dataclass(frozen=True)
class PipelineSpec:
    """一个「数据集种类」的完整流水线定义。"""

    kind: str
    acquire: DataAcquirePort
    transform: DataTransformPort
    persist: DataPersistPort
    read: DataReadPort
    rule_version: str = ""


PipelineBuilder = Any  # 见 注册表_v1.PipelineBuilder（Callable，含 lake 与旧服务）


__all__ = [
    "PIPELINE_STAGES",
    "AcquireOutcome",
    "DataAcquirePort",
    "DataPersistPort",
    "DataReadPort",
    "DataTransformPort",
    "DatasetKey",
    "PipelineBuilder",
    "PipelineContext",
    "PipelineSpec",
    "PublicationOutcome",
    "TransformOutcome",
]
