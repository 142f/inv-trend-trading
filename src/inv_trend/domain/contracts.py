"""Infrastructure-free value contracts shared by research and execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Generic, Mapping, TypeVar


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(_freeze(item) for item in value)
    return value


def _frozen(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return _freeze(value or {})


@dataclass(frozen=True)
class SignalResult:
    strategy_id: str
    symbol: str
    observed_at: Any
    action: str
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", _frozen(self.evidence))


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    action: str
    side: int
    quantity: float
    decided_at: Any
    earliest_fill_at: Any
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.side not in {-1, 1} or self.quantity <= 0:
            raise ValueError("order side must be -1/1 and quantity must be positive")
        if self.earliest_fill_at <= self.decided_at:
            raise ValueError("an order cannot fill at or before its decision time")
        object.__setattr__(self, "metadata", _frozen(self.metadata))


@dataclass(frozen=True)
class FillEvent:
    symbol: str
    side: int
    quantity: float
    price: float
    filled_at: Any
    fee: float = 0.0
    slippage: float = 0.0

    def __post_init__(self) -> None:
        if self.side not in {-1, 1} or min(self.quantity, self.price) <= 0:
            raise ValueError("fill side, quantity, and price are invalid")
        if min(self.fee, self.slippage) < 0:
            raise ValueError("fill costs cannot be negative")


@dataclass(frozen=True)
class PortfolioState:
    cash: float
    positions: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "positions", _frozen(self.positions))


T = TypeVar("T")


@dataclass(frozen=True)
class StageOutput(Generic[T]):
    stage: str
    result: T
    result_hash: str
    input_hashes: tuple[str, ...] = ()
    schema_version: str = "1"

    def __post_init__(self) -> None:
        if not self.stage or not self.result_hash:
            raise ValueError("stage and result_hash are required")
