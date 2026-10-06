"""SQLite decision/state/event/outbox commit boundary, separate from market data."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import sqlite3

import pandas as pd

from .模型 import DecisionSnapshot, StateRecord, canonical, digest, timeframe


class StateConflictError(RuntimeError):
    pass


class EventStore:
    def __init__(self, path: str | Path, *, purpose='live'):
        if purpose not in {'live', 'replay'}:
            raise ValueError('monitoring store purpose must be live/replay')
        self.purpose = purpose
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version not in {0, 1, 2, 3}:
                raise ValueError(f'unsupported monitoring schema version: {version}')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version == 0 and tables:
                raise ValueError('cannot adopt an unrelated database as monitoring storage')
            if version in {1, 2, 3}:
                required = {'monitoring_metadata', 'monitoring_state', 'decisions', 'events', 'outbox'}
                if version >= 2:
                    required.add('monitoring_mode')
                if version == 3:
                    required.add('notification_attempts')
                if not required.issubset(tables):
                    raise ValueError('monitoring database identity or schema is incomplete')
                owner = db.execute('SELECT kind FROM monitoring_metadata').fetchall()
                if len(owner) != 1 or owner[0][0] != 'inv_trend_monitoring':
                    raise ValueError('monitoring database owner marker is invalid')
                if version >= 2:
                    modes = db.execute('SELECT purpose FROM monitoring_mode').fetchall()
                    if len(modes) != 1 or modes[0][0] != purpose:
                        raise ValueError('monitoring store purpose does not match its persisted role')
                else:
                    modes = {json.loads(row[0]).get('context', {}).get('mode')
                             for row in db.execute('SELECT payload FROM decisions')}
                    if modes and modes != {purpose}:
                        raise ValueError('legacy monitoring store purpose cannot be verified; use a separate store')
                if version == 3:
                    outbox_columns = {row['name'] for row in db.execute('PRAGMA table_info(outbox)')}
                    attempt_columns = {row['name'] for row in db.execute('PRAGMA table_info(notification_attempts)')}
                    if (not {'channel_id', 'lease_token', 'delivered_at'}.issubset(outbox_columns) or
                            not {'lease_token', 'notification_id', 'channel_id', 'attempt', 'started_at',
                                 'finished_at', 'status', 'error', 'http_status', 'remote_request_id'}.issubset(attempt_columns)):
                        raise ValueError('notification storage schema is incomplete')
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('monitoring database integrity check failed')
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS monitoring_metadata(kind TEXT PRIMARY KEY);
                INSERT OR IGNORE INTO monitoring_metadata VALUES('inv_trend_monitoring');
                CREATE TABLE IF NOT EXISTS monitoring_mode(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    purpose TEXT NOT NULL CHECK(purpose IN ('live','replay')));
                CREATE TABLE IF NOT EXISTS monitoring_state(
                    symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                    revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(symbol,timeframe));
                CREATE TABLE IF NOT EXISTS decisions(
                    input_key TEXT PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(
                    event_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL,
                    kind TEXT NOT NULL, code TEXT NOT NULL, severity TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(decision_id) REFERENCES decisions(decision_id));
                CREATE TABLE IF NOT EXISTS outbox(
                    notification_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt TEXT, lease_until TEXT, last_error TEXT,
                    FOREIGN KEY(event_id) REFERENCES events(event_id));
            ''')
            db.execute('INSERT OR IGNORE INTO monitoring_mode VALUES(1,?)', (purpose,))
            if db.execute('SELECT purpose FROM monitoring_mode WHERE id=1').fetchone()[0] != purpose:
                raise ValueError('monitoring store purpose was claimed by a different runtime')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(outbox)')}
            for name in ('channel_id', 'lease_token', 'delivered_at'):
                if name not in columns:
                    if version == 3:
                        raise ValueError('notification outbox schema is incomplete')
                    db.execute(f'ALTER TABLE outbox ADD COLUMN {name} TEXT')
            db.execute('''CREATE TABLE IF NOT EXISTS notification_attempts(
                lease_token TEXT PRIMARY KEY, notification_id TEXT NOT NULL,
                channel_id TEXT NOT NULL, attempt INTEGER NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                error TEXT, http_status INTEGER, remote_request_id TEXT,
                FOREIGN KEY(notification_id) REFERENCES outbox(notification_id),
                UNIQUE(notification_id, attempt))''')
            db.execute('PRAGMA user_version=3')
            db.commit()

    @contextmanager
    def _connection(self):
        db = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
        except BaseException:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def state(self, symbol: str, frame: str) -> StateRecord:
        symbol, frame = symbol.upper(), timeframe(frame)
        with self._connection() as db:
            row = db.execute('SELECT payload FROM monitoring_state WHERE symbol=? AND timeframe=?',
                             (symbol, frame)).fetchone()
        return StateRecord.from_dict(json.loads(row['payload'])) if row else StateRecord(symbol, frame)

    def decision_for_input(self, input_key: str) -> dict | None:
        with self._connection() as db:
            row = db.execute('SELECT payload FROM decisions WHERE input_key=?', (input_key,)).fetchone()
        return json.loads(row['payload']) if row else None

    def commit(self, snapshot: DecisionSnapshot, before: StateRecord, after: StateRecord, *, enqueue=True, guard=None) -> dict:
        if snapshot.context.get('mode', self.purpose) != self.purpose:
            raise ValueError('decision mode differs from monitoring store purpose')
        if self.purpose == 'replay' and enqueue:
            raise ValueError('replay store cannot enqueue notifications')
        identity = (snapshot.symbol, snapshot.timeframe)
        if identity != (before.symbol, before.timeframe) or identity != (after.symbol, after.timeframe):
            raise ValueError('decision/state identity mismatch')
        if snapshot.state_before != before.phase or snapshot.state_after != after.phase:
            raise ValueError('decision/state phase mismatch')
        if after.revision != before.revision + 1:
            raise ValueError('state revision must advance exactly once')
        with self._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if guard is not None:
                guard()
            if db.execute('SELECT purpose FROM monitoring_mode WHERE id=1').fetchone()[0] != self.purpose:
                raise ValueError('persisted monitoring store purpose has changed')
            previous = db.execute('SELECT payload FROM decisions WHERE input_key=?',
                                  (snapshot.input_key,)).fetchone()
            if previous:
                db.commit()
                return {'created': False, 'snapshot': json.loads(previous['payload'])}
            row = db.execute('SELECT revision,payload FROM monitoring_state WHERE symbol=? AND timeframe=?',
                             identity).fetchone()
            actual = row['revision'] if row else 0
            if actual != before.revision:
                raise StateConflictError(f'stale monitoring state: expected {before.revision}, actual {actual}')
            if row and canonical(before.to_dict()) != row['payload']:
                raise StateConflictError('monitoring state contents do not match the recorded revision')
            runtime = dict(before.details.get('_runtime', {}))
            last_bar = runtime.get('last_bar_time')
            if last_bar and snapshot.bar_time and pd.Timestamp(snapshot.bar_time) < pd.Timestamp(last_bar):
                raise StateConflictError('an older bar cannot rewind monitoring state')
            payload = snapshot.to_dict()
            payload['decision_id'] = snapshot.decision_id
            payload['input_key'] = snapshot.input_key
            db.execute('INSERT INTO decisions VALUES(?,?,?,?,?,?)',
                       (snapshot.input_key, snapshot.decision_id, *identity, snapshot.evaluated_at, canonical(payload)))
            if snapshot.bar_time:
                runtime.update(last_bar_time=snapshot.bar_time, dataset_version=snapshot.dataset_version)
            runtime['last_decision_id'] = snapshot.decision_id
            recorded_after = replace(after, details=dict(after.details) | {'_runtime': runtime})
            db.execute('INSERT INTO monitoring_state VALUES(?,?,?,?) ON CONFLICT(symbol,timeframe) '
                       'DO UPDATE SET revision=excluded.revision,payload=excluded.payload',
                       (*identity, after.revision, canonical(recorded_after.to_dict())))
            if snapshot.event:
                event_id = self._write_event(db, snapshot)
                if enqueue:
                    db.execute('INSERT INTO outbox(notification_id,event_id) VALUES(?,?)', (event_id, event_id))
            for event in snapshot.additional_events:
                event_id = self._write_event(db, snapshot, event)
                if enqueue:
                    db.execute('INSERT INTO outbox(notification_id,event_id) VALUES(?,?)', (event_id, event_id))
            if guard is not None:
                guard()
            db.commit()
        return {'created': True, 'snapshot': payload}

    def _write_event(self, db, snapshot, event=None):
        event = event or snapshot.event
        event_id = digest({'decision_id': snapshot.decision_id, 'event': event.to_dict()})
        db.execute('INSERT INTO events VALUES(?,?,?,?,?,?)',
                   (event_id, snapshot.decision_id, event.kind.value, event.code,
                    event.severity, canonical(event.to_dict())))
        return event_id

    def rows(self, table: str, *, limit=100) -> list[dict]:
        if table not in {'monitoring_state', 'decisions', 'events', 'outbox', 'notification_attempts'}:
            raise ValueError('unknown monitoring table')
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit must be between 1 and 1000')
        with self._connection() as db:
            rows = db.execute(f'SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?', (limit,)).fetchall()
        return [dict(row) for row in rows]
