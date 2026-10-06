"""State-based health events share the decision/event/outbox transaction."""
from __future__ import annotations

from dataclasses import replace
import math
from threading import RLock

import pandas as pd

from inv_trend.observability.脱敏 import redact

from .事件存储 import StateConflictError
from .执行记录 import RuntimeLedger
from .模型 import DecisionSnapshot, Event, EventKind, digest, timeframe


class HealthRecorder:
    def __init__(self, ledger: RuntimeLedger, *, code_version, config_hash):
        self.ledger, self.store = ledger, ledger.store
        self.code, self.config_hash = code_version, config_hash
        self._lock = RLock()

    def observe(self, component, scope, code, *, now, severity='INFO', details=None,
                kind=EventKind.SYSTEM_HEALTH, pulse=True, stable_details=False):
        if kind not in {EventKind.SYSTEM_HEALTH, EventKind.NOTIFICATION_HEALTH, EventKind.DATA_HEALTH}:
            raise ValueError('health recorder accepts only typed health events')
        details = redact(details or {})
        if pulse:
            self.ledger.pulse(component, scope, now=now, details={'status': code, **details})
        symbol = '_HEALTH_' + digest({'component': component, 'scope': scope})[:24].upper()
        with self._lock:
            for attempt in range(4):
                before = self.store.state(symbol, 'D1')
                old = before.details.get('health', {})
                changed = (old.get('code'), old.get('severity')) != (code, severity)
                generation = old.get('generation', 0) + int(changed)
                current = {'code': code, 'severity': severity, 'generation': generation, 'details': details}
                if stable_details and not changed:
                    current = dict(old)
                after = replace(before, revision=before.revision + 1,
                                details=dict(before.details) | {'health': current})
                event = Event(kind, code, severity, {'component': component, 'scope': scope, **details}) if changed else None
                snapshot = DecisionSnapshot('system:' + component + ':' + scope, symbol, 'D1', None,
                    pd.Timestamp(now).isoformat(), None, 'monitoring-health-v1', self.config_hash,
                    self.code.git_commit, self.code.source_hash, None, {},
                    {'quality_gate': False, 'freshness_gate': False}, before.phase, after.phase, (code,), event,
                    context={'mode': 'live', 'component': component, 'scope': scope, 'health': current},
                    state_context={'before': before.to_dict()})
                try:
                    return self.store.commit(snapshot, before, after)
                except StateConflictError:
                    if attempt == 3:
                        raise

    def check_heartbeats(self, expectations, *, now=None, clock=None):
        """An independent watchdog can detect a stalled scheduler or scanner."""
        if (now is None) == (clock is None):
            raise ValueError('provide exactly one heartbeat observation time or clock')
        before_read = pd.Timestamp(clock()) if clock is not None else pd.Timestamp(now)
        if before_read.tzinfo is None or pd.isna(before_read):
            raise ValueError('heartbeat observation time must be timezone aware')
        pulses = {(row['component'], row['scope']): row for row in self.ledger.heartbeats()}
        registered = {(row['component'],row['scope']): row for row in self.ledger.expectations(include_retired=True)}
        observed_at = pd.Timestamp(clock()) if clock is not None else before_read
        if observed_at.tzinfo is None or pd.isna(observed_at) or observed_at < before_read:
            raise ValueError('heartbeat observation clock moved backward or is invalid')
        results = []
        for component, scope, max_age in expectations:
            if isinstance(max_age, bool) or not math.isfinite(max_age) or max_age <= 0:
                raise ValueError('heartbeat age must be finite and positive')
            row = pulses.get((component, scope))
            expected = registered.get((component,scope))
            if expected is not None and expected['status'] == 'RETIRED':
                continue
            if (expected is not None and expected['epoch'] > 1 and row is not None
                    and pd.Timestamp(row['observed_at']) < pd.Timestamp(expected['registered_at'])):
                row = None
            age = None if row is None else (observed_at - pd.Timestamp(row['observed_at'])).total_seconds()
            good = age is not None and 0 <= age <= max_age
            startup_age = None if expected is None else (observed_at - pd.Timestamp(expected['registered_at'])).total_seconds()
            starting = row is None and startup_age is not None and 0 <= startup_age <= max_age
            result = self.observe('heartbeat', component + ':' + scope,
                                  'HEARTBEAT_RECOVERED' if good else 'HEARTBEAT_STARTING' if starting else 'HEARTBEAT_MISSING', now=observed_at,
                                  severity='INFO' if good or starting else 'ERROR', pulse=False, stable_details=True,
                                  details={'component': component, 'scope': scope,
                                           'last_seen': row['observed_at'] if row else None,
                                           'max_age_seconds': max_age})
            results.append(result)
        return results

    def check_failure_streak(self, symbol, frame, threshold, *, now):
        if type(threshold) is not int or threshold <= 0:
            raise ValueError('failure threshold must be a positive integer')
        scope = symbol.upper() + ':' + timeframe(frame)
        count = self.ledger.consecutive_failures(symbol,frame)
        identity = '_HEALTH_' + digest({'component':'scan-failures','scope':scope})[:24].upper()
        old = self.store.state(identity,'D1').details.get('health',{})
        if count < threshold and old.get('code') != 'CONSECUTIVE_SCAN_FAILURES':
            return None
        # A partially improved streak remains failed until an actual success.
        if count and count < threshold:
            return None
        return self.observe('scan-failures',scope,
            'CONSECUTIVE_SCAN_FAILURES' if count >= threshold else 'SCAN_FAILURES_RECOVERED',
            now=now, severity='ERROR' if count >= threshold else 'INFO', pulse=False, stable_details=True,
            details={'count_at_transition':count,'threshold':threshold})
