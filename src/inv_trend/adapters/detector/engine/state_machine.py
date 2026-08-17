"""State transitions kept separate from indicator and notification code."""

from __future__ import annotations

from dataclasses import replace

from ..models import (
    ConfirmationStatus,
    DetectorState,
    Direction,
    PositionStatus,
    SignalType,
    StrategyConfig,
)
from ..risk.stops import initial_stop, next_add_price


def enter_position(
    state: DetectorState,
    direction: Direction,
    system: int,
    time: str,
    price: float,
    breakout_level: float,
    atr: float,
    config: StrategyConfig,
    pending: bool,
) -> DetectorState:
    return replace(
        state,
        status=(
            PositionStatus.PENDING_CONFIRMATION
            if pending
            else PositionStatus.ENTERED
        ),
        direction=direction,
        system=system,
        entry_time=None if pending else time,
        entry_price=price,
        breakout_level=breakout_level,
        atr_at_entry=atr,
        stop_price=initial_stop(price, atr, direction, config.stop_atr),
        next_add_price=next_add_price(
            price,
            atr,
            direction,
            config.pyramid_step_atr,
        ),
        additions=0,
        holding_bars=0,
        pending_since=time if pending else None,
    )


def confirm_pending(state: DetectorState, time: str, price: float) -> DetectorState:
    return replace(
        state,
        status=PositionStatus.ENTERED,
        entry_time=time,
        entry_price=price,
        pending_since=None,
        holding_bars=0,
    )


def add_unit(
    state: DetectorState,
    fill_price: float,
    atr: float,
    config: StrategyConfig,
) -> DetectorState:
    if state.direction is Direction.NONE:
        raise ValueError("cannot add without a direction")
    stop = state.stop_price
    if config.stop_mode == "unified":
        candidate = initial_stop(fill_price, atr, state.direction, config.stop_atr)
        if stop is None:
            stop = candidate
        elif state.direction is Direction.LONG:
            stop = max(stop, candidate)
        else:
            stop = min(stop, candidate)
    return replace(
        state,
        additions=state.additions + 1,
        stop_price=stop,
        next_add_price=next_add_price(
            fill_price,
            atr,
            state.direction,
            config.pyramid_step_atr,
        ),
    )


def close_position(
    state: DetectorState,
    exit_price: float,
    invalidated: bool = False,
) -> DetectorState:
    won = state.last_system1_won
    if state.system == 1 and state.entry_price is not None:
        won = state.direction is Direction.LONG and exit_price > state.entry_price
        if state.direction is Direction.SHORT:
            won = exit_price < state.entry_price
    return replace(
        state,
        status=(
            PositionStatus.INVALIDATED if invalidated else PositionStatus.EXITED
        ),
        direction=Direction.NONE,
        system=0,
        entry_time=None,
        entry_price=None,
        breakout_level=None,
        atr_at_entry=None,
        stop_price=None,
        next_add_price=None,
        additions=0,
        holding_bars=0,
        pending_since=None,
        last_system1_won=won,
    )


def confirmation_for(
    signal_type: SignalType,
    mode: str,
) -> ConfirmationStatus:
    if signal_type is SignalType.RETEST_CONFIRMED:
        return ConfirmationStatus.RETEST_CONFIRMED
    if signal_type is SignalType.CLOSE_CONFIRMED or mode == "close":
        return ConfirmationStatus.CLOSE_CONFIRMED
    if signal_type in {SignalType.SYSTEM1_BREAKOUT, SignalType.SYSTEM2_BREAKOUT}:
        return ConfirmationStatus.INTRADAY_TRIGGERED
    return ConfirmationStatus.NONE
