"""I/O-free signal identities shared by scanners, storage and reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5


CORRECTED_STRATEGY_VERSION = "corrected-v2"


@dataclass(frozen=True)
class SignalEvent:
    signal_id: str
    signal_key: str
    instrument_id: str
    symbol: str
    timeframe: str
    signal_type: str
    direction: str
    signal_time: str
    detected_at: str
    trigger_price: float
    reference_value: float | None
    dataset_version: str
    indicator_name: str
    indicator_parameters: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, *, strategy_version: str = CORRECTED_STRATEGY_VERSION, **values: Any) -> "SignalEvent":
        if strategy_version != CORRECTED_STRATEGY_VERSION:
            raise ValueError("only the executable strategy version 'corrected-v2' is supported")
        key = "|".join((
            str(values["instrument_id"]), str(values["timeframe"]),
            str(values["signal_type"]), str(values["signal_time"]), strategy_version,
        ))
        return cls(
            signal_id=str(uuid5(NAMESPACE_URL, key)), signal_key=key,
            instrument_id=str(values["instrument_id"]), symbol=str(values["symbol"]),
            timeframe=str(values["timeframe"]), signal_type=str(values["signal_type"]),
            direction=str(values["direction"]), signal_time=str(values["signal_time"]),
            detected_at=str(values["detected_at"]), trigger_price=float(values["trigger_price"]),
            reference_value=(None if values.get("reference_value") is None else float(values["reference_value"])),
            dataset_version=str(values["dataset_version"]), indicator_name=str(values["indicator_name"]),
            indicator_parameters=dict(values.get("indicator_parameters", {})),
            metadata={**dict(values.get("metadata", {})), "strategy_version": strategy_version},
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def crossed_above(previous_left: float, previous_right: float, left: float, right: float) -> bool:
    return previous_left <= previous_right and left > right


def crossed_below(previous_left: float, previous_right: float, left: float, right: float) -> bool:
    return previous_left >= previous_right and left < right
