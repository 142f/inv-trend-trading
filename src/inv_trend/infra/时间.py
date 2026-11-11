"""Aware-UTC timestamp helpers shared by data artifacts and storage records."""

from __future__ import annotations

from datetime import datetime, timezone


def require_aware_utc(value: datetime) -> datetime:
    """Return ``value`` in UTC, rejecting naive datetimes at the boundary.

    Single implementation for every caller that validates a caller-supplied
    time boundary before querying history.
    """
    if value.tzinfo is None:
        raise ValueError("datetime boundaries must be timezone-aware")
    return value.astimezone(timezone.utc)


def parse_utc_datetime(value: datetime | str) -> datetime:
    """Parse current RFC 3339 and legacy ISO-8601 values as aware UTC."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("naive timestamps are forbidden")
    return parsed.astimezone(timezone.utc)


def to_rfc3339_utc(value: datetime | str) -> str:
    """Normalize an aware timestamp before writing a new artifact."""
    return parse_utc_datetime(value).isoformat(timespec="microseconds")
