"""Bounded, non-overlapping update/load/scan jobs and an independent scheduler."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
from pathlib import Path
import sqlite3
from threading import Event as ThreadEvent, Lock
import time

from inv_trend.data.locking import FileLock
from inv_trend.data.models import DataLineageError, DataQualityError
from inv_trend.observability.脱敏 import redact

from .执行记录 import RuntimeLedger
from .健康 import HealthRecorder
from .模型 import EventKind, digest
from .策略适配 import ReferenceAccount
from .运行配置 import RuntimeConfig, Target


class ScanTimeoutError(TimeoutError):
    pass


class RuntimeStoppedError(RuntimeError):
    pass


@dataclass
class _JobGuard:
    timeout: float
    monotonic: object
    stopped: ThreadEvent
    cancelled: ThreadEvent = field(default_factory=ThreadEvent)
    started: float | None = None
    timeout_recorded: bool = False
    decision_committed: bool = False

    def check(self):
        if self.stopped.is_set():
            raise RuntimeStoppedError('runtime stopping before decision commit')
        if self.cancelled.is_set() or (self.started is not None and self.monotonic() - self.started > self.timeout):
            raise ScanTimeoutError('scan deadline exceeded')


class MonitoringRuntime:
    def __init__(self, scanner, config: RuntimeConfig, *, updater=None, account_source=None,
                 clock=None, monotonic=None):
        if scanner.store.purpose != 'live':
            raise ValueError('monitoring runtime requires a live scanner')
        self.scanner, self.config = scanner, config
        self.repository = scanner.repository
        if (hasattr(self.repository, 'lake')
                and Path(str(self.repository.lake.root)).resolve() != config.data_root):
            raise ValueError('runtime data root differs from its Repository')
        self.targets = config.targets(self.repository)
        if any(target.symbol not in scanner.evaluator.engine.specs for target in self.targets):
            raise ValueError('every monitoring target requires a shared strategy asset specification')
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic or time.monotonic
        self.updater = updater or getattr(self.repository, 'update', None)
        if config.refresh and self.updater is None:
            raise ValueError('refresh requires the Repository update interface')
        self.account_source = account_source or (lambda target, now: ReferenceAccount(
            'paper', config.paper_equity) if config.paper_equity is not None else ReferenceAccount())
        self.ledger = RuntimeLedger(scanner.store, config.data_root)
        self.health = HealthRecorder(self.ledger, code_version=scanner.code, config_hash=config.config_hash)
        self.ledger.expect('scheduler','main',config.heartbeat_timeout_seconds,now=self.clock())
        for target in self.targets:
            self.ledger.expect('scanner',target.key,config.interval_seconds + config.scan_timeout_seconds +
                               config.heartbeat_timeout_seconds,now=self.clock())
        self._stop = ThreadEvent()
        self._pool = ThreadPoolExecutor(max_workers=config.workers, thread_name_prefix='trend-monitor')
        self._scheduler_lock = Lock()
        self._inflight = {}
        self._next_due = {target.key: 0. for target in self.targets}
        self._closed = False
        self.log = logging.getLogger('inv_trend.monitoring')
        recovered = self.ledger.recover(now=self.clock())
        if recovered:
            self.health.observe('runtime', 'startup', 'TASKS_INTERRUPTED', now=self.clock(),
                                severity='WARNING', details={'recovered_jobs': recovered})
            for target in self.targets:
                self.health.check_failure_streak(target.symbol,target.timeframe,
                                                 config.consecutive_failure_threshold,now=self.clock())

    def _target_lock(self, target):
        instrument = self.repository.instrument(target.symbol)
        key = digest({'instrument_id': instrument.instrument_id, 'timeframe': target.timeframe})
        return FileLock(self.config.data_root / '.monitoring_locks' / (key + '.lock'),
                        timeout=self.config.lock_timeout_seconds)

    def scan_target(self, target: Target, *, guard=None):
        if target not in self.targets:
            raise ValueError('target is not enabled by runtime configuration')
        guard = guard or _JobGuard(self.config.scan_timeout_seconds, self.monotonic, self._stop)
        guard.started = self.monotonic()
        guard.check()
        lock = self._target_lock(target)
        try:
            lock.acquire()
        except TimeoutError:
            self.health.observe('lock', target.key, 'LOCK_BUSY', now=self.clock(), severity='WARNING')
            return {'target': target.key, 'status': 'SKIPPED', 'reason': 'LOCK_BUSY'}
        try:
            self.ledger.recover(now=self.clock())
            try:
                job_id = self.ledger.begin(target.symbol, target.timeframe, now=self.clock(),
                                            details={'refresh': self.config.refresh,
                                                     'runtime_config_hash': self.config.config_hash})
            except sqlite3.IntegrityError:
                # The durable active-job guard remains authoritative even if
                # an owner's liveness is unknown after a released file lock.
                self.health.observe('lock', target.key, 'ACTIVE_TASK_EXISTS', now=self.clock(), severity='WARNING')
                return {'target': target.key, 'status': 'SKIPPED', 'reason': 'ACTIVE_TASK_EXISTS'}
            phase, result, status, failure = 'scanner', None, 'FAILED', None
            try:
                self.health.observe('lock', target.key, 'LOCK_READY', now=self.clock())
                self.ledger.pulse('scanner', target.key, now=self.clock(), details={'job_id': job_id, 'status': 'RUNNING'})
                guard.check()
                if self.config.refresh:
                    phase = 'update'
                    manifest = self.updater(target.symbol, target.timeframe, end=self.clock(),
                                            retries=self.config.provider_retries)
                    if manifest.quality_passed is not True or manifest.quality_status != 'CURATED':
                        raise DataQualityError('updated data is not formally certified: ' + manifest.quality_status)
                phase = 'scanner'
                guard.check()
                account = self.account_source(target, self.clock())
                result = self.scanner.scan(target.symbol, target.timeframe, now=self.clock(),
                                            account=account, guard=guard.check)
                guard.decision_committed = True
                status = 'SUCCESS' if result['snapshot']['conditions'].get('quality_gate') is True else 'FAILED'
            except Exception as exc:
                # Provider and strategy boundaries record explicit failure;
                # KeyboardInterrupt/SystemExit are intentionally not swallowed.
                failure = redact(str(exc))
                if isinstance(exc, RuntimeStoppedError):
                    status, category, kind = 'INTERRUPTED', 'SCANNER_INTERRUPTED', EventKind.SYSTEM_HEALTH
                elif isinstance(exc, ScanTimeoutError):
                    status, category, kind = 'TIMED_OUT', 'SCAN_TIMEOUT', EventKind.SYSTEM_HEALTH
                elif isinstance(exc, (DataLineageError, DataQualityError)):
                    category, kind = None, EventKind.DATA_HEALTH
                elif phase == 'update':
                    category, kind = 'PROVIDER_FAILURE', EventKind.DATA_HEALTH
                else:
                    category, kind = 'SCANNER_FAILURE', EventKind.SYSTEM_HEALTH
                self.log.error('%s %s: %s', category or type(exc).__name__, target.key, failure)
                result = self.scanner.record_failure(self.repository.instrument(target.symbol), target.timeframe,
                            now=self.clock(), error=exc, category=category, kind=kind)
            finally:
                decision_id = result['snapshot']['decision_id'] if result else None
                self.ledger.finish(job_id, now=self.clock(), status=status, decision_id=decision_id,
                                   details={'error': failure} if failure else {})
                self.ledger.pulse('scanner', target.key, now=self.clock(),
                                  details={'job_id': job_id, 'status': status, 'decision_id': decision_id})
                self.health.check_failure_streak(target.symbol,target.timeframe,
                    self.config.consecutive_failure_threshold,now=self.clock())
            return {'target': target.key, 'job_id': job_id, 'status': status, **(result or {})}
        finally:
            lock.release()

    def _submit(self, target):
        if target.key in self._inflight:
            return False
        guard = _JobGuard(self.config.scan_timeout_seconds, self.monotonic, self._stop)
        future = self._pool.submit(self.scan_target, target, guard=guard)
        self._inflight[target.key] = (target, future, guard)
        return True

    def _progress(self):
        now = self.clock()
        self.ledger.pulse('scheduler', 'main', now=now, details={'active_jobs': len(self._inflight)})
        results = []
        for key, (target, future, guard) in list(self._inflight.items()):
            if future.done():
                try:
                    results.append(future.result())
                except Exception as exc:
                    self.health.observe('scheduler', key, 'SCANNER_RUNTIME_FAILURE', now=now, severity='CRITICAL',
                                        details={'error_type': type(exc).__name__, 'message': redact(str(exc))})
                    self.log.error('SCANNER_RUNTIME_FAILURE %s: %s', key, redact(str(exc)))
                    results.append({'target': key, 'status': 'FAILED', 'reason': type(exc).__name__})
                del self._inflight[key]
            elif (guard.started is not None and not guard.decision_committed
                  and self.monotonic() - guard.started > guard.timeout and not guard.timeout_recorded):
                guard.cancelled.set()
                guard.timeout_recorded = True
                self.scanner.record_failure(self.repository.instrument(target.symbol), target.timeframe,
                    now=now, error=ScanTimeoutError('scan deadline exceeded'), category='SCAN_TIMEOUT', kind=EventKind.SYSTEM_HEALTH)
                # A worker can pulse after the scheduler sampled `now` above.
                # Preserve that newer evidence instead of overwriting it.
                self.ledger.pulse('scanner', target.key, now=now,
                                  details={'status': 'TIMED_OUT'}, ignore_stale=True)
        return results

    def tick(self):
        if self._closed or self._stop.is_set():
            raise RuntimeStoppedError('scheduler is closed')
        if not self._scheduler_lock.acquire(blocking=False):
            return {'status': 'BUSY', 'submitted': [], 'completed': []}
        try:
            completed = self._progress()
            self.health.observe('scheduler', 'main', 'SCHEDULER_READY', now=self.clock(), pulse=False)
            submitted = []
            for target in self.targets:
                if self.monotonic() >= self._next_due[target.key] and self._submit(target):
                    self._next_due[target.key] = self.monotonic() + self.config.interval_seconds
                    submitted.append(target.key)
            return {'status': 'RUNNING', 'submitted': submitted, 'completed': completed}
        except Exception as exc:
            self.log.error('SCHEDULER_FAILURE: %s', redact(str(exc)))
            self.health.observe('scheduler', 'main', 'SCHEDULER_FAILURE', now=self.clock(), severity='CRITICAL',
                                details={'error_type': type(exc).__name__, 'message': redact(str(exc))}, pulse=False)
            raise
        finally:
            self._scheduler_lock.release()

    def run_once(self):
        if self._closed:
            raise RuntimeStoppedError('runtime is closed')
        if not self._scheduler_lock.acquire(blocking=False):
            raise RuntimeError('a scheduler cycle is already active')
        try:
            for target in self.targets:
                self._submit(target)
            results = []
            while self._inflight:
                results.extend(self._progress())
                if self._inflight:
                    self._stop.wait(min(self.config.poll_seconds, .25))
            return results
        finally:
            self._scheduler_lock.release()

    def serve(self, *, stop_requested=None):
        while not self._stop.is_set() and not (stop_requested and stop_requested()):
            self.tick()
            self.health.check_heartbeats([('scheduler', 'main', self.config.heartbeat_timeout_seconds)] +
                [('scanner', target.key, self.config.interval_seconds + self.config.scan_timeout_seconds +
                  self.config.heartbeat_timeout_seconds) for target in self.targets], clock=self.clock)
            self._stop.wait(self.config.poll_seconds)

    def close(self):
        if not self._closed:
            self._stop.set()
            self._pool.shutdown(wait=True, cancel_futures=True)
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
