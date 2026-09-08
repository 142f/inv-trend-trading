"""Strict, dependency-free stage contracts. No file/database/network access."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Generic, Protocol, TypeVar

SCHEMA_VERSION = "1.0"
STAGES = ("acquire", "process", "indicators", "signal", "backtest", "evaluate", "report")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def timestamp(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must explicitly use UTC")
    return result


def canonical(value: Any) -> bytes:
    return json.dumps(asdict(value) if is_dataclass(value) else value, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def finite(name: str, value: float, *, minimum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name}: finite number required")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name}: must be >= {minimum}")


@dataclass(frozen=True, slots=True)
class RunContext:
    run_id: str
    dataset_version: str
    strategy_version: str
    code_version: str
    parameters_hash: str
    scope: str = "RESEARCH_ONLY"

    def __post_init__(self) -> None:
        if not all(isinstance(getattr(self, x.name), str) and getattr(self, x.name)
                   for x in fields(self)):
            raise ValueError("all context fields are required nonempty strings")
        if self.scope not in ("TRAIN", "OOS", "STRESS", "TEST", "RESEARCH_ONLY"):
            raise ValueError("unknown scope; no production authorization exists")


@dataclass(frozen=True, slots=True)
class Quality:
    status: str = "PASS"
    checks: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in ("PASS", "WARN", "FAIL"):
            raise ValueError("invalid quality status")
        if self.errors and self.status != "FAIL":
            raise ValueError("errors require FAIL status")


@dataclass(frozen=True, slots=True)
class RawBar:
    symbol: str
    timeframe: str
    source: str
    opened_at: str
    available_at: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    sequence: int


@dataclass(frozen=True, slots=True)
class Bar(RawBar):
    """Completed bar; available_at is its decision clock, not its date label."""
    def __post_init__(self) -> None:
        if not self.symbol or not self.source or self.timeframe != "D1":
            raise ValueError("symbol/source required; v1 implements explicit D1 sessions only")
        object.__setattr__(self, "opened_at", timestamp(self.opened_at).isoformat())
        object.__setattr__(self, "available_at", timestamp(self.available_at).isoformat())
        if timestamp(self.opened_at) >= timestamp(self.available_at):
            raise ValueError("bar open must precede close availability")
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("sequence must be a nonnegative integer")
        for key in ("open", "high", "low", "close"):
            finite(key, getattr(self, key), minimum=1e-12)
        finite("volume", self.volume, minimum=0)
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("inconsistent OHLC; no silent cleaning")


@dataclass(frozen=True, slots=True)
class FeatureFrame:
    bar: Bar
    values: tuple[tuple[str, float | None], ...]
    ready: bool
    state_hash: str
    parameters: tuple[tuple[str, int], ...]

    def value(self, name: str) -> float | None:
        for key, value in self.values:
            if key == name:
                return value
        raise KeyError(name)


@dataclass(frozen=True, slots=True)
class Signal:
    features: FeatureFrame
    direction: int
    target_weight: float
    candidate_id: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.direction) is not int or self.direction not in (-1, 0, 1):
            raise ValueError("direction must be -1, 0, 1")
        finite("target_weight", self.target_weight)
        if abs(self.target_weight) > 1:
            raise ValueError("unlevered contract restricts abs(weight)<=1")
        if self.target_weight * self.direction < 0:
            raise ValueError("direction and target weight disagree")


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: str
    signal_at: str
    filled_at: str
    requested: float
    quantity: float
    price: float
    fee: float
    slippage_cost: float
    status: str
    reason: str


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    symbol: str
    as_of: str
    cash: float
    quantity: float
    close: float
    equity: float
    initial_cash: float
    fees: float
    slippage_cost: float
    fills: tuple[Fill, ...]
    pending_orders: int
    signal_hash: str


@dataclass(frozen=True, slots=True)
class Evaluation:
    as_of: str
    metrics: tuple[tuple[str, float | int | None], ...]
    quality_gate: str
    production_enabled: bool = False


@dataclass(frozen=True, slots=True)
class Report:
    as_of: str
    title: str
    metrics: tuple[tuple[str, float | int | None], ...]
    verdict: str
    production_enabled: bool = False


@dataclass(frozen=True, slots=True)
class StageFailure:
    code: str
    message: str
    retryable: bool = False


PAYLOAD_TYPES = {x.__name__: x for x in
                 (RawBar, Bar, FeatureFrame, Signal, Fill, AccountSnapshot, Evaluation, Report, StageFailure)}
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Envelope(Generic[T]):
    context: RunContext
    stage: str
    stage_version: str
    payload: T
    generated_at: str
    as_of: str
    input_hash: str
    output_hash: str
    parent_ids: tuple[str, ...]
    quality: Quality
    schema_version: str = SCHEMA_VERSION

    def validate(self, *, allow_failed: bool = False) -> None:
        if self.schema_version != SCHEMA_VERSION or self.stage not in STAGES:
            raise ValueError("unsupported schema/stage")
        if not self.stage_version:
            raise ValueError("stage_version required")
        timestamp(self.generated_at)
        timestamp(self.as_of)
        if self.quality.status == "FAIL" and not allow_failed:
            raise ValueError("upstream quality failed: " + "; ".join(self.quality.errors))
        if self.output_hash != digest(self.payload):
            raise ValueError("payload integrity mismatch")

    @property
    def artifact_id(self) -> str:
        return digest({"run": self.context.run_id, "context": asdict(self.context),
                       "stage": self.stage, "version": self.stage_version,
                       "payload": self.output_hash, "as_of": self.as_of,
                       "input": self.input_hash, "parents": self.parent_ids,
                       "quality": asdict(self.quality), "schema": self.schema_version})


def emit(context: RunContext, stage: str, payload: T, as_of: str, *,
         parent: Envelope | None = None, quality: Quality | None = None,
         stage_version: str = "1.0") -> Envelope[T]:
    if parent:
        parent.validate()
        if parent.context != context:
            raise ValueError("cross-run/context contamination")
        if timestamp(as_of) < timestamp(parent.as_of):
            raise ValueError("stage time cannot move backwards")
    packet = Envelope(context, stage, stage_version, payload, utc_now(), as_of,
                      parent.output_hash if parent else "", digest(payload),
                      (parent.artifact_id,) if parent else (), quality or Quality())
    packet.validate()
    return packet


class Stage(Protocol):
    def run(self, input: Envelope) -> Envelope: ...
    def snapshot(self) -> dict[str, Any]: ...
    def restore(self, state: dict[str, Any]) -> None: ...


def encode_envelope(packet: Envelope) -> bytes:
    packet.validate(allow_failed=True)
    return canonical({"payload_type": type(packet.payload).__name__, "envelope": asdict(packet)})


def decode_payload(name: str, value: dict[str, Any]) -> Any:
    cls = PAYLOAD_TYPES.get(name)
    if cls is None or set(value) != {f.name for f in fields(cls)}:
        raise ValueError("unknown payload type or unexpected/missing fields")
    value = dict(value)
    if name == "FeatureFrame":
        value["bar"] = decode_payload("Bar", value["bar"])
        value["values"] = tuple(tuple(x) for x in value["values"])
        value["parameters"] = tuple(tuple(x) for x in value["parameters"])
    elif name == "Signal":
        value["features"] = decode_payload("FeatureFrame", value["features"])
        value["evidence"] = tuple(value["evidence"])
    elif name == "AccountSnapshot":
        value["fills"] = tuple(decode_payload("Fill", f) for f in value["fills"])
    elif name in ("Evaluation", "Report"):
        value["metrics"] = tuple(tuple(x) for x in value["metrics"])
    return cls(**value)


def decode_envelope(raw: bytes | str) -> Envelope:
    value = json.loads(raw)
    if set(value) != {"payload_type", "envelope"}:
        raise ValueError("invalid envelope document")
    body = value["envelope"]
    if set(body) != {f.name for f in fields(Envelope)}:
        raise ValueError("unexpected/missing envelope fields")
    body["payload"] = decode_payload(value["payload_type"], body["payload"])
    body["context"] = RunContext(**body["context"])
    quality = body["quality"]
    body["quality"] = Quality(quality["status"], tuple(quality["checks"]),
                              tuple(quality["errors"]), tuple(quality["warnings"]))
    body["parent_ids"] = tuple(body["parent_ids"])
    result = Envelope(**body)
    result.validate(allow_failed=True)
    return result
