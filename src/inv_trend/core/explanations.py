"""Canonical, JSON-safe explanation models for rules, anomalies and state changes.

These models are deliberately strategy-agnostic.  Strategy adapters populate them,
while reports, audit logs and regression tests only serialize/compare them.  That
keeps business calculation out of the presentation layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


_JSON_SCALARS = (str, int, float, bool, type(None))


def _json_value(value: Any) -> Any:
    if isinstance(value, _JSON_SCALARS):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "item"):
        try:
            return _json_value(value.item())
        except (TypeError, ValueError):
            pass
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


@dataclass(frozen=True)
class ConditionEvaluation:
    """One atomic strategy/quality condition and the evidence used to judge it."""

    condition_id: str
    name: str
    actual_value: Any = None
    reference_value: Any = None
    operator: str = "custom"
    passed: bool | None = None
    relative_position: str = "unavailable"
    impact: str = "informational"
    weight: float = 0.0
    timestamp: str | None = None
    price: float | None = None
    status: str = "evaluated"
    reason: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "name": self.name,
            "actual_value": _json_value(self.actual_value),
            "reference_value": _json_value(self.reference_value),
            "operator": self.operator,
            "passed": self.passed,
            "relative_position": self.relative_position,
            "impact": self.impact,
            "weight": float(self.weight),
            "timestamp": self.timestamp,
            "price": self.price,
            "status": self.status,
            "reason": self.reason,
            "metadata": _json_value(self.metadata),
        }


@dataclass(frozen=True)
class RuleEvaluation:
    """A complete multi-condition rule evaluation.

    ``match_policy`` is one of ``all``/``any``/``custom``.  Even when a rule is
    not triggered, every evaluated child condition remains in ``conditions``.
    """

    rule_id: str
    name: str
    triggered: bool
    conditions: tuple[ConditionEvaluation, ...] = ()
    match_policy: str = "all"
    direction: str | None = None
    score_impact: float = 0.0
    timestamp: str | None = None
    price: float | None = None
    outcome: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "triggered": bool(self.triggered),
            "match_policy": self.match_policy,
            "direction": self.direction,
            "score_impact": float(self.score_impact),
            "timestamp": self.timestamp,
            "price": self.price,
            "outcome": self.outcome,
            "conditions": [item.to_dict() for item in self.conditions],
            "metadata": _json_value(self.metadata),
        }


@dataclass(frozen=True)
class AnomalyEvent:
    """Report-only anomaly evidence.  It does not change strategy semantics."""

    anomaly_id: str
    anomaly_type: str
    severity: str
    timestamp: str
    price: float | None
    conditions: tuple[ConditionEvaluation, ...]
    summary: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "anomaly_id": self.anomaly_id,
            "anomaly_type": self.anomaly_type,
            "severity": self.severity,
            "timestamp": self.timestamp,
            "price": self.price,
            "summary": self.summary,
            "conditions": [item.to_dict() for item in self.conditions],
            "metadata": _json_value(self.metadata),
        }


@dataclass(frozen=True)
class StateTransition:
    """A state change with explicit before/after values and supporting reason."""

    transition_id: str
    state_name: str
    from_state: Any
    to_state: Any
    timestamp: str
    price: float | None
    reason: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "state_name": self.state_name,
            "from_state": _json_value(self.from_state),
            "to_state": _json_value(self.to_state),
            "timestamp": self.timestamp,
            "price": self.price,
            "reason": self.reason,
            "metadata": _json_value(self.metadata),
        }


__all__ = [
    "AnomalyEvent",
    "ConditionEvaluation",
    "RuleEvaluation",
    "StateTransition",
]
