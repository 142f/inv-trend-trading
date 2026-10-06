"""UTC normalization shared by workflow clocks and market timestamps."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd


def utc_datetime(value: datetime) -> datetime:
    """Interpret a naive clock reading as UTC and convert an aware one."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_timestamp(value: Any) -> pd.Timestamp:
    """Normalize a pandas-compatible timestamp without losing precision."""
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")
