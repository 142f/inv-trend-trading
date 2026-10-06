"""Operational runs and heartbeat evidence in the owned live monitoring store."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from uuid import uuid4

import pandas as pd

from inv_trend.data.locking import _owner_process_state
from inv_trend.observability.脱敏 import redact

from .事件存储 import EventStore
from .模型 import canonical, timeframe, timestamp


class RuntimeLedger:
    def __init__(self, store: EventStore, data_root: str | Path, *, owner_id=None):
        if store.purpose != 'live':
            raise ValueError('runtime requires a live monitoring store')
        self.store = store
        self.data_root = Path(data_root).resolve()
        self.owner_id = owner_id or uuid4().hex
        self.pid = os.getpid()
        with store._connection() as db:
            db.executescript('''
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS runtime_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime_tasks(
                    job_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, pid INTEGER NOT NULL,
                    symbol TEXT NOT NULL, timeframe TEXT NOT NULL, status TEXT NOT NULL,
                    started_at TEXT NOT NULL, finished_at TEXT, decision_id TEXT,
                    details TEXT NOT NULL,
                    FOREIGN KEY(decision_id) REFERENCES decisions(decision_id));
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_monitor_task ON runtime_tasks(symbol,timeframe)
                    WHERE finished_at IS NULL AND status IN ('RUNNING','TIMED_OUT');
                CREATE TABLE IF NOT EXISTS runtime_heartbeats(
                    component TEXT NOT NULL, scope TEXT NOT NULL, observed_at TEXT NOT NULL,
                    owner_id TEXT NOT NULL, pid INTEGER NOT NULL, details TEXT NOT NULL,
                    PRIMARY KEY(component,scope));
                CREATE TABLE IF NOT EXISTS runtime_expectations(
                    component TEXT NOT NULL, scope TEXT NOT NULL, max_age REAL NOT NULL,
                    registered_at TEXT NOT NULL, PRIMARY KEY(component,scope));
                CREATE TABLE IF NOT EXISTS runtime_expectation_lifecycle(
                    component TEXT NOT NULL, scope TEXT NOT NULL, epoch INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('ACTIVE','RETIRED')),
                    changed_at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
                    PRIMARY KEY(component,scope),
                    FOREIGN KEY(component,scope) REFERENCES runtime_expectations(component,scope));
                CREATE TABLE IF NOT EXISTS runtime_expectation_changes(
                    component TEXT NOT NULL, scope TEXT NOT NULL, epoch INTEGER NOT NULL,
                    action TEXT NOT NULL, changed_at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
                    PRIMARY KEY(component,scope,epoch),
                    FOREIGN KEY(component,scope) REFERENCES runtime_expectations(component,scope));
            ''')
            schema = db.execute("SELECT value FROM runtime_metadata WHERE key='expectation_lifecycle_schema'").fetchone()
            if schema is None:
                db.execute("INSERT INTO runtime_expectation_lifecycle SELECT component,scope,1,'ACTIVE',"
                           "registered_at,'migration','legacy_registration' FROM runtime_expectations")
                db.execute('INSERT INTO runtime_expectation_changes SELECT component,scope,epoch,status,'
                           'changed_at,actor,reason FROM runtime_expectation_lifecycle')
                db.execute("INSERT INTO runtime_metadata VALUES('expectation_lifecycle_schema','1')")
            elif schema[0] != '1':
                raise ValueError('unsupported expectation lifecycle schema')
            missing = db.execute('SELECT 1 FROM runtime_expectations e LEFT JOIN runtime_expectation_lifecycle l '
                                 'USING(component,scope) WHERE l.component IS NULL LIMIT 1').fetchone()
            broken = db.execute('SELECT 1 FROM runtime_expectation_lifecycle l LEFT JOIN runtime_expectation_changes a '
                                'USING(component,scope,epoch) WHERE a.component IS NULL OR l.status<>a.action '
                                'OR l.changed_at<>a.changed_at OR l.actor<>a.actor OR l.reason<>a.reason OR l.epoch<1 '
                                'OR (SELECT COUNT(*) FROM runtime_expectation_changes h WHERE h.component=l.component '
                                'AND h.scope=l.scope)<>l.epoch OR (SELECT COUNT(*) FROM runtime_expectation_changes h '
                                'WHERE h.component=l.component AND h.scope=l.scope AND h.epoch>=1 '
                                'AND h.epoch<=l.epoch)<>l.epoch LIMIT 1').fetchone()
            if missing or broken:
                raise ValueError('expectation lifecycle audit is incomplete or inconsistent')
            for key, value in {'kind': 'inv_trend_runtime_v1', 'data_root': str(self.data_root)}.items():
                previous = db.execute('SELECT value FROM runtime_metadata WHERE key=?', (key,)).fetchone()
                if previous is not None and previous[0] != value:
                    raise ValueError(f'runtime store {key} binding does not match')
                db.execute('INSERT OR IGNORE INTO runtime_metadata VALUES(?,?)', (key, value))
            db.commit()

    def begin(self, symbol, frame, *, now, details=None):
        symbol, frame = symbol.upper(), timeframe(frame)
        job_id = uuid4().hex
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO runtime_tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (job_id, self.owner_id, self.pid, symbol, frame, 'RUNNING',
                        timestamp(pd.Timestamp(now).isoformat()), None, None, canonical(redact(details or {}))))
            db.commit()
        return job_id

    def finish(self, job_id, *, now, status, decision_id=None, details=None):
        if status not in {'SUCCESS', 'FAILED', 'TIMED_OUT', 'INTERRUPTED'}:
            raise ValueError('invalid terminal task status')
        when = timestamp(pd.Timestamp(now).isoformat())
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT started_at,details FROM runtime_tasks WHERE job_id=? AND owner_id=? '
                                  'AND finished_at IS NULL', (job_id, self.owner_id)).fetchone()
            if previous is None:
                raise ValueError('task is no longer active or belongs to another runtime')
            if pd.Timestamp(when) < pd.Timestamp(previous['started_at']):
                raise ValueError('runtime clock cannot finish a task before its start')
            payload = json.loads(previous['details']) | redact(details or {})
            db.execute('UPDATE runtime_tasks SET status=?,finished_at=?,decision_id=?,details=? WHERE job_id=?',
                       (status, when, decision_id, canonical(payload), job_id))
            db.commit()

    def pulse(self, component, scope, *, now, details=None, ignore_stale=False):
        when = timestamp(pd.Timestamp(now).isoformat())
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            lifecycle = db.execute('SELECT status FROM runtime_expectation_lifecycle WHERE component=? AND scope=?',
                                   (component,scope)).fetchone()
            if lifecycle is not None and lifecycle['status'] == 'RETIRED':
                raise ValueError('retired expectation requires explicit registration before heartbeat')
            previous = db.execute('SELECT observed_at FROM runtime_heartbeats WHERE component=? AND scope=?',
                                  (component, scope)).fetchone()
            if previous is not None and pd.Timestamp(when) < pd.Timestamp(previous[0]):
                if ignore_stale:
                    return False
                raise ValueError('runtime heartbeat clock moved backwards')
            db.execute('INSERT INTO runtime_heartbeats VALUES(?,?,?,?,?,?) '
                       'ON CONFLICT(component,scope) DO UPDATE SET observed_at=excluded.observed_at,'
                       'owner_id=excluded.owner_id,pid=excluded.pid,details=excluded.details',
                       (component, scope, when, self.owner_id, self.pid, canonical(redact(details or {}))))
            db.commit()
        return True

    def recover(self, *, now):
        """Only a known-dead process relinquishes unfinished durable jobs."""
        recovered = []
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT job_id,pid,details FROM runtime_tasks WHERE finished_at IS NULL "
                              "AND status IN ('RUNNING','TIMED_OUT')").fetchall()
            for row in rows:
                if _owner_process_state({'pid': row['pid']}) is not False:
                    continue
                detail = json.loads(row['details']) | {'recovery': 'known_owner_process_exited'}
                db.execute('UPDATE runtime_tasks SET status=?,finished_at=?,details=? WHERE job_id=?',
                           ('INTERRUPTED', timestamp(pd.Timestamp(now).isoformat()), canonical(detail), row['job_id']))
                recovered.append(row['job_id'])
            db.commit()
        return recovered

    def tasks(self, *, symbol=None, frame=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('task limit must be between 1 and 1000')
        clauses, params = [], []
        if symbol is not None:
            clauses.append('symbol=?'); params.append(symbol.upper())
        if frame is not None:
            clauses.append('timeframe=?'); params.append(timeframe(frame))
        query = 'SELECT * FROM runtime_tasks' + (' WHERE ' + ' AND '.join(clauses) if clauses else '')
        with self.store._connection() as db:
            rows = db.execute(query + ' ORDER BY rowid DESC LIMIT ?', (*params, limit)).fetchall()
        return [dict(row) | {'details': json.loads(row['details'])} for row in rows]

    def heartbeats(self):
        with self.store._connection() as db:
            rows = db.execute('SELECT * FROM runtime_heartbeats ORDER BY component,scope').fetchall()
        return [dict(row) | {'details': json.loads(row['details'])} for row in rows]

    def active_tasks(self):
        """Unfinished tasks cannot disappear behind the bounded history page."""
        with self.store._connection() as db:
            rows = db.execute("SELECT * FROM runtime_tasks WHERE finished_at IS NULL "
                              "AND status IN ('RUNNING','TIMED_OUT') ORDER BY rowid").fetchall()
        return [dict(row) | {'details':json.loads(row['details'])} for row in rows]

    def expect(self, component, scope, max_age, *, now):
        if (component not in {'scheduler','scanner','notification'} or not isinstance(scope,str) or not scope
                or isinstance(max_age,bool) or not math.isfinite(max_age) or max_age <= 0):
            raise ValueError('expected component requires an identity and finite positive age')
        when = timestamp(pd.Timestamp(now).isoformat())
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            lifecycle = db.execute('SELECT * FROM runtime_expectation_lifecycle WHERE component=? AND scope=?',
                                   (component,scope)).fetchone()
            reactivate = lifecycle is not None and lifecycle['status'] == 'RETIRED'
            owner_changed = lifecycle is not None and lifecycle['actor'] != self.owner_id
            if (reactivate or owner_changed) and pd.Timestamp(when) < pd.Timestamp(lifecycle['changed_at']):
                raise ValueError('expectation reactivation clock moved backwards')
            db.execute('INSERT INTO runtime_expectations VALUES(?,?,?,?) '
                       'ON CONFLICT(component,scope) DO UPDATE SET max_age=excluded.max_age',
                       (component,scope,max_age,when))
            if lifecycle is None or reactivate or owner_changed:
                epoch = 1 if lifecycle is None else lifecycle['epoch'] + 1
                if reactivate:
                    db.execute('UPDATE runtime_expectations SET registered_at=? WHERE component=? AND scope=?',
                               (when,component,scope))
                reason = 'runtime_reactivation' if reactivate else 'owner_registration' if owner_changed else 'runtime_registration'
                db.execute('INSERT INTO runtime_expectation_lifecycle VALUES(?,?,?,?,?,?,?) '
                           'ON CONFLICT(component,scope) DO UPDATE SET epoch=excluded.epoch,status=excluded.status,'
                           'changed_at=excluded.changed_at,actor=excluded.actor,reason=excluded.reason',
                           (component,scope,epoch,'ACTIVE',when,self.owner_id,reason))
                db.execute('INSERT INTO runtime_expectation_changes VALUES(?,?,?,?,?,?,?)',
                           (component,scope,epoch,'ACTIVE',when,self.owner_id,reason))
            db.commit()

    def expectations(self, *, include_retired=False):
        with self.store._connection() as db:
            query = ('SELECT e.*,l.epoch,l.status,l.changed_at,l.actor,l.reason FROM runtime_expectations e '
                     'JOIN runtime_expectation_lifecycle l USING(component,scope)')
            if not include_retired:
                query += " WHERE l.status='ACTIVE'"
            return [dict(row) for row in db.execute(query+' ORDER BY component,scope')]

    def retired_components(self):
        return {(row['component'],row['scope']) for row in self.expectations(include_retired=True)
                if row['status'] == 'RETIRED'}

    def expectation_history(self, component, scope):
        with self.store._connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM runtime_expectation_changes '
                        'WHERE component=? AND scope=? ORDER BY epoch',(component,scope))]

    def retire(self, component, scope, *, expected_epoch, actor, reason, now):
        if (type(expected_epoch) is not int or expected_epoch < 1 or not isinstance(actor,str)
                or not actor.strip() or not isinstance(reason,str) or not reason.strip()):
            raise ValueError('retirement requires epoch, actor and reason')
        actor, reason = redact(actor.strip()), redact(reason.strip())
        when = timestamp(pd.Timestamp(now).isoformat())
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM runtime_expectation_lifecycle WHERE component=? AND scope=?',
                             (component,scope)).fetchone()
            if old is None:
                raise ValueError('expectation is not registered')
            if (old['status'] == 'RETIRED' and old['epoch'] == expected_epoch+1
                    and old['actor'] == actor and old['reason'] == reason):
                return dict(old)
            if old['status'] != 'ACTIVE' or old['epoch'] != expected_epoch:
                raise ValueError('expectation retirement revision conflict')
            if pd.Timestamp(when) < pd.Timestamp(old['changed_at']):
                raise ValueError('expectation retirement clock moved backwards')
            pulse = db.execute('SELECT pid FROM runtime_heartbeats WHERE component=? AND scope=?',
                               (component,scope)).fetchone()
            if pulse is not None and _owner_process_state({'pid':pulse['pid']}) is not False:
                raise ValueError('expectation owner is alive or unknown')
            if component == 'scanner':
                symbol, separator, frame = scope.rpartition(':')
                if not separator or not symbol:
                    raise ValueError('scanner expectation scope is invalid')
                task = db.execute("SELECT 1 FROM runtime_tasks WHERE symbol=? AND timeframe=? AND finished_at IS NULL "
                                  "AND status IN ('RUNNING','TIMED_OUT')",(symbol,timeframe(frame))).fetchone()
                if task is not None:
                    raise ValueError('expectation has unfinished tasks')
            epoch = expected_epoch+1
            db.execute('UPDATE runtime_expectation_lifecycle SET epoch=?,status=?,changed_at=?,actor=?,reason=? '
                       'WHERE component=? AND scope=?',(epoch,'RETIRED',when,actor,reason,component,scope))
            db.execute('INSERT INTO runtime_expectation_changes VALUES(?,?,?,?,?,?,?)',
                       (component,scope,epoch,'RETIRED',when,actor,reason))
            result = dict(db.execute('SELECT * FROM runtime_expectation_lifecycle WHERE component=? AND scope=?',
                                     (component,scope)).fetchone())
            db.commit()
            return result

    def expected_components(self):
        return [(row['component'],row['scope'],row['max_age']) for row in self.expectations()]

    def consecutive_failures(self, symbol, frame):
        symbol, frame = symbol.upper(), timeframe(frame)
        with self.store._connection() as db:
            row = db.execute('''SELECT COUNT(*) FROM runtime_tasks WHERE symbol=? AND timeframe=?
                AND finished_at IS NOT NULL AND status IN ('FAILED','TIMED_OUT','INTERRUPTED')
                AND rowid > COALESCE((SELECT MAX(rowid) FROM runtime_tasks
                    WHERE symbol=? AND timeframe=? AND status='SUCCESS' AND finished_at IS NOT NULL),0)''',
                (symbol,frame,symbol,frame)).fetchone()
        return row[0]
