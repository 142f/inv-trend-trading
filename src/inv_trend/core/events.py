"""Versioned audit event model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class EventType(str, Enum):
    RUN_STARTED = "RUN_STARTED"
    RUN_COMPLETED = "RUN_COMPLETED"
    DATA_VALIDATION_FAILED = "DATA_VALIDATION_FAILED"
    SIGNAL_CANDIDATE = "SIGNAL_CANDIDATE"
    SIGNAL_FILTERED = "SIGNAL_FILTERED"
    ELIGIBILITY_REJECTED = "ELIGIBILITY_REJECTED"
    STATE_COMMITTED = "STATE_COMMITTED"
    ORDER_INTENT_CREATED = "ORDER_INTENT_CREATED"
    ORDER_INTENT_EXPIRED = "ORDER_INTENT_EXPIRED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_REJECTED_AT_FILL = "ORDER_REJECTED_AT_FILL"
    ORDER_ADJUSTED_AT_FILL = "ORDER_ADJUSTED_AT_FILL"
    FAST_SYSTEM_SKIPPED = "FAST_SYSTEM_SKIPPED"
    FAST_SYSTEM_RESOLVED = "FAST_SYSTEM_RESOLVED"
    POSITION_EXITED = "POSITION_EXITED"
    RISK_LIMIT_REACHED = "RISK_LIMIT_REACHED"


@dataclass(frozen=True)
class DecisionEvent:
    event_id: str
    run_id: str
    event_time: str
    event_type: EventType
    stage: str
    event_schema_version: str = "1.0"
    bar_time: str | None = None
    symbol: str | None = None
    action: str | None = None
    reason_code: str = ""
    message: str = ""
    metrics: Mapping[str, Any] = field(default_factory=dict)
    rule_snapshot_hash: str = ""
    data_fingerprint: str = ""
    context: Mapping[str, Any] = field(default_factory=dict)
