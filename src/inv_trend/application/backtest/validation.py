"""Causal expanding-window schedules for unified backtests."""

from __future__ import annotations

import pandas as pd

from .models import FoldWindow, ValidationPolicy


def build_walk_forward_windows(
    calendar: pd.DatetimeIndex,
    policy: ValidationPolicy,
) -> tuple[tuple[FoldWindow, ...], tuple[str, str] | None, str]:
    index = pd.DatetimeIndex(sorted(set(pd.Timestamp(item) for item in calendar)))
    required = (
        policy.train_bars
        + policy.validation_bars * policy.min_folds
        + policy.holdout_bars
    )
    if len(index) < required:
        return (), None, "INSUFFICIENT_HISTORY"
    holdout_start_position = len(index) - policy.holdout_bars
    pre_holdout_end = holdout_start_position - 1
    windows: list[FoldWindow] = []
    train_end = policy.train_bars - 1
    fold = 1
    while train_end + policy.validation_bars <= pre_holdout_end:
        validation_start = train_end + 1
        validation_end = validation_start + policy.validation_bars - 1
        windows.append(
            FoldWindow(
                fold=fold,
                train_start=_text(index[0]),
                train_end=_text(index[train_end]),
                validation_start=_text(index[validation_start]),
                validation_end=_text(index[validation_end]),
            )
        )
        fold += 1
        train_end += policy.step_bars
    if len(windows) < policy.min_folds:
        return (), None, "INSUFFICIENT_HISTORY"
    holdout = (_text(index[holdout_start_position]), _text(index[-1]))
    return tuple(windows), holdout, "READY"


def _text(value: pd.Timestamp) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp.isoformat()


__all__ = ["build_walk_forward_windows"]
