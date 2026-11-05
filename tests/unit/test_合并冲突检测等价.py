"""Differential test: vectorised bar-conflict detection vs the row-by-row original.

``merge_current`` used to walk every incoming row and compare each column with
``_same_bar_value``.  It now hash-joins the timestamps and compares numeric
columns in one pass, re-checking only the rows that look revised.  This module
pins that the two formulations agree on the ugly cases: duplicate existing
timestamps (last occurrence wins), ``NaT`` rows, incomplete bars, missing
columns, infinities and non-numeric columns.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import inv_trend.data.pipeline.行情阶段_v1 as stage


def _core() -> stage.BarsCore:
    return object.__new__(stage.BarsCore)


def _same_bar_value(left: object, right: object) -> bool:
    if pd.isna(left) and pd.isna(right):
        return True
    try:
        return bool(abs(float(left) - float(right)) <= 1e-12)
    except (TypeError, ValueError):
        return left == right


def _legacy_merge(
    existing: pd.DataFrame, incoming: pd.DataFrame
) -> tuple[pd.DataFrame, list[dict[str, object]], pd.DataFrame]:
    """The pre-vectorisation implementation, kept verbatim as the oracle."""

    conflicts: list[dict[str, object]] = []
    rejected: list[pd.Series] = []
    if not existing.empty and not incoming.empty:
        value_columns = [
            column
            for column in (
                "open", "high", "low", "close", "adjusted_close",
                "volume", "is_complete",
            )
            if column in existing and column in incoming
        ]
        positions: dict[object, int] = {}
        missing: int | None = None
        for position, timestamp in enumerate(existing["timestamp"].tolist()):
            if pd.isna(timestamp):
                missing = position
            else:
                positions[timestamp] = position
        existing_values = {column: existing[column].tolist() for column in value_columns}
        incoming_values = {column: incoming[column].tolist() for column in value_columns}
        keep_incoming: list[bool] = []
        rejected_positions: list[int] = []
        for incoming_position, timestamp in enumerate(incoming["timestamp"].tolist()):
            existing_position = (
                missing if pd.isna(timestamp) else positions.get(timestamp)
            )
            if existing_position is None or not _flag(existing, "is_complete", existing_position):
                keep_incoming.append(True)
                continue
            changed = [
                column
                for column in value_columns
                if not _same_bar_value(
                    existing_values[column][existing_position],
                    incoming_values[column][incoming_position],
                )
            ]
            if not changed:
                keep_incoming.append(False)
                continue
            old = existing.iloc[existing_position]
            row = incoming.iloc[incoming_position]
            conflicts.append(
                {
                    "timestamp": timestamp.isoformat(),
                    "existing_value": {
                        column: None if pd.isna(old[column]) else old[column]
                        for column in value_columns
                    },
                    "incoming_value": {
                        column: None if pd.isna(row[column]) else row[column]
                        for column in value_columns
                    },
                    "changed_fields": changed,
                }
            )
            rejected_positions.append(incoming_position)
            keep_incoming.append(False)
        if rejected_positions:
            rejected.extend(incoming.iloc[position] for position in rejected_positions)
        incoming = incoming.loc[keep_incoming]

    non_empty = [frame for frame in (existing, incoming) if not frame.empty]
    if not non_empty:
        combined = pd.DataFrame(columns=incoming.columns)
    elif len(non_empty) == 1:
        combined = non_empty[0].copy()
    else:
        combined = pd.concat(non_empty, ignore_index=True, sort=False)
    order = ["timestamp"] + (["ingested_at"] if "ingested_at" in combined else [])
    if not combined.empty:
        combined = (
            combined.sort_values(order)
            .drop_duplicates("timestamp", keep="last")
            .reset_index(drop=True)
        )
    rejected_frame = (
        pd.DataFrame(rejected, columns=incoming.columns)
        if rejected
        else pd.DataFrame()
    )
    return combined, conflicts, rejected_frame


def _flag(frame: pd.DataFrame, column: str, position: int) -> bool:
    if column not in frame.columns:
        return False
    return bool(frame[column].iloc[position])


def _frame(rows: int, *, start: str = "2024-01-01") -> pd.DataFrame:
    stamps = pd.date_range(start, periods=rows, freq="D", tz="UTC")
    close = 100.0 + np.arange(rows, dtype=float)
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.full(rows, 1_000.0),
            "is_complete": True,
        }
    )


def _compare(existing: pd.DataFrame, incoming: pd.DataFrame) -> None:
    expected = _legacy_merge(existing.copy(), incoming.copy())
    actual = _core().merge_current("SPY", "D1", incoming.copy(), existing=existing.copy())

    pd.testing.assert_frame_equal(actual[0], expected[0])
    assert actual[1] == expected[1]
    pd.testing.assert_frame_equal(actual[2], expected[2])


def test_identical_overlap_is_not_a_conflict() -> None:
    existing = _frame(40)
    _compare(existing, existing.iloc[-10:].reset_index(drop=True))


def test_revised_completed_bar_is_quarantined() -> None:
    existing = _frame(40)
    incoming = existing.iloc[-10:].reset_index(drop=True).copy()
    incoming.loc[3, "close"] = 999.0
    incoming.loc[7, "volume"] = 0.0
    _compare(existing, incoming)


def test_incomplete_existing_bar_accepts_the_incoming_row() -> None:
    existing = _frame(30)
    existing.loc[25, "is_complete"] = False
    incoming = existing.iloc[-6:].reset_index(drop=True).copy()
    incoming.loc[1, "close"] = 500.0
    _compare(existing, incoming)


def test_duplicate_existing_timestamps_use_the_last_occurrence() -> None:
    existing = _frame(20)
    duplicated = pd.concat([existing, existing.iloc[[5, 6]]], ignore_index=True)
    duplicated.loc[len(duplicated) - 1, "close"] = 4242.0
    incoming = existing.iloc[[5, 6, 7]].reset_index(drop=True).copy()
    _compare(duplicated, incoming)


def test_missing_existing_row_is_kept() -> None:
    existing = _frame(20)
    incoming = _frame(20).iloc[15:].reset_index(drop=True).copy()
    incoming["timestamp"] = incoming["timestamp"] + pd.Timedelta(days=100)
    _compare(existing, incoming)


def test_nat_timestamps_compare_against_the_last_missing_row() -> None:
    existing = _frame(12)
    existing.loc[7, "timestamp"] = pd.NaT
    incoming = _frame(12).iloc[7:].reset_index(drop=True).copy()
    incoming.loc[0, "timestamp"] = pd.NaT
    incoming.loc[0, "close"] = 777.0
    _compare(existing, incoming)


def test_missing_columns_are_ignored() -> None:
    existing = _frame(15).drop(columns=["volume", "is_complete"])
    incoming = existing.iloc[-5:].reset_index(drop=True).copy()
    incoming.loc[2, "close"] += 1.0
    _compare(existing, incoming)


def test_non_numeric_column_falls_back_to_the_scalar_rule() -> None:
    existing = _frame(15)
    existing["close"] = [f"{value:.6f}" for value in existing["close"]]
    incoming = existing.iloc[-5:].reset_index(drop=True).copy()
    # "100.000000" vs "100.0" is equal under the scalar float rule but not as text.
    incoming.loc[1, "close"] = "100.0"
    _compare(existing, incoming)


@pytest.mark.parametrize("value", [np.inf, -np.inf])
def test_infinite_values_are_treated_as_revisions(value: float) -> None:
    existing = _frame(15)
    existing.loc[12, "high"] = value
    incoming = existing.iloc[-5:].reset_index(drop=True).copy()
    incoming.loc[2, "high"] = value
    _compare(existing, incoming)


def test_randomised_overlaps_match_the_row_by_row_oracle() -> None:
    rng = np.random.default_rng(20261105)
    for trial in range(25):
        rows = int(rng.integers(3, 40))
        existing = _frame(rows)
        if trial % 3 == 0:
            existing.loc[rng.integers(0, rows), "is_complete"] = False
        start = max(0, rows - int(rng.integers(1, rows + 1)))
        incoming = existing.iloc[start:].reset_index(drop=True).copy()
        incoming["timestamp"] = incoming["timestamp"] + pd.to_timedelta(
            rng.integers(-2, 3, len(incoming)), unit="D"
        )
        for column in ("open", "high", "low", "close", "volume"):
            mask = rng.random(len(incoming)) < 0.25
            if mask.any():
                incoming.loc[mask, column] = incoming.loc[mask, column] + rng.normal(
                    0.0, 0.5, int(mask.sum())
                )
        _compare(existing, incoming)
