"""流水线注册表：``kind`` → ``PipelineSpec`` 的解析与 ``bars`` 项目注册。"""

from __future__ import annotations

from typing import Callable, Mapping

from ..models import InstrumentConfig
from ..providers import BarsProvider
from ..storage import DataLake
from .契约_v1 import PipelineSpec
from .行情阶段_v1 import BarsAcquire, BarsPersist, BarsRead, BarsTransform, build_bars_core

# 构造一个 kind 的完整流水线。
# ``load_update_base`` / ``manifest_for_pointer`` 由 facade 注入：它们同时服务于
# facade 上的 update*/missing_intervals 等方法，注入可避免 pipeline 反向依赖 api。
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
    *,
    load_update_base: Callable[[str, str], tuple[object, dict | None]] | None = None,
    manifest_for_pointer: Callable[[dict | None], object] | None = None,
    backend: object | None = None,
) -> PipelineSpec:
    """构造 bars 流水线：四个阶段共享同一个 ``BarsCore``。

    ``load_update_base`` / ``manifest_for_pointer`` 由 facade 注入（它们同时服务于
    facade 上的 update*/missing_intervals）。传入 ``backend``（一个
    ``HistoricalDataService`` 兼容对象）时会从它身上取这两个方法，便于
    直接以服务对象构造流水线。
    """

    if backend is not None:
        load_update_base = load_update_base or backend._load_update_base  # type: ignore[attr-defined]
        manifest_for_pointer = (
            manifest_for_pointer or backend._manifest_for_pointer  # type: ignore[attr-defined]
        )
    if load_update_base is None or manifest_for_pointer is None:
        raise TypeError(
            "build_bars_spec requires load_update_base and manifest_for_pointer (or a backend)"
        )

    core = build_bars_core(
        lake=lake,
        instruments=instruments,
        providers=providers,
        load_update_base=load_update_base,
        manifest_for_pointer=manifest_for_pointer,
    )
    return PipelineSpec(
        kind="bars",
        acquire=BarsAcquire(core=core),
        transform=BarsTransform(core=core),
        persist=BarsPersist(core=core),
        read=BarsRead(core=core),
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
