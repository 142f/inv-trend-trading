from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List

import pandas as pd

from data.kline_fetcher import timeframe_to_ms


@dataclass
class DataQualityReport:
    can_open_new_positions: bool = True
    issues: List[str] = field(default_factory=list)


def _expected_delta(tf: str) -> pd.Timedelta:
    return pd.to_timedelta(timeframe_to_ms(tf), unit="ms")


def validate_frames(
    frames: Dict[str, pd.DataFrame],
    required_timeframes: List[str],
    now: datetime | None = None,
    max_gap_multiple: int = 2,
) -> DataQualityReport:
    now = now or datetime.now(timezone.utc)
    report = DataQualityReport()

    for tf in required_timeframes:
        df = frames.get(tf)
        if df is None or df.empty:
            report.can_open_new_positions = False
            report.issues.append(f"{tf}: missing or empty frame")
            continue

        if not df.index.is_monotonic_increasing:
            report.can_open_new_positions = False
            report.issues.append(f"{tf}: index not sorted")
        if df.index.has_duplicates:
            report.can_open_new_positions = False
            report.issues.append(f"{tf}: duplicate timestamps")

        if "close_ts" not in df.columns:
            report.can_open_new_positions = False
            report.issues.append(f"{tf}: close_ts column missing")
            continue

        # no unclosed bars
        if df["close_ts"].iloc[-1] > pd.Timestamp(now):
            report.can_open_new_positions = False
            report.issues.append(f"{tf}: contains unclosed bars")

        # rough continuity check
        if len(df) >= 2:
            deltas = df.index.to_series().diff().dropna()
            expected = _expected_delta(tf)
            max_allowed = expected * max_gap_multiple
            if (deltas > max_allowed).any():
                report.can_open_new_positions = False
                report.issues.append(f"{tf}: timestamp gaps exceed tolerance")

    return report
