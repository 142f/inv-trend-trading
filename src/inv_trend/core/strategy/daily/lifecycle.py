"""Causal, report-only indicator lifecycle projections.

All indicator values are consumed from :class:`PreparedDailyAnalysis`; this
module classifies and groups those values but never recalculates trading
signals.  The resulting strengths explain evidence and do not alter ratings,
execution decisions, persistence, or notifications.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from typing import Any, Mapping, TYPE_CHECKING

import pandas as pd

from ...explanations import IndicatorSignalAnalysis, IndicatorSignalEpisode

if TYPE_CHECKING:
    from .analysis import PreparedDailyAnalysis


STRENGTH_CHANGE_EPSILON = 2.0


@dataclass(frozen=True)
class IndicatorLifecycleProjection:
    analyses: tuple[IndicatorSignalAnalysis, ...]
    episodes: tuple[IndicatorSignalEpisode, ...]


@dataclass(frozen=True)
class _Point:
    timestamp: str
    d1_position: int
    price: float | None
    available: bool
    active_state: bool
    direction: str
    state_key: str
    strength: float
    current_values: Mapping[str, Any] = field(default_factory=dict)
    trigger_conditions: tuple[Mapping[str, Any], ...] = ()
    reinforce: bool = False
    invalidation_reason: str = "condition_no_longer_satisfied"
    invalidation_conditions: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class _Spec:
    indicator_id: str
    indicator_name: str
    timeframe: str
    role: str
    decision_weight: float
    strength_meaning: str
    rule_ids: tuple[str, ...]
    points: tuple[_Point, ...]


@dataclass
class _OpenEpisode:
    spec: _Spec
    state_key: str
    direction: str
    points: list[_Point]
    reinforcement_count: int = 0
    last_reinforcement_timestamp: str | None = None


def build_indicator_lifecycles(
    prepared: PreparedDailyAnalysis,
) -> IndicatorLifecycleProjection:
    """Build every configured indicator lifecycle from one prepared snapshot."""

    if prepared.base.empty:
        return IndicatorLifecycleProjection((), ())
    specs: list[_Spec] = []
    config = prepared.strategy.config
    for entry_period, exit_period in config.turtle_systems:
        specs.append(_turtle_spec(prepared, entry_period, exit_period))
    specs.extend(
        (
            _sma_cross_spec(prepared),
            _sma_stack_spec(prepared),
            _ema_spec(prepared),
        )
    )
    for sessions in config.macd_session_periods:
        specs.append(_macd_spec(prepared, sessions))
    specs.extend((_adx_spec(prepared), _atr_spec(prepared), _volume_spec(prepared)))

    analyses: list[IndicatorSignalAnalysis] = []
    episodes: list[IndicatorSignalEpisode] = []
    for spec in specs:
        analysis, values = _group_spec(spec)
        analyses.append(analysis)
        episodes.extend(values)
    return IndicatorLifecycleProjection(tuple(analyses), tuple(episodes))


def _group_spec(
    spec: _Spec,
) -> tuple[IndicatorSignalAnalysis, tuple[IndicatorSignalEpisode, ...]]:
    completed: list[IndicatorSignalEpisode] = []
    opened: _OpenEpisode | None = None
    last_transition: tuple[str, str | None] | None = None

    for point in spec.points:
        if not point.available:
            if opened is not None:
                completed.append(
                    _close_episode(
                        opened,
                        status="evidence_interrupted",
                        invalidation_point=point,
                        reason="evidence_interrupted",
                        conditions=("required indicator value is unavailable",),
                    )
                )
                opened = None
                last_transition = ("evidence_interrupted", point.timestamp)
            continue
        if not point.active_state:
            if opened is not None:
                completed.append(
                    _close_episode(
                        opened,
                        status="invalidated",
                        invalidation_point=point,
                        reason=point.invalidation_reason,
                        conditions=point.invalidation_conditions,
                    )
                )
                opened = None
                last_transition = ("invalidated", point.timestamp)
            continue
        if opened is None or opened.state_key != point.state_key:
            if opened is not None:
                completed.append(
                    _close_episode(
                        opened,
                        status="invalidated",
                        invalidation_point=point,
                        reason=point.invalidation_reason,
                        conditions=point.invalidation_conditions,
                    )
                )
                last_transition = ("invalidated", point.timestamp)
            opened = _OpenEpisode(spec, point.state_key, point.direction, [point])
            continue
        previous_strength = opened.points[-1].strength
        opened.points.append(point)
        if point.reinforce or point.strength - previous_strength >= STRENGTH_CHANGE_EPSILON:
            opened.reinforcement_count += 1
            opened.last_reinforcement_timestamp = point.timestamp

    active_episode: IndicatorSignalEpisode | None = None
    if opened is not None:
        active_episode = _close_episode(opened, status="active")
        completed.append(active_episode)

    latest = spec.points[-1] if spec.points else None
    last_episode = completed[-1] if completed else None
    reference = active_episode or last_episode
    availability = "ready" if latest is not None and latest.available else "unavailable"
    if active_episode is not None:
        lifecycle_state = active_episode.lifecycle_state
        direction = active_episode.direction
        active = True
    elif last_transition and latest is not None and last_transition[1] == latest.timestamp:
        lifecycle_state = last_transition[0]
        direction = "neutral"
        active = False
    else:
        lifecycle_state = "not_started" if completed == [] else "invalidated"
        direction = "neutral"
        active = False

    strength = _round_strength(latest.strength if latest and latest.available else 0.0)
    support_effect = _support_effect(spec.role, direction, latest)
    explanation = _analysis_explanation(
        spec, direction, lifecycle_state, strength, reference, latest
    )
    analysis = IndicatorSignalAnalysis(
        indicator_id=spec.indicator_id,
        indicator_name=spec.indicator_name,
        timeframe=spec.timeframe,
        role=spec.role,
        availability=availability,
        direction=direction if direction in {"long", "short"} else "neutral",
        lifecycle_state=lifecycle_state,
        active=active,
        decision_weight=spec.decision_weight,
        strength=strength,
        strength_meaning=spec.strength_meaning,
        strength_delta=reference.strength_delta if reference else 0.0,
        strength_trend=reference.strength_trend if reference else "stable",
        first_trigger_timestamp=reference.start_timestamp if reference else None,
        first_trigger_price=reference.start_price if reference else None,
        duration_periods=reference.duration_periods if reference and active else 0,
        duration_d1_bars=reference.duration_d1_bars if reference and active else 0,
        elapsed_days=reference.elapsed_days if reference and active else 0,
        peak_strength=reference.peak_strength if reference else 0.0,
        average_strength=reference.average_strength if reference else 0.0,
        current_episode_id=active_episode.episode_id if active_episode else None,
        last_episode_id=last_episode.episode_id if last_episode else None,
        last_reinforcement_timestamp=(
            reference.last_reinforcement_timestamp if reference else None
        ),
        reinforcement_count=reference.reinforcement_count if reference else 0,
        invalidation_timestamp=(
            None if active_episode else (last_episode.invalidation_timestamp if last_episode else None)
        ),
        invalidation_reason=(
            None if active_episode else (last_episode.invalidation_reason if last_episode else None)
        ),
        support_effect=support_effect,
        explanation=explanation,
        current_values=dict(latest.current_values) if latest else {},
        trigger_conditions=reference.trigger_conditions if reference else (),
        invalidation_conditions=(
            () if active_episode else (last_episode.invalidation_conditions if last_episode else ())
        ),
        metadata={
            "rule_ids": list(spec.rule_ids),
            "strength_change_epsilon": STRENGTH_CHANGE_EPSILON,
            **(dict(latest.metadata) if latest else {}),
        },
    )
    return analysis, tuple(completed)


def _close_episode(
    opened: _OpenEpisode,
    *,
    status: str,
    invalidation_point: _Point | None = None,
    reason: str | None = None,
    conditions: tuple[str, ...] = (),
) -> IndicatorSignalEpisode:
    first, last = opened.points[0], opened.points[-1]
    strengths = [point.strength for point in opened.points]
    delta = strengths[-1] - strengths[-2] if len(strengths) > 1 else 0.0
    trend = _strength_trend(delta)
    lifecycle_state = (
        status
        if status != "active"
        else (
            "triggered"
            if len(opened.points) == 1
            else "strengthening"
            if trend == "strengthening"
            else "weakening"
            if trend == "weakening"
            else "continuing"
        )
    )
    elapsed = max(
        1,
        (pd.Timestamp(last.timestamp) - pd.Timestamp(first.timestamp)).days + 1,
    )
    return IndicatorSignalEpisode(
        episode_id=_stable_id(
            "indicator_episode", opened.spec.indicator_id, opened.state_key, first.timestamp
        ),
        indicator_id=opened.spec.indicator_id,
        indicator_name=opened.spec.indicator_name,
        timeframe=opened.spec.timeframe,
        role=opened.spec.role,
        direction=opened.direction if opened.direction in {"long", "short"} else "neutral",
        state_key=opened.state_key,
        start_timestamp=first.timestamp,
        start_price=first.price,
        end_timestamp=last.timestamp,
        end_price=last.price,
        status=status,
        lifecycle_state=lifecycle_state,
        duration_periods=len(opened.points),
        duration_d1_bars=max(1, last.d1_position - first.d1_position + 1),
        elapsed_days=elapsed,
        current_strength=_round_strength(strengths[-1]),
        peak_strength=_round_strength(max(strengths)),
        average_strength=_round_strength(sum(strengths) / len(strengths)),
        strength_delta=_round_delta(delta),
        strength_trend=trend,
        trigger_conditions=first.trigger_conditions,
        rule_ids=opened.spec.rule_ids,
        last_reinforcement_timestamp=opened.last_reinforcement_timestamp,
        reinforcement_count=opened.reinforcement_count,
        invalidation_timestamp=(
            invalidation_point.timestamp if invalidation_point is not None else None
        ),
        invalidation_price=(
            invalidation_point.price if invalidation_point is not None else None
        ),
        invalidation_reason=reason,
        invalidation_conditions=conditions,
        metadata={
            "strength_meaning": opened.spec.strength_meaning,
            **dict(first.metadata),
        },
    )


def _turtle_spec(
    prepared: PreparedDailyAnalysis, entry_period: int, exit_period: int
) -> _Spec:
    frame = prepared.base
    points: list[_Point] = []
    active_direction: str | None = None
    trigger_level: float | None = None
    trigger_atr: float | None = None
    trigger_conditions: tuple[Mapping[str, Any], ...] = ()
    for position, (timestamp, row) in enumerate(frame.iterrows()):
        close = _number(row.get("close"))
        atr = _number(row.get("atr"))
        entry_high = _number(row.get(f"channel_high_{entry_period}"))
        entry_low = _number(row.get(f"channel_low_{entry_period}"))
        exit_high = _number(row.get(f"channel_high_{exit_period}"))
        exit_low = _number(row.get(f"channel_low_{exit_period}"))
        values = (close, atr, entry_high, entry_low, exit_high, exit_low)
        timestamp_text = timestamp.isoformat()
        current_values = {
            "close": close,
            "atr": atr,
            "entry_high": entry_high,
            "entry_low": entry_low,
            "exit_high": exit_high,
            "exit_low": exit_low,
            "entry_period": entry_period,
            "exit_period": exit_period,
        }
        if any(value is None for value in values) or float(atr) <= 0.0:
            active_direction = None
            trigger_level = None
            trigger_atr = None
            trigger_conditions = ()
            points.append(
                _Point(
                    timestamp_text,
                    position,
                    close,
                    False,
                    False,
                    "neutral",
                    "unavailable",
                    0.0,
                    current_values,
                )
            )
            continue

        invalidation_reason = "turtle_exit_channel_breached"
        invalidation_conditions = (
            f"long invalidates when close < {exit_period}-day lower channel",
            f"short invalidates when close > {exit_period}-day upper channel",
        )
        if active_direction == "long" and float(close) < float(exit_low):
            active_direction = None
            trigger_level = None
            trigger_atr = None
            trigger_conditions = ()
        elif active_direction == "short" and float(close) > float(exit_high):
            active_direction = None
            trigger_level = None
            trigger_atr = None
            trigger_conditions = ()

        new_direction = (
            "long"
            if float(close) > float(entry_high)
            else "short"
            if float(close) < float(entry_low)
            else None
        )
        newly_triggered = active_direction is None and new_direction is not None
        if newly_triggered:
            active_direction = new_direction
            trigger_level = float(entry_high if new_direction == "long" else entry_low)
            trigger_atr = float(atr)
            trigger_conditions = (
                {
                    "condition_id": f"turtle_{entry_period}.close_breakout",
                    "actual_value": close,
                    "reference_value": trigger_level,
                    "operator": ">" if new_direction == "long" else "<",
                    "passed": True,
                    "timestamp": timestamp_text,
                    "price": close,
                },
            )
        if active_direction is None:
            points.append(
                _Point(
                    timestamp_text,
                    position,
                    close,
                    True,
                    False,
                    "neutral",
                    "inside",
                    0.0,
                    current_values,
                    invalidation_reason=invalidation_reason,
                    invalidation_conditions=invalidation_conditions,
                )
            )
            continue
        excursion = max(
            0.0,
            (float(close) - float(trigger_level))
            * (1.0 if active_direction == "long" else -1.0),
        )
        strength = _sat(excursion, float(trigger_atr))
        reinforce = not newly_triggered and new_direction == active_direction
        points.append(
            _Point(
                timestamp_text,
                position,
                close,
                True,
                True,
                active_direction,
                active_direction,
                strength,
                {
                    **current_values,
                    "trigger_level": trigger_level,
                    "trigger_atr": trigger_atr,
                    "formal_breakout_today": new_direction == active_direction,
                },
                trigger_conditions if newly_triggered else (),
                reinforce=reinforce,
                invalidation_reason=invalidation_reason,
                invalidation_conditions=invalidation_conditions,
                metadata={"entry_period": entry_period, "exit_period": exit_period},
            )
        )
    return _Spec(
        f"turtle_{entry_period}",
        f"收盘确认型海龟 {entry_period}/{exit_period} 日系统",
        "D1",
        "directional",
        1.0,
        "收盘相对首次突破位的有利距离，以触发时 ATR 归一化",
        (f"turtle_{entry_period}_breakout",),
        tuple(points),
    )


def _sma_cross_spec(prepared: PreparedDailyAnalysis) -> _Spec:
    points: list[_Point] = []
    for position, (timestamp, row) in enumerate(prepared.base.iterrows()):
        fast, slow, atr, price = (_number(row.get(name)) for name in ("sma_10", "sma_20", "atr", "close"))
        points.append(
            _directional_gap_point(
                timestamp.isoformat(), position, price, fast, slow, atr,
                state_prefix="sma_cross", labels=("SMA10", "SMA20"),
            )
        )
    return _Spec(
        "sma_10_20", "SMA10/20 关系", "D1", "diagnostic", 0.0,
        "SMA10 与 SMA20 的绝对间距，以当前 ATR 归一化",
        ("sma_golden_cross", "sma_death_cross"), tuple(points),
    )


def _sma_stack_spec(prepared: PreparedDailyAnalysis) -> _Spec:
    periods = prepared.strategy.config.sma_periods
    points: list[_Point] = []
    for position, (timestamp, row) in enumerate(prepared.base.iterrows()):
        values = [_number(row.get(f"sma_{period}")) for period in periods]
        atr, price = _number(row.get("atr")), _number(row.get("close"))
        available = all(value is not None for value in values) and atr is not None and atr > 0
        direction = "neutral"
        active = False
        strength = 0.0
        if available:
            numeric = [float(value) for value in values if value is not None]
            if all(left > right for left, right in zip(numeric, numeric[1:])):
                direction, active = "long", True
            elif all(left < right for left, right in zip(numeric, numeric[1:])):
                direction, active = "short", True
            if active:
                weakest_gap = min(abs(left - right) for left, right in zip(numeric, numeric[1:]))
                strength = _sat(weakest_gap, float(atr))
        conditions = tuple(
            {
                "condition_id": f"sma_stack.{left}_{right}",
                "actual_value": values[index],
                "reference_value": values[index + 1],
                "operator": ">" if direction == "long" else "<",
                "passed": active,
                "timestamp": timestamp.isoformat(),
                "price": price,
            }
            for index, (left, right) in enumerate(zip(periods, periods[1:]))
        ) if active else ()
        points.append(
            _Point(
                timestamp.isoformat(), position, price, available, active, direction,
                direction if active else "none", strength,
                {"periods": list(periods), "values": dict(zip(map(str, periods), values)), "atr": atr},
                conditions,
                invalidation_reason="sma_order_broken",
                invalidation_conditions=("strict SMA ordering no longer holds",),
            )
        )
    return _Spec(
        "sma_stack", "SMA 多周期排列", "D1", "directional", 2.0,
        "完整均线排列中最弱相邻间距，以当前 ATR 归一化",
        ("sma_stack_bullish", "sma_stack_bearish"), tuple(points),
    )


def _ema_spec(prepared: PreparedDailyAnalysis) -> _Spec:
    short, long = prepared.strategy.config.ema_periods
    points: list[_Point] = []
    for position, (timestamp, row) in enumerate(prepared.base.iterrows()):
        fast, slow, atr, price = (
            _number(row.get(f"ema_{short}")), _number(row.get(f"ema_{long}")),
            _number(row.get("atr")), _number(row.get("close")),
        )
        point = _directional_gap_point(
            timestamp.isoformat(), position, price, fast, slow, atr,
            state_prefix="ema", labels=(f"EMA{short}", f"EMA{long}"),
        )
        if position + 1 < long:
            point = _Point(
                point.timestamp, point.d1_position, point.price, False, False,
                "neutral", "unavailable", 0.0, point.current_values,
            )
        points.append(point)
    return _Spec(
        "ema_trend", f"EMA{short}/{long} 长周期趋势", "D1", "directional", 1.0,
        "EMA 快慢线绝对间距，以当前 ATR 归一化",
        ("ema_trend",), tuple(points),
    )


def _macd_spec(prepared: PreparedDailyAnalysis, sessions: int) -> _Spec:
    label = f"D{sessions}"
    frame = prepared.strategy.macd_frames.get(label, pd.DataFrame())
    minimum = prepared.strategy.config.macd_slow + prepared.strategy.config.macd_signal - 1
    positions = {timestamp: index for index, timestamp in enumerate(prepared.base.index)}
    points: list[_Point] = []
    for native_position, (timestamp, row) in enumerate(frame.iterrows()):
        dif, dea, atr, price = (
            _number(row.get("dif")), _number(row.get("dea")),
            _number(row.get("atr")), _number(row.get("close")),
        )
        available = (
            native_position + 1 >= minimum
            and None not in (dif, dea, atr)
            and float(atr) > 0.0
        )
        direction = "neutral"
        if available and float(dif) > 0.0 and float(dea) > 0.0:
            direction = "long"
        elif available and float(dif) < 0.0 and float(dea) < 0.0:
            direction = "short"
        active = direction != "neutral"
        axis_distance = min(abs(float(dif)), abs(float(dea))) if active else 0.0
        strength = _sat(axis_distance, float(atr)) if active else 0.0
        momentum = (
            "long" if available and float(dif) > float(dea)
            else "short" if available and float(dif) < float(dea)
            else "neutral"
        )
        d1_position = positions.get(timestamp, 0)
        points.append(
            _Point(
                timestamp.isoformat(), d1_position, price, available, active, direction,
                direction if active else "mixed_zero_axis", strength,
                {
                    "dif": dif, "dea": dea, "atr": atr,
                    "zero_axis_direction": direction,
                    "momentum_direction": momentum,
                    "histogram": _number(row.get("histogram")),
                },
                ({
                    "condition_id": f"macd_{label}.zero_axis",
                    "actual_value": [dif, dea],
                    "reference_value": 0.0,
                    "operator": "both_above" if direction == "long" else "both_below",
                    "passed": True,
                    "timestamp": timestamp.isoformat(),
                    "price": price,
                },) if active else (),
                invalidation_reason="macd_zero_axis_consensus_lost",
                invalidation_conditions=("DIF and DEA no longer remain on the same zero-axis side",),
                metadata={"sessions_per_bar": sessions, "momentum_direction": momentum},
            )
        )
    return _Spec(
        f"macd_{label.lower()}", f"{label} MACD 零轴趋势", label,
        "directional", 0.75,
        "DIF、DEA 到零轴的较小距离，以对应原生周期 ATR 归一化",
        (f"macd_{label.lower()}_zero_axis",), tuple(points),
    )


def _adx_spec(prepared: PreparedDailyAnalysis) -> _Spec:
    threshold = prepared.strategy.config.adx_threshold
    points: list[_Point] = []
    for position, (timestamp, row) in enumerate(prepared.base.iterrows()):
        adx, plus_di, minus_di, price = (
            _number(row.get("adx")), _number(row.get("plus_di")),
            _number(row.get("minus_di")), _number(row.get("close")),
        )
        available = None not in (adx, plus_di, minus_di)
        confirmed = available and float(adx) >= threshold
        direction = (
            "long" if confirmed and float(plus_di) > float(minus_di)
            else "short" if confirmed and float(plus_di) < float(minus_di)
            else "neutral"
        )
        active = direction != "neutral"
        denominator = float(plus_di) + float(minus_di) if available else 0.0
        separation = (
            100.0 * abs(float(plus_di) - float(minus_di)) / denominator
            if denominator > 0.0 else 0.0
        )
        strength = (min(100.0, float(adx)) + separation) / 2.0 if active else 0.0
        points.append(
            _Point(
                timestamp.isoformat(), position, price, available, active, direction,
                direction if active else "not_confirmed", strength,
                {"adx": adx, "plus_di": plus_di, "minus_di": minus_di, "threshold": threshold},
                ({
                    "condition_id": "adx_dmi.confirmed_direction",
                    "actual_value": adx,
                    "reference_value": threshold,
                    "operator": ">=",
                    "passed": True,
                    "timestamp": timestamp.isoformat(),
                    "price": price,
                },) if active else (),
                invalidation_reason="adx_or_dmi_confirmation_lost",
                invalidation_conditions=("ADX falls below threshold or DMI direction becomes neutral/reverses",),
            )
        )
    return _Spec(
        "adx_dmi", "ADX/DMI 趋势确认", "D1", "directional_quality", 1.0,
        "ADX 强度与 +DI/-DI 分离百分比的等权平均",
        ("adx_dmi_confirmation",), tuple(points),
    )


def _atr_spec(prepared: PreparedDailyAnalysis) -> _Spec:
    config = prepared.strategy.config
    points: list[_Point] = []
    for position, (timestamp, row) in enumerate(prepared.base.iterrows()):
        percentile, atr, atr_pct, price = (
            _number(row.get("atr_percentile")), _number(row.get("atr")),
            _number(row.get("atr_pct")), _number(row.get("close")),
        )
        available = percentile is not None
        state = (
            "normal" if available and config.atr_normal_percentile_low <= float(percentile) <= config.atr_normal_percentile_high
            else "low" if available and float(percentile) < config.atr_normal_percentile_low
            else "high" if available else "unavailable"
        )
        strength = min(100.0, abs(float(percentile) - 0.5) * 200.0) if available else 0.0
        quality_effect = "positive" if state == "normal" else "negative" if available else "neutral"
        points.append(
            _Point(
                timestamp.isoformat(), position, price, available, available, "neutral",
                f"atr_{state}", strength,
                {"atr": atr, "atr_pct": atr_pct, "percentile": percentile, "state": state},
                ({
                    "condition_id": "atr.quality_state",
                    "actual_value": percentile,
                    "reference_value": [config.atr_normal_percentile_low, config.atr_normal_percentile_high],
                    "operator": "between" if state == "normal" else "outside",
                    "passed": state == "normal",
                    "timestamp": timestamp.isoformat(),
                    "price": price,
                },) if available else (),
                invalidation_reason="atr_quality_state_changed",
                invalidation_conditions=("ATR percentile crosses a configured quality boundary",),
                metadata={"quality_effect": quality_effect, "quality_state": state},
            )
        )
    return _Spec(
        "atr_quality", "ATR 波动质量", "D1", "quality", 1.0,
        "ATR 分位偏离 50% 中位状态的程度；高分表示更极端，不代表更利多",
        ("atr_normal_range",), tuple(points),
    )


def _volume_spec(prepared: PreparedDailyAnalysis) -> _Spec:
    threshold = prepared.strategy.config.volume_confirmation_ratio
    points: list[_Point] = []
    for position, (timestamp, row) in enumerate(prepared.base.iterrows()):
        relative, volume, price = (
            _number(row.get("relative_volume")), _number(row.get("volume")),
            _number(row.get("close")),
        )
        available = relative is not None
        confirmed = available and float(relative) >= threshold
        state = "confirmed" if confirmed else "unconfirmed"
        strength = min(100.0, max(0.0, float(relative) / threshold * 100.0)) if available else 0.0
        points.append(
            _Point(
                timestamp.isoformat(), position, price, available, available, "neutral",
                f"volume_{state}", strength,
                {"relative_volume": relative, "volume": volume, "threshold": threshold, "confirmed": confirmed},
                ({
                    "condition_id": "volume.relative_confirmation",
                    "actual_value": relative,
                    "reference_value": threshold,
                    "operator": ">=",
                    "passed": confirmed,
                    "timestamp": timestamp.isoformat(),
                    "price": price,
                },) if available else (),
                invalidation_reason="relative_volume_state_changed",
                invalidation_conditions=("relative volume crosses the confirmation threshold",),
                metadata={"quality_effect": "positive" if confirmed else "neutral", "quality_state": state},
            )
        )
    return _Spec(
        "relative_volume", "相对成交量确认", "D1", "quality", 1.0,
        "相对成交量达到确认阈值时为 100，阈值以下按比例计分",
        ("relative_volume_confirmation",), tuple(points),
    )


def _directional_gap_point(
    timestamp: str,
    position: int,
    price: float | None,
    fast: float | None,
    slow: float | None,
    atr: float | None,
    *,
    state_prefix: str,
    labels: tuple[str, str],
) -> _Point:
    available = None not in (fast, slow, atr) and float(atr) > 0.0
    direction = (
        "long" if available and float(fast) > float(slow)
        else "short" if available and float(fast) < float(slow)
        else "neutral"
    )
    active = direction != "neutral"
    strength = _sat(abs(float(fast) - float(slow)), float(atr)) if active else 0.0
    return _Point(
        timestamp, position, price, available, active, direction,
        f"{state_prefix}_{direction}" if active else f"{state_prefix}_neutral",
        strength,
        {"fast": fast, "slow": slow, "atr": atr, "fast_label": labels[0], "slow_label": labels[1]},
        ({
            "condition_id": f"{state_prefix}.direction",
            "actual_value": fast,
            "reference_value": slow,
            "operator": ">" if direction == "long" else "<",
            "passed": True,
            "timestamp": timestamp,
            "price": price,
        },) if active else (),
        invalidation_reason=f"{state_prefix}_relationship_lost_or_reversed",
        invalidation_conditions=(f"{labels[0]} equals or crosses {labels[1]}",),
    )


def _support_effect(role: str, direction: str, latest: _Point | None) -> str:
    if direction in {"long", "short"}:
        return "directional_support"
    if role == "quality" and latest is not None:
        effect = str(latest.metadata.get("quality_effect") or "neutral")
        return f"quality_{effect}"
    return "neutral"


def _analysis_explanation(
    spec: _Spec,
    direction: str,
    lifecycle_state: str,
    strength: float,
    episode: IndicatorSignalEpisode | None,
    latest: _Point | None,
) -> str:
    direction_text = {"long": "做多", "short": "做空"}.get(direction, "中性")
    if latest is None or not latest.available:
        return f"{spec.indicator_name}证据不可用，方向按中性处理。"
    duration = episode.duration_periods if episode and episode.status == "active" else 0
    return (
        f"{spec.indicator_name}当前支持{direction_text}；生命周期为 {lifecycle_state}，"
        f"强度 {strength:.1f}/100，已持续 {duration} 个{spec.timeframe}周期。"
    )


def _sat(value: float, scale: float) -> float:
    value, scale = max(0.0, float(value)), float(scale)
    if not math.isfinite(value) or not math.isfinite(scale) or scale <= 0.0:
        return 0.0
    return 100.0 * value / (value + scale)


def _strength_trend(delta: float) -> str:
    if delta >= STRENGTH_CHANGE_EPSILON:
        return "strengthening"
    if delta <= -STRENGTH_CHANGE_EPSILON:
        return "weakening"
    return "stable"


def _round_strength(value: float) -> float:
    return round(min(100.0, max(0.0, float(value))), 4)


def _round_delta(value: float) -> float:
    return round(float(value), 4)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _stable_id(*parts: Any) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:24]


__all__ = [
    "IndicatorLifecycleProjection",
    "STRENGTH_CHANGE_EPSILON",
    "build_indicator_lifecycles",
]
