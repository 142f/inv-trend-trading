"""流水线注册表：``kind`` → ``PipelineSpec`` 的解析与 ``bars`` 项目注册。"""

from __future__ import annotations

from typing import Callable, Mapping

from ..models import InstrumentConfig
from ..providers import BarsProvider
from ..storage import DataLake
from .契约_v1 import PipelineSpec
from .行情阶段_v1 import BarsAcquire, BarsBackend, BarsPersist, BarsRead, BarsTransform

# 构造一个 kind 的完整流水线。``backend`` 是 P1 过渡参数：builder 需要一个
# 已打开 lake 的服务对象来整体委托；P2 拆分四阶段后该参数可移除。
PipelineBuilder = Callable[..., PipelineSpec]


class PipelineRegistry:
    """以 ``kind`` 解析 ``PipelineSpec`` 构造器。"""

    def __init__(self) -> None:
        self._builders: dict[str, PipelineBuilder] = {}

    def register(self, kind: str, builder: PipelineBuilder) -> None:
        key = str(kind)
        if not key:
            raise ValueError("pipeline kind must be a non-empty string")
        if key in self._builders:
            raise ValueError(f"pipeline kind already registered: {key}")
        self._builders[key] = builder

    def resolve(self, kind: str) -> PipelineBuilder:
        try:
            return self._builders[str(kind)]
        except KeyError as exc:
            raise KeyError(f"no pipeline registered for kind: {kind}") from exc

    def kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._builders))

    def clear(self) -> None:
        """仅供测试隔离使用。"""

        self._builders.clear()


registry = PipelineRegistry()


def build_bars_spec(
    instruments: Mapping[str, InstrumentConfig],
    providers: Mapping[str, BarsProvider],
    lake: DataLake,
    backend: BarsBackend,
) -> PipelineSpec:
    """构造 bars 流水线：四个阶段共享同一个 backend 与 lake。"""

    del instruments, providers, lake  # P1 由 backend 承载；P2 起按阶段注入
    return PipelineSpec(
        kind="bars",
        acquire=BarsAcquire(backend=backend),
        transform=BarsTransform(),
        persist=BarsPersist(),
        read=BarsRead(backend=backend),
    )


def register_pipeline(kind: str, builder: PipelineBuilder) -> None:
    registry.register(kind, builder)


def get_pipeline(kind: str) -> PipelineBuilder:
    return registry.resolve(kind)


def ensure_bars_registered() -> None:
    """幂等注册内置的 bars 项目。"""

    if "bars" not in registry.kinds():
        registry.register("bars", build_bars_spec)


ensure_bars_registered()


__all__ = [
    "PipelineBuilder",
    "PipelineRegistry",
    "build_bars_spec",
    "ensure_bars_registered",
    "get_pipeline",
    "register_pipeline",
    "registry",
]
