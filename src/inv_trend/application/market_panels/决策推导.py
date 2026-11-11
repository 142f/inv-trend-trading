"""Derive one symbol's decision-panel payload from completed D1 bars.

All rating / family-vote / volatility logic is delegated to
:mod:`inv_trend.core.strategy.daily.strategy_checks` so the panel shares the
daily chain's single scoring source.  This module only adds the panel
specific derivations that the daily chain does not emit: breakout distance,
stop levels, exit channels and the risk-budgeted share count.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from inv_trend.adapters.multi_asset.strategy.sizing import _risk_sized_qty
from inv_trend.core.strategy.daily.strategy_checks import (
    analyze_prepared_strategy_checks,
    prepare_daily_strategy_checks,
    turtle_entry_periods,
)

def derive_symbol_decision(
    bars: pd.DataFrame,
    prepared: pd.DataFrame,
    config,
    *,
    session_anchor: pd.Timestamp | str,
    equity: float,
    risk_pct: float,
    stop_n: float,
) -> dict[str, Any]:
    """Return the decision block for the latest completed bar.

    ``bars`` is the timestamp-indexed OHLCV frame, ``prepared`` the feature
    frame built from the same bars with a feature request whose donchian
    periods cover ``turtle_entry_periods(config)`` plus the exit period 10
    (the snapshot script's ``REQUEST`` guarantees both).
    """

    checks = prepare_daily_strategy_checks(
        bars, config, session_anchor=session_anchor, prepared_base=prepared,
    )
    state = analyze_prepared_strategy_checks(checks)
    rating = state["rating"]
    volatility = state["volatility"]
    volume = state["volume"]
    trend = state["trend_quality"]

    row = prepared.iloc[-1]
    close = _optional(row.get("close"))
    atr = _optional(row.get("atr"))
    stops = _stops(close, atr, stop_n)
    return {
        "family_votes": dict(rating.get("family_votes") or {}),
        "rating": {
            "grade": rating.get("grade"),
            "direction": rating.get("direction"),
            "score": _round(rating.get("score")),
            "aligned_families": list(rating.get("aligned_families") or []),
            "quality_adjustments": list(rating.get("quality_adjustments") or []),
        },
        "volatility": {
            "state": volatility.get("state"),
            "percentile": _round(volatility.get("percentile")),
        },
        "trend_quality": {
            "adx": _round(trend.get("adx")),
            "confirmed": bool(trend.get("confirmed")),
        },
        "volume": {
            "relative_volume": _round(volume.get("relative_volume")),
            "confirmed": bool(volume.get("confirmed")),
        },
        "breakouts": _breakouts(row, config),
        "stops": stops,
        "exits": _exits(row),
        "suggested_qty": {
            "long": _qty(equity, risk_pct, stop_n, atr),
            "short": _qty(equity, risk_pct, stop_n, atr),
        },
    }


def _breakouts(row: pd.Series, config) -> list[dict[str, Any]]:
    """Entry trigger per turtle entry period and direction.

    ``distance_pct`` is positive while the trigger has not been reached and
    negative once the close is beyond it; the comparison operators match
    ``_turtle_direction`` exactly (strict ``>`` / ``<``).
    """

    close = _optional(row.get("close"))
    entries: list[dict[str, Any]] = []
    for period in turtle_entry_periods(config):
        for direction, level_key in (("long", f"channel_high_{period}"), ("short", f"channel_low_{period}")):
            level = _optional(row.get(level_key))
            if close is None or level is None:
                entries.append({"period": period, "direction": direction, "level": None,
                                "distance_pct": None, "triggered": False})
                continue
            if direction == "long":
                distance = (level - close) / close * 100.0
                triggered = close > level
            else:
                distance = (close - level) / close * 100.0
                triggered = close < level
            entries.append({
                "period": period,
                "direction": direction,
                "level": _round(level),
                "distance_pct": _round(distance, 2),
                "triggered": bool(triggered),
            })
    return entries


def _stops(close: float | None, atr: float | None, stop_n: float) -> dict[str, float | None]:
    if close is None or atr is None:
        return {"long": None, "short": None}
    return {
        "long": _round(close - stop_n * atr),
        "short": _round(close + stop_n * atr),
    }


def _exits(row: pd.Series) -> dict[str, float | None]:
    return {
        "long_fast": _round(row.get("channel_low_10")),
        "long_slow": _round(row.get("channel_low_20")),
        "short_fast": _round(row.get("channel_high_10")),
        "short_slow": _round(row.get("channel_high_20")),
    }


def _qty(equity: float, risk_pct: float, stop_n: float, atr: float | None) -> int:
    if atr is None:
        return 0
    return int(_risk_sized_qty(float(equity), float(risk_pct), stop_n * atr, 1.0, 1.0))


def _optional(value) -> float | None:
    if value is None or pd.isna(value):
        return None
    return float(value)


def _round(value, digits: int = 4):
    if value is None or pd.isna(value):
        return None
    return round(float(value), digits)
