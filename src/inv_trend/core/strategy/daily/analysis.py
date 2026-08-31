"""Pure single-pass D1 feature preparation and rule evaluation.

The functions in this module consume only in-memory bars, structural strategy
configuration, and core value types.  Application code may project their
results into reports, but this module has no application, adapter, data, or
presentation dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from typing import Any, Mapping

import pandas as pd

from ...explanations import AnomalyEvent, RuleEvaluation, StateTransition
from ...features import FeatureRequest, PreparedBars
from ...rules import evaluate_condition
from .signals import (
    analyze_prepared_daily_signals,
    daily_signal_feature_request,
    normalize_completed_daily_bars,
)
from .strategy_checks import (
    DailyChecksConfigLike,
    PreparedDailyStrategyChecks,
    analyze_prepared_strategy_checks,
    daily_strategy_feature_request,
    prepare_daily_strategy_checks,
)


@dataclass(frozen=True)
class PreparedDailyAnalysis:
    """One completed-bar snapshot with every D1 feature prepared exactly once."""

    base: pd.DataFrame
    strategy: PreparedDailyStrategyChecks
    feature_request: FeatureRequest
    dataset_version: str


def prepare_daily_analysis(
    bars: pd.DataFrame,
    config: DailyChecksConfigLike,
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
    config = prepared.strategy.config
    for position in range(start, len(frame)):
        row = frame.iloc[position]
        timestamp = frame.index[position].isoformat()
        previous_timestamp = frame.index[position - 1].isoformat()
        price = _number(row.get("close"))
        previous_atr = _number(row.get("previous_atr"))
        gap = _number(row.get("gap_abs"))
        gap_ratio = _number(row.get("gap_atr_ratio"))
        range_value = _number(row.get("range_abs"))
        range_ratio = _number(row.get("range_atr_ratio"))
        if previous_atr is not None and previous_atr > 0:
            gap_condition = evaluate_condition(
                condition_id="anomaly.gap_vs_atr",
                name=(
                    "开盘跳空 / 前一完整 K 线 ATR "
                    f"> {config.anomaly_gap_atr_multiplier:g}"
                ),
                actual=gap_ratio,
                operator=">",
                reference=config.anomaly_gap_atr_multiplier,
                timestamp=timestamp,
                price=price,
                impact="report_only",
                metadata={
                    "absolute_move": gap,
                    "atr_reference": previous_atr,
                    "atr_reference_timestamp": previous_timestamp,
                    "unit": "ATR_multiple",
                },
            )
            if gap_condition.passed:
                result.append(
                    _anomaly(
                        "gap_extreme",
                        timestamp,
                        price,
                        gap_condition,
                        "high",
                        "开盘相对前收的跳空超过前一完整 K 线 ATR 阈值",
                    )
                )
            range_condition = evaluate_condition(
                condition_id="anomaly.range_vs_atr",
                name=(
                    "单日振幅 / 前一完整 K 线 ATR "
                    f"> {config.anomaly_range_atr_multiplier:g}"
                ),
                actual=range_ratio,
                operator=">",
                reference=config.anomaly_range_atr_multiplier,
                timestamp=timestamp,
                price=price,
                impact="report_only",
                metadata={
                    "absolute_move": range_value,
                    "atr_reference": previous_atr,
                    "atr_reference_timestamp": previous_timestamp,
                    "unit": "ATR_multiple",
                },
            )
            if range_condition.passed:
                result.append(
                    _anomaly(
                        "range_extreme",
                        timestamp,
                        price,
                        range_condition,
                        "medium",
                        "单日高低价振幅超过前一完整 K 线 ATR 阈值",
                    )
                )
        percentile = _number(row.get("atr_percentile"))
        percentile_condition = evaluate_condition(
            condition_id="anomaly.atr_percentile_extreme",
            name="ATR 分位位于极端区间",
            actual=percentile,
            operator="outside",
            reference=(
                config.anomaly_atr_percentile_low,
                config.anomaly_atr_percentile_high,
            ),
            timestamp=timestamp,
            price=price,
            impact="report_only",
            metadata={
                "lookback": config.atr_percentile_lookback,
                "method": "trailing_midrank_current_included",
            },
        )
        if percentile_condition.passed:
            result.append(
                _anomaly(
                    "atr_percentile_extreme",
                    timestamp,
                    price,
                    percentile_condition,
                    "medium",
                    "ATR 百分位进入配置的极端区间",
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
    "build_rule_evaluations",
    "detect_anomalies",
    "prepare_daily_analysis",
    "state_transitions",
]
