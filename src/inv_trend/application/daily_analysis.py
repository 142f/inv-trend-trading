"""Application compatibility projections for pure D1 analysis.

Feature preparation, indicator evaluation, rules, anomalies, and transitions
live in inv_trend.core.strategy.daily.analysis. This module retains the
application-level report projection functions and the established import path
for downstream callers.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Iterable, Mapping

import pandas as pd

from inv_trend.core.strategy.daily.analysis import (
    PreparedDailyAnalysis,
    analyze_prepared_daily_analysis,
    build_rule_evaluations,
    detect_anomalies,
    prepare_daily_analysis,
    state_transitions,
)

from .daily_models import (
    BreakoutAssessment,
    DEFAULT_CHANGE_LOG,
    InstrumentReportBundle,
    MarketAssessment,
)


def build_breakout_assessments(
    prepared: PreparedDailyAnalysis,
    analysis: Mapping[str, Any],
    *,
    position: int = -1,
    signal_ids: Mapping[tuple[str, str, str], str] | None = None,
) -> tuple[BreakoutAssessment, ...]:
    """Project Turtle signals into report rows without changing signal semantics.

    ``analysis`` must be the result for ``position``.  The function only reads
    already prepared features and strategy-check outputs, so replay rows never
    use a later bar's trend, rating, or price.
    """

    if prepared.base.empty:
        return ()
    stop = _stop_position(prepared.base, position)
    timestamp = prepared.base.index[stop - 1].isoformat()
    checks = analysis.get("strategy_checks", {})
    indicators = analysis.get("indicators", {})
    rating = checks.get("rating", {}) if isinstance(checks, Mapping) else {}
    results: list[BreakoutAssessment] = []

    for raw in analysis.get("signals", []):
        if not isinstance(raw, Mapping):
            continue
        indicator = str(raw.get("indicator", ""))
        if indicator not in {"turtle_20", "turtle_55"}:
            continue
        direction = str(raw.get("direction", ""))
        if direction not in {"long", "short"}:
            continue
        period = int(indicator.removeprefix("turtle_"))
        event_time = str(raw.get("signal_time") or timestamp)
        breakout_level = _number(raw.get("breakout_level"))
        if breakout_level is None:
            result = indicators.get(indicator, {}) if isinstance(indicators, Mapping) else {}
            breakout_level = _number(result.get("breakout_level")) if isinstance(result, Mapping) else None
        previous_state = _channel_state(prepared.base, period, stop - 2)
        post_state = _channel_state(prepared.base, period, stop - 1)
        outside_state = "位于上轨上方" if direction == "long" else "位于下轨下方"
        if previous_state == outside_state:
            post_state = f"{post_state}（突破持续）"
        breakout_type = "向上突破" if direction == "long" else "向下突破"
        boundary = "上轨" if direction == "long" else "下轨"
        breakout_object = f"海龟 {period} 日唐奇安{boundary}"
        trend, trend_basis = _trend_projection(checks, rating, direction, post_state, breakout_object)
        entry_direction, conclusion = _entry_projection(rating, direction, breakout_type, trend)
        triggered, unmet, quality_notes = _assessment_conditions(checks, rating, direction, period)
        signal_id = (signal_ids or {}).get((indicator, event_time, direction))
        assessment_id = signal_id or _stable_id(
            "breakout_assessment", indicator, event_time, direction
        )
        results.append(
            BreakoutAssessment(
                assessment_id=assessment_id,
                signal_id=signal_id,
                timestamp=event_time,
                timeframe=str(raw.get("timeframe") or "D1"),
                current_price=_number(analysis.get("latest_bar", {}).get("close")),
                breakout_type=breakout_type,
                breakout_object=breakout_object,
                breakout_level=breakout_level,
                previous_state=previous_state,
                post_state=post_state,
                trend=trend,
                trend_basis=trend_basis,
                entry_direction=entry_direction,
                signal_strength=_signal_strength(rating),
                triggered_conditions=triggered,
                unmet_conditions=unmet,
                quality_notes=quality_notes,
                conclusion=conclusion,
            )
        )
    return tuple(results)


def build_market_assessment(
    prepared: PreparedDailyAnalysis,
    analysis: Mapping[str, Any],
) -> MarketAssessment:
    """Return a latest-bar report conclusion without emitting an order or event."""

    latest = analysis.get("latest_bar", {})
    checks = analysis.get("strategy_checks", {})
    rating = checks.get("rating", {}) if isinstance(checks, Mapping) else {}
    if prepared.base.empty or not isinstance(latest, Mapping):
        return MarketAssessment(
            as_of=None,
            market_status="数据不足",
            trend="趋势不明确",
            entry_reason="没有可用于趋势与入场判断的完整 D1 K 线。",
        )

    current_breakouts = build_breakout_assessments(prepared, analysis)
    direction = str(rating.get("direction") or "")
    trend, basis = _trend_projection(
        checks,
        rating,
        direction if direction in {"long", "short"} else None,
        "最新完整 D1 收盘状态",
        "当前市场",
    )
    if current_breakouts:
        selected = next(
            (item for item in current_breakouts if item.entry_direction != "不入场"),
            current_breakouts[0],
        )
        entry_direction = selected.entry_direction
        entry_reason = selected.conclusion
    else:
        entry_direction = "不入场"
        entry_reason = "最新完整 D1 K 线没有触发海龟价格突破，因此不形成入场判断。"
    state = analysis.get("status", {})
    market_status = (
        "已使用最新完整 D1 K 线"
        if isinstance(state, Mapping) and state.get("state") == "ready"
        else "指标预热中或部分数据不可用"
    )
    return MarketAssessment(
        as_of=str(latest.get("timestamp") or "") or None,
        market_status=market_status,
        trend=trend,
        trend_basis=basis,
        entry_direction=entry_direction,
        entry_reason=entry_reason,
        rating_grade=str(rating.get("grade") or "") or None,
        rating_score=_number(rating.get("score")),
    )


def _channel_state(frame: pd.DataFrame, period: int, position: int) -> str:
    if position < 0 or position >= len(frame):
        return "不可用（缺少前一完整 K 线）"
    row = frame.iloc[position]
    close = _number(row.get("close"))
    high = _number(row.get(f"channel_high_{period}"))
    low = _number(row.get(f"channel_low_{period}"))
    if None in (close, high, low):
        return "不可用（通道尚未预热）"
    if float(close) > float(high):
        return "位于上轨上方"
    if float(close) < float(low):
        return "位于下轨下方"
    return "位于通道区间内"


def _trend_projection(
    checks: Mapping[str, Any],
    rating: Mapping[str, Any],
    breakout_direction: str | None,
    price_state: str,
    price_object: str,
) -> tuple[str, tuple[str, ...]]:
    grade = str(rating.get("grade") or "NONE")
    rating_direction = str(rating.get("direction") or "")
    if grade == "CONFLICT" or rating_direction == "conflict":
        trend = "震荡"
    elif rating_direction in {"long", "short"} and grade in {"A", "B"}:
        if breakout_direction is None or rating_direction == breakout_direction:
            trend = "上升趋势" if rating_direction == "long" else "下降趋势"
        else:
            trend = "震荡"
    else:
        trend = "趋势不明确"

    sma = checks.get("sma_alignment", {}) if isinstance(checks, Mapping) else {}
    ema = checks.get("ema_trend", {}) if isinstance(checks, Mapping) else {}
    macd = checks.get("macd_summary", {}) if isinstance(checks, Mapping) else {}
    quality = checks.get("trend_quality", {}) if isinstance(checks, Mapping) else {}
    return trend, (
        f"价格位置：{price_state}（{price_object}）",
        f"SMA 排列：{_direction_text(_mapping_value(sma, 'direction'))}",
        f"EMA 趋势：{_direction_text(_mapping_value(ema, 'direction'))}",
        f"MACD 多周期：{_direction_text(_mapping_value(macd, 'direction'))}",
        "ADX/DMI："
        f"{_direction_text(_mapping_value(quality, 'direction'))}"
        f"，{'趋势确认' if _mapping_value(quality, 'confirmed') else '趋势未确认'}",
        f"组合评级：{_rating_text(rating)}",
    )


def _assessment_conditions(
    checks: Mapping[str, Any],
    rating: Mapping[str, Any],
    direction: str,
    period: int,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    side = "多头" if direction == "long" else "空头"
    triggered = [f"海龟 {period} 日{side}价格突破"]
    unmet: list[str] = []
    for label, key in (("SMA 排列", "sma_alignment"), ("EMA 趋势", "ema_trend"), ("MACD 多周期", "macd_summary")):
        value = checks.get(key, {}) if isinstance(checks, Mapping) else {}
        state = _mapping_value(value, "direction")
        if state == direction:
            triggered.append(f"{label}与突破方向一致")
        elif state in {"long", "short", "conflict"}:
            unmet.append(f"{label}未与{side}突破一致（当前：{_direction_text(state)}）")
        else:
            unmet.append(f"{label}尚未形成同向确认")
    if rating.get("grade") == "A" and rating.get("direction") == direction:
        triggered.append(f"A 级共振与{side}突破同向")
    else:
        unmet.append(f"未达到与{side}突破同向的 A 级共振（当前：{_rating_text(rating)}）")

    quality_notes: list[str] = []
    quality = checks.get("trend_quality", {}) if isinstance(checks, Mapping) else {}
    volatility = checks.get("volatility", {}) if isinstance(checks, Mapping) else {}
    volume = checks.get("volume", {}) if isinstance(checks, Mapping) else {}
    if _mapping_value(quality, "confirmed") and _mapping_value(quality, "direction") == direction:
        triggered.append("ADX/DMI 趋势质量确认（加分项）")
    else:
        quality_notes.append("ADX/DMI 未形成同向趋势确认（非硬门槛）")
    if _mapping_value(volatility, "state") == "normal":
        triggered.append("ATR 波动处于正常区间（加分项）")
    else:
        quality_notes.append("ATR 波动不在正常区间（非硬门槛）")
    if _mapping_value(volume, "confirmed"):
        triggered.append("相对成交量确认（加分项）")
    else:
        quality_notes.append("相对成交量未确认（非硬门槛）")
    return tuple(triggered), tuple(unmet), tuple(quality_notes)


def _entry_projection(
    rating: Mapping[str, Any],
    direction: str,
    breakout_type: str,
    trend: str,
) -> tuple[str, str]:
    entry = "做多" if direction == "long" else "做空"
    if rating.get("grade") == "A" and rating.get("direction") == direction:
        return entry, f"{breakout_type}与 A 级{_direction_text(direction)}共振同向，展示性入场判断为{entry}。"
    if rating.get("grade") == "CONFLICT" or trend == "震荡":
        return "不入场", f"已发生{breakout_type}，但策略族方向冲突，当前判断为震荡，不入场。"
    return "不入场", f"已发生{breakout_type}，但未达到同向 A 级共振，当前不入场。"


def _signal_strength(rating: Mapping[str, Any]) -> str:
    grade = str(rating.get("grade") or "无")
    score = _number(rating.get("score"))
    return f"{grade}级 / {score:.1f} 分" if score is not None else f"{grade}级 / 评分不可用"


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else None


def _direction_text(value: Any) -> str:
    return {
        "long": "多头",
        "short": "空头",
        "conflict": "方向冲突",
        "none": "未形成",
        None: "不可用",
    }.get(value, str(value) if value else "不可用")


def _rating_text(rating: Mapping[str, Any]) -> str:
    grade = str(rating.get("grade") or "无")
    direction = _direction_text(rating.get("direction"))
    score = _number(rating.get("score"))
    return f"{grade}级 / {direction}" + (f" / {score:.1f} 分" if score is not None else "")


def build_instrument_report_bundle(
    prepared: PreparedDailyAnalysis,
    *,
    symbol: str,
    instrument_id: str,
    generated_at: str,
    analysis: Mapping[str, Any] | None = None,
    signals: Iterable[Mapping[str, Any]] | None = None,
    breakout_assessments: Iterable[BreakoutAssessment] | None = None,
    market_assessment: MarketAssessment | None = None,
    data_update_result: Mapping[str, Any] | None = None,
    strategy_screening_result: Mapping[str, Any] | None = None,
    trend_decision_result: Mapping[str, Any] | None = None,
    event_decisions: Iterable[Mapping[str, Any]] | None = None,
    chart_bars: int = 180,
) -> InstrumentReportBundle:
    if chart_bars < 1:
        raise ValueError("chart_bars must be positive")
    current = dict(analysis or analyze_prepared_daily_analysis(prepared))
    rules = tuple(
        build_rule_evaluations(prepared, current)
        if prepared.base.size
        else ()
    )
    assessments = tuple(breakout_assessments or ())
    series_start = max(0, len(prepared.base) - chart_bars)
    assessment_times = {item.timestamp for item in assessments}
    if assessment_times:
        for position, timestamp in enumerate(prepared.base.index):
            if timestamp.isoformat() in assessment_times:
                series_start = min(series_start, position)
    start_position = max(1, series_start)
    anomalies = detect_anomalies(prepared, start_position=start_position)
    transitions = state_transitions(prepared, start_position=start_position)
    series = tuple(_series_payload(prepared.base.iloc[series_start:]))
    signal_rows = tuple(dict(item) for item in (signals if signals is not None else current.get("signals", [])))
    explanation = current.get("explanation_summary") or {}
    summary = {
        "bars": len(prepared.base),
        "visible_bars": len(series),
        "signals": len(signal_rows),
        "rules": len(rules),
        "triggered_rules": sum(item.triggered for item in rules),
        "conditions": sum(len(item.conditions) for item in rules),
        "conditions_passed": int(explanation.get("conditions_passed", 0)),
        "conditions_failed": int(explanation.get("conditions_failed", 0)),
        "conditions_unavailable": int(explanation.get("conditions_unavailable", 0)),
        "anomalies": len(anomalies),
        "state_transitions": len(transitions),
        "breakout_assessments": len(assessments),
    }
    latest = current.get("latest_bar")
    return InstrumentReportBundle(
        symbol=symbol,
        instrument_id=instrument_id,
        timeframe="D1",
        dataset_version=prepared.dataset_version,
        generated_at=generated_at,
        latest_bar=latest if isinstance(latest, Mapping) else None,
        series=series,
        signals=signal_rows,
        rule_evaluations=rules,
        market_assessment=market_assessment or build_market_assessment(prepared, current),
        breakout_assessments=assessments,
        data_update_result=dict(data_update_result or {}),
        strategy_screening_result=dict(strategy_screening_result or {}),
        trend_decision_result=dict(trend_decision_result or {}),
        event_decisions=tuple(dict(item) for item in (event_decisions or ())),
        anomalies=anomalies,
        state_transitions=transitions,
        summary=summary,
        strategy_snapshot={
            "indicators": current.get("indicators", {}),
            "strategy_checks": current.get("strategy_checks", {}),
            "resonance": current.get("resonance", {}),
            "status": current.get("status", {}),
            "feature_request": repr(prepared.feature_request),
        },
        change_log=DEFAULT_CHANGE_LOG,
    )


def _series_payload(frame: pd.DataFrame) -> list[dict[str, Any]]:
    columns = (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "channel_high_20",
        "channel_low_20",
        "channel_high_55",
        "channel_low_55",
        "sma_5",
        "sma_10",
        "sma_20",
        "sma_55",
        "sma_120",
        "ema_144",
        "ema_169",
        "dif",
        "dea",
        "histogram",
        "adx",
        "plus_di",
        "minus_di",
        "atr",
        "atr_pct",
        "atr_percentile",
        "relative_volume",
    )
    rows: list[dict[str, Any]] = []
    for timestamp, values in frame.iterrows():
        row: dict[str, Any] = {"timestamp": timestamp.isoformat()}
        for column in columns:
            row[column] = _number(values.get(column))
        rows.append(row)
    return rows


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _stop_position(frame: pd.DataFrame, position: int) -> int:
    stop = position + 1 if position >= 0 else len(frame) + position + 1
    if stop < 1 or stop > len(frame):
        raise IndexError("daily analysis position is outside the prepared frame")
    return stop


def _stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:24]


__all__ = [
    "PreparedDailyAnalysis",
    "analyze_prepared_daily_analysis",
    "build_breakout_assessments",
    "build_instrument_report_bundle",
    "build_market_assessment",
    "build_rule_evaluations",
    "detect_anomalies",
    "prepare_daily_analysis",
    "state_transitions",
]
