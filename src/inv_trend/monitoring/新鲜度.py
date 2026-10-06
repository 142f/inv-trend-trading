"""Calendar-aware completed-bar freshness; exact H4 boundaries remain explicit."""
from datetime import datetime

import pandas as pd

from inv_trend.adapters.detector.freshness import FreshnessPolicy, FreshnessResult
from inv_trend.data.calendar import latest_completed_us_bar
from inv_trend.data.models import InstrumentConfig

from .模型 import timeframe


class MonitoringFreshnessPolicy:
    def __init__(self, *, grace_seconds: float = 60.):
        if not 0 <= grace_seconds <= 3600:
            raise ValueError('freshness grace must be between zero and one hour')
        self.grace_seconds = grace_seconds
        self.daily = FreshnessPolicy()

    def evaluate(self, instrument: InstrumentConfig, frame: str, latest, *, now: datetime):
        frame = timeframe(frame)
        if now.utcoffset() is None:
            raise ValueError('freshness clock must be timezone-aware')
        clock = pd.Timestamp(now).tz_convert('UTC')
        actual = None if latest is None else pd.Timestamp(latest)
        if actual is not None and (actual.tzinfo is None or pd.isna(actual)):
            return FreshnessResult('STALE_DATA', '', str(actual), 'invalid_bar_timestamp')
        if actual is not None and actual > clock:
            return FreshnessResult('STALE_DATA', '', actual.isoformat(), 'future_bar_timestamp')
        if frame == 'D1':
            result = self.daily.evaluate(instrument, actual, now=now)
            if actual is not None and actual.date().isoformat() > result.expected_date:
                return FreshnessResult('STALE_DATA', result.expected_date, actual.isoformat(), 'unsettled_session')
            return result
        if instrument.session == 'regular' and instrument.market in {'xnas', 'xnys'}:
            expected = latest_completed_us_bar(clock - pd.Timedelta(seconds=self.grace_seconds),
                                               market=instrument.market, freq='4h')
            status = 'FRESH' if actual is not None and actual.tz_convert('UTC') == expected else 'STALE_DATA'
            return FreshnessResult(status, expected.isoformat(), actual.isoformat() if actual is not None else None,
                                   'latest_expected_bar_present' if status == 'FRESH' else 'latest_completed_bar_missing')
        if instrument.session not in {'24x7', '24x5'}:
            # A stock exchange H4 calendar needs explicit session boundaries;
            # UTC buckets cannot silently substitute for 09:30/16:00 sessions.
            return FreshnessResult('STALE_DATA', '', actual.isoformat() if actual is not None else None,
                                   'H4_SESSION_CALENDAR_UNVERIFIED')
        local = (clock - pd.Timedelta(seconds=self.grace_seconds)).tz_convert(instrument.session_timezone)
        anchor = 17 if instrument.primary_source == 'dukascopy' else 0
        expected = ((local - pd.Timedelta(hours=anchor)).floor('4h') +
                    pd.Timedelta(hours=anchor) - pd.Timedelta(hours=4))
        if instrument.session == '24x5':
            while expected.weekday() >= 5:
                expected -= pd.Timedelta(hours=4)
        expected = expected.tz_convert('UTC')
        status = 'FRESH' if actual is not None and actual.tz_convert('UTC') == expected else 'STALE_DATA'
        return FreshnessResult(status, expected.isoformat(), actual.isoformat() if actual is not None else None,
                               'latest_expected_bar_present' if status == 'FRESH' else 'latest_completed_bar_missing')
