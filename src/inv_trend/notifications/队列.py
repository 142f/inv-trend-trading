"""Durable delivery leases; HTTP always runs outside the scan transaction."""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import uuid

import pandas as pd

from inv_trend.monitoring.模型 import DecisionSnapshot, digest, timestamp
from inv_trend.observability.脱敏 import redact
from .渠道 import NotificationError


@dataclass(frozen=True)
class NotificationPolicy:
    setup: bool = False
    candidate: bool = True
    confirmed_entry: bool = True
    exit: bool = True
    idle: bool = False
    position: bool = True

    def __post_init__(self):
        if any(type(value) is not bool for value in vars(self).values()):
            raise ValueError('notification state controls must be boolean')

    def allows(self, event):
        kind = event['event_type']
        # Delivery-health events remain auditable without generating a loop
        # of undeliverable alerts on the same failing channel.
        if kind == 'NOTIFICATION_HEALTH':
            return False
        if kind in {'DATA_HEALTH', 'SYSTEM_HEALTH'}:
            return event['severity'] in {'WARNING', 'ERROR', 'CRITICAL'}
        return getattr(self, event['state_after'].lower())


class NotificationQueue:
    def __init__(self, store):
        if store.purpose != 'live':
            raise ValueError('replay storage cannot deliver notifications')
        self.store = store

    @staticmethod
    def _envelope(row):
        snapshot = DecisionSnapshot.from_dict(json.loads(row['decision_payload']))
        event = json.loads(row['event_payload'])
        if digest({'decision_id': snapshot.decision_id, 'event': event}) != row['event_id']:
            raise ValueError('notification event content hash does not match')
        if event not in [item.to_dict() for item in
                         ((snapshot.event,) if snapshot.event else ()) + snapshot.additional_events]:
            raise ValueError('notification event is not bound to its decision')
        evidence = snapshot.to_dict()
        return ({key: evidence[key] for key in (
            'instrument_id', 'symbol', 'timeframe', 'bar_time', 'dataset_version',
            'strategy_version', 'config_hash', 'git_commit', 'code_hash', 'market',
            'indicators', 'conditions', 'state_before', 'state_after', 'reasons')}
        | {'event_id': row['event_id'], 'dedup_key': row['event_id'],
           'snapshot_id': snapshot.decision_id, 'created_at': snapshot.evaluated_at,
           'event_type': event['kind'], 'event_name': event['code'],
           'severity': event['severity'], 'details': redact(event['details']),
           'delivered_at': row['delivered_at'], 'delivery_status': row['status']})

    def claim(self, channel_id, *, now, lease_seconds, policy, max_attempts):
        if (not isinstance(channel_id, str) or not channel_id or type(max_attempts) is not int or
                max_attempts < 1 or isinstance(lease_seconds, bool) or
                not math.isfinite(lease_seconds) or lease_seconds <= 0):
            raise ValueError('notification claim identity, budget or lease is invalid')
        clock = timestamp(now)
        deadline = (pd.Timestamp(clock) + pd.Timedelta(seconds=lease_seconds)).isoformat()
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT purpose FROM monitoring_mode WHERE id=1').fetchone()[0] != 'live':
                raise ValueError('persisted notification storage is not live')
            # Timestamps are parsed, not compared as strings: offsets can differ.
            rows = db.execute('''SELECT o.*, e.payload AS event_payload,
                d.payload AS decision_payload FROM outbox o
                JOIN events e ON e.event_id=o.event_id
                JOIN decisions d ON d.decision_id=e.decision_id
                WHERE o.status IN ('PENDING','RETRY','IN_FLIGHT')
                AND (o.channel_id IS NULL OR o.channel_id=?) ORDER BY o.rowid''',
                (channel_id,)).fetchall()
            for row in rows:
                if row['status'] == 'IN_FLIGHT':
                    if row['lease_until'] and pd.Timestamp(row['lease_until']) > pd.Timestamp(clock):
                        continue
                    db.execute("UPDATE notification_attempts SET status='LEASE_EXPIRED',finished_at=? "
                               "WHERE lease_token=? AND status='IN_FLIGHT'", (clock, row['lease_token']))
                elif row['next_attempt'] and pd.Timestamp(row['next_attempt']) > pd.Timestamp(clock):
                    continue
                if row['attempts'] >= max_attempts:
                    db.execute("UPDATE outbox SET status='FAILED',lease_token=NULL,lease_until=NULL "
                               'WHERE notification_id=?', (row['notification_id'],))
                    continue
                try:
                    envelope = self._envelope(row)
                except (ValueError, TypeError, KeyError) as exc:
                    db.execute("UPDATE outbox SET status='FAILED',last_error=? WHERE notification_id=?",
                               (redact(str(exc)), row['notification_id']))
                    continue
                if not policy.allows(envelope):
                    db.execute("UPDATE outbox SET status='SUPPRESSED',channel_id=?,lease_token=NULL,"
                               'lease_until=NULL WHERE notification_id=?', (channel_id, row['notification_id']))
                    continue
                token = uuid.uuid4().hex
                attempt = row['attempts'] + 1
                db.execute("UPDATE outbox SET status='IN_FLIGHT',attempts=?,channel_id=?,lease_token=?,"
                           'lease_until=?,next_attempt=NULL WHERE notification_id=?',
                           (attempt, channel_id, token, deadline, row['notification_id']))
                db.execute('INSERT INTO notification_attempts '
                           '(lease_token,notification_id,channel_id,attempt,started_at,status) VALUES(?,?,?,?,?,?)',
                           (token, row['notification_id'], channel_id, attempt, clock, 'IN_FLIGHT'))
                db.commit()
                return {'notification_id': row['notification_id'], 'lease_token': token,
                        'attempt': attempt, 'envelope': envelope | {'delivery_status': 'IN_FLIGHT'}}
            db.commit()
        return None

    def finish(self, claim, *, now, receipt=None, error=None, max_attempts, retry_seconds):
        clock = timestamp(now)
        with self.store._connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM outbox WHERE notification_id=?',
                             (claim['notification_id'],)).fetchone()
            if (not row or row['status'] != 'IN_FLIGHT' or row['lease_token'] != claim['lease_token']
                    or pd.Timestamp(row['lease_until']) <= pd.Timestamp(clock)):
                db.commit()
                return False
            if receipt is not None:
                if (receipt.event_id != row['event_id'] or receipt.channel_id != row['channel_id']
                        or not 200 <= receipt.http_status < 300):
                    raise ValueError('delivery receipt does not match the claimed event/channel')
                status, message, due, delivered = 'DELIVERED', None, None, clock
                http_status, remote_id = receipt.http_status, redact(receipt.remote_request_id)
            elif error is not None:
                status = 'RETRY' if error.retryable and row['attempts'] < max_attempts else 'FAILED'
                message, delivered = redact(str(error)), None
                due = ((pd.Timestamp(clock) + pd.Timedelta(seconds=retry_seconds)).isoformat()
                       if status == 'RETRY' else None)
                http_status, remote_id = error.http_status, None
            else:
                raise ValueError('delivery completion requires a receipt or error')
            db.execute('UPDATE outbox SET status=?,last_error=?,next_attempt=?,delivered_at=?, '
                       'lease_token=NULL,lease_until=NULL WHERE notification_id=?',
                       (status, message, due, delivered, claim['notification_id']))
            db.execute('UPDATE notification_attempts SET status=?,finished_at=?,error=?,http_status=?, '
                       'remote_request_id=? WHERE lease_token=?',
                       (status, clock, message, http_status, remote_id, claim['lease_token']))
            db.commit()
        return True


class NotificationWorker:
    def __init__(self, store, channel, *, policy=None, max_attempts=5,
                 lease_seconds=30., retry_seconds=5., max_retry_seconds=300., health=None,
                 clock=None, heartbeat_timeout_seconds=180.):
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError('notification attempts must be a positive integer')
        for value in (lease_seconds, retry_seconds, max_retry_seconds, heartbeat_timeout_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError('notification timing must be finite and positive')
        if lease_seconds <= getattr(channel, 'timeout_seconds', 0):
            raise ValueError('notification lease must exceed channel timeout')
        if not isinstance(channel.channel_id, str) or not channel.channel_id:
            raise ValueError('notification channel identity is required')
        self.queue, self.channel = NotificationQueue(store), channel
        self.policy = policy or NotificationPolicy()
        self.max_attempts, self.lease_seconds = max_attempts, lease_seconds
        self.retry_seconds, self.max_retry_seconds = retry_seconds, max_retry_seconds
        self.health, self.clock = health, clock or (lambda: pd.Timestamp.now(tz='UTC').isoformat())
        if health is not None:
            health.ledger.expect('notification', channel.channel_id, heartbeat_timeout_seconds, now=self.clock())

    def run_once(self):
        if self.health is not None:
            self.health.ledger.pulse('notification', self.channel.channel_id, now=self.clock())
        claim = self.queue.claim(self.channel.channel_id, now=self.clock(),
                                 lease_seconds=self.lease_seconds, policy=self.policy,
                                 max_attempts=self.max_attempts)
        if claim is None:
            return None
        receipt, error = None, None
        try:
            receipt = self.channel.send(claim['envelope'])
            if (receipt.channel_id != self.channel.channel_id
                    or receipt.event_id != claim['envelope']['event_id']
                    or not 200 <= receipt.http_status < 300):
                raise NotificationError('notification adapter returned an invalid receipt', retryable=False)
        except NotificationError as exc:
            error = exc
        except Exception as exc:
            # Adapter bugs are durable failures, never swallowed or retried forever.
            error = NotificationError('notification adapter failed: ' + str(exc), retryable=False)
        delay = min(self.max_retry_seconds, self.retry_seconds * 2 ** min(claim['attempt'] - 1, 30))
        now = self.clock()
        accepted = self.queue.finish(claim, now=now, receipt=receipt if error is None else None,
                                     error=error, max_attempts=self.max_attempts, retry_seconds=delay)
        if self.health is not None:
            from inv_trend.monitoring.模型 import EventKind
            self.health.observe('notification', self.channel.channel_id,
                'NOTIFICATION_FAILURE' if error or not accepted else 'NOTIFICATION_RECOVERED',
                now=now, severity='ERROR' if error or not accepted else 'INFO',
                kind=EventKind.NOTIFICATION_HEALTH,
                details={'event_id': claim['envelope']['event_id'], 'attempt': claim['attempt'],
                         'lease_accepted': accepted, 'error': str(error) if error else None})
        return {'event_id': claim['envelope']['event_id'], 'accepted': accepted,
                'error': str(error) if error else None}
