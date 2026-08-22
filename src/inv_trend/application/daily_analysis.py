"""Single-pass D1 analysis with explainable rules and report-bundle output.

This module is the application-level calculation boundary.  It prepares the D1
feature superset once, reuses that frame for legacy signal semantics and the
parallel strategy checks, then emits a report model that renderers consume
without recalculating trading logic.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Any, Iterable, Mapping

import pandas as pd

from inv_trend.adapters.detector.indicators.daily_signals import (
    analyze_prepared_daily_signals,
    daily_signal_feature_request,
    normalize_completed_daily_bars,
)
from inv_trend.adapters.detector.indicators.daily_strategy_checks import (
    PreparedDailyStrategyChecks,
    analyze_prepared_strategy_checks,
    daily_strategy_feature_request,
    prepare_daily_strategy_checks,
)
from inv_trend.application.strategy_config import DailyChecksConfig
from inv_trend.core.explanations import AnomalyEvent, RuleEvaluation, StateTransition
from inv_trend.core.features import FeatureRequest, PreparedBars
from inv_trend.core.rules import evaluate_condition
from .daily_models import DEFAULT_CHANGE_LOG, InstrumentReportBundle


@dataclass(frozen=True)
class PreparedDailyAnalysis:
    """One completed-bar snapshot with every D1 feature prepared exactly once."""

    base: pd.DataFrame
    strategy: PreparedDailyStrategyChecks
    feature_request: FeatureRequest
    dataset_version: str


def prepare_daily_analysis(
    bars: pd.DataFrame,
    config: DailyChecksConfig,
    *,
    session_anchor: pd.Timestamp | str,
) -> PreparedDailyAnalysis:
    completed = normalize_completed_daily_bars(bars)
    dataset_version = str(
        bars.attrs.get("dataset_version")
        or completed.attrs.get("dataset_version")
        or _column_scalar(completed, "dataset_version")
        or "unknown"
    )
    if completed.empty:
        strategy = prepare_daily_strategy_checks(
            completed,
            config,
            session_anchor=session_anchor,
        )
        return PreparedDailyAnalysis(completed, strategy, FeatureRequest(), dataset_version)

    request = FeatureRequest.merge(
        daily_signal_feature_request(),
        daily_strategy_feature_request(config),
    )
    base = PreparedBars.build(completed, request).frame.copy()
    for period in config.sma_periods:
        base[f"sma_{period}"] = base[f"sma_{period}_lag_0"]
    # Legacy daily-signal names are guaranteed even if the report config changes later.
    for period in (10, 20):
        alias = f"sma_{period}"
        source = f"sma_{period}_lag_0"
        if alias not in base and source in base:
            base[alias] = base[source]

    strategy = prepare_daily_strategy_checks(
        completed,
        config,
        session_anchor=session_anchor,
        prepared_base=base,
    )
    # Strategy preparation appends report-only derived columns (ATR percentile,
    # relative volume).  Reuse that exact enriched frame everywhere afterwards.
    base = strategy.base
    return PreparedDailyAnalysis(base, strategy, request, dataset_version)


def analyze_prepared_daily_analysis(
    prepared: PreparedDailyAnalysis,
    *,
    position: int = -1,
) -> dict[str, Any]:
    legacy = analyze_prepared_daily_signals(prepared.base, position)
    checks = analyze_prepared_strategy_checks(prepared.strategy, position=position)
    legacy["strategy_checks"] = checks
    legacy["signals"] = [*legacy["signals"], *checks["signals"]]
    rules = build_rule_evaluations(prepared, legacy, position=position)
    legacy["rule_evaluations"] = [item.to_dict() for item in rules]
    legacy["explanation_summary"] = {
        "rules": len(rules),
        "triggered_rules": sum(item.triggered for item in rules),
        "conditions": sum(len(item.conditions) for item in rules),
        "conditions_passed": sum(
            condition.passed is True for item in rules for condition in item.conditions
        ),
        "conditions_failed": sum(
            condition.passed is False for item in rules for condition in item.conditions
        ),
        "conditions_unavailable": sum(
            condition.passed is None for item in rules for condition in item.conditions
        ),
    }
    return legacy


def build_rule_evaluations(
    prepared: PreparedDailyAnalysis,
    analysis: Mapping[str, Any],
    *,
    position: int = -1,
) -> tuple[RuleEvaluation, ...]:
    if prepared.base.empty:
        return ()
    stop = _stop_position(prepared.base, position)
    row = prepared.base.iloc[stop - 1]
    previous = prepared.base.iloc[stop - 2] if stop > 1 else None
    timestamp = prepared.base.index[stop - 1].isoformat()
    price = _number(row.get("close"))
    indicators = analysis.get("indicators", {})
    checks = analysis.get("strategy_checks", {})
    config = prepared.strategy.config
    rules: list[RuleEvaluation] = []

    for period in (20, 55):
        indicator = indicators.get(f"turtle_{period}", {})
        high = indicator.get("channel_high")
        low = indicator.get("channel_low")
        long_condition = evaluate_condition(
            condition_id=f"turtle_{period}.close_above_high",
            name=f"收盘价 > {period}日 Donchian 上轨",
            actual=price,
            operator=">",
            reference=high,
            timestamp=timestamp,
            price=price,
            impact="entry_direction",
            weight=2.0,
        )
        short_condition = evaluate_condition(
            condition_id=f"turtle_{period}.close_below_low",
            name=f"收盘价 < {period}日 Donchian 下轨",
            actual=price,
            operator="<",
            reference=low,
            timestamp=timestamp,
            price=price,
            impact="entry_direction",
            weight=2.0,
        )
        direction = indicator.get("direction")
        rules.append(
            RuleEvaluation(
                rule_id=f"turtle_{period}_breakout",
                name=f"Turtle {period}日突破",
                triggered=direction in {"long", "short"},
                match_policy="any",
                direction=direction if direction in {"long", "short"} else None,
                score_impact=2.0 if direction in {"long", "short"} else 0.0,
                timestamp=timestamp,
                price=price,
                outcome=indicator.get("event") or "not_triggered",
                conditions=(long_condition, short_condition),
            )
        )

    sma = indicators.get("sma_10_20", {})
    current_fast, current_slow = sma.get("sma_10"), sma.get("sma_20")
    previous_fast, previous_slow = sma.get("previous_sma_10"), sma.get("previous_sma_20")
    golden_conditions = (
        evaluate_condition(
            condition_id="sma_cross.current_fast_above_slow",
            name="当前 SMA10 > SMA20",
            actual=current_fast,
            operator=">",
            reference=current_slow,
            timestamp=timestamp,
            price=price,
            impact="cross_signal",
            weight=2.0,
        ),
        evaluate_condition(
            condition_id="sma_cross.previous_fast_not_above_slow",
            name="前一日 SMA10 <= SMA20",
            actual=previous_fast,
            operator="<=",
            reference=previous_slow,
            timestamp=timestamp,
            price=price,
            impact="cross_signal",
            weight=2.0,
        ),
    )
    death_conditions = (
        evaluate_condition(
            condition_id="sma_cross.current_fast_below_slow",
            name="当前 SMA10 < SMA20",
            actual=current_fast,
            operator="<",
            reference=current_slow,
            timestamp=timestamp,
            price=price,
            impact="cross_signal",
            weight=2.0,
        ),
        evaluate_condition(
            condition_id="sma_cross.previous_fast_not_below_slow",
            name="前一日 SMA10 >= SMA20",
            actual=previous_fast,
            operator=">=",
            reference=previous_slow,
            timestamp=timestamp,
            price=price,
            impact="cross_signal",
            weight=2.0,
        ),
    )
    rules.extend(
        (
            _rule_from_conditions(
                "sma_10_20_golden_cross",
                "SMA10/20 金叉",
                golden_conditions,
                timestamp,
                price,
                direction="long",
                outcome=sma.get("event"),
                score_impact=2.0,
            ),
            _rule_from_conditions(
                "sma_10_20_death_cross",
                "SMA10/20 死叉",
                death_conditions,
                timestamp,
                price,
                direction="short",
                outcome=sma.get("event"),
                score_impact=2.0,
            ),
        )
    )

    sma_alignment = checks.get("sma_alignment", {})
    values = sma_alignment.get("values", {}) if isinstance(sma_alignment, Mapping) else {}
    long_stack = []
    short_stack = []
    for left, right in zip(config.sma_periods, config.sma_periods[1:]):
        long_stack.append(
            evaluate_condition(
                condition_id=f"sma_stack.{left}_above_{right}",
                name=f"SMA{left} > SMA{right}",
                actual=values.get(str(left)),
                operator=">",
                reference=values.get(str(right)),
                timestamp=timestamp,
                price=price,
                impact="rating_family",
                weight=2.0 / max(1, len(config.sma_periods) - 1),
            )
        )
        short_stack.append(
            evaluate_condition(
                condition_id=f"sma_stack.{left}_below_{right}",
                name=f"SMA{left} < SMA{right}",
                actual=values.get(str(left)),
                operator="<",
                reference=values.get(str(right)),
                timestamp=timestamp,
                price=price,
                impact="rating_family",
                weight=2.0 / max(1, len(config.sma_periods) - 1),
            )
        )
    rules.extend(
        (
            _rule_from_conditions(
                "sma_alignment_long",
                "SMA 多头排列",
                tuple(long_stack),
                timestamp,
                price,
                direction="long",
                outcome=sma_alignment.get("direction"),
                score_impact=2.0,
            ),
            _rule_from_conditions(
                "sma_alignment_short",
                "SMA 空头排列",
                tuple(short_stack),
                timestamp,
                price,
                direction="short",
                outcome=sma_alignment.get("direction"),
                score_impact=2.0,
            ),
        )
    )

    ema = checks.get("ema_trend", {})
    ema_values = ema.get("values", {}) if isinstance(ema, Mapping) else {}
    ema_fast, ema_slow = config.ema_periods
    rules.extend(
        (
            _single_rule(
                "ema_trend_long",
                f"EMA{ema_fast} > EMA{ema_slow}",
                evaluate_condition(
                    condition_id="ema_trend.fast_above_slow",
                    name=f"EMA{ema_fast} > EMA{ema_slow}",
                    actual=ema_values.get(str(ema_fast)),
                    operator=">",
                    reference=ema_values.get(str(ema_slow)),
                    timestamp=timestamp,
                    price=price,
                    impact="rating_family",
                    weight=1.0,
                ),
                timestamp,
                price,
                "long",
                ema.get("direction"),
                1.0,
            ),
            _single_rule(
                "ema_trend_short",
                f"EMA{ema_fast} < EMA{ema_slow}",
                evaluate_condition(
                    condition_id="ema_trend.fast_below_slow",
                    name=f"EMA{ema_fast} < EMA{ema_slow}",
                    actual=ema_values.get(str(ema_fast)),
                    operator="<",
                    reference=ema_values.get(str(ema_slow)),
                    timestamp=timestamp,
                    price=price,
                    impact="rating_family",
                    weight=1.0,
                ),
                timestamp,
                price,
                "short",
                ema.get("direction"),
                1.0,
            ),
        )
    )

    for timeframe, values in (checks.get("macd") or {}).items():
        if not isinstance(values, Mapping):
            continue
        dif, dea = values.get("dif"), values.get("dea")
        for direction, operator in (("long", ">"), ("short", "<")):
            condition = evaluate_condition(
                condition_id=f"macd_{timeframe}.dif_{'above' if operator == '>' else 'below'}_dea",
                name=f"{timeframe} MACD DIF {operator} DEA",
                actual=dif,
                operator=operator,
                reference=dea,
                timestamp=str(values.get("bar_end") or timestamp),
                price=price,
                impact="rating_family",
                weight=3.0,
                metadata={"zero_axis": values.get("zero_axis")},
            )
            rules.append(
                _single_rule(
                    f"macd_{timeframe}_{direction}",
                    f"{timeframe} MACD {direction}",
                    condition,
                    str(values.get("bar_end") or timestamp),
                    price,
                    direction,
                    values.get("direction"),
                    3.0,
                )
            )

    trend = checks.get("trend_quality", {})
    adx_condition = evaluate_condition(
        condition_id="trend_quality.adx_threshold",
        name=f"ADX >= {config.adx_threshold:g}",
        actual=trend.get("adx"),
        operator=">=",
        reference=config.adx_threshold,
        timestamp=timestamp,
        price=price,
        impact="rating_adjustment",
        weight=1.0,
    )
    rules.append(
        _single_rule(
            "trend_quality_confirmed",
            "ADX 趋势强度确认",
            adx_condition,
            timestamp,
            price,
            trend.get("direction"),
            "confirmed" if trend.get("confirmed") else "not_confirmed",
            1.0,
        )
    )

    volatility = checks.get("volatility", {})
    atr_condition = evaluate_condition(
        condition_id="volatility.atr_percentile_normal",
        name="ATR 分位位于正常区间",
        actual=volatility.get("percentile"),
        operator="between",
        reference=(config.atr_normal_percentile_low, config.atr_normal_percentile_high),
        timestamp=timestamp,
        price=price,
        impact="rating_adjustment",
        weight=1.0,
    )
    rules.append(
        _single_rule(
            "atr_normal_range",
            "ATR 正常波动区间",
            atr_condition,
            timestamp,
            price,
            None,
            volatility.get("state"),
            1.0 if atr_condition.passed else -1.0 if atr_condition.passed is False else 0.0,
        )
    )

    volume = checks.get("volume", {})
    volume_condition = evaluate_condition(
        condition_id="volume.relative_confirmation",
        name=f"相对成交量 >= {config.volume_confirmation_ratio:g}",
        actual=volume.get("relative_volume"),
        operator=">=",
        reference=config.volume_confirmation_ratio,
        timestamp=timestamp,
        price=price,
        impact="rating_adjustment",
        weight=1.0,
    )
    rules.append(
        _single_rule(
            "relative_volume_confirmation",
            "相对成交量确认",
            volume_condition,
            timestamp,
            price,
            None,
            "confirmed" if volume.get("confirmed") else "not_confirmed",
            1.0,
        )
    )

    rating = checks.get("rating", {})
    aligned = rating.get("aligned_families") or []
    grade_a_conditions = (
        evaluate_condition(
            condition_id="rating_a.aligned_families",
            name=f"对齐指标族数量 >= {config.rating_a_min_families}",
            actual=len(aligned),
            operator=">=",
            reference=config.rating_a_min_families,
            timestamp=timestamp,
            price=price,
            impact="final_rating",
        ),
        evaluate_condition(
            condition_id="rating_a.score",
            name=f"综合评分 >= {config.rating_a_min_score:g}",
            actual=rating.get("score"),
            operator=">=",
            reference=config.rating_a_min_score,
            timestamp=timestamp,
            price=price,
            impact="final_rating",
        ),
    )
    rules.append(
        _rule_from_conditions(
            "strategy_rating_a",
            "策略 A 级判定",
            grade_a_conditions,
            timestamp,
            price,
            direction=rating.get("direction"),
            outcome=rating.get("grade"),
            score_impact=float(rating.get("score") or 0.0),
        )
    )
    return tuple(rules)


def detect_anomalies(
    prepared: PreparedDailyAnalysis,
    *,
    start_position: int = 1,
) -> tuple[AnomalyEvent, ...]:
    """Detect report-only price/volatility anomalies without changing signals."""

    frame = prepared.base
    if len(frame) < 2:
        return ()
    result: list[AnomalyEvent] = []
    start = max(1, start_position)
    for position in range(start, len(frame)):
        row, previous = frame.iloc[position], frame.iloc[position - 1]
        timestamp = frame.index[position].isoformat()
        price = _number(row.get("close"))
        atr = _number(row.get("atr"))
        if atr is not None and atr > 0:
            gap = abs(float(row.get("open")) - float(previous.get("close")))
            gap_condition = evaluate_condition(
                condition_id="anomaly.gap_vs_atr",
                name="开盘跳空绝对值 > 2×ATR",
                actual=gap,
                operator=">",
                reference=2.0 * atr,
                timestamp=timestamp,
                price=price,
                impact="report_only",
            )
            if gap_condition.passed:
                result.append(
                    _anomaly(
                        "gap_extreme",
                        timestamp,
                        price,
                        gap_condition,
                        "high",
                        "开盘相对前收出现超过 2×ATR 的跳空",
                    )
                )
            bar_range = float(row.get("high")) - float(row.get("low"))
            range_condition = evaluate_condition(
                condition_id="anomaly.range_vs_atr",
                name="单日振幅 > 3×ATR",
                actual=bar_range,
                operator=">",
                reference=3.0 * atr,
                timestamp=timestamp,
                price=price,
                impact="report_only",
            )
            if range_condition.passed:
                result.append(
                    _anomaly(
                        "range_extreme",
                        timestamp,
                        price,
                        range_condition,
                        "medium",
                        "单日高低价振幅超过 3×ATR",
                    )
                )
        percentile = _number(row.get("atr_percentile"))
        percentile_condition = evaluate_condition(
            condition_id="anomaly.atr_percentile_extreme",
            name="ATR 分位位于极端区间",
            actual=percentile,
            operator="outside",
            reference=(0.02, 0.98),
            timestamp=timestamp,
            price=price,
            impact="report_only",
        )
        if percentile_condition.passed:
            result.append(
                _anomaly(
                    "atr_percentile_extreme",
                    timestamp,
                    price,
                    percentile_condition,
                    "medium",
                    "ATR 百分位进入上下 2% 极端区间",
                )
            )
    return tuple(result)


def state_transitions(
    prepared: PreparedDailyAnalysis,
    *,
    start_position: int = 1,
) -> tuple[StateTransition, ...]:
    """Return every monitored state change in the requested prepared range.

    The function reuses already-prepared frames and evaluates state only; it does
    not rebuild indicators.  This gives the HTML a real transition timeline rather
    than only the latest before/after state.
    """

    if len(prepared.base) < 2:
        return ()

    def snapshot(position: int) -> dict[str, Any]:
        legacy = analyze_prepared_daily_signals(prepared.base, position)
        checks = analyze_prepared_strategy_checks(prepared.strategy, position=position)
        return {
            "strategy_rating": checks.get("rating", {}).get("grade"),
            "strategy_direction": checks.get("rating", {}).get("direction"),
            "resonance": legacy.get("resonance", {}).get("status"),
            "volatility_state": checks.get("volatility", {}).get("state"),
        }

    start = max(1, start_position)
    previous = snapshot(start - 1)
    result: list[StateTransition] = []
    for position in range(start, len(prepared.base)):
        current = snapshot(position)
        timestamp = prepared.base.index[position].isoformat()
        price = _number(prepared.base.iloc[position].get("close"))
        for name in (
            "strategy_rating",
            "strategy_direction",
            "resonance",
            "volatility_state",
        ):
            before, after = previous.get(name), current.get(name)
            if before == after:
                continue
            result.append(
                StateTransition(
                    transition_id=_stable_id(name, timestamp, before, after),
                    state_name=name,
                    from_state=before,
                    to_state=after,
                    timestamp=timestamp,
                    price=price,
                    reason=f"{name} changed from {before!r} to {after!r}",
                )
            )
        previous = current
    return tuple(result)


def build_instrument_report_bundle(
    prepared: PreparedDailyAnalysis,
    *,
    symbol: str,
    instrument_id: str,
    generated_at: str,
    analysis: Mapping[str, Any] | None = None,
    signals: Iterable[Mapping[str, Any]] | None = None,
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
    start_position = max(1, len(prepared.base) - chart_bars)
    anomalies = detect_anomalies(prepared, start_position=start_position)
    transitions = state_transitions(prepared, start_position=start_position)
    series = tuple(_series_payload(prepared.base.tail(chart_bars)))
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


def _rule_from_conditions(
    rule_id: str,
    name: str,
    conditions: tuple,
    timestamp: str,
    price: float | None,
    *,
    direction: str | None,
    outcome: Any,
    score_impact: float,
) -> RuleEvaluation:
    available = [item.passed for item in conditions if item.passed is not None]
    triggered = bool(available) and len(available) == len(conditions) and all(available)
    return RuleEvaluation(
        rule_id=rule_id,
        name=name,
        triggered=triggered,
        conditions=conditions,
        match_policy="all",
        direction=direction if triggered else None,
        score_impact=score_impact if triggered else 0.0,
        timestamp=timestamp,
        price=price,
        outcome=str(outcome) if outcome is not None else "not_triggered",
    )


def _single_rule(
    rule_id: str,
    name: str,
    condition,
    timestamp: str,
    price: float | None,
    direction: str | None,
    outcome: Any,
    score_impact: float,
) -> RuleEvaluation:
    return RuleEvaluation(
        rule_id=rule_id,
        name=name,
        triggered=condition.passed is True,
        conditions=(condition,),
        direction=direction if condition.passed else None,
        score_impact=score_impact if condition.passed else 0.0,
        timestamp=timestamp,
        price=price,
        outcome=str(outcome) if outcome is not None else "not_triggered",
    )


def _anomaly(
    anomaly_type: str,
    timestamp: str,
    price: float | None,
    condition,
    severity: str,
    summary: str,
) -> AnomalyEvent:
    return AnomalyEvent(
        anomaly_id=_stable_id(anomaly_type, timestamp),
        anomaly_type=anomaly_type,
        severity=severity,
        timestamp=timestamp,
        price=price,
        conditions=(condition,),
        summary=summary,
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


def _column_scalar(frame: pd.DataFrame, column: str) -> Any:
    if column not in frame or frame.empty:
        return None
    values = frame[column].dropna().astype(str).unique()
    return values[-1] if len(values) else None


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
    "build_instrument_report_bundle",
    "build_rule_evaluations",
    "detect_anomalies",
    "prepare_daily_analysis",
    "state_transitions",
]
