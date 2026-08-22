"""Reusable helpers for producing canonical condition evidence."""

from __future__ import annotations

import math
from typing import Any, Mapping

from .explanations import ConditionEvaluation


def evaluate_condition(
    *,
    condition_id: str,
    name: str,
    actual: Any,
    operator: str,
    reference: Any,
    timestamp: str | None = None,
    price: float | None = None,
    impact: str = "informational",
    weight: float = 0.0,
    metadata: Mapping[str, Any] | None = None,
) -> ConditionEvaluation:
    """Evaluate one comparison without throwing on unavailable numeric inputs."""

    try:
        passed = _compare(actual, operator, reference)
        position = _relative_position(actual, operator, reference)
        return ConditionEvaluation(
            condition_id=condition_id,
            name=name,
            actual_value=actual,
            reference_value=reference,
            operator=operator,
            passed=passed,
            relative_position=position,
            impact=impact,
            weight=weight,
            timestamp=timestamp,
            price=price,
            metadata=metadata or {},
        )
    except (TypeError, ValueError, ArithmeticError) as exc:
        return unavailable_condition(
            condition_id=condition_id,
            name=name,
            actual=actual,
            reference=reference,
            operator=operator,
            timestamp=timestamp,
            price=price,
            impact=impact,
            weight=weight,
            reason=str(exc),
            metadata=metadata,
        )


def boolean_condition(
    *,
    condition_id: str,
    name: str,
    passed: bool | None,
    actual: Any,
    reference: Any,
    operator: str = "custom",
    relative_position: str = "unavailable",
    timestamp: str | None = None,
    price: float | None = None,
    impact: str = "informational",
    weight: float = 0.0,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ConditionEvaluation:
    status = "evaluated" if passed is not None else "unavailable"
    return ConditionEvaluation(
        condition_id=condition_id,
        name=name,
        actual_value=actual,
        reference_value=reference,
        operator=operator,
        passed=passed,
        relative_position=relative_position,
        impact=impact,
        weight=weight,
        timestamp=timestamp,
        price=price,
        status=status,
        reason=reason,
        metadata=metadata or {},
    )


def unavailable_condition(
    *,
    condition_id: str,
    name: str,
    actual: Any = None,
    reference: Any = None,
    operator: str = "custom",
    timestamp: str | None = None,
    price: float | None = None,
    impact: str = "informational",
    weight: float = 0.0,
    reason: str = "required input is unavailable",
    metadata: Mapping[str, Any] | None = None,
) -> ConditionEvaluation:
    return ConditionEvaluation(
        condition_id=condition_id,
        name=name,
        actual_value=actual,
        reference_value=reference,
        operator=operator,
        passed=None,
        relative_position="unavailable",
        impact=impact,
        weight=weight,
        timestamp=timestamp,
        price=price,
        status="unavailable",
        reason=reason,
        metadata=metadata or {},
    )


def _finite_number(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("value is non-finite")
    return number


def _compare(actual: Any, operator: str, reference: Any) -> bool:
    if operator in {">", ">=", "<", "<=", "==", "!="}:
        left = _finite_number(actual)
        right = _finite_number(reference)
        if operator == ">":
            return left > right
        if operator == ">=":
            return left >= right
        if operator == "<":
            return left < right
        if operator == "<=":
            return left <= right
        if operator == "==":
            return left == right
        return left != right
    if operator in {"between", "outside"}:
        low, high = reference
        value = _finite_number(actual)
        lower, upper = _finite_number(low), _finite_number(high)
        inside = lower <= value <= upper
        return inside if operator == "between" else not inside
    raise ValueError(f"unsupported comparison operator: {operator}")


def _relative_position(actual: Any, operator: str, reference: Any) -> str:
    if operator in {"between", "outside"}:
        low, high = reference
        value = _finite_number(actual)
        lower, upper = _finite_number(low), _finite_number(high)
        if value < lower:
            return "outside_below"
        if value > upper:
            return "outside_above"
        return "inside"
    left, right = _finite_number(actual), _finite_number(reference)
    if left > right:
        return "above"
    if left < right:
        return "below"
    return "equal"


__all__ = ["boolean_condition", "evaluate_condition", "unavailable_condition"]
