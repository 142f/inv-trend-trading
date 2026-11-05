"""Guard the O(n) → O(n·log n) ``_state_at`` optimisation.

``_state_at`` used to rebuild a growing prefix (``base.iloc[: position + 1]``)
and a full boolean mask (``frame.loc[frame.index <= timestamp]``) for every bar.
It now reads the row directly and passes the causal prefix length to
``_macd_state``.  This module pins that the two formulations agree on *every*
bar of a prepared history, which is what makes the speed-up behaviour-free.
"""

from __future__ import annotations

import pandas as pd
import pytest

from inv_trend.application.strategy_config import DailyChecksConfig
from inv_trend.core.strategy.daily import strategy_checks
from inv_trend.core.strategy.daily.analysis import prepare_daily_analysis


def _bars(count: int) -> pd.DataFrame:
    index = pd.date_range("2021-01-01", periods=count, freq="D", tz="UTC")
    closes = [100.0 + (value % 7) * 0.4 + value * 0.05 for value in range(count)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 1.0 for value in closes],
            "low": [value - 1.0 for value in closes],
            "close": closes,
            "volume": [1_000.0] * count,
            "is_complete": True,
        },
        index=index,
    )


def _prefix_snapshot(prepared, position: int) -> dict[str, object]:
    """The pre-optimisation formulation: slice the prefix, mask the sub-frames."""

    base = prepared.base.iloc[: position + 1]
    row = base.iloc[-1]
    timestamp = base.index[-1]
    macd = {
        label: strategy_checks._macd_state(
            frame.loc[frame.index <= timestamp], prepared.config
        )
        for label, frame in prepared.macd_frames.items()
    }
    return {
        "sma_alignment": strategy_checks._sma_alignment(row, prepared.config),
        "ema_trend": strategy_checks._ema_trend(row, prepared.config, len(base)),
        "turtle": strategy_checks._turtle_direction(row, prepared.config),
        "macd": macd,
        "macd_summary": strategy_checks._macd_summary(macd),
    }


@pytest.mark.parametrize("count", [70, 130])
def test_state_snapshot_equals_the_prefix_slice_formulation(count: int) -> None:
    bars = _bars(count)
    prepared = prepare_daily_analysis(
        bars, DailyChecksConfig(), session_anchor=bars.index[0]
    ).strategy

    mismatches: list[int] = []
    for position in range(len(prepared.base)):
        current = strategy_checks._state_at(prepared, position)
        reference = _prefix_snapshot(prepared, position)
        if any(
            current[key] != reference[key]
            for key in ("sma_alignment", "ema_trend", "macd", "macd_summary")
        ) or current["rating"] != strategy_checks._rating(
            reference["turtle"],
            reference["sma_alignment"],
            reference["ema_trend"],
            reference["macd_summary"],
            strategy_checks._trend_quality(prepared.base.iloc[position], prepared.config),
            strategy_checks._volatility(prepared.base.iloc[position], prepared.config),
            strategy_checks._volume(prepared.base.iloc[position], prepared.config),
            prepared.config,
        ):
            mismatches.append(position)

    assert mismatches == []


def test_row_count_through_matches_the_boolean_mask() -> None:
    frame = pd.DataFrame({"value": range(10)}, index=pd.date_range("2024-01-01", periods=10))

    for timestamp in frame.index:
        assert strategy_checks._row_count_through(frame, timestamp) == int(
            (frame.index <= timestamp).sum()
        )
    # A timestamp between two rows keeps the "everything before it" definition.
    gap = frame.index[4] + pd.Timedelta(hours=6)
    assert strategy_checks._row_count_through(frame, gap) == 5
    # Non-monotonic fallback: the mask definition still wins.
    reversed_frame = frame.iloc[::-1]
    assert strategy_checks._row_count_through(
        reversed_frame, reversed_frame.index[3]
    ) == int((reversed_frame.index <= reversed_frame.index[3]).sum())
