"""Read-only, watermark-bounded streaming adapter over the existing v3 authority."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
import sqlite3

from inv_trend.core.阶段协议_v1 import RawBar, Bar, digest, timestamp, utc_now
from inv_trend.storage.结构化存储_v3 import UnifiedStore

EQUITIES = ("AMD", "AMZN", "AVGO", "MSFT", "NVDA", "ORCL", "QQQ", "TSM")


class PointInTimeRepository:
    """No read-all API. Each query has a frozen end-exclusive knowledge watermark.

    Exchange calendars, delistings, splits and source revision vintages have NOT
    been independently certified. Therefore this adapter never returns LIVE data.
    A long-lived read transaction gives a stable source snapshot while result writes
    are separately committed to the SAME SQLite database.
    """
    def __init__(self, data_root, dataset_version: str, *, allowed_before: str):
        self.store = UnifiedStore(data_root)
        self.allowed_before = timestamp(allowed_before)
        self.db = sqlite3.connect(self.store.path.as_uri() + "?mode=ro", uri=True, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA query_only=ON")
        self.db.execute("BEGIN")
        row = self.db.execute("SELECT * FROM market_datasets_v3 WHERE dataset_hash=?",
                              (dataset_version,)).fetchone()
        if row is None or row["status"] not in ("RESEARCH_ONLY", "CURATED"):
            self.db.close()
            raise ValueError("unknown/unreadable pinned dataset version")
        self.dataset = dict(row)
        self.dataset_version = dataset_version
        self.query_log: list[dict] = []
        self.rows_read = 0

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _bounds(self, symbol: str, end: str) -> None:
        if symbol not in EQUITIES:
            raise ValueError("v1 cash contract supports only explicitly listed equities/ETF")
        if timestamp(end) > self.allowed_before:
            raise ValueError("attempt to access quarantined holdout/future interval")

    @staticmethod
    def _date_upper(end: str) -> str:
        local = timestamp(end).astimezone(ZoneInfo("America/New_York"))
        day = local.date() + (timedelta(days=1) if local.time() > time(16) else timedelta())
        return day.isoformat()

    @staticmethod
    def _convert(row) -> RawBar:
        d = datetime.fromisoformat(row["date"]).date()
        zone = ZoneInfo("America/New_York")
        # Explicit approximation, not a certified NYSE/Nasdaq holiday/half-day calendar.
        opened = datetime.combine(d, time(9, 30), zone).astimezone(ZoneInfo("UTC")).isoformat()
        available = datetime.combine(d, time(16), zone).astimezone(ZoneInfo("UTC")).isoformat()
        raw = RawBar(row["symbol"], row["timeframe"], row["source"], opened, available,
                     row["open"], row["high"], row["low"], row["close"], row["volume"], row["ordinal"])
        Bar(**asdict(raw))  # fail closed at adapter boundary, without silently deleting rows
        return raw

    def stream(self, symbol: str, *, start: str, end: str):
        self._bounds(symbol, end)
        if timestamp(start) >= timestamp(end):
            raise ValueError("start must precede end")
        record = {"symbol": symbol, "start": start, "end_exclusive": end,
                  "kind": "STREAM", "rows": 0, "max_available_at": None, "requested_at": utc_now()}
        self.query_log.append(record)
        # Stored date is normalized YYYY-MM-DD + time. Bounds by date retain the index;
        # the availability predicate below handles sub-day end/start boundaries exactly.
        cursor = self.db.execute(
            "SELECT * FROM market_bars_v3 WHERE dataset_id=? AND symbol=? "
            "AND date>=? AND date<? ORDER BY date",
            (self.dataset["dataset_id"], symbol, timestamp(start).date().isoformat(),
             self._date_upper(end)))
        for row in cursor:
            bar = self._convert(row)
            if timestamp(start) <= timestamp(bar.available_at) < timestamp(end):
                record["rows"] += 1
                record["max_available_at"] = bar.available_at
                self.rows_read += 1
                yield bar

    def history(self, symbol: str, *, end: str, limit: int) -> tuple[Bar, ...]:
        self._bounds(symbol, end)
        if type(limit) is not int or limit <= 0:
            raise ValueError("history requires a positive bounded limit")
        # Reverse indexed query; at most one additional same-day record is inspected.
        rows = self.db.execute(
            "SELECT * FROM market_bars_v3 WHERE dataset_id=? AND symbol=? "
            "AND date<? ORDER BY date DESC LIMIT ?",
            (self.dataset["dataset_id"], symbol,
             self._date_upper(end), limit))
        bars = []
        for row in rows:
            b = self._convert(row)
            if timestamp(b.available_at) < timestamp(end):
                bars.append(Bar(**asdict(b)))
                if len(bars) == limit:
                    break
        bars.reverse()
        self.rows_read += len(bars)
        self.query_log.append({"symbol": symbol, "end_exclusive": end, "kind": "HISTORY",
                               "limit": limit, "rows": len(bars),
                               "max_available_at": bars[-1].available_at if bars else None,
                               "prefix_hash": digest([asdict(b) for b in bars]), "requested_at": utc_now()})
        return tuple(bars)
