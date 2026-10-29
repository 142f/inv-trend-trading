"""Replay session ledger: progress, comparison and failures for the dashboard.

The monitoring event store stays a strict decision/event/outbox boundary.  A
replay run is operational metadata, so it lives in its own SQLite file that the
dashboard reads with ``mode=ro``.  Nothing here can enqueue notifications.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3
from pathlib import Path

from .模型 import digest, timeframe

STATUSES = ('RUNNING', 'SUCCEEDED', 'FAILED')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS replay_sessions(
    session_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('RUNNING','SUCCEEDED','FAILED')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    total_bars INTEGER NOT NULL,
    bar_index INTEGER NOT NULL DEFAULT 0,
    compared_bars INTEGER,
    mismatch_count INTEGER,
    uncompared_bars INTEGER,
    error TEXT,
    updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS replay_sessions_started ON replay_sessions(started_at DESC);
'''


def _utc(now: datetime | None) -> datetime:
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        return clock.replace(tzinfo=timezone.utc)
    return clock.astimezone(timezone.utc)


class ReplaySessionStore:
    """Write-side recorder used by ``trend-monitor replay``."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript(SCHEMA)

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def start(self, symbol: str, frame: str, dataset_version: str, total_bars: int,
              *, now: datetime | None = None) -> str:
        symbol, frame = symbol.upper(), timeframe(frame)
        if not isinstance(dataset_version, str) or not dataset_version.strip():
            raise ValueError('replay dataset version is required')
        if type(total_bars) is not int or total_bars < 1:
            raise ValueError('replay total bars must be a positive integer')
        stamp = _utc(now).isoformat()
        session_id = digest({'symbol': symbol, 'timeframe': frame,
                             'dataset_version': dataset_version, 'started_at': stamp})
        with self._connection() as db:
            db.execute('INSERT INTO replay_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (session_id, symbol, frame, dataset_version, 'RUNNING', stamp,
                        None, total_bars, 0, None, None, None, None, stamp))
        return session_id

    def advance(self, session_id: str, bar_index: int, *, now: datetime | None = None):
        if type(bar_index) is not int or bar_index < 0:
            raise ValueError('replay bar index must be a nonnegative integer')
        with self._connection() as db:
            db.execute("UPDATE replay_sessions SET bar_index=?, updated_at=? "
                       "WHERE session_id=? AND status='RUNNING'",
                       (bar_index, _utc(now).isoformat(), session_id))

    def finish(self, session_id: str, *, status: str, compared_bars: int | None = None,
               mismatch_count: int | None = None, uncompared_bars: int | None = None,
               error: str | None = None, now: datetime | None = None):
        if status not in {'SUCCEEDED', 'FAILED'}:
            raise ValueError('replay finish status must be SUCCEEDED or FAILED')
        for name, value in (('compared_bars', compared_bars), ('mismatch_count', mismatch_count),
                            ('uncompared_bars', uncompared_bars)):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f'{name} must be a nonnegative integer')
        stamp = _utc(now).isoformat()
        with self._connection() as db:
            db.execute('UPDATE replay_sessions SET status=?, finished_at=?, compared_bars=?, '
                       'mismatch_count=?, uncompared_bars=?, error=?, updated_at=? '
                       'WHERE session_id=?',
                       (status, stamp, compared_bars, mismatch_count, uncompared_bars,
                        error, stamp, session_id))


class ReplaySessionReader:
    """Read-side view used by the FastAPI dashboard; never writes."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path is not None else None

    def _available(self) -> bool:
        return self.path is not None and self.path.is_file()

    def list(self, *, limit: int = 50) -> list[dict]:
        if not self._available():
            return []
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('replay list limit must be between 1 and 500')
        with self._read() as db:
            rows = db.execute('SELECT * FROM replay_sessions ORDER BY started_at DESC, rowid DESC '
                              'LIMIT ?', (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get(self, session_id: str) -> dict | None:
        if not self._available() or not isinstance(session_id, str) or not session_id:
            return None
        with self._read() as db:
            row = db.execute('SELECT * FROM replay_sessions WHERE session_id=?',
                             (session_id,)).fetchone()
        return dict(row) if row else None

    def summary(self) -> dict:
        sessions = self.list(limit=500)
        return {status: sum(1 for item in sessions if item['status'] == status)
                for status in STATUSES} | {'total': len(sessions)}

    @contextmanager
    def _read(self):
        db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            yield db
        finally:
            db.close()
