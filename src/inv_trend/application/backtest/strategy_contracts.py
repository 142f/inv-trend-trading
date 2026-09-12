"""Compatibility adapters exposing maintained strategies through the domain protocol."""

from __future__ import annotations

from dataclasses import fields
from typing import Any, Mapping

import pandas as pd

from inv_trend.core.EMA趋势策略 import Config as EMAConfig, signals as ema_signals
from inv_trend.core.strategy.daily.signals import analyze_daily_signals
from inv_trend.domain.contracts import SignalResult


class EMAContractStrategy:
    strategy_id = "ema-crossover-v1"
    required_features = ("timestamp", "open", "high", "low", "close", "volume")
    parameter_schema = {
        item.name: str(item.type) for item in fields(EMAConfig)
    }
    _parameter_names = frozenset(parameter_schema)

    def compute_signals(
        self,
        completed_bars: pd.DataFrame,
        position: Any | None,
        config: Mapping[str, Any],
    ) -> SignalResult:
        parameters = {key: value for key, value in config.items() if key in self._parameter_names}
        frame = ema_signals(completed_bars, EMAConfig(**parameters))
        row = frame.iloc[-1]
        states = {
            str(side): [str(row[f"state_{side}_{index}"]) for index in range(3)]
            for side in (1, -1)
        }
        long_ready = any(value in {"确认成功", "持续有效"} for value in states["1"])
        short_ready = any(value in {"确认成功", "持续有效"} for value in states["-1"])
        action = "LONG" if long_ready and not short_ready else "SHORT" if short_ready and not long_ready else "WAIT"
        return SignalResult(
            strategy_id=self.strategy_id,
            symbol=str(config.get("symbol", "UNKNOWN")),
            observed_at=row["timestamp"],
            action=action,
            reason="EMA_RELATION_STATE",
            evidence={"states": states, "position_supplied": position is not None},
        )


class TurtleContractStrategy:
    strategy_id = "turtle-d1-v1"
    required_features = ("open", "high", "low", "close")
    parameter_schema = {"position": "int"}

    def compute_signals(
        self,
        completed_bars: pd.DataFrame,
        position: Any | None,
        config: Mapping[str, Any],
    ) -> SignalResult:
        analysis = analyze_daily_signals(completed_bars)
        events = tuple(analysis.get("signals", ()))
        directions = {str(item.get("direction", "")).upper() for item in events}
        action = "LONG" if directions == {"LONG"} else "SHORT" if directions == {"SHORT"} else "WAIT"
        latest = analysis.get("latest_bar") or {}
        return SignalResult(
            strategy_id=self.strategy_id,
            symbol=str(config.get("symbol", "UNKNOWN")),
            observed_at=latest.get("timestamp"),
            action=action,
            reason="TURTLE_D1_SIGNAL",
            evidence={"events": events, "status": analysis.get("status", {})},
        )


__all__ = ["EMAContractStrategy", "TurtleContractStrategy"]
