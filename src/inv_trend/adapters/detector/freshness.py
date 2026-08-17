"""Strict, calendar-aware freshness policy for completed D1 bars."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import time
from zoneinfo import ZoneInfo

import pandas as pd

from inv_trend.data.calendar import is_nyse_trading_day
from inv_trend.data.models import InstrumentConfig


@dataclass(frozen=True)
class FreshnessResult:
    status: str
    expected_date: str
    actual_date: str | None
    reason: str

    @property
    def is_fresh(self) -> bool:
        return self.status == "FRESH"

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


class FreshnessPolicy:
    """Require the latest completed session; no extra stale-bar tolerance."""

    def evaluate(
        self,
        instrument: InstrumentConfig,
        latest_complete: object,
        *,
        now: datetime,
    ) -> FreshnessResult:
        if now.tzinfo is None:
            raise ValueError("freshness now must be timezone-aware")
        expected = self.expected_date(instrument, now=now)
        if latest_complete is None or pd.isna(latest_complete):
            return FreshnessResult("STALE_DATA", expected.isoformat(), None, "no_completed_bar")
        actual = pd.Timestamp(latest_complete)
        actual_date = actual.date()
        status = "FRESH" if actual_date >= expected else "STALE_DATA"
        reason = "latest_expected_session_present" if status == "FRESH" else "latest_session_missing"
        return FreshnessResult(status, expected.isoformat(), actual_date.isoformat(), reason)

    def expected_date(self, instrument: InstrumentConfig, *, now: datetime) -> date:
        utc_date = now.astimezone(timezone.utc).date()
        if instrument.session == "24x7":
            return utc_date - timedelta(days=1)
        if instrument.session == "regular" and instrument.market in {"xnas", "xnys"}:
            # A US session becomes eligible only after its New York close.
            # ZoneInfo handles EST/EDT transitions; 16:15 leaves a small
            # provider-publication grace period without treating it as stale.
            local = now.astimezone(ZoneInfo("America/New_York"))
            candidate = local.date()
            if local.timetz().replace(tzinfo=None) < time(16, 15):
                candidate -= timedelta(days=1)
            while not is_nyse_trading_day(candidate):
                candidate -= timedelta(days=1)
            return candidate
        candidate = utc_date - timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate -= timedelta(days=1)
        return candidate


__all__ = ["FreshnessPolicy", "FreshnessResult"]
