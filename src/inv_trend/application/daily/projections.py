"""Compatibility projections from immutable daily-stage evidence.

These helpers contain no feature calculation, market-data access, SQLite work,
or HTML rendering.  They turn already decided D1 evidence into the legacy row
shape required by run accounting and the report JSON contract.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Iterable, Mapping

from ..daily_models import BreakoutAssessment, DataUpdateResult, MarketAssessment


def attach_stage_results(
    row: dict[str, Any],
    data_update: Mapping[str, Any],
    strategy_screening: Mapping[str, Any],
    trend_decision: Mapping[str, Any],
    event_decisions: Iterable[Mapping[str, Any]],
) -> None:
    """Attach full immutable stage payloads to a compatibility row."""

    row["data_update_result"] = dict(data_update)
    row["strategy_screening_result"] = dict(strategy_screening)
    row["trend_decision_result"] = dict(trend_decision)
    row["event_decisions"] = [dict(item) for item in event_decisions]
    row["stages"] = {
        "data_update": row["data_update_result"],
        "strategy_screening": row["strategy_screening_result"],
        "trend_decision": row["trend_decision_result"],
    }


def data_gate_status(
    result: DataUpdateResult,
    update: Mapping[str, Any],
    *,
    research_mode: bool,
) -> str:
    """Preserve the public row statuses while respecting formal readiness."""

    freshness = result.freshness.get("status")
    if freshness == "STALE_DATA":
        return "stale"
    if not result.formal_ready:
        readable = "no_readable_completed_d1" not in result.blocking_reasons
        if research_mode and readable:
            return str(update.get("status", "unchanged"))
        if update.get("status") == "failed" and not readable:
            return "blocked" if update.get("error_type") == "ProviderIdentityError" else "failed"
        return "blocked"
    if update.get("status") == "failed":
        return "blocked" if update.get("error_type") == "ProviderIdentityError" else "failed"
    return str(update.get("status", "unchanged"))


def market_assessment_from_decision(decision: Any) -> MarketAssessment:
    """Create a display projection without recalculating trend evidence."""

    direction = str(decision.trend_direction)
    trend = {"LONG": "上升趋势", "SHORT": "下降趋势"}.get(direction, "趋势不明确")
    entry = _entry_direction(str(decision.execution_state))
    confidence = decision.confidence
    evidence = (
        decision.long_evidence
        if direction == "LONG"
        else decision.short_evidence
        if direction == "SHORT"
        else decision.reverse_evidence
    )
    return MarketAssessment(
        as_of=decision.as_of,
        market_status=(
            "观察性结果"
            if decision.observation_only
            else "已使用结构化三阶段结果"
        ),
        trend=trend,
        trend_basis=tuple(evidence),
        entry_direction=entry,
        entry_reason=decision.conclusion,
        rating_grade=confidence.get("rating_grade"),
        rating_score=confidence.get("rating_score"),
    )


def decision_backed_assessment(
    assessment: BreakoutAssessment,
    decision: Any | None,
) -> BreakoutAssessment:
    """Project an event-day decision into an already-created assessment."""

    if decision is None:
        return assessment
    trend = {"LONG": "上升趋势", "SHORT": "下降趋势"}.get(
        str(decision.trend_direction), "趋势不明确"
    )
    entry = _entry_direction(str(decision.execution_state))
    confidence = decision.confidence
    strength = f"{confidence.get('rating_grade') or '无'} 级 / {confidence.get('rating_score', '不可用')} 分"
    direction_evidence = (
        decision.long_evidence
        if decision.trend_direction == "LONG"
        else decision.short_evidence
        if decision.trend_direction == "SHORT"
        else ()
    )
    return replace(
        assessment,
        assessment_id=decision.result_id,
        trend=trend,
        trend_basis=tuple(direction_evidence),
        entry_direction=entry,
        signal_strength=strength,
        triggered_conditions=tuple(direction_evidence),
        unmet_conditions=tuple(decision.reverse_evidence) + tuple(decision.risk_blocks),
        conclusion=decision.conclusion,
    )


def _entry_direction(execution_state: str) -> str:
    return {
        "ENTER_LONG": "做多",
        "ENTER_SHORT": "做空",
        "ENTRY_CANDIDATE_LONG": "做多候选",
        "ENTRY_CANDIDATE_SHORT": "做空候选",
    }.get(execution_state, "不入场")


def summary(
    rows: list[Mapping[str, Any]],
    signals_new: int,
    delivery: Mapping[str, int],
) -> dict[str, int]:
    """Build the historical aggregate run summary from row payloads."""

    statuses = [str(row.get("run_status", "failed")) for row in rows]
    return {
        "selected": len(rows),
        "updated": statuses.count("updated"),
        "unchanged": statuses.count("unchanged"),
        "blocked": statuses.count("blocked"),
        "stale": statuses.count("stale"),
        "failed": statuses.count("failed"),
        "scanned": sum(
            1
            for row in rows
            if isinstance(row.get("scan", {}).get("status"), Mapping)
        ),
        "signals_detected": sum(int(row.get("signals_detected", 0)) for row in rows),
        "signals_new": signals_new,
        "signals_duplicate": sum(int(row.get("signals_duplicate", 0)) for row in rows),
        "signals_notified": int(delivery.get("notified", 0)),
        "delivery_errors": int(delivery.get("errors", 0)),
        "recovered_deliveries": int(delivery.get("recovered", 0)),
        "error_count": sum(
            1 for row in rows if row.get("run_status") in {"failed", "blocked", "stale"}
        ),
    }


__all__ = [
    "attach_stage_results",
    "data_gate_status",
    "decision_backed_assessment",
    "market_assessment_from_decision",
    "summary",
]
