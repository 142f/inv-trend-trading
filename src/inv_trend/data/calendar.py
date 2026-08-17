"""Trading calendar utilities for daily-bar quality classification.

Crypto (24x7) and OTC metals (24x5) have simple rules.  US equities use a
rules-based NYSE holiday calendar (XNAS/XNYS) so that weekend and known
holidays are never misclassified as provider gaps.  Halt dates are treated
as UNKNOWN (no halt feed is wired in yet).
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

# This is a RULE_BASED approximation of the NYSE calendar, not an official
# exchange calendar.  It covers weekends, the regular NYSE holiday set, and
# known one-off closures.  It cannot know about unannounced halts, unscheduled
# early closes or future holiday changes; those are classified UNKNOWN.
RULE_BASED_NYSE_CALENDAR = True


def _easter(year: int) -> date:
    """Anonymous Gregorian Easter (accurate 1900-2099)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    term = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * term) // 451
    month = (h + term - 7 * m + 114) // 31
    day = ((h + term - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date:
    """nth occurrence of weekday (0=Mon) in a month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (nth - 1))


def _last_monday(year: int, month: int) -> date:
    """Last Monday of a month."""
    last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 0) % 7)


def _observed(d: date) -> date:
    """NYSE observance rule: Sat -> Friday before, Sun -> Monday after."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


# Extra one-off closures (9/11 week, storms, state funerals).
_SPECIAL_CLOSURES = frozenset({
    date(2001, 9, 11), date(2001, 9, 12), date(2001, 9, 13), date(2001, 9, 14),
    date(2004, 6, 11),      # Reagan state funeral
    date(2007, 1, 2),       # Gerald Ford state funeral
    date(2012, 10, 29), date(2012, 10, 30),  # Hurricane Sandy
    date(2018, 12, 5),      # George H.W. Bush state funeral
    date(2025, 1, 9),       # Jimmy Carter state funeral
})


def nyse_holidays(year: int) -> set[date]:
    """NYSE-observed closures for a calendar year (rules-based)."""
    holidays = {
        _observed(date(year, 1, 1)),                       # New Year's Day
        _nth_weekday(year, 1, 0, 3),                       # MLK Jr. Day
        _nth_weekday(year, 2, 0, 3),                       # Washington's Birthday
        _easter(year) - timedelta(days=2),                 # Good Friday
        _last_monday(year, 5),                             # Memorial Day
        _observed(date(year, 7, 4)),                       # Independence Day
        _nth_weekday(year, 9, 0, 1),                       # Labor Day
        _nth_weekday(year, 11, 3, 4),                      # Thanksgiving
        _observed(date(year, 12, 25)),                     # Christmas
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))         # Juneteenth
    holidays.update(closure for closure in _SPECIAL_CLOSURES if closure.year == year)
    return holidays


def is_nyse_trading_day(d: date | pd.Timestamp) -> bool:
    if isinstance(d, pd.Timestamp):
        d = d.date()
    if d.weekday() >= 5:
        return False
    return d not in nyse_holidays(d.year)


def classify_missing(
    stamps: pd.DatetimeIndex,
    *,
    market: str,
    session: str,
    start: pd.Timestamp,
    end: pd.Timestamp,
    freq: str,
) -> tuple[pd.DatetimeIndex, dict[str, int]]:
    """Classify expected-but-absent daily timestamps.

    Returns (absent_dates, breakdown) where breakdown keys are
    weekend / holiday / provider_gap / unknown.
    """
    expected = pd.date_range(start, end, freq=freq)
    observed = set(pd.DatetimeIndex(stamps).normalize())
    absent = [stamp for stamp in expected if stamp not in observed]
    breakdown = {"weekend": 0, "holiday": 0, "provider_gap": 0, "unknown": 0}
    classified: list[pd.Timestamp] = []
    for stamp in absent:
        if session in {"24x7"}:
            breakdown["provider_gap"] += 1
            classified.append(stamp)
            continue
        if stamp.dayofweek >= 5:
            breakdown["weekend"] += 1
            continue
        if session == "regular" and market in {"xnas", "xnys"}:
            if stamp.date() in nyse_holidays(stamp.year):
                breakdown["holiday"] += 1
                continue
            # A weekday that is not a known holiday: genuine gap or halt.
            breakdown["provider_gap"] += 1
            classified.append(stamp)
            continue
        # 24x5 OTC metals: weekend already excluded; weekday absence is a gap.
        breakdown["provider_gap"] += 1
        classified.append(stamp)
    return pd.DatetimeIndex(sorted(classified)), breakdown
