"""Causal Turtle detector and multi-timeframe scanner."""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping

import numpy as np
import pandas as pd

from inv_trend.core.features import FeatureCache, FeatureRequest
from inv_trend.core.math_utils import rolling_percentile

from ..alerts.notifier import Notifier
from ..data.calendar import timeframe_rank, validate_market_calendar
from ..data.normalizer import normalize_bars
from ..models import (
    AssetConfig,
    DetectionResult,
    DetectorState,
    Direction,
    PositionStatus,
    SignalType,
    StrategyConfig,
    TurtleSignal,
)
from ..risk.stops import initial_stop, next_add_price
from ..signals.filters import evaluate_filters
from ..storage.signal_repository import InMemorySignalRepository, SignalRepository
from .state_machine import (
    add_unit,
    close_position,
    confirm_pending,
    confirmation_for,
    enter_position,
)


class TurtleScanner:
    def __init__(
        self,
        config: StrategyConfig | None = None,
        repository: SignalRepository | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.config = config or StrategyConfig()
        self.repository = repository or InMemorySignalRepository()
        self.notifier = notifier
        self._features = FeatureCache()

    def prepare(
        self,
        bars: pd.DataFrame,
        asset: AssetConfig,
        timeframe: str,
    ) -> pd.DataFrame:
        out = normalize_bars(bars, asset, timeframe)
        validate_market_calendar(out.index, asset, timeframe)
        periods = {
            self.config.system1_entry,
            self.config.system2_entry,
            self.config.system1_exit,
            self.config.system2_exit,
        }
        request = FeatureRequest.turtle(
            atr_period=self.config.atr_period,
            channel_periods=periods,
            sma_lags=((period, 1) for period in {
                self.config.trend_ma_period,
                self.config.long_trend_ma_period,
            }),
        )
        out = self._features.prepare(out, request).frame.copy()
        out["atr_pct"] = out["atr"] / out["close"]
        out["volatility_percentile"] = rolling_percentile(
            out["atr_pct"],
            self.config.volatility_lookback,
        )
        out["volume_mean"] = (
            out["volume"].rolling(self.config.volume_lookback).mean().shift(1)
        )
        out["previous_close"] = out["close"].shift(1)
        for period in {self.config.trend_ma_period, self.config.long_trend_ma_period}:
            out[f"sma_{period}"] = out[f"sma_{period}_lag_1"]
        return out

    def detect(
        self,
        bars: pd.DataFrame,
        asset: AssetConfig,
        timeframe: str,
        state: DetectorState | None = None,
    ) -> DetectionResult:
        prepared = self.prepare(bars, asset, timeframe)
        return self.detect_prepared(prepared, asset, timeframe, state)

    def detect_prepared(
        self,
        prepared: pd.DataFrame,
        asset: AssetConfig,
        timeframe: str,
        state: DetectorState | None = None,
    ) -> DetectionResult:
        """Advance the state machine using an already prepared causal frame.

        This is the shared chronological boundary for candidate detection and
        backtests.  It prevents both callers from recomputing rolling features
        for every decision while preserving :meth:`detect` as the compatible
        raw-bar public entry point.
        """
        if prepared.empty:
            raise ValueError("cannot scan empty bars")
        return self.detect_row(prepared.iloc[-1], asset, timeframe, state)

    def detect_row(
        self,
        row: pd.Series,
        asset: AssetConfig,
        timeframe: str,
        state: DetectorState | None = None,
    ) -> DetectionResult:
        """Advance one causal prepared row without allocating a frame slice."""
        state = state or DetectorState(asset.symbol, timeframe.upper())
        if state.symbol != asset.symbol or state.timeframe != timeframe.upper():
            raise ValueError("state identity does not match asset/timeframe")

        signal_time = pd.Timestamp(row.name).isoformat()
        if state.last_processed_time == signal_time:
            return DetectionResult(
                self._no_signal(row, asset, timeframe, state),
                state,
                False,
            )
        next_state = replace(state, last_processed_time=signal_time)
        if not np.isfinite(float(row.get("atr", np.nan))):
            return DetectionResult(
                self._no_signal(row, asset, timeframe, next_state),
                next_state,
                next_state != state,
            )

        if state.status is PositionStatus.PENDING_CONFIRMATION:
            signal, next_state = self._pending_signal(
                row, asset, timeframe, next_state
            )
        elif state.status is PositionStatus.ENTERED:
            next_state = replace(next_state, holding_bars=state.holding_bars + 1)
            signal, next_state = self._position_signal(
                row, asset, timeframe, next_state
            )
        else:
            signal, next_state = self._flat_signal(
                row, asset, timeframe, next_state
            )
        return DetectionResult(signal, next_state, next_state != state)

    def scan_and_store(
        self,
        bars: pd.DataFrame,
        asset: AssetConfig,
        timeframe: str,
    ) -> TurtleSignal | None:
        state = self.repository.load_state(asset.symbol, timeframe.upper())
        result = self.detect(bars, asset, timeframe, state)
        if result.state_changed:
            self.repository.save_state(result.state)
        signal = result.signal
        if signal.signal_type is SignalType.NO_SIGNAL:
            return None
        if not self.repository.is_new(signal):
            return None
        self.repository.save_signal(signal)
        if self.notifier is not None:
            self.notifier.notify(signal)
        return signal

    def scan_multi_timeframe(
        self,
        frames: Mapping[str, pd.DataFrame],
        asset: AssetConfig,
    ) -> dict[str, DetectionResult]:
        results: dict[str, DetectionResult] = {}
        for timeframe, bars in frames.items():
            state = self.repository.load_state(asset.symbol, timeframe.upper())
            results[timeframe.upper()] = self.detect(
                bars,
                asset,
                timeframe,
                state,
            )
        directional = [
            (timeframe, result.signal.direction)
            for timeframe, result in results.items()
            if result.signal.direction is not Direction.NONE
        ]
        alignment = _alignment_description(directional)
        return {
            timeframe: replace(
                result,
                signal=replace(
                    result.signal,
                    metadata={**result.signal.metadata, "timeframe_alignment": alignment},
                ),
            )
            for timeframe, result in results.items()
        }

    def _flat_signal(
        self,
        row: pd.Series,
        asset: AssetConfig,
        timeframe: str,
        state: DetectorState,
    ) -> tuple[TurtleSignal, DetectorState]:
        atr = float(row["atr"])
        mode = self.config.confirmation_mode
        long_price = float(row["close"] if mode == "close" else row["high"])
        short_price = float(row["close"] if mode == "close" else row["low"])
        s1_high = float(row.get(f"channel_high_{self.config.system1_entry}", np.nan))
        s1_low = float(row.get(f"channel_low_{self.config.system1_entry}", np.nan))
        s2_high = float(row.get(f"channel_high_{self.config.system2_entry}", np.nan))
        s2_low = float(row.get(f"channel_low_{self.config.system2_entry}", np.nan))

        candidates: list[tuple[int, Direction, float, float]] = []
        if np.isfinite(s2_high) and long_price > s2_high:
            candidates.append((2, Direction.LONG, long_price, s2_high))
        if np.isfinite(s2_low) and short_price < s2_low and asset.allow_short:
            candidates.append((2, Direction.SHORT, short_price, s2_low))
        if not candidates:
            if np.isfinite(s1_high) and long_price > s1_high:
                candidates.append((1, Direction.LONG, long_price, s1_high))
            if np.isfinite(s1_low) and short_price < s1_low and asset.allow_short:
                candidates.append((1, Direction.SHORT, short_price, s1_low))
        if len(candidates) > 1:
            signal = self._build_signal(
                row,
                asset,
                timeframe,
                SignalType.NO_SIGNAL,
                SignalType.NO_SIGNAL,
                Direction.NONE,
                float(row["close"]),
                None,
                state,
                ("ambiguous_two_sided_intrabar_breakout",),
            )
            return signal, state
        if candidates:
            system, direction, price, level = candidates[0]
            raw_type = (
                SignalType.SYSTEM1_BREAKOUT
                if system == 1
                else SignalType.SYSTEM2_BREAKOUT
            )
            distance = abs(price - level) / atr
            filtered = list(evaluate_filters(row, direction, asset, self.config))
            if (
                system == 1
                and self.config.skip_system1_after_win
                and state.last_system1_won
            ):
                filtered.append("system1_skipped_after_winner")
            signal_type = raw_type
            if distance > self.config.overextended_atr:
                signal_type = SignalType.OVEREXTENDED
                filtered.append("breakout_overextended")
            tradeable = not filtered
            pending = mode == "intraday" and tradeable
            next_state = state
            if tradeable:
                next_state = enter_position(
                    state,
                    direction,
                    system,
                    row.name.isoformat(),
                    price,
                    level,
                    atr,
                    self.config,
                    pending,
                )
                next_state.last_processed_time = row.name.isoformat()
            return (
                self._build_signal(
                    row,
                    asset,
                    timeframe,
                    signal_type,
                    raw_type,
                    direction,
                    price,
                    level,
                    next_state,
                    tuple(filtered),
                ),
                next_state,
            )

        distances: list[tuple[float, Direction, float]] = []
        if np.isfinite(s1_high):
            distances.append(((s1_high - float(row["close"])) / atr, Direction.LONG, s1_high))
        if np.isfinite(s1_low) and asset.allow_short:
            distances.append(((float(row["close"]) - s1_low) / atr, Direction.SHORT, s1_low))
        distances = [item for item in distances if 0 <= item[0] <= self.config.approaching_atr]
        if distances:
            _, direction, level = min(distances, key=lambda item: item[0])
            return (
                self._build_signal(
                    row,
                    asset,
                    timeframe,
                    SignalType.APPROACHING_BREAKOUT,
                    SignalType.APPROACHING_BREAKOUT,
                    direction,
                    float(row["close"]),
                    level,
                    state,
                    (),
                ),
                state,
            )
        return self._no_signal(row, asset, timeframe, state), state

    def _pending_signal(
        self,
        row: pd.Series,
        asset: AssetConfig,
        timeframe: str,
        state: DetectorState,
    ) -> tuple[TurtleSignal, DetectorState]:
        close = float(row["close"])
        level = float(state.breakout_level)
        confirmed = (
            close > level
            if state.direction is Direction.LONG
            else close < level
        )
        if confirmed:
            next_state = confirm_pending(state, row.name.isoformat(), close)
            return (
                self._build_signal(
                    row,
                    asset,
                    timeframe,
                    SignalType.CLOSE_CONFIRMED,
                    (
                        SignalType.SYSTEM1_BREAKOUT
                        if state.system == 1
                        else SignalType.SYSTEM2_BREAKOUT
                    ),
                    state.direction,
                    close,
                    level,
                    next_state,
                    evaluate_filters(row, state.direction, asset, self.config),
                ),
                next_state,
            )
        next_state = close_position(state, close, invalidated=True)
        return (
            self._build_signal(
                row,
                asset,
                timeframe,
                SignalType.FALSE_BREAKOUT,
                SignalType.FALSE_BREAKOUT,
                state.direction,
                close,
                level,
                state,
                (),
            ),
            next_state,
        )

    def _position_signal(
        self,
        row: pd.Series,
        asset: AssetConfig,
        timeframe: str,
        state: DetectorState,
    ) -> tuple[TurtleSignal, DetectorState]:
        close = float(row["close"])
        high = float(row["high"])
        low = float(row["low"])
        atr = float(row["atr"])
        direction = state.direction
        level = state.breakout_level
        stop = float(state.stop_price)

        stop_hit = low <= stop if direction is Direction.LONG else high >= stop
        if stop_hit:
            next_state = close_position(state, stop)
            return (
                self._build_signal(
                    row, asset, timeframe, SignalType.ATR_STOP,
                    SignalType.ATR_STOP, direction, stop, level, state, ()
                ),
                next_state,
            )

        exit_period = (
            self.config.system1_exit if state.system == 1 else self.config.system2_exit
        )
        exit_high = float(row.get(f"channel_high_{exit_period}", np.nan))
        exit_low = float(row.get(f"channel_low_{exit_period}", np.nan))
        exit_hit = (
            direction is Direction.LONG and np.isfinite(exit_low) and close < exit_low
        ) or (
            direction is Direction.SHORT and np.isfinite(exit_high) and close > exit_high
        )
        if exit_hit:
            next_state = close_position(state, close)
            return (
                self._build_signal(
                    row, asset, timeframe, SignalType.EXIT_SIGNAL,
                    SignalType.EXIT_SIGNAL, direction, close, level, state, ()
                ),
                next_state,
            )

        if (
            level is not None
            and state.holding_bars <= self.config.false_breakout_bars
            and (
                (direction is Direction.LONG and close <= level)
                or (direction is Direction.SHORT and close >= level)
            )
        ):
            next_state = close_position(state, close, invalidated=True)
            return (
                self._build_signal(
                    row, asset, timeframe, SignalType.FALSE_BREAKOUT,
                    SignalType.FALSE_BREAKOUT, direction, close, level, state, ()
                ),
                next_state,
            )

        add_level = state.next_add_price
        add_hit = add_level is not None and (
            (direction is Direction.LONG and high >= add_level)
            or (direction is Direction.SHORT and low <= add_level)
        )
        if add_hit and state.additions < self.config.max_additions:
            next_state = add_unit(state, float(add_level), atr, self.config)
            return (
                self._build_signal(
                    row, asset, timeframe, SignalType.PYRAMID_ADD,
                    SignalType.PYRAMID_ADD, direction, float(add_level), level,
                    next_state, ()
                ),
                next_state,
            )

        if level is not None:
            tolerance = self.config.retest_tolerance_atr * atr
            retest = (
                direction is Direction.LONG
                and low <= level + tolerance
                and close > level
            ) or (
                direction is Direction.SHORT
                and high >= level - tolerance
                and close < level
            )
            if retest:
                return (
                    self._build_signal(
                        row, asset, timeframe, SignalType.RETEST_CONFIRMED,
                        SignalType.RETEST_CONFIRMED, direction, close, level, state, ()
                    ),
                    state,
                )

        if self.config.trend_filter:
            ma = float(row.get(f"sma_{self.config.trend_ma_period}", np.nan))
            invalid = np.isfinite(ma) and (
                (direction is Direction.LONG and close < ma)
                or (direction is Direction.SHORT and close > ma)
            )
            if invalid:
                next_state = close_position(state, close, invalidated=True)
                return (
                    self._build_signal(
                        row, asset, timeframe, SignalType.TREND_INVALIDATED,
                        SignalType.TREND_INVALIDATED, direction, close, level,
                        state, ()
                    ),
                    next_state,
                )
        if (
            self.config.max_holding_bars > 0
            and state.holding_bars >= self.config.max_holding_bars
        ):
            next_state = close_position(state, close)
            return (
                self._build_signal(
                    row, asset, timeframe, SignalType.EXIT_SIGNAL,
                    SignalType.EXIT_SIGNAL, direction, close, level, state,
                    ("maximum_holding_period",)
                ),
                next_state,
            )
        return self._no_signal(row, asset, timeframe, state), state

    def _build_signal(
        self,
        row: pd.Series,
        asset: AssetConfig,
        timeframe: str,
        signal_type: SignalType,
        raw_type: SignalType,
        direction: Direction,
        trigger_price: float,
        breakout_level: float | None,
        state: DetectorState,
        filtered_reasons: tuple[str, ...],
    ) -> TurtleSignal:
        atr = float(row["atr"])
        if breakout_level is None or direction is Direction.NONE:
            distance = 0.0
        else:
            distance = direction_sign(direction) * (trigger_price - breakout_level) / atr
        vol_percentile = float(row.get("volatility_percentile", np.nan))
        if not np.isfinite(vol_percentile):
            vol_percentile = 0.5
        risk_multiplier = max(0.25, 1.0 - max(0.0, vol_percentile - 0.5))
        trend_status = self._trend_status(row)
        stop = state.stop_price
        add_price = state.next_add_price
        if direction is not Direction.NONE and signal_type in {
            SignalType.APPROACHING_BREAKOUT,
            SignalType.SYSTEM1_BREAKOUT,
            SignalType.SYSTEM2_BREAKOUT,
            SignalType.OVEREXTENDED,
        }:
            stop = initial_stop(trigger_price, atr, direction, self.config.stop_atr)
            add_price = next_add_price(
                trigger_price,
                atr,
                direction,
                self.config.pyramid_step_atr,
            )
        tradeable = not filtered_reasons and signal_type not in {
            SignalType.NO_SIGNAL,
            SignalType.APPROACHING_BREAKOUT,
            SignalType.OVEREXTENDED,
            SignalType.FALSE_BREAKOUT,
            SignalType.EXIT_SIGNAL,
            SignalType.ATR_STOP,
            SignalType.TREND_INVALIDATED,
        }
        note = (
            "原始海龟事件通过可选过滤器；仍不代表确定性盈利。"
            if tradeable
            else "保留原始事件，但当前仅用于观察或风险管理。"
        )
        entry_period = (
            self.config.system1_entry
            if state.system in {0, 1}
            else self.config.system2_entry
        )
        channel_high = _finite_or_none(row.get(f"channel_high_{entry_period}"))
        channel_low = _finite_or_none(row.get(f"channel_low_{entry_period}"))
        return TurtleSignal(
            symbol=asset.symbol,
            instrument=asset.instrument,
            market=asset.market.value,
            timeframe=timeframe.upper(),
            signal_type=signal_type,
            raw_signal_type=raw_type,
            direction=direction,
            signal_time=row.name.isoformat(),
            trigger_price=float(trigger_price),
            channel_high=channel_high,
            channel_low=channel_low,
            atr=atr,
            atr_pct=atr / float(row["close"]),
            stop_price=stop,
            next_add_price=add_price,
            distance_to_breakout_atr=float(distance),
            volatility_percentile=vol_percentile,
            suggested_risk_unit=asset.risk_unit_pct * risk_multiplier,
            trend_status=trend_status,
            confirmation_status=confirmation_for(signal_type, self.config.confirmation_mode),
            data_source=asset.data_source,
            generated_at=TurtleSignal.now_iso(),
            tradeable=tradeable,
            filtered_reasons=filtered_reasons,
            confidence_note=note,
            metadata={
                "system": state.system,
                "instrument_identity": asset.identity,
                "holding_bars": state.holding_bars,
                "additions": state.additions,
            },
        )

    def _no_signal(
        self,
        row: pd.Series,
        asset: AssetConfig,
        timeframe: str,
        state: DetectorState,
    ) -> TurtleSignal:
        return self._build_signal(
            row,
            asset,
            timeframe,
            SignalType.NO_SIGNAL,
            SignalType.NO_SIGNAL,
            state.direction,
            float(row["close"]),
            state.breakout_level,
            state,
            (),
        )

    def _trend_status(self, row: pd.Series) -> str:
        close = float(row["close"])
        ma = _finite_or_none(row.get(f"sma_{self.config.trend_ma_period}"))
        long_ma = _finite_or_none(
            row.get(f"sma_{self.config.long_trend_ma_period}")
        )
        if ma is None or long_ma is None:
            return "not_warm"
        if close > ma > long_ma:
            return "uptrend"
        if close < ma < long_ma:
            return "downtrend"
        return "mixed"


def direction_sign(direction: Direction) -> int:
    if direction is Direction.LONG:
        return 1
    if direction is Direction.SHORT:
        return -1
    return 0


def _finite_or_none(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _alignment_description(
    directional: list[tuple[str, Direction]],
) -> str:
    if not directional:
        return "no_directional_signal"
    directions = {direction for _, direction in directional}
    ordered = sorted(directional, key=lambda item: timeframe_rank(item[0]))
    if len(directions) == 1 and len(ordered) > 1:
        return f"aligned_{ordered[0][1].value}_{len(ordered)}_timeframes"
    if len(directions) > 1:
        return "timeframe_conflict"
    return f"single_timeframe_{ordered[0][1].value}"
