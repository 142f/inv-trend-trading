"""Read-only FastAPI views over verified Repository metadata and recorded evidence."""
from __future__ import annotations

from contextlib import contextmanager
from html import escape
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
import pandas as pd

from inv_trend.data.models import CatalogAccessError, CatalogBackendConflictError, DataUnavailableError, DataLineageError
from inv_trend.observability.脱敏 import redact
from .模型 import DecisionSnapshot, StateRecord, timeframe
from .新鲜度 import MonitoringFreshnessPolicy
from .策略适配 import ReferenceAccount


class DashboardReader:
    def __init__(self, store, config, repository=None, *, repository_error=None, clock=None):
        if store.purpose != 'live':
            raise ValueError('dashboard requires live monitoring storage')
        self.store, self.config, self.repository = store, config, repository
        self.repository_error = redact(repository_error) if repository_error else None
        self.clock = clock or (lambda: pd.Timestamp.now(tz='UTC'))
        with self._read() as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'runtime_metadata' in tables:
                bound = db.execute("SELECT value FROM runtime_metadata WHERE key='data_root'").fetchone()
                if bound is None or bound[0] != str(config.data_root):
                    raise ValueError('dashboard data root differs from its runtime store binding')
        if repository is not None and Path(str(repository.lake.root)).resolve() != config.data_root:
            raise ValueError('dashboard data root differs from its Repository')

    @contextmanager
    def _read(self):
        db = sqlite3.connect(self.store.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            yield db
        finally:
            db.close()

    def _diagnose(self, symbol, frame):
        if self.repository is None:
            return {'status': 'REPOSITORY_UNAVAILABLE', 'error': self.repository_error or 'Repository unavailable'}
        try:
            result = self.repository.diagnose_current(symbol, frame)
            return {'status': 'VERIFIED' if result['certified'] else 'QUALITY_FAILURE', **result}
        except DataUnavailableError as exc:
            return {'status': 'DATA_MISSING', 'error': redact(str(exc))}
        except (CatalogAccessError, CatalogBackendConflictError) as exc:
            return {'status': 'CATALOG_FAILURE', 'error': redact(str(exc))}
        except DataLineageError as exc:
            return {'status': 'MANIFEST_FAILURE', 'error': redact(str(exc))}
        except Exception as exc:
            return {'status': 'REPOSITORY_FAILURE', 'error': redact(str(exc))}

    @staticmethod
    def _payload(row):
        return json.loads(row['payload']) if row else None

    def _target(self, db, symbol, frame, *, detail=False):
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        state_row = db.execute('SELECT payload FROM monitoring_state WHERE symbol=? AND timeframe=?',
                               (symbol, frame)).fetchone()
        state = StateRecord.from_dict(self._payload(state_row)) if state_row else StateRecord(symbol, frame)
        decision_row = db.execute('SELECT payload FROM decisions WHERE symbol=? AND timeframe=? '
                                  'ORDER BY rowid DESC LIMIT 1', (symbol, frame)).fetchone()
        error, snapshot = None, None
        if decision_row:
            try:
                saved = DecisionSnapshot.from_dict(self._payload(decision_row))
                if (saved.symbol, saved.timeframe) != (symbol, frame):
                    raise ValueError('dashboard decision identity mismatch')
                if self.repository is not None and saved.instrument_id != self.repository.instrument(symbol).instrument_id:
                    raise ValueError('dashboard decision instrument differs from registry')
                snapshot = saved.to_dict() | {'decision_id': saved.decision_id}
            except (ValueError, TypeError, KeyError) as exc:
                error = redact(str(exc))
        events = [dict(row) | {'event': self._payload(row)} for row in db.execute(
            'SELECT e.*, o.status AS delivery_status,o.attempts,o.delivered_at,o.last_error '
            'FROM events e JOIN decisions d ON d.decision_id=e.decision_id '
            'LEFT JOIN outbox o ON o.event_id=e.event_id WHERE d.symbol=? AND d.timeframe=? '
            'ORDER BY e.rowid DESC LIMIT ?', (symbol, frame, 50 if detail else 1))]
        tasks = [dict(row) for row in db.execute('SELECT * FROM runtime_tasks WHERE symbol=? AND timeframe=? '
                    'ORDER BY rowid DESC LIMIT ?', (symbol, frame, 50 if detail else 1))] if 'runtime_tasks' in tables else []
        for task in tasks:
            task['details'] = json.loads(task['details'])
        latest = tasks[0] if tasks else None
        last_scan = latest['finished_at'] or latest['started_at'] if latest else None
        metadata = self._diagnose(symbol, frame)
        health = state.details.get('data_health', 'UNKNOWN')
        blockers = []
        if error:
            blockers.append('SNAPSHOT_FAILURE')
        if metadata['status'] != 'VERIFIED':
            health = metadata['status']
            blockers.append(health)
        if not snapshot:
            blockers.append('NO_DECISION')
        else:
            if metadata.get('dataset_version') != snapshot['dataset_version']:
                blockers.append('VERSION_CHANGED')
            if snapshot['conditions'].get('quality_gate') is not True:
                blockers.append('QUALITY_GATE_BLOCKED')
            if state.phase.value == 'CONFIRMED_ENTRY' and any(
                    snapshot['conditions'].get(name) is not True for name in
                    ('close_confirmation', 'trend_gate', 'execution_gate', 'freshness_gate')):
                blockers.append('FINAL_GATES_BLOCKED')
            if state.phase.value == 'CONFIRMED_ENTRY':
                age_limit = snapshot['context'].get('account_max_age_seconds')
                account = snapshot['context'].get('account')
                if (not isinstance(age_limit, (float, int)) or isinstance(age_limit, bool) or age_limit <= 0 or
                        not account or not ReferenceAccount.from_dict(account).is_known(
                            pd.Timestamp(self.clock()).to_pydatetime(), age_limit)):
                    blockers.append('ACCOUNT_OBSERVATION_UNKNOWN')
            if self.repository is not None and snapshot['bar_time']:
                fresh = MonitoringFreshnessPolicy().evaluate(self.repository.instrument(symbol), frame,
                                    snapshot['bar_time'], now=pd.Timestamp(self.clock()).to_pydatetime())
                if not fresh.is_fresh:
                    health = 'STALE_DATA' if metadata['status'] == 'VERIFIED' else health
                    blockers.append('STALE_DATA')
            else:
                blockers.append('NO_VERIFIED_BAR')
        if health != 'FRESH':
            blockers.append(health)
        age = None if last_scan is None else (pd.Timestamp(self.clock()) - pd.Timestamp(last_scan)).total_seconds()
        max_age = self.config.interval_seconds + self.config.scan_timeout_seconds + self.config.heartbeat_timeout_seconds
        if age is None or age < 0 or age > max_age or latest['status'] != 'SUCCESS':
            blockers.append('SCAN_NOT_CURRENT')
        row = {'asset': symbol, 'timeframe': frame, 'data_health': health,
            'trend': ('UP' if state.direction > 0 else 'DOWN' if state.direction < 0 else 'UNKNOWN'),
            'current_signal': 'BLOCKED' if blockers else state.phase.value,
            'monitoring_state': state.phase.value, 'blockers': sorted(set(blockers)),
            'last_bar': snapshot['bar_time'] if snapshot else None, 'last_scan': last_scan,
            'last_event': events[0] if events else None,
            'snapshot_id': snapshot['decision_id'] if snapshot else None,
            'dataset_version': snapshot['dataset_version'] if snapshot else None,
            'current_dataset_version': metadata.get('dataset_version'), 'snapshot_error': error}
        if detail:
            row.update(snapshot=redact(snapshot), state=state.to_dict(), metadata=redact(metadata),
                       events=redact(events), run_history=redact(tasks))
        return row

    def overview(self):
        with self._read() as db:
            if self.repository is not None:
                targets = [(item.symbol, item.timeframe) for item in self.config.targets(self.repository)]
            elif self.config.symbols:
                targets = [(symbol, frame) for symbol in self.config.symbols for frame in self.config.timeframes]
            else:
                targets = [(row['symbol'], row['timeframe']) for row in db.execute(
                    "SELECT symbol,timeframe FROM monitoring_state WHERE symbol NOT LIKE '_HEALTH_%' ORDER BY symbol,timeframe")]
            rows = [self._target(db, *target) for target in targets]
            health = [json.loads(row['payload']) for row in db.execute(
                "SELECT payload FROM monitoring_state WHERE symbol LIKE '_HEALTH_%' ORDER BY symbol")]
            queue = {row['status']: row['total'] for row in db.execute(
                'SELECT status,COUNT(*) AS total FROM outbox GROUP BY status')}
        return {'as_of': pd.Timestamp(self.clock()).isoformat(), 'repository_error': self.repository_error,
                'targets': rows, 'system_health': redact(health), 'notification_queue': queue}

    def detail(self, symbol, frame):
        symbol, frame = symbol.upper(), timeframe(frame)
        if self.repository is not None:
            permitted = {(item.symbol, item.timeframe) for item in self.config.targets(self.repository)}
        else:
            permitted = {(row['asset'], row['timeframe']) for row in self.overview()['targets']}
        if (symbol, frame) not in permitted:
            raise KeyError('unconfigured dashboard target')
        with self._read() as db:
            return self._target(db, symbol, frame, detail=True)


def _page(title, body):
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<title>' + escape(title) + '</title><style>'
        'body{font:15px system-ui;margin:0;background:#f5f7fb;color:#14233b}main{box-sizing:border-box;width:100%;'
        'min-width:0;max-width:1500px;margin:auto;padding:28px}p{overflow-wrap:anywhere}'
        'a{color:#175aba}table{border-collapse:collapse;width:100%;background:white}th,td{padding:10px;text-align:left;'
        'border-bottom:1px solid #e0e6ee;vertical-align:top;white-space:nowrap}th{font-size:12px;color:#52647b}h1{font-size:26px}'
        '.scroll{overflow:auto;border:1px solid #e0e6ee;border-radius:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere;'
        'background:white;padding:18px;border:1px solid #e0e6ee}.muted{color:#52647b}.blocked{color:#a52d26}'
        '@media(max-width:600px){main{padding:16px}h1{font-size:23px}}'
        '</style><main>' + body + '</main></html>')


def create_app(reader):
    app = FastAPI(title='趋势监控', docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
        return response

    @app.get('/api/status')
    def status():
        return reader.overview()

    @app.get('/api/targets/{symbol}/{frame}')
    def target_json(symbol: str, frame: str):
        try:
            return reader.detail(symbol, frame)
        except (KeyError, ValueError):
            raise HTTPException(404, '未配置的监控目标')

    @app.get('/', response_class=HTMLResponse)
    def home():
        model = reader.overview()
        headers = ['Asset', 'Timeframe', 'Data Health', 'Trend', 'Current Signal', 'Monitoring State',
                   'Last Bar', 'Last Scan', 'Last Event', 'Decision Snapshot', 'Dataset Version']
        rows = []
        for row in model['targets']:
            link = '/targets/' + quote(row['asset'], safe='') + '/' + row['timeframe']
            event = row['last_event']['code'] if row['last_event'] else '—'
            def display_time(value):
                return pd.Timestamp(value).tz_convert('Asia/Shanghai').strftime('%m-%d %H:%M') if value else None
            values = [row['timeframe'], row['data_health'], row['trend'], row['current_signal'],
                      row['monitoring_state'], display_time(row['last_bar']), display_time(row['last_scan']), event,
                      row['snapshot_id'][:10] if row['snapshot_id'] else None,
                      row['dataset_version'][:12] if row['dataset_version'] else None]
            rows.append('<tr><td><a href="' + link + '">' + escape(row['asset']) + '</a></td>' +
                        ''.join('<td>' + escape(str(value or '—')) + '</td>' for value in values) + '</tr>')
        body = '<h1>趋势策略监控</h1><p class="muted">数据、状态与通知审计 · ' + escape(model['as_of']) + ' · 表格时间为北京时间</p>'
        body += '<p>当前信号受数据验证、新鲜度与扫描健康门禁约束；监控状态保留上次决定。</p>'
        if model['repository_error']:
            body += '<p class="blocked">Repository unavailable: ' + escape(model['repository_error']) + '</p>'
        body += '<div class="scroll"><table><thead><tr>' + ''.join('<th>' + title + '</th>' for title in headers)
        body += '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
        body += '<h2>通知队列</h2><pre>' + escape(json.dumps(model['notification_queue'], ensure_ascii=False, indent=2)) + '</pre>'
        body += '<h2>系统健康</h2><pre>' + escape(json.dumps(model['system_health'], ensure_ascii=False, indent=2)) + '</pre>'
        return _page('趋势监控', body)

    @app.get('/targets/{symbol}/{frame}', response_class=HTMLResponse)
    def target_html(symbol: str, frame: str):
        model = target_json(symbol, frame)
        body = '<a href="/">← 返回监控列表</a><h1>' + escape(model['asset'] + ' / ' + model['timeframe']) + '</h1>'
        body += '<p>Data Health: ' + escape(model['data_health']) + ' · Current Signal: ' + escape(model['current_signal']) + '</p>'
        for title, value in [('阻断原因', model['blockers']), ('Decision Snapshot', model['snapshot']),
                              ('状态', model['state']), ('Quality Report / Manifest', model['metadata']),
                              ('事件与通知历史', model['events']), ('运行历史', model['run_history'])]:
            body += '<h2>' + title + '</h2><pre>' + escape(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre>'
        return _page(model['asset'] + '监控详情', body)

    return app
