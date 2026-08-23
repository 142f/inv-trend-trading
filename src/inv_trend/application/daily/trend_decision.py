"""D1 trend-decision stage that consumes screening evidence only."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import math
from typing import Any, Mapping

from inv_trend.core.signals import SignalEvent
from inv_trend.core.strategy.daily import build_daily_signal_events

from ..daily_models import (
    BreakoutAssessment,
    DataUpdateResult,
    StrategyScreeningResult,
    TrendDecisionResult,
)
from ..strategy_config import TrendDecisionConfig
from .projections import decision_backed_assessment, market_assessment_from_decision
from .stage_payloads import json_safe, rows


class TrendDecisionService:
    """Derive a compatibility decision from screening evidence only.

    The service intentionally accepts results/mappings rather than a price
    frame.  Introducing a K-line or indicator dependency here would break the
    stage boundary and invalidate the screening evidence contract.
    """

    def __init__(self, config: TrendDecisionConfig | None = None) -> None:
        self.config = config or TrendDecisionConfig()

    def decide(
        self,
        data: DataUpdateResult,
        screening: StrategyScreeningResult,
        *,
        event_snapshot: Mapping[str, Any] | None = None,
    ) -> TrendDecisionResult:
        evidence = event_snapshot or {
            "as_of": screening.as_of,
            "strategy_checks": screening.strategy_checks,
            "turtle_breakouts": screening.turtle_breakouts,
            "eligibility": screening.eligibility,
        }
        checks = _mapping(evidence.get("strategy_checks"))
        rating = _mapping(checks.get("rating"))
        grade = str(rating.get("grade") or "NONE")
        raw_direction = str(rating.get("direction") or "")
        family_votes = _mapping(rating.get("family_votes"))
        family_conflict = raw_direction == "conflict" or any(
            value == "conflict" for value in family_votes.values()
        )
        has_direction = raw_direction in {"long", "short"} and not family_conflict
        trend_direction = (
            raw_direction.upper() if has_direction and grade in {"A", "B"} else "NEUTRAL"
        )
        candidates = tuple(_mapping(item) for item in evidence.get("turtle_breakouts", ()))
        same_direction = tuple(
            item for item in candidates if str(item.get("direction")) == raw_direction
        )
        eligibility = _mapping(evidence.get("eligibility"))
        risk_blocks = _eligibility_blocks(eligibility, raw_direction)
        long_evidence, short_evidence, reverse_evidence = _directional_evidence(
            checks, candidates, raw_direction
        )
        confidence = _confidence_evidence(rating, checks)

        observation_only = bool(data.observation_only or screening.observation_only)
        if observation_only:
            decision, execution_state = "WAIT", "WAIT"
            conclusion = "研究模式仅生成观察性结论，不可执行、不推进 cursor，也不会进入通知队列。"
        elif not data.formal_ready:
            decision, execution_state = "WAIT", "WAIT"
            conclusion = "数据就绪闸门未通过，停止正式策略筛选与信号持久化，等待数据恢复。"
            trend_direction = "NEUTRAL"
        elif screening.screening_status != "READY":
            decision, execution_state = "WAIT", "WAIT"
            conclusion = "策略筛选结果不可用，等待完整 D1 筛选证据。"
            trend_direction = "NEUTRAL"
        elif not has_direction or grade in {"C", "CONFLICT", "NONE"}:
            decision, execution_state = "NEUTRAL", "NOT_APPLICABLE"
            conclusion = "策略评级没有形成可执行的同向趋势，维持中性观察。"
            trend_direction = "NEUTRAL"
        elif grade == "B":
            decision, execution_state = "WAIT", "WAIT"
            conclusion = "已形成 B 级同向趋势，但 B 级仅用于等待确认，不形成执行信号。"
        elif not same_direction:
            decision, execution_state = "WAIT", "WAIT"
            conclusion = "已形成 A 级同向趋势，但当日没有同向海龟价格突破，继续等待。"
        elif risk_blocks:
            decision, execution_state = "WAIT", "WAIT"
            conclusion = "同向 A 级海龟突破已出现，但资格校验存在阻断，暂不执行。"
        else:
            decision = trend_direction
            execution_state = "ENTER_LONG" if trend_direction == "LONG" else "ENTER_SHORT"
            side = "多头" if trend_direction == "LONG" else "空头"
            conclusion = f"同向 A 级{side}评级与当日海龟价格突破一致，可形成 {execution_state}。"

        return TrendDecisionResult(
            symbol=screening.symbol,
            instrument_id=screening.instrument_id,
            timeframe=screening.timeframe,
            input_data_hash=data.result_hash,
            input_screening_hash=screening.result_hash,
            as_of=str(evidence.get("as_of") or screening.as_of or "") or None,
            trend_direction=trend_direction,
            execution_state=execution_state,
            decision=decision,
            confidence=confidence,
            long_evidence=long_evidence,
            short_evidence=short_evidence,
            reverse_evidence=reverse_evidence,
            risk_blocks=risk_blocks,
            conclusion=conclusion,
            observation_only=observation_only,
        )

    run = decide


class TrendDecisionProjectionBuilder:
    """Build the run-bound commit/report plan from immutable stage evidence.

    ``TrendDecisionService`` remains pure and produces only a business
    decision.  This companion builder runs in the decision stage after that
    decision exists.  It consumes the screening result's hash-bound replay
    evidence, never K-lines or a data/state adapter, and records every
    operation-specific value (including ``detected_at``) in a separately
    hash-checked projection.  Commit can therefore only deserialize and
    persist this plan.
    """

    @staticmethod
    def build(
        *,
        data: DataUpdateResult,
        screening: StrategyScreeningResult,
        decision: TrendDecisionResult,
        data_payload: Mapping[str, Any],
        screening_payload: Mapping[str, Any],
        context: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        runtime = _mapping(screening.commit_evidence)
        positions = [int(value) for value in runtime.get("positions", ())]
        analyses = _mapping(runtime.get("replay_analyses"))
        strategy_version = str(
            _mapping(context.get("configuration")).get("strategy_version", "corrected-v2")
        )
        detected_at = str(context.get("started_at") or "")
        if not detected_at:
            raise ValueError("staging context has no started_at for decision projection")
        market = str(metadata.get("market") or "")
        refresh_failed = bool(_mapping(data.update).get("refresh_failed", False))

        events_by_position: dict[int, list[SignalEvent]] = {}
        detected: list[SignalEvent] = []
        for position in positions:
            analysis = _mapping(analyses.get(str(position)))
            if not analysis:
                events_by_position[position] = []
                continue
            events = build_daily_signal_events(
                analysis,
                instrument_id=data.instrument_id,
                symbol=data.symbol,
                market=market,
                dataset_version=data.dataset_version or "unknown",
                detected_at=detected_at,
                refresh_failed=refresh_failed,
                strategy_version=strategy_version,
            )
            events_by_position[position] = events
            detected.extend(events)

        event_decisions = [dict(_mapping(item)) for item in decision.event_decisions]
        decisions_by_position = {
            int(item.get("position", -1)): item for item in event_decisions
        }
        assessments: list[BreakoutAssessment] = []
        base_assessments = _mapping(runtime.get("base_breakout_assessments"))
        for position in positions:
            signal_ids = {
                (event.indicator_name, event.signal_time, event.direction.lower()): event.signal_id
                for event in events_by_position.get(position, ())
            }
            event_decision = decisions_by_position.get(position)
            decision_result = _decision_from_event_payload(event_decision)
            for raw_assessment in rows(base_assessments.get(str(position))):
                assessment = _breakout_assessment(raw_assessment)
                signal_id = signal_ids.get(_assessment_signal_key(assessment))
                if signal_id:
                    assessment = replace(
                        assessment, signal_id=signal_id, assessment_id=signal_id
                    )
                assessments.append(
                    decision_backed_assessment(assessment, decision_result)
                )

        decision_payload = decision.to_dict()
        report_bundle = _report_bundle_projection(
            _mapping(runtime.get("report_bundle_seed")),
            data_update=data_payload,
            screening=screening_payload,
            decision=decision_payload,
            event_decisions=event_decisions,
            signals=[event.to_dict() for event in detected],
            assessments=assessments,
        )
        research_mode = bool(_mapping(context.get("configuration")).get("research_mode", False))
        formal_eligible = (
            data.formal_ready
            and not research_mode
            and not data.observation_only
            and not screening.observation_only
            and not decision.observation_only
        )
        notification_signal_ids = sorted(
            event.signal_id
            for event in detected
            if event.signal_type.startswith("STRATEGY_GRADE_A_")
        )
        return json_safe(
            {
                "schema_version": "1",
                "operational_context_hash": str(
                    context.get("operational_context_hash") or ""
                ),
                "input_screening_commit_evidence_hash": screening.commit_evidence_hash,
                "decision_result_hash": decision.result_hash,
                "formal_signal_events": [event.to_dict() for event in detected],
                "notification_signal_ids": notification_signal_ids,
                "persistence": {
                    "instrument_id": data.instrument_id,
                    "timeframe": "D1",
                    "strategy_version": strategy_version,
                    "last_signal_time": str(
                        runtime.get("latest_time") or data.latest_complete_d1 or ""
                    ),
                    "session_anchor": str(runtime.get("session_anchor") or ""),
                    "enqueue_notifications": not bool(
                        _mapping(context.get("configuration")).get(
                            "backfill_signals", False
                        )
                    ),
                    "formal_eligible": formal_eligible,
                    "cursor_before": runtime.get("cursor_before"),
                    "cursor_after": (
                        str(runtime.get("latest_time") or data.latest_complete_d1 or "")
                        if formal_eligible
                        else runtime.get("cursor_before")
                    ),
                    "bars_replayed": len(positions),
                },
                "row_projection": {
                    "scan": json_safe(_mapping(runtime.get("analysis"))),
                    "chart": json_safe(rows(_mapping(runtime.get("report_bundle_seed")).get("series"))),
                    "signals": [event.to_dict() for event in detected],
                    "breakout_assessments": [item.to_dict() for item in assessments],
                    "report_bundle": report_bundle,
                },
            }
        )


def _directional_evidence(
    checks: Mapping[str, Any],
    candidates: tuple[Mapping[str, Any], ...],
    direction: str,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    long_items: list[str] = []
    short_items: list[str] = []
    reverse: list[str] = []
    labels = {
        "sma_alignment": "SMA 排列",
        "ema_trend": "EMA 趋势",
        "macd_summary": "MACD 多周期",
        "trend_quality": "ADX/DMI",
    }
    for key, label in labels.items():
        item = _mapping(checks.get(key))
        state = item.get("direction")
        if state == "long":
            long_items.append(f"{label}为多头")
        elif state == "short":
            short_items.append(f"{label}为空头")
        elif state == "conflict":
            reverse.append(f"{label}方向冲突")
        elif state is None:
            reverse.append(f"{label}不可用")
    for candidate in candidates:
        item = (
            f"海龟 {candidate.get('period')} 日"
            f"{'向上' if candidate.get('direction') == 'long' else '向下'}价格突破"
        )
        if candidate.get("direction") == "long":
            long_items.append(item)
        else:
            short_items.append(item)
    if direction == "long" and short_items:
        reverse.extend(short_items)
    if direction == "short" and long_items:
        reverse.extend(long_items)
    return tuple(long_items), tuple(short_items), tuple(dict.fromkeys(reverse))


def _confidence_evidence(rating: Mapping[str, Any], checks: Mapping[str, Any]) -> dict[str, Any]:
    quality = {
        "adx_dmi": _mapping(checks.get("trend_quality")),
        "atr": _mapping(checks.get("volatility")),
        "relative_volume": _mapping(checks.get("volume")),
    }
    return {
        "rating_grade": rating.get("grade"),
        "rating_score": rating.get("score"),
        "rating_direction": rating.get("direction"),
        "family_votes": _mapping(rating.get("family_votes")),
        "aligned_families": list(rating.get("aligned_families") or []),
        "quality_adjustments": list(rating.get("quality_adjustments") or []),
        "quality_evidence": quality,
        "note": "置信度沿用既有评级等级、评分、指标族对齐及质量调整项；未新增百分比算法。",
    }


def _eligibility_blocks(eligibility: Mapping[str, Any], direction: str) -> tuple[str, ...]:
    if eligibility.get("status") != "BLOCKED":
        return ()
    blocks: list[str] = []
    for item in eligibility.get("results", []):
        result = _mapping(item)
        if result.get("direction") != direction:
            continue
        for block in result.get("hard_blocks", []):
            blocks.append(str(block))
        if not result.get("trade_eligible") and not result.get("hard_blocks"):
            blocks.append(str(result.get("status") or "eligibility_not_passed"))
    return tuple(dict.fromkeys(blocks or ["eligibility_blocked"]))


def _decision_from_event_payload(payload: Mapping[str, Any] | None) -> TrendDecisionResult | None:
    """Restore the event-day decision projection without touching indicators."""

    if not payload:
        return None
    return TrendDecisionResult(
        symbol=str(payload.get("symbol") or ""),
        instrument_id=str(payload.get("instrument_id") or ""),
        timeframe=str(payload.get("timeframe") or "D1"),
        input_data_hash=str(payload.get("input_data_hash") or ""),
        input_screening_hash=str(payload.get("input_screening_hash") or ""),
        as_of=str(payload.get("as_of") or "") or None,
        trend_direction=str(payload.get("trend_direction") or "NEUTRAL"),
        execution_state=str(payload.get("execution_state") or "WAIT"),
        decision=str(payload.get("decision") or "WAIT"),
        confidence=_mapping(payload.get("confidence")),
        long_evidence=tuple(str(item) for item in payload.get("long_evidence", ())),
        short_evidence=tuple(str(item) for item in payload.get("short_evidence", ())),
        reverse_evidence=tuple(str(item) for item in payload.get("reverse_evidence", ())),
        risk_blocks=tuple(str(item) for item in payload.get("risk_blocks", ())),
        event_decisions=tuple(rows(payload.get("event_decisions"))),
        conclusion=str(payload.get("conclusion") or ""),
        observation_only=bool(payload.get("observation_only", False)),
        schema_version=str(payload.get("schema_version") or "1"),
    )


def _report_bundle_projection(
    seed: Mapping[str, Any],
    *,
    data_update: Mapping[str, Any],
    screening: Mapping[str, Any],
    decision: Mapping[str, Any],
    event_decisions: list[Mapping[str, Any]],
    signals: list[Mapping[str, Any]],
    assessments: list[BreakoutAssessment],
) -> dict[str, Any]:
    """Finish a legacy report bundle from staged values only.

    It intentionally does not calculate an indicator or invoke a report
    renderer.  The nested decision is the business payload before this
    operational projection is attached, preventing a self-referential JSON
    structure while preserving old report readers' fields.
    """

    result = json_safe(seed)
    if not isinstance(result, dict):
        result = {}
    result["data_update_result"] = json_safe(data_update)
    result["strategy_screening_result"] = json_safe(screening)
    result["trend_decision_result"] = json_safe(decision)
    result["event_decisions"] = json_safe(event_decisions)
    result["signals"] = json_safe(signals)
    result["breakout_assessments"] = [item.to_dict() for item in assessments]
    result["market_assessment"] = market_assessment_from_decision(
        _decision_from_event_payload(decision) or TrendDecisionResult(
            symbol="",
            instrument_id="",
            timeframe="D1",
            input_data_hash="",
            input_screening_hash="",
            as_of=None,
            trend_direction="NEUTRAL",
            execution_state="NOT_APPLICABLE",
            decision="NEUTRAL",
        )
    ).to_dict()
    result_summary = dict(_mapping(result.get("summary")))
    result_summary["signals"] = len(signals)
    result_summary["breakout_assessments"] = len(assessments)
    result["summary"] = result_summary
    result.pop("result_id", None)
    result.pop("result_hash", None)
    digest = hashlib.sha256(
        json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    result["result_id"] = digest[:24]
    result["result_hash"] = digest
    return result


def _breakout_assessment(payload: Mapping[str, Any]) -> BreakoutAssessment:
    return BreakoutAssessment(
        assessment_id=str(payload["assessment_id"]),
        signal_id=_optional_text(payload.get("signal_id")),
        timestamp=str(payload["timestamp"]),
        timeframe=str(payload.get("timeframe", "D1")),
        current_price=_float_or_none(payload.get("current_price")),
        breakout_type=str(payload.get("breakout_type", "")),
        breakout_object=str(payload.get("breakout_object", "")),
        breakout_level=_float_or_none(payload.get("breakout_level")),
        previous_state=str(payload.get("previous_state", "")),
        post_state=str(payload.get("post_state", "")),
        trend=str(payload.get("trend", "")),
        trend_basis=tuple(str(item) for item in payload.get("trend_basis", ())),
        entry_direction=str(payload.get("entry_direction", "")),
        signal_strength=str(payload.get("signal_strength", "")),
        triggered_conditions=tuple(
            str(item) for item in payload.get("triggered_conditions", ())
        ),
        unmet_conditions=tuple(str(item) for item in payload.get("unmet_conditions", ())),
        quality_notes=tuple(str(item) for item in payload.get("quality_notes", ())),
        conclusion=str(payload.get("conclusion", "")),
    )


def _assessment_signal_key(assessment: BreakoutAssessment) -> tuple[str, str, str]:
    indicator = "turtle_20" if "20" in assessment.breakout_object else "turtle_55"
    text = assessment.breakout_type.lower()
    direction = "long" if "up" in text or "\u5411\u4e0a" in assessment.breakout_type else "short"
    return indicator, assessment.timestamp, direction


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _float_or_none(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


__all__ = [
    "TrendDecisionProjectionBuilder",
    "TrendDecisionResult",
    "TrendDecisionService",
]
