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


@dataclass(frozen=True)
class IndicatorSignalEpisode:
    """One causally evaluated interval of an indicator state."""

    episode_id: str
    indicator_id: str
    indicator_name: str
    timeframe: str
    role: str
    direction: str
    state_key: str
    start_timestamp: str
    start_price: float | None
    end_timestamp: str
    end_price: float | None
    status: str
    lifecycle_state: str
    duration_periods: int
    duration_d1_bars: int
    elapsed_days: int
    current_strength: float
    peak_strength: float
    average_strength: float
    strength_delta: float
    strength_trend: str
    trigger_conditions: tuple[Mapping[str, Any], ...] = ()
    rule_ids: tuple[str, ...] = ()
    last_reinforcement_timestamp: str | None = None
    reinforcement_count: int = 0
    invalidation_timestamp: str | None = None
    invalidation_price: float | None = None
    invalidation_reason: str | None = None
    invalidation_conditions: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "indicator_id": self.indicator_id,
            "indicator_name": self.indicator_name,
            "timeframe": self.timeframe,
            "role": self.role,
            "direction": self.direction,
            "state_key": self.state_key,
            "start_timestamp": self.start_timestamp,
            "start_price": self.start_price,
            "end_timestamp": self.end_timestamp,
            "end_price": self.end_price,
            "status": self.status,
            "lifecycle_state": self.lifecycle_state,
            "duration_periods": self.duration_periods,
            "duration_d1_bars": self.duration_d1_bars,
            "elapsed_days": self.elapsed_days,
            "current_strength": float(self.current_strength),
            "peak_strength": float(self.peak_strength),
            "average_strength": float(self.average_strength),
            "strength_delta": float(self.strength_delta),
            "strength_trend": self.strength_trend,
            "trigger_conditions": [_json_value(item) for item in self.trigger_conditions],
            "rule_ids": list(self.rule_ids),
            "last_reinforcement_timestamp": self.last_reinforcement_timestamp,
            "reinforcement_count": int(self.reinforcement_count),
            "invalidation_timestamp": self.invalidation_timestamp,
            "invalidation_price": self.invalidation_price,
            "invalidation_reason": self.invalidation_reason,
            "invalidation_conditions": list(self.invalidation_conditions),
            "metadata": _json_value(self.metadata),
        }


@dataclass(frozen=True)
class IndicatorSignalAnalysis:
    """Latest indicator interpretation backed by one lifecycle history."""

    indicator_id: str
    indicator_name: str
    timeframe: str
    role: str
    availability: str
    direction: str
    lifecycle_state: str
    active: bool
    decision_weight: float
    strength: float
    strength_meaning: str
    strength_delta: float
    strength_trend: str
    first_trigger_timestamp: str | None = None
    first_trigger_price: float | None = None
    duration_periods: int = 0
    duration_d1_bars: int = 0
    elapsed_days: int = 0
    peak_strength: float = 0.0
    average_strength: float = 0.0
    current_episode_id: str | None = None
    last_episode_id: str | None = None
    last_reinforcement_timestamp: str | None = None
    reinforcement_count: int = 0
    invalidation_timestamp: str | None = None
    invalidation_reason: str | None = None
    support_effect: str = "neutral"
    explanation: str = ""
    current_values: Mapping[str, Any] = field(default_factory=dict)
    trigger_conditions: tuple[Mapping[str, Any], ...] = ()
    invalidation_conditions: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "indicator_id": self.indicator_id,
            "indicator_name": self.indicator_name,
            "timeframe": self.timeframe,
            "role": self.role,
            "availability": self.availability,
            "direction": self.direction,
            "lifecycle_state": self.lifecycle_state,
            "active": bool(self.active),
            "decision_weight": float(self.decision_weight),
            "strength": float(self.strength),
            "strength_meaning": self.strength_meaning,
            "strength_delta": float(self.strength_delta),
            "strength_trend": self.strength_trend,
            "first_trigger_timestamp": self.first_trigger_timestamp,
            "first_trigger_price": self.first_trigger_price,
            "duration_periods": int(self.duration_periods),
            "duration_d1_bars": int(self.duration_d1_bars),
            "elapsed_days": int(self.elapsed_days),
            "peak_strength": float(self.peak_strength),
            "average_strength": float(self.average_strength),
            "current_episode_id": self.current_episode_id,
            "last_episode_id": self.last_episode_id,
            "last_reinforcement_timestamp": self.last_reinforcement_timestamp,
            "reinforcement_count": int(self.reinforcement_count),
            "invalidation_timestamp": self.invalidation_timestamp,
            "invalidation_reason": self.invalidation_reason,
            "support_effect": self.support_effect,
            "explanation": self.explanation,
            "current_values": _json_value(self.current_values),
            "trigger_conditions": [_json_value(item) for item in self.trigger_conditions],
            "invalidation_conditions": list(self.invalidation_conditions),
            "metadata": _json_value(self.metadata),
        }


@dataclass(frozen=True)
class DecisionEvidenceItem:
    """One auditable contribution to a report-only confidence explanation."""

    indicator_id: str
    indicator_name: str
    timeframe: str
    role: str
    direction: str
    active: bool
    strength: float
    weight: float
    contribution: float
    contribution_type: str
    first_trigger_timestamp: str | None
    duration_periods: int
    duration_d1_bars: int
    explanation: str
    episode_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "indicator_id": self.indicator_id,
            "indicator_name": self.indicator_name,
            "timeframe": self.timeframe,
            "role": self.role,
            "direction": self.direction,
            "active": bool(self.active),
            "strength": float(self.strength),
            "weight": float(self.weight),
            "contribution": float(self.contribution),
            "contribution_type": self.contribution_type,
            "first_trigger_timestamp": self.first_trigger_timestamp,
            "duration_periods": int(self.duration_periods),
            "duration_d1_bars": int(self.duration_d1_bars),
            "explanation": self.explanation,
            "episode_id": self.episode_id,
            "metadata": _json_value(self.metadata),
        }


__all__ = [
    "AnomalyEvent",
    "ConditionEvaluation",
    "DecisionEvidenceItem",
    "IndicatorSignalAnalysis",
    "IndicatorSignalEpisode",
    "RuleEvaluation",
    "StateTransition",
]
