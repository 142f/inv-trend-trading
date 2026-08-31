"""Stable execution-decision events for persistence and notification.

The daily strategy produces many technical evidence events.  Only a final,
confirmed execution decision may enter the formal notification outbox.  This
module keeps that business identity independent from run IDs and wall-clock
retry attempts while projecting the event into the existing ``SignalEvent``
storage contract for backward-compatible SQLite persistence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import math
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from .signals import SignalEvent


EXECUTION_DECISION_SCHEMA_VERSION = "1"
DEFAULT_DECISION_RULE_VERSION = "daily-a-breakout-eligibility-v1"


class ExecutionDecisionEventType(str, Enum):
    """Versioned event types emitted by an execution decision policy."""

    ENTRY_DECISION_LONG = "ENTRY_DECISION_LONG"
    ENTRY_DECISION_SHORT = "ENTRY_DECISION_SHORT"
    EXIT_DECISION_LONG = "EXIT_DECISION_LONG"
    EXIT_DECISION_SHORT = "EXIT_DECISION_SHORT"
    RISK_BLOCKED = "RISK_BLOCKED"


_ACTION_EVENT_TYPE = {
    "ENTER_LONG": ExecutionDecisionEventType.ENTRY_DECISION_LONG,
    "ENTER_SHORT": ExecutionDecisionEventType.ENTRY_DECISION_SHORT,
    "EXIT_LONG": ExecutionDecisionEventType.EXIT_DECISION_LONG,
    "EXIT_SHORT": ExecutionDecisionEventType.EXIT_DECISION_SHORT,
    "RISK_BLOCKED": ExecutionDecisionEventType.RISK_BLOCKED,
}
_ACTION_DIRECTION = {
    "ENTER_LONG": "LONG",
    "ENTER_SHORT": "SHORT",
    "EXIT_LONG": "LONG",
    "EXIT_SHORT": "SHORT",
    "RISK_BLOCKED": "NONE",
}
_ENTRY_ACTIONS = frozenset({"ENTER_LONG", "ENTER_SHORT"})


def _event_key(
    *,
    strategy_version: str,
    decision_rule_version: str,
    instrument_id: str,
    timeframe: str,
    as_of: str,
    action: str,
) -> str:
    return "|".join(
        (
            "execution-decision",
            strategy_version,
            decision_rule_version,
            instrument_id,
            timeframe,
            as_of,
            action,
        )
    )


@dataclass(frozen=True)
class ExecutionDecisionEvent:
    """A final policy decision with a deterministic, retry-safe identity.

    ``event_id`` and ``event_key`` contain only stable business dimensions.  A
    rerun of the same strategy rule for the same instrument/bar/action therefore
    produces the same identity, even when ``detected_at`` or ``run_id`` differs.
    """

    event_id: str
    event_key: str
    event_type: ExecutionDecisionEventType
    instrument_id: str
    symbol: str
    timeframe: str
    as_of: str
    action: str
    decision_hash: str
    strategy_version: str
    decision_rule_version: str
    dataset_version: str
    detected_at: str
    trigger_price: float
    reference_value: float | None = None
    trigger_signal_ids: tuple[str, ...] = ()
    reason_code: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = EXECUTION_DECISION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        action = str(self.action).upper()
        try:
            event_type = (
                self.event_type
                if isinstance(self.event_type, ExecutionDecisionEventType)
                else ExecutionDecisionEventType(str(self.event_type))
            )
            expected_type = _ACTION_EVENT_TYPE[action]
        except (KeyError, ValueError) as exc:
            raise ValueError(f"unsupported execution action/type: {self.action}") from exc
        if event_type is not expected_type:
            raise ValueError("execution event type does not match action")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "event_type", event_type)

        required = {
            "event_id": self.event_id,
            "event_key": self.event_key,
            "instrument_id": self.instrument_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "as_of": self.as_of,
            "decision_hash": self.decision_hash,
            "strategy_version": self.strategy_version,
            "decision_rule_version": self.decision_rule_version,
            "dataset_version": self.dataset_version,
            "detected_at": self.detected_at,
        }
        missing = [name for name, value in required.items() if not str(value)]
        if missing:
            raise ValueError(f"execution decision event is missing fields: {missing}")

        trigger_signal_ids = tuple(
            sorted({str(value) for value in self.trigger_signal_ids if str(value)})
        )
        if action in _ENTRY_ACTIONS and not trigger_signal_ids:
            raise ValueError("entry execution event requires at least one trigger signal ID")
        object.__setattr__(self, "trigger_signal_ids", trigger_signal_ids)
        object.__setattr__(self, "metadata", dict(self.metadata))

        trigger_price = float(self.trigger_price)
        reference_value = (
            None if self.reference_value is None else float(self.reference_value)
        )
        if not math.isfinite(trigger_price):
            raise ValueError("execution event trigger_price must be finite")
        if reference_value is not None and not math.isfinite(reference_value):
            raise ValueError("execution event reference_value must be finite")
        object.__setattr__(self, "trigger_price", trigger_price)
        object.__setattr__(self, "reference_value", reference_value)

        expected_key = _event_key(
            strategy_version=str(self.strategy_version),
            decision_rule_version=str(self.decision_rule_version),
            instrument_id=str(self.instrument_id),
            timeframe=str(self.timeframe),
            as_of=str(self.as_of),
            action=action,
        )
        expected_id = str(uuid5(NAMESPACE_URL, expected_key))
        if self.event_key != expected_key or self.event_id != expected_id:
            raise ValueError("execution decision event identity does not match its business key")

    @classmethod
    def create(
        cls,
        *,
        instrument_id: str,
        symbol: str,
        timeframe: str,
        as_of: str,
        action: str,
        decision_hash: str,
        strategy_version: str,
        dataset_version: str,
        detected_at: str,
        trigger_price: float,
        reference_value: float | None = None,
        trigger_signal_ids: tuple[str, ...] = (),
        reason_code: str = "",
        decision_rule_version: str = DEFAULT_DECISION_RULE_VERSION,
        metadata: Mapping[str, Any] | None = None,
    ) -> "ExecutionDecisionEvent":
        normalized_action = str(action).upper()
        try:
            event_type = _ACTION_EVENT_TYPE[normalized_action]
        except KeyError as exc:
            raise ValueError(f"unsupported execution action: {action}") from exc
        key = _event_key(
            strategy_version=str(strategy_version),
            decision_rule_version=str(decision_rule_version),
            instrument_id=str(instrument_id),
            timeframe=str(timeframe),
            as_of=str(as_of),
            action=normalized_action,
        )
        return cls(
            event_id=str(uuid5(NAMESPACE_URL, key)),
            event_key=key,
            event_type=event_type,
            instrument_id=str(instrument_id),
            symbol=str(symbol),
            timeframe=str(timeframe),
            as_of=str(as_of),
            action=normalized_action,
            decision_hash=str(decision_hash),
            strategy_version=str(strategy_version),
            decision_rule_version=str(decision_rule_version),
            dataset_version=str(dataset_version),
            detected_at=str(detected_at),
            trigger_price=float(trigger_price),
            reference_value=(
                None if reference_value is None else float(reference_value)
            ),
            trigger_signal_ids=tuple(trigger_signal_ids),
            reason_code=str(reason_code),
            metadata=dict(metadata or {}),
        )

    @property
    def direction(self) -> str:
        return _ACTION_DIRECTION[self.action]

    def to_signal_event(self) -> SignalEvent:
        """Project into the established storage/notifier event contract."""

        metadata = {
            **dict(self.metadata),
            "event_kind": "execution_decision",
            "execution_event_type": self.event_type.value,
            "execution_action": self.action,
            "decision_hash": self.decision_hash,
            "decision_rule_version": self.decision_rule_version,
            "decision_event_schema_version": self.schema_version,
            "trigger_signal_ids": list(self.trigger_signal_ids),
            "reason_code": self.reason_code,
            "strategy_version": self.strategy_version,
        }
        return SignalEvent(
            signal_id=self.event_id,
            signal_key=self.event_key,
            instrument_id=self.instrument_id,
            symbol=self.symbol,
            timeframe=self.timeframe,
            signal_type=self.event_type.value,
            direction=self.direction,
            signal_time=self.as_of,
            detected_at=self.detected_at,
            trigger_price=self.trigger_price,
            reference_value=self.reference_value,
            dataset_version=self.dataset_version,
            indicator_name="execution_decision",
            indicator_parameters={
                "decision_rule_version": self.decision_rule_version,
                "trigger_signal_count": len(self.trigger_signal_ids),
            },
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["event_type"] = self.event_type.value
        return payload


__all__ = [
    "DEFAULT_DECISION_RULE_VERSION",
    "EXECUTION_DECISION_SCHEMA_VERSION",
    "ExecutionDecisionEvent",
    "ExecutionDecisionEventType",
]
