"""Causal D1 technical-signal calculations.

This module consumes only an in-memory OHLCV frame and core feature helpers.
It deliberately has no adapter, application, persistence, or presentation
dependency, so one prepared frame can be reused by scans, reports, and tests.
"""

from __future__ import annotations

import math
from typing import Any, Mapping

import numpy as np
import pandas as pd

from ...features import FeatureRequest, PreparedBars
from ...signals import SignalEvent, crossed_above, crossed_below


TURTLE_PERIODS = (20, 55)
SMA_FAST_PERIOD = 10
SMA_SLOW_PERIOD = 20
MACD_FAST_PERIOD = 12
MACD_SLOW_PERIOD = 26
MACD_SIGNAL_PERIOD = 9


def daily_signal_feature_request() -> FeatureRequest:
    """Return the exact D1 feature contract required by legacy daily signals."""

    return FeatureRequest(
        donchian_periods=TURTLE_PERIODS,
        sma_lags=((SMA_FAST_PERIOD, 0), (SMA_SLOW_PERIOD, 0)),
        macd_periods=(MACD_FAST_PERIOD, MACD_SLOW_PERIOD, MACD_SIGNAL_PERIOD),
    )


def normalize_completed_daily_bars(bars: pd.DataFrame) -> pd.DataFrame:
    """Normalize canonical input once before a merged feature request is built."""

    return _completed_bars(bars)


def prepare_daily_signal_frame(bars: pd.DataFrame) -> pd.DataFrame:
    """Return completed, UTC-indexed bars with causal daily indicators.

    ``bars`` accepts a frame with a canonical timestamp column or a
    ``DatetimeIndex``. Incomplete rows are excluded before every calculation.
    Donchian channels are shifted by one bar; moving averages and MACD use the
    close of the evaluated row.
    """

    out = _completed_bars(bars)
    if out.empty:
        return out

    prepared = PreparedBars.build(out, daily_signal_feature_request()).frame
    out = prepared.copy()
    out[f"sma_{SMA_FAST_PERIOD}"] = out[f"sma_{SMA_FAST_PERIOD}_lag_0"]
    out[f"sma_{SMA_SLOW_PERIOD}"] = out[f"sma_{SMA_SLOW_PERIOD}_lag_0"]
    return out


def analyze_daily_signals(bars: pd.DataFrame) -> dict[str, Any]:
    """Analyze the latest completed D1 bar and return a JSON-safe mapping."""

    return analyze_prepared_daily_signals(prepare_daily_signal_frame(bars))


def analyze_prepared_daily_signals(
    prepared: pd.DataFrame, position: int = -1
) -> dict[str, Any]:
    """Evaluate one row of an already prepared frame without recomputing features."""

    if prepared.empty:
        return _empty_analysis(input_bars=0)
    stop = position + 1 if position >= 0 else len(prepared) + position + 1
    if stop < 1 or stop > len(prepared):
        raise IndexError("daily signal position is outside the prepared frame")
    prepared = prepared.iloc[:stop]
    count = len(prepared)

    row = prepared.iloc[-1]
    previous = prepared.iloc[-2] if count > 1 else None
    signal_time = _timestamp_text(prepared.index[-1])

    turtle_20 = _turtle_result(prepared, 20)
    turtle_55 = _turtle_result(prepared, 55)
    sma = _sma_result(row, previous, count)
    macd = _macd_result(row, previous, count)
    indicators = {
        "turtle_20": turtle_20,
        "turtle_55": turtle_55,
        "sma_10_20": sma,
        "macd_12_26_9": macd,
    }

    signals = _event_payloads(indicators, signal_time)
    resonance = _resonance(turtle_20, turtle_55, sma, macd)
    unavailable = [
        name for name, result in indicators.items() if result["status"] != "ready"
    ]
    signal_ready = (
        turtle_20["status"] == "ready"
        and turtle_55["status"] == "ready"
        and sma["cross_status"] == "ready"
        and macd["cross_status"] == "ready"
    )
    return {
        "latest_bar": _latest_bar_payload(row, signal_time),
        "indicators": indicators,
        "signals": signals,
        "resonance": resonance,
        "status": {
            "state": "ready" if signal_ready else "warming_up",
            "input_bars": count,
            "completed_bars": count,
            "latest_bar_time": signal_time,
            "unavailable_indicators": unavailable,
            "signal_ready": signal_ready,
        },
    }


def build_daily_signal_events(
    analysis: Mapping[str, Any],
    *,
    instrument_id: str,
    symbol: str,
    market: str,
    dataset_version: str,
    detected_at: str,
    refresh_failed: bool,
    strategy_version: str,
) -> list[SignalEvent]:
    """Project prepared D1 evidence into stable, persistence-ready events.

    Inputs are already-calculated strategy evidence and scalar identity/audit
    values only.  This core helper deliberately performs no data access,
    database work, notification, or rendering.
    """

    latest_bar = analysis.get("latest_bar")
    close = latest_bar.get("close") if isinstance(latest_bar, Mapping) else None
    if close is None:
        return []
    indicator_values = analysis.get("indicators")
    indicators = indicator_values if isinstance(indicator_values, Mapping) else {}
    raw_events = analysis.get("signals")
    if not isinstance(raw_events, (list, tuple)):
        return []
    events: list[SignalEvent] = []
    for raw in raw_events:
        if not isinstance(raw, Mapping):
            continue
        indicator_name = str(raw["indicator"])
        event_name = str(raw["event"])
        direction = str(raw["direction"])
        signal_type, default_parameters = _signal_identity(
            indicator_name, event_name, direction
        )
        raw_indicator = indicators.get(indicator_name)
        indicator = raw_indicator if isinstance(raw_indicator, Mapping) else {}
        raw_parameters = raw.get("parameters")
        parameters = (
            dict(raw_parameters)
            if isinstance(raw_parameters, Mapping) and raw_parameters
            else default_parameters
        )
        reference_value = raw.get("reference_value")
        if reference_value is None:
            reference_value = _reference_value(indicator_name, indicator)
        events.append(
            SignalEvent.create(
                strategy_version=strategy_version,
                instrument_id=instrument_id,
                symbol=symbol,
                timeframe=str(raw.get("timeframe", "D1")),
                signal_type=signal_type,
                direction=direction.upper(),
                signal_time=str(raw["signal_time"]),
                detected_at=detected_at,
                trigger_price=float(close),
                reference_value=reference_value,
                dataset_version=dataset_version,
                indicator_name=indicator_name,
                indicator_parameters=parameters,
                metadata={"market": market, "refresh_failed": refresh_failed},
            )
        )
    return events


def _completed_bars(bars: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(bars, pd.DataFrame):
        raise TypeError("bars must be a pandas DataFrame")
    required = {"high", "low", "close"}
    missing = sorted(required - set(bars.columns))
    if missing:
        raise ValueError(f"bars require columns: {missing}")

    out = bars.copy()
    if "is_complete" in out:
        out = out.loc[_complete_mask(out["is_complete"])].copy()
    if out.empty:
        out.index = pd.DatetimeIndex([], tz="UTC", name="timestamp")
        return out

    timestamps = _timestamps(out)
    if timestamps.isna().any():
        raise ValueError("bars contain invalid timestamps")
    out["_daily_signal_timestamp"] = timestamps.to_numpy()
    out = out.sort_values("_daily_signal_timestamp", kind="stable")
    if out["_daily_signal_timestamp"].duplicated().any():
        raise ValueError("bars require unique timestamps")
    out.index = pd.DatetimeIndex(
        out.pop("_daily_signal_timestamp"), tz="UTC", name="timestamp"
    )

    for column in ("high", "low", "close"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    values = out[["high", "low", "close"]].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("bars contain missing or non-finite high/low/close values")
    if bool((out["high"] < out["low"]).any()):
        raise ValueError("bars contain high values below low values")
    return out


def _complete_mask(values: pd.Series) -> pd.Series:
    if pd.api.types.is_bool_dtype(values):
        return values.fillna(False).astype(bool)
    if pd.api.types.is_numeric_dtype(values):
        return values.fillna(0).astype(bool)
    return values.astype("string").str.strip().str.lower().isin({"1", "true", "yes", "y"})


def _timestamps(bars: pd.DataFrame) -> pd.DatetimeIndex:
    timestamp_column = next(
        (column for column in ("timestamp", "time", "date", "datetime") if column in bars),
        None,
    )
    values: Any
    if timestamp_column is not None:
        values = bars[timestamp_column]
    elif isinstance(bars.index, pd.DatetimeIndex):
        values = bars.index
    else:
        raise ValueError("bars require a timestamp column or DatetimeIndex")
    return pd.DatetimeIndex(pd.to_datetime(values, utc=True, errors="coerce"))


def _turtle_result(prepared: pd.DataFrame, period: int) -> dict[str, Any]:
    row = prepared.iloc[-1]
    high = _json_float(row[f"channel_high_{period}"])
    low = _json_float(row[f"channel_low_{period}"])
    close = _json_float(row["close"])
    if high is None or low is None or close is None:
        return {
            "status": "unavailable",
            "completed_bars": len(prepared),
            "required_completed_bars": period + 1,
            "channel_high": high,
            "channel_low": low,
            "direction": None,
            "event": None,
            "breakout_level": None,
        }

    if close > high:
        direction, event, level = "long", "breakout_up", high
    elif close < low:
        direction, event, level = "short", "breakout_down", low
    else:
        direction, event, level = "none", None, None
    return {
        "status": "ready",
        "completed_bars": len(prepared),
        "required_completed_bars": period + 1,
        "channel_high": high,
        "channel_low": low,
        "direction": direction,
        "event": event,
        "breakout_level": level,
    }


def _sma_result(
    row: pd.Series, previous: pd.Series | None, completed_bars: int
) -> dict[str, Any]:
    current_fast = _json_float(row[f"sma_{SMA_FAST_PERIOD}"])
    current_slow = _json_float(row[f"sma_{SMA_SLOW_PERIOD}"])
    previous_fast = _json_float(
        previous[f"sma_{SMA_FAST_PERIOD}"] if previous is not None else None
    )
    previous_slow = _json_float(
        previous[f"sma_{SMA_SLOW_PERIOD}"] if previous is not None else None
    )
    ready = current_fast is not None and current_slow is not None
    cross_ready = ready and previous_fast is not None and previous_slow is not None
    direction = _relative_direction(current_fast, current_slow) if ready else None
    event = _cross_event(previous_fast, previous_slow, current_fast, current_slow)
    return {
        "status": "ready" if ready else "unavailable",
        "cross_status": "ready" if cross_ready else "unavailable",
        "completed_bars": completed_bars,
        "required_completed_bars": SMA_SLOW_PERIOD,
        "cross_required_completed_bars": SMA_SLOW_PERIOD + 1,
        "sma_10": current_fast,
        "sma_20": current_slow,
        "previous_sma_10": previous_fast,
        "previous_sma_20": previous_slow,
        "direction": direction,
        "event": event,
    }


def _macd_result(
    row: pd.Series, previous: pd.Series | None, completed_bars: int
) -> dict[str, Any]:
    minimum = MACD_SLOW_PERIOD + MACD_SIGNAL_PERIOD - 1
    cross_minimum = minimum + 1
    ready = completed_bars >= minimum
    cross_ready = completed_bars >= cross_minimum
    ema_12 = _json_float(row["ema_12"]) if ready else None
    ema_26 = _json_float(row["ema_26"]) if ready else None
    dif = _json_float(row["dif"]) if ready else None
    dea = _json_float(row["dea"]) if ready else None
    histogram = _json_float(row["histogram"]) if ready else None
    macd_bar = _json_float(row["macd_bar"]) if ready else None
    previous_dif = _json_float(previous["dif"]) if cross_ready and previous is not None else None
    previous_dea = _json_float(previous["dea"]) if cross_ready and previous is not None else None
    direction = _relative_direction(dif, dea) if ready else None
    event = _cross_event(previous_dif, previous_dea, dif, dea)
    return {
        "status": "ready" if ready else "unavailable",
        "cross_status": "ready" if cross_ready else "unavailable",
        "completed_bars": completed_bars,
        "required_completed_bars": minimum,
        "cross_required_completed_bars": cross_minimum,
        "ema_12": ema_12,
        "ema_26": ema_26,
        "dif": dif,
        "dea": dea,
        "histogram": histogram,
        "macd_bar": macd_bar,
        "macd": macd_bar,
        "previous_dif": previous_dif,
        "previous_dea": previous_dea,
        "direction": direction,
        "event": event,
    }


def _event_payloads(
    indicators: dict[str, dict[str, Any]], signal_time: str
) -> list[dict[str, Any]]:
    signals: list[dict[str, Any]] = []
    for indicator, result in indicators.items():
        event = result["event"]
        if event is None:
            continue
        payload: dict[str, Any] = {
            "indicator": indicator,
            "event": event,
            "direction": result["direction"],
            "signal_time": signal_time,
        }
        if indicator.startswith("turtle_"):
            payload["breakout_level"] = result["breakout_level"]
        signals.append(payload)
    return signals


def _signal_identity(
    indicator: str, event: str, direction: str | None = None
) -> tuple[str, dict[str, Any]]:
    if indicator == "turtle_20":
        return "TURTLE_20_BREAKOUT", {"period": 20, "confirmation": "close"}
    if indicator == "turtle_55":
        return "TURTLE_55_BREAKOUT", {"period": 55, "confirmation": "close"}
    if indicator == "sma_10_20":
        return (
            "MA_GOLDEN_CROSS_10_20" if event == "golden_cross" else "MA_DEATH_CROSS_10_20",
            {"fast": 10, "slow": 20, "average": "SMA"},
        )
    if indicator == "macd_12_26_9":
        return (
            "MACD_GOLDEN_CROSS" if event == "golden_cross" else "MACD_DEATH_CROSS",
            {"fast": 12, "slow": 26, "signal": 9, "adjust": False},
        )
    if indicator == "sma_alignment":
        return (
            "SMA_STACK_BULLISH" if direction == "long" else "SMA_STACK_BEARISH",
            {"average": "SMA"},
        )
    if indicator == "ema_trend":
        return (
            "EMA_144_169_BULLISH" if direction == "long" else "EMA_144_169_BEARISH",
            {"average": "EMA", "fast": 144, "slow": 169},
        )
    if indicator == "strategy_rating":
        return (
            "STRATEGY_GRADE_A_LONG" if direction == "long" else "STRATEGY_GRADE_A_SHORT",
            {"grade": "A"},
        )
    raise ValueError(f"unsupported daily signal indicator: {indicator}")


def _reference_value(name: str, indicator: Mapping[str, Any]) -> float | None:
    if name.startswith("turtle_"):
        return _json_float(indicator.get("breakout_level"))
    if name == "sma_10_20":
        return _json_float(indicator.get("sma_20"))
    if name == "macd_12_26_9":
        return _json_float(indicator.get("dea"))
    return None


def _resonance(
    turtle_20: dict[str, Any],
    turtle_55: dict[str, Any],
    sma: dict[str, Any],
    macd: dict[str, Any],
) -> dict[str, Any]:
    turtle_values = [
        result["direction"]
        for result in (turtle_20, turtle_55)
        if result["status"] == "ready" and result["direction"] != "none"
    ]
    turtle_directions = set(turtle_values)
    turtle_direction: str | None
    if len(turtle_directions) == 1:
        turtle_direction = next(iter(turtle_directions))
    elif len(turtle_directions) > 1:
        turtle_direction = "conflict"
    else:
        turtle_direction = "none"

    components = {
        "turtle": turtle_direction,
        "turtle_20": turtle_20["direction"],
        "turtle_55": turtle_55["direction"],
        "sma_10_20": sma["direction"],
        "macd_12_26_9": macd["direction"],
    }
    unavailable = []
    if turtle_20["status"] != "ready":
        unavailable.append("turtle_20")
    if sma["status"] != "ready":
        unavailable.append("sma_10_20")
    if macd["status"] != "ready":
        unavailable.append("macd_12_26_9")
    if unavailable:
        return {
            "status": "unavailable",
            "components": components,
            "unavailable_indicators": unavailable,
        }

    directions = {
        direction
        for direction in (turtle_direction, sma["direction"], macd["direction"])
        if direction in {"long", "short"}
    }
    if turtle_direction == "conflict" or len(directions) > 1:
        return {
            "status": "conflict",
            "components": components,
            "unavailable_indicators": [],
        }
    if (
        turtle_direction == "long"
        and sma["direction"] == "long"
        and macd["direction"] == "long"
    ):
        return {
            "status": "bullish",
            "direction": "long",
            "components": components,
            "unavailable_indicators": [],
        }
    if (
        turtle_direction == "short"
        and sma["direction"] == "short"
        and macd["direction"] == "short"
    ):
        return {
            "status": "bearish",
            "direction": "short",
            "components": components,
            "unavailable_indicators": [],
        }
    return {
        "status": "none",
        "components": components,
        "unavailable_indicators": [],
    }


def _latest_bar_payload(row: pd.Series, timestamp: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "timestamp": timestamp,
        "high": _json_float(row["high"]),
        "low": _json_float(row["low"]),
        "close": _json_float(row["close"]),
        "is_complete": True,
    }
    for column in ("open", "volume"):
        if column in row:
            payload[column] = _json_float(row[column])
    if "bar_end" in row:
        payload["bar_end"] = _timestamp_or_none(row["bar_end"])
    return payload


def _empty_analysis(*, input_bars: int) -> dict[str, Any]:
    def turtle(period: int) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "completed_bars": 0,
            "required_completed_bars": period + 1,
            "channel_high": None,
            "channel_low": None,
            "direction": None,
            "event": None,
            "breakout_level": None,
        }

    sma = {
        "status": "unavailable",
        "cross_status": "unavailable",
        "completed_bars": 0,
        "required_completed_bars": SMA_SLOW_PERIOD,
        "cross_required_completed_bars": SMA_SLOW_PERIOD + 1,
        "sma_10": None,
        "sma_20": None,
        "previous_sma_10": None,
        "previous_sma_20": None,
        "direction": None,
        "event": None,
    }
    macd = {
        "status": "unavailable",
        "cross_status": "unavailable",
        "completed_bars": 0,
        "required_completed_bars": MACD_SLOW_PERIOD + MACD_SIGNAL_PERIOD - 1,
        "cross_required_completed_bars": MACD_SLOW_PERIOD + MACD_SIGNAL_PERIOD,
        "ema_12": None,
        "ema_26": None,
        "dif": None,
        "dea": None,
        "histogram": None,
        "macd_bar": None,
        "macd": None,
        "previous_dif": None,
        "previous_dea": None,
        "direction": None,
        "event": None,
    }
    indicators = {
        "turtle_20": turtle(20),
        "turtle_55": turtle(55),
        "sma_10_20": sma,
        "macd_12_26_9": macd,
    }
    return {
        "latest_bar": None,
        "indicators": indicators,
        "signals": [],
        "resonance": {
            "status": "unavailable",
            "components": {
                "turtle": None,
                "turtle_20": None,
                "turtle_55": None,
                "sma_10_20": None,
                "macd_12_26_9": None,
            },
            "unavailable_indicators": ["turtle_20", "sma_10_20", "macd_12_26_9"],
        },
        "status": {
            "state": "insufficient_history",
            "input_bars": input_bars,
            "completed_bars": 0,
            "latest_bar_time": None,
            "unavailable_indicators": list(indicators),
            "signal_ready": False,
        },
    }


def _relative_direction(left: float | None, right: float | None) -> str | None:
    if left is None or right is None:
        return None
    if left > right:
        return "long"
    if left < right:
        return "short"
    return "none"


def _cross_event(
    previous_fast: float | None,
    previous_slow: float | None,
    current_fast: float | None,
    current_slow: float | None,
) -> str | None:
    if None in (previous_fast, previous_slow, current_fast, current_slow):
        return None
    if crossed_above(previous_fast, previous_slow, current_fast, current_slow):
        return "golden_cross"
    if crossed_below(previous_fast, previous_slow, current_fast, current_slow):
        return "death_cross"
    return None


def _json_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_text(value: Any) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat()


def _timestamp_or_none(value: Any) -> str | None:
    if pd.isna(value):
        return None
    return _timestamp_text(value)


__all__ = [
    "analyze_daily_signals",
    "analyze_prepared_daily_signals",
    "daily_signal_feature_request",
    "normalize_completed_daily_bars",
    "prepare_daily_signal_frame",
]
