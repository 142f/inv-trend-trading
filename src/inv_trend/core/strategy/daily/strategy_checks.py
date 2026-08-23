"""Parallel, causal D1 strategy checks.

The checks operate only on completed in-memory bars and a structural strategy
configuration. They neither know data providers nor participate in detector
state, execution, persistence, or report rendering.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Protocol

import numpy as np
import pandas as pd

from ...features import FeatureRequest, PreparedBars
from ...resampling import aggregate_completed_sessions
from ...signals import crossed_above, crossed_below


class DailyChecksConfigLike(Protocol):
    """Structural contract consumed by pure D1 strategy calculations."""

    sma_periods: tuple[int, ...]
    ema_periods: tuple[int, int]
    macd_fast: int
    macd_slow: int
    macd_signal: int
    macd_session_periods: tuple[int, ...]
    dmi_period: int
    adx_threshold: float
    atr_period: int
    atr_percentile_lookback: int
    atr_normal_percentile_low: float
    atr_normal_percentile_high: float
    volume_lookback: int
    volume_confirmation_ratio: float
    rating_a_min_score: float
    rating_b_min_score: float
    rating_a_min_families: int
    rating_b_min_families: int


TURTLE_PERIODS = (20, 55)


@dataclass(frozen=True)
class PreparedDailyStrategyChecks:
    """Base D1 and completed higher-session MACD frames for one input snapshot."""

    base: pd.DataFrame
    macd_frames: Mapping[str, pd.DataFrame]
    config: DailyChecksConfigLike
    session_anchor: pd.Timestamp


def daily_strategy_feature_request(config: DailyChecksConfigLike) -> FeatureRequest:
    """Return the D1 feature contract for report-only strategy dimensions."""

    return FeatureRequest(
        atr_period=config.atr_period,
        donchian_periods=TURTLE_PERIODS,
        sma_lags=tuple((period, 0) for period in config.sma_periods),
        ema_periods=config.ema_periods,
        macd_periods=(config.macd_fast, config.macd_slow, config.macd_signal),
        dmi_period=config.dmi_period,
        volume_sma_lags=((config.volume_lookback, 1),),
        include_true_range=True,
    )


def prepare_daily_strategy_checks(
    bars: pd.DataFrame,
    config: DailyChecksConfigLike,
    *,
    session_anchor: pd.Timestamp | str,
    prepared_base: pd.DataFrame | None = None,
) -> PreparedDailyStrategyChecks:
    """Prepare all strategy dimensions from completed, timestamp-indexed D1 bars."""

    if not isinstance(bars, pd.DataFrame):
        raise TypeError("bars must be a pandas DataFrame")
    if not isinstance(bars.index, pd.DatetimeIndex):
        raise ValueError("bars require a DatetimeIndex")
    if bars.empty:
        anchor = _utc_timestamp(session_anchor)
        return PreparedDailyStrategyChecks(bars.copy(), {}, config, anchor)

    anchor = _utc_timestamp(session_anchor)
    if prepared_base is None:
        base = PreparedBars.build(bars, daily_strategy_feature_request(config)).frame.copy()
    else:
        if not isinstance(prepared_base.index, pd.DatetimeIndex):
            raise ValueError("prepared_base requires a DatetimeIndex")
        if not prepared_base.index.equals(bars.index):
            raise ValueError("prepared_base must represent the same completed D1 bars")
        missing = [
            column
            for column in daily_strategy_feature_request(config).required_columns()
            if column not in prepared_base
        ]
        if missing:
            raise ValueError(f"prepared_base is missing strategy features: {missing}")
        base = prepared_base.copy()
    for period in config.sma_periods:
        base[f"sma_{period}"] = base[f"sma_{period}_lag_0"]
    close = pd.to_numeric(base["close"], errors="coerce")
    base["atr_pct"] = base["atr"] / close
    base["atr_percentile"] = base["atr_pct"].rolling(
        config.atr_percentile_lookback,
        min_periods=config.atr_percentile_lookback,
    ).rank(method="max", pct=True)
    volume_average = base[f"volume_sma_{config.volume_lookback}_lag_1"]
    if "volume" in base:
        volume = pd.to_numeric(base["volume"], errors="coerce")
        base["relative_volume"] = (volume / volume_average).where(volume_average > 0.0)
    else:
        base["relative_volume"] = np.nan

    macd_frames: dict[str, pd.DataFrame] = {"D1": base}
    for sessions in config.macd_session_periods:
        if sessions == 1:
            continue
        label = _timeframe_label(sessions)
        aggregated = aggregate_completed_sessions(bars, sessions, anchor=anchor)
        if aggregated.empty:
            macd_frames[label] = aggregated
            continue
        macd_frames[label] = PreparedBars.build(
            aggregated,
            FeatureRequest(
                macd_periods=(config.macd_fast, config.macd_slow, config.macd_signal)
            ),
        ).frame
    return PreparedDailyStrategyChecks(base, macd_frames, config, anchor)


def analyze_prepared_strategy_checks(
    prepared: PreparedDailyStrategyChecks,
    *,
    position: int = -1,
) -> dict[str, Any]:
    """Evaluate one D1 bar and emit only newly formed independent events."""

    base = prepared.base
    if base.empty:
        return _empty_checks(prepared.config)
    stop = _stop_position(base, position)
    current = _state_at(prepared, stop - 1)
    previous = _state_at(prepared, stop - 2) if stop > 1 else None
    signals: list[dict[str, Any]] = []
    timestamp = base.index[stop - 1].isoformat()

    for name, indicator, parameters in (
        (
            "sma_alignment",
            current["sma_alignment"],
            {"periods": list(prepared.config.sma_periods)},
        ),
        (
            "ema_trend",
            current["ema_trend"],
            {"periods": list(prepared.config.ema_periods)},
        ),
    ):
        prior_direction = previous[name]["direction"] if previous is not None else None
        direction = indicator["direction"]
        if direction in {"long", "short"} and direction != prior_direction:
            signals.append(
                {
                    "indicator": name,
                    "event": "alignment_formed",
                    "direction": direction,
                    "timeframe": "D1",
                    "signal_time": timestamp,
                    "reference_value": indicator.get("reference_value"),
                    "parameters": parameters,
                }
            )

    for timeframe, values in current["macd"].items():
        # D1 remains the existing, backward-compatible daily MACD event.
        if (
            timeframe == "D1"
            or values["event"] is None
            or values["bar_end"] != timestamp
        ):
            continue
        signals.append(
            {
                "indicator": "macd_12_26_9",
                "event": values["event"],
                "direction": values["cross_direction"],
                "timeframe": timeframe,
                "signal_time": values["bar_end"],
                "reference_value": values.get("dea"),
                "parameters": {
                    "fast": prepared.config.macd_fast,
                    "slow": prepared.config.macd_slow,
                    "signal": prepared.config.macd_signal,
                    "adjust": False,
                    "sessions_per_bar": _sessions_from_timeframe(timeframe),
                },
            }
        )

    prior_rating = previous["rating"] if previous is not None else None
    rating = current["rating"]
    if rating["grade"] == "A" and (
        prior_rating is None
        or prior_rating["grade"] != "A"
        or prior_rating.get("direction") != rating.get("direction")
    ):
        direction = str(rating["direction"])
        signals.append(
            {
                "indicator": "strategy_rating",
                "event": "grade_a_entered",
                "direction": direction,
                "timeframe": "D1",
                "signal_time": timestamp,
                "reference_value": rating["score"],
                "parameters": {
                    "grade": "A",
                    "score": rating["score"],
                    "families": rating["aligned_families"],
                    "votes": rating["family_votes"],
                },
            }
        )
    current["signals"] = signals
    return current


def _state_at(prepared: PreparedDailyStrategyChecks, position: int) -> dict[str, Any]:
    base = prepared.base.iloc[: position + 1]
    row = base.iloc[-1]
    timestamp = base.index[-1]
    sma = _sma_alignment(row, prepared.config)
    ema = _ema_trend(row, prepared.config, len(base))
    turtle = _turtle_direction(row)
    macd = {
        label: _macd_state(frame.loc[frame.index <= timestamp], prepared.config)
        for label, frame in prepared.macd_frames.items()
    }
    macd_summary = _macd_summary(macd)
    trend_quality = _trend_quality(row, prepared.config)
    volatility = _volatility(row, prepared.config)
    volume = _volume(row, prepared.config)
    rating = _rating(
        turtle,
        sma,
        ema,
        macd_summary,
        trend_quality,
        volatility,
        volume,
        prepared.config,
    )
    return {
        "sma_alignment": sma,
        "ema_trend": ema,
        "macd": macd,
        "macd_summary": macd_summary,
        "trend_quality": trend_quality,
        "volatility": volatility,
        "volume": volume,
        "rating": rating,
    }


def _sma_alignment(row: pd.Series, config: DailyChecksConfigLike) -> dict[str, Any]:
    values = [_finite_float(row.get(f"sma_{period}")) for period in config.sma_periods]
    if any(value is None for value in values):
        return _unavailable_alignment(config.sma_periods)
    direction = _ordered_direction([float(value) for value in values])
    return {
        "status": "ready",
        "direction": direction,
        "periods": list(config.sma_periods),
        "values": {str(period): value for period, value in zip(config.sma_periods, values)},
        "reference_value": values[-1],
    }


def _ema_trend(
    row: pd.Series, config: DailyChecksConfigLike, completed_bars: int
) -> dict[str, Any]:
    short, long = config.ema_periods
    short_value = _finite_float(row.get(f"ema_{short}"))
    long_value = _finite_float(row.get(f"ema_{long}"))
    if short_value is None or long_value is None or completed_bars < long:
        return _unavailable_alignment(config.ema_periods)
    return {
        "status": "ready",
        "direction": _relative_direction(short_value, long_value),
        "periods": list(config.ema_periods),
        "values": {str(short): short_value, str(long): long_value},
        "reference_value": long_value,
    }


def _turtle_direction(row: pd.Series) -> dict[str, Any]:
    directions: list[str] = []
    ready = False
    for period in TURTLE_PERIODS:
        close = _finite_float(row.get("close"))
        high = _finite_float(row.get(f"channel_high_{period}"))
        low = _finite_float(row.get(f"channel_low_{period}"))
        if close is None or high is None or low is None:
            continue
        ready = True
        if close > high:
            directions.append("long")
        elif close < low:
            directions.append("short")
    unique = set(directions)
    direction = next(iter(unique)) if len(unique) == 1 else ("conflict" if unique else "none")
    return {"status": "ready" if ready else "unavailable", "direction": direction}


def _macd_state(frame: pd.DataFrame, config: DailyChecksConfigLike) -> dict[str, Any]:
    minimum = config.macd_slow + config.macd_signal - 1
    cross_minimum = minimum + 1
    if len(frame) < minimum:
        return {
            "status": "unavailable",
            "cross_status": "unavailable",
            "bar_end": None,
            "dif": None,
            "dea": None,
            "histogram": None,
            "direction": None,
            "zero_axis": None,
            "event": None,
            "cross_direction": None,
        }
    row = frame.iloc[-1]
    dif, dea = _finite_float(row.get("dif")), _finite_float(row.get("dea"))
    if dif is None or dea is None:
        return {
            "status": "unavailable",
            "cross_status": "unavailable",
            "bar_end": None,
            "dif": None,
            "dea": None,
            "histogram": None,
            "direction": None,
            "zero_axis": None,
            "event": None,
            "cross_direction": None,
        }
    previous = frame.iloc[-2] if len(frame) >= cross_minimum else None
    previous_dif = _finite_float(previous.get("dif")) if previous is not None else None
    previous_dea = _finite_float(previous.get("dea")) if previous is not None else None
    event = None
    cross_direction = None
    if None not in (previous_dif, previous_dea):
        if crossed_above(float(previous_dif), float(previous_dea), dif, dea):
            event, cross_direction = "golden_cross", "long"
        elif crossed_below(float(previous_dif), float(previous_dea), dif, dea):
            event, cross_direction = "death_cross", "short"
    zero_axis = (
        "long"
        if dif > 0.0 and dea > 0.0
        else ("short" if dif < 0.0 and dea < 0.0 else "none")
    )
    return {
        "status": "ready",
        "cross_status": "ready" if len(frame) >= cross_minimum else "unavailable",
        "bar_end": frame.index[-1].isoformat(),
        "dif": dif,
        "dea": dea,
        "histogram": _finite_float(row.get("histogram")),
        "direction": _relative_direction(dif, dea),
        "zero_axis": zero_axis,
        "event": event,
        "cross_direction": cross_direction,
    }


def _macd_summary(macd: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    values = [
        item["zero_axis"]
        for item in macd.values()
        if item.get("zero_axis") in {"long", "short"}
    ]
    directions = set(values)
    if len(directions) == 1:
        direction = next(iter(directions))
    elif len(directions) > 1:
        direction = "conflict"
    else:
        direction = "none"
    return {
        "status": "ready" if values else "unavailable",
        "direction": direction,
        "active_timeframes": [
            label
            for label, item in macd.items()
            if item.get("zero_axis") in {"long", "short"}
        ],
    }


def _trend_quality(row: pd.Series, config: DailyChecksConfigLike) -> dict[str, Any]:
    adx = _finite_float(row.get("adx"))
    plus_di = _finite_float(row.get("plus_di"))
    minus_di = _finite_float(row.get("minus_di"))
    if None in (adx, plus_di, minus_di):
        return {
            "status": "unavailable",
            "direction": None,
            "adx": None,
            "plus_di": None,
            "minus_di": None,
            "confirmed": False,
        }
    return {
        "status": "ready",
        "direction": _relative_direction(float(plus_di), float(minus_di)),
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "confirmed": float(adx) >= config.adx_threshold,
    }


def _volatility(row: pd.Series, config: DailyChecksConfigLike) -> dict[str, Any]:
    atr = _finite_float(row.get("atr"))
    atr_pct = _finite_float(row.get("atr_pct"))
    percentile = _finite_float(row.get("atr_percentile"))
    if None in (atr, atr_pct, percentile):
        return {
            "status": "unavailable",
            "atr": atr,
            "atr_pct": atr_pct,
            "percentile": percentile,
            "state": None,
        }
    if config.atr_normal_percentile_low <= float(percentile) <= config.atr_normal_percentile_high:
        state = "normal"
    elif float(percentile) < config.atr_normal_percentile_low:
        state = "low"
    else:
        state = "high"
    return {
        "status": "ready",
        "atr": atr,
        "atr_pct": atr_pct,
        "percentile": percentile,
        "state": state,
    }


def _volume(row: pd.Series, config: DailyChecksConfigLike) -> dict[str, Any]:
    relative = _finite_float(row.get("relative_volume"))
    baseline = _finite_float(row.get(f"volume_sma_{config.volume_lookback}_lag_1"))
    current = _finite_float(row.get("volume"))
    if None in (relative, baseline, current):
        return {
            "status": "unavailable",
            "volume": current,
            "baseline": baseline,
            "relative_volume": relative,
            "confirmed": False,
        }
    return {
        "status": "ready",
        "volume": current,
        "baseline": baseline,
        "relative_volume": relative,
        "confirmed": float(relative) >= config.volume_confirmation_ratio,
    }


def _rating(
    turtle: Mapping[str, Any],
    sma: Mapping[str, Any],
    ema: Mapping[str, Any],
    macd: Mapping[str, Any],
    trend: Mapping[str, Any],
    volatility: Mapping[str, Any],
    volume: Mapping[str, Any],
    config: DailyChecksConfigLike,
) -> dict[str, Any]:
    family_votes = {
        "turtle": turtle.get("direction"),
        "sma": sma.get("direction"),
        "ema": ema.get("direction"),
        "macd": macd.get("direction"),
    }
    directional = {value for value in family_votes.values() if value in {"long", "short"}}
    if len(directional) > 1 or any(value == "conflict" for value in family_votes.values()):
        return {
            "grade": "CONFLICT",
            "direction": None,
            "score": 0.0,
            "family_votes": family_votes,
            "aligned_families": [],
            "quality_adjustments": [],
        }
    if not directional:
        return {
            "grade": "NONE",
            "direction": None,
            "score": 0.0,
            "family_votes": family_votes,
            "aligned_families": [],
            "quality_adjustments": [],
        }
    direction = next(iter(directional))
    aligned = [name for name, value in family_votes.items() if value == direction]
    score = 0.0
    if family_votes["turtle"] == direction:
        score += 2.0
    if family_votes["sma"] == direction:
        score += 2.0
    if family_votes["ema"] == direction:
        score += 1.0
    if family_votes["macd"] == direction:
        score += 3.0
    adjustments: list[str] = []
    if trend.get("confirmed") and trend.get("direction") == direction:
        score += 1.0
        adjustments.append("adx_dmi_confirmed:+1")
    if volatility.get("state") == "normal":
        score += 1.0
        adjustments.append("atr_normal:+1")
    elif volatility.get("state") in {"low", "high"}:
        score -= 1.0
        adjustments.append("atr_extreme:-1")
    if volume.get("confirmed"):
        score += 1.0
        adjustments.append("relative_volume_confirmed:+1")
    if len(aligned) >= config.rating_a_min_families and score >= config.rating_a_min_score:
        grade = "A"
    elif len(aligned) >= config.rating_b_min_families and score >= config.rating_b_min_score:
        grade = "B"
    else:
        grade = "C"
    return {
        "grade": grade,
        "direction": direction,
        "score": score,
        "family_votes": family_votes,
        "aligned_families": aligned,
        "quality_adjustments": adjustments,
    }


def _unavailable_alignment(periods: tuple[int, ...]) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "direction": None,
        "periods": list(periods),
        "values": {str(period): None for period in periods},
        "reference_value": None,
    }


def _ordered_direction(values: list[float]) -> str:
    if all(left > right for left, right in zip(values, values[1:])):
        return "long"
    if all(left < right for left, right in zip(values, values[1:])):
        return "short"
    return "none"


def _relative_direction(left: float, right: float) -> str:
    return "long" if left > right else ("short" if left < right else "none")


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timeframe_label(sessions: int) -> str:
    return f"D{sessions}"


def _sessions_from_timeframe(timeframe: str) -> int:
    return int(timeframe.removeprefix("D"))


def _utc_timestamp(value: pd.Timestamp | str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def _stop_position(frame: pd.DataFrame, position: int) -> int:
    stop = position + 1 if position >= 0 else len(frame) + position + 1
    if stop < 1 or stop > len(frame):
        raise IndexError("daily strategy position is outside the prepared frame")
    return stop


def _empty_checks(config: DailyChecksConfigLike) -> dict[str, Any]:
    unavailable_sma = _unavailable_alignment(config.sma_periods)
    unavailable_ema = _unavailable_alignment(config.ema_periods)
    return {
        "sma_alignment": unavailable_sma,
        "ema_trend": unavailable_ema,
        "macd": {
            label: _macd_state(pd.DataFrame(), config)
            for label in (_timeframe_label(value) for value in config.macd_session_periods)
        },
        "macd_summary": {"status": "unavailable", "direction": None, "active_timeframes": []},
        "trend_quality": {
            "status": "unavailable",
            "direction": None,
            "adx": None,
            "plus_di": None,
            "minus_di": None,
            "confirmed": False,
        },
        "volatility": {
            "status": "unavailable",
            "atr": None,
            "atr_pct": None,
            "percentile": None,
            "state": None,
        },
        "volume": {
            "status": "unavailable",
            "volume": None,
            "baseline": None,
            "relative_volume": None,
            "confirmed": False,
        },
        "rating": {
            "grade": "NONE",
            "direction": None,
            "score": 0.0,
            "family_votes": {},
            "aligned_families": [],
            "quality_adjustments": [],
        },
        "signals": [],
    }


__all__ = [
    "DailyChecksConfigLike",
    "PreparedDailyStrategyChecks",
    "analyze_prepared_strategy_checks",
    "daily_strategy_feature_request",
    "prepare_daily_strategy_checks",
]
