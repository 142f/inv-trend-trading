"""Unified structural strategy contract."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .contracts import SignalResult


@runtime_checkable
class Strategy(Protocol):
    @property
    def strategy_id(self) -> str: ...

    @property
    def parameter_schema(self) -> Mapping[str, Any]: ...

    @property
    def required_features(self) -> tuple[str, ...]: ...

    def compute_signals(
        self,
        completed_bars: Sequence[Mapping[str, Any]] | Any,
        position: Any | None,
        config: Mapping[str, Any],
    ) -> SignalResult: ...


__all__ = ["Strategy"]
