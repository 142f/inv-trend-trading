"""Read-only FastAPI views over verified Repository metadata and recorded evidence.

The dashboard is the only HTTP surface in the repository.  JSON lives under
``/api/v1`` (``/api/*`` stays as a legacy alias), HTML pages live under ``/`` and
``/r/*``.  Pages are rendered server-side: filters are query parameters, so the
page never needs a script and the CSP can stay strict.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from html import escape
import json
from pathlib import Path
import sqlite3
from urllib.parse import quote, urlencode

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import pandas as pd

from inv_trend.data.models import CatalogAccessError, CatalogBackendConflictError, DataUnavailableError, DataLineageError
from inv_trend.observability.脱敏 import redact
from .页面 import (
    SORT_LABELS, display_time, filter_form, links, page, progress_bar, replay_table, status_pill,
)
from .回放记录 import ReplaySessionReader
from .模型 import DecisionSnapshot, StateRecord, timeframe
from .新鲜度 import MonitoringFreshnessPolicy
from .策略适配 import ReferenceAccount

FILTER_KEYS = ('symbol', 'health', 'state', 'trend', 'blocked', 'sort', 'desc')
SORT_KEYS = {
    'symbol': 'asset', 'timeframe': 'timeframe', 'health': 'data_health',
    'state': 'monitoring_state', 'trend': 'trend', 'last_scan': 'last_scan',
    'last_bar': 'last_bar', 'signal': 'current_signal',
}


def _filters(values: dict | None) -> dict:
    raw = dict(values or {})
    unknown = set(raw) - set(FILTER_KEYS)
    if unknown:
        raise ValueError(f'unsupported dashboard filters: {sorted(unknown)}')
    spec = {name: str(raw.get(name) or '').strip() for name in FILTER_KEYS}
    if spec['symbol']:
        spec['symbol'] = spec['symbol'].upper()
    spec['desc'] = '1' if spec['desc'] in {'1', 'true', 'on'} else '0'
    if spec['sort'] and spec['sort'] not in SORT_KEYS:
        raise ValueError(f'unsupported dashboard sort: {spec["sort"]}')
    return spec


def _match(row: dict, spec: dict) -> bool:
    if spec['symbol'] and not row['asset'].upper().startswith(spec['symbol']):
        return False
    if spec['health'] and row['data_health'] != spec['health']:
        return False
    if spec['state'] and row['monitoring_state'] != spec['state']:
        return False
    if spec['trend'] and row['trend'] != spec['trend']:
        return False
    if spec['blocked'] == '1' and not row['blockers']:
        return False
    return True


def _apply(rows: list[dict], spec: dict) -> list[dict]:
    selected = [row for row in rows if _match(row, spec)]
    if spec['sort']:
        key = SORT_KEYS[spec['sort']]
        present = [row for row in selected if row.get(key) is not None]
        missing = [row for row in selected if row.get(key) is None]
        present.sort(key=lambda row: row[key], reverse=spec['desc'] == '1')
        selected = present + missing
    return selected


class DashboardReader:
    def __init__(self, store, config, repository=None, *, repository_error=None, clock=None,
                 replay=None):
        if store.purpose != 'live':
            raise ValueError('dashboard requires live monitoring storage')
        self.store, self.config, self.repository = store, config, repository
        self.repository_error = redact(repository_error) if repository_error else None
        self.clock = clock or (lambda: pd.Timestamp.now(tz='UTC'))
        self.replay = replay or ReplaySessionReader(None)
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

    def _targets(self):
        with self._read() as db:
            if self.repository is not None:
                return [(item.symbol, item.timeframe) for item in self.config.targets(self.repository)]
            if self.config.symbols:
                return [(symbol, frame) for symbol in self.config.symbols for frame in self.config.timeframes]
            return [(row['symbol'], row['timeframe']) for row in db.execute(
                "SELECT symbol,timeframe FROM monitoring_state WHERE symbol NOT LIKE '_HEALTH_%' "
                "ORDER BY symbol,timeframe")]

    def overview(self, filters: dict | None = None):
        spec = _filters(filters)
        with self._read() as db:
            rows = [self._target(db, *target) for target in self._targets()]
            health = [json.loads(row['payload']) for row in db.execute(
                "SELECT payload FROM monitoring_state WHERE symbol LIKE '_HEALTH_%' ORDER BY symbol")]
            queue = {row['status']: row['total'] for row in db.execute(
                'SELECT status,COUNT(*) AS total FROM outbox GROUP BY status')}
        selected = _apply(rows, spec)
        summary = {
            'targets': len(rows),
            'selected': len(selected),
            'blocked': sum(1 for row in rows if row['blockers']),
            'by_health': dict(Counter(row['data_health'] for row in rows)),
            'by_state': dict(Counter(row['monitoring_state'] for row in rows)),
            'by_trend': dict(Counter(row['trend'] for row in rows)),
            'notification_queue': queue,
            'replay': self.replay.summary(),
        }
        return {'as_of': pd.Timestamp(self.clock()).isoformat(), 'repository_error': self.repository_error,
                'filters': spec, 'summary': summary, 'targets': selected,
                'system_health': redact(health), 'notification_queue': queue}

    def detail(self, symbol, frame):
        symbol, frame = symbol.upper(), timeframe(frame)
        if self.repository is not None:
            permitted = {(item.symbol, item.timeframe) for item in self.config.targets(self.repository)}
        else:
            permitted = {tuple(item) for item in self._targets()}
        if (symbol, frame) not in permitted:
            raise KeyError('unconfigured dashboard target')
        with self._read() as db:
            return self._target(db, symbol, frame, detail=True)

    def notifications(self, *, status=None, symbol=None, limit=100):
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('notification limit must be between 1 and 500')
        clauses, values = [], []
        if status:
            clauses.append('o.status=?')
            values.append(str(status))
        if symbol:
            clauses.append('d.symbol=?')
            values.append(str(symbol).upper())
        where = (' WHERE ' + ' AND '.join(clauses)) if clauses else ''
        with self._read() as db:
            rows = db.execute(
                'SELECT o.notification_id,o.event_id,o.status,o.attempts,o.next_attempt,o.lease_until,'
                'o.last_error,o.delivered_at,e.kind,e.code,e.severity,d.symbol,d.timeframe,'
                'd.evaluated_at,d.decision_id '
                'FROM outbox o LEFT JOIN events e ON e.event_id=o.event_id '
                'LEFT JOIN decisions d ON d.decision_id=e.decision_id' + where +
                ' ORDER BY o.rowid DESC LIMIT ?', (*values, limit)).fetchall()
        return [redact(dict(row)) for row in rows]

    def notification(self, notification_id):
        with self._read() as db:
            row = db.execute(
                'SELECT o.*,e.kind,e.code,e.severity,e.payload,d.symbol,d.timeframe,d.evaluated_at,'
                'd.decision_id FROM outbox o LEFT JOIN events e ON e.event_id=o.event_id '
                'LEFT JOIN decisions d ON d.decision_id=e.decision_id WHERE o.notification_id=?',
                (notification_id,)).fetchone()
            if row is None:
                return None
            attempts = db.execute('SELECT * FROM notification_attempts WHERE notification_id=? '
                                  'ORDER BY attempt', (notification_id,)).fetchall()
        return redact(dict(row) | {'attempts': [dict(item) for item in attempts]})

    def replays(self, limit=50):
        return self.replay.list(limit=limit)

    def replay_session(self, session_id):
        return self.replay.get(session_id)


def _count(value):
    return '—' if value is None else str(int(value))


def create_app(reader):
    app = FastAPI(title='趋势监控', docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = (
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'")
        return response

    def overview_for(symbol=None, health=None, state=None, trend=None, blocked=None,
                     sort=None, desc=None):
        try:
            return reader.overview({'symbol': symbol, 'health': health, 'state': state,
                                    'trend': trend, 'blocked': blocked, 'sort': sort, 'desc': desc})
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get('/api/v1/status')
    def status(symbol: str | None = None, health: str | None = None, state: str | None = None,
               trend: str | None = None, blocked: str | None = None, sort: str | None = None,
               desc: str | None = None):
        return overview_for(symbol, health, state, trend, blocked, sort, desc)

    @app.get('/api/status')
    def status_legacy(symbol: str | None = None, health: str | None = None,
                      state: str | None = None, trend: str | None = None,
                      blocked: str | None = None, sort: str | None = None, desc: str | None = None):
        return overview_for(symbol, health, state, trend, blocked, sort, desc)

    def detail_or_404(symbol: str, frame: str):
        try:
            return reader.detail(symbol, frame)
        except (KeyError, ValueError):
            raise HTTPException(404, '未配置的监控目标')

    @app.get('/api/v1/targets/{symbol}/{frame}')
    def target_json(symbol: str, frame: str):
        return detail_or_404(symbol, frame)

    @app.get('/api/targets/{symbol}/{frame}')
    def target_json_legacy(symbol: str, frame: str):
        return detail_or_404(symbol, frame)

    @app.get('/api/v1/notifications')
    def notifications(status: str | None = None, symbol: str | None = None, limit: int = 100):
        try:
            return reader.notifications(status=status, symbol=symbol, limit=limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get('/api/v1/notifications/{notification_id}')
    def notification(notification_id: str):
        row = reader.notification(notification_id)
        if row is None:
            raise HTTPException(404, '未找到通知记录')
        return row

    @app.get('/api/v1/replay')
    def replay_list(limit: int = 50):
        try:
            return {'sessions': reader.replays(limit=limit)}
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.get('/api/v1/replay/{session_id}')
    def replay_detail(session_id: str):
        row = reader.replay_session(session_id)
        if row is None:
            raise HTTPException(404, '未找到回放会话')
        return row

    @app.get('/', response_class=HTMLResponse)
    def home(symbol: str | None = None, health: str | None = None, state: str | None = None,
             trend: str | None = None, blocked: str | None = None, sort: str | None = None,
             desc: str | None = None):
        model = overview_for(symbol, health, state, trend, blocked, sort, desc)
        spec, summary = model['filters'], model['summary']
        headers = ['Asset', 'Timeframe', 'Data Health', 'Trend', 'Current Signal', 'Monitoring State',
                   'Last Bar', 'Last Scan', 'Last Event', 'Decision Snapshot', 'Dataset Version']
        rows = []
        for row in model['targets']:
            link = '/targets/' + quote(row['asset'], safe='') + '/' + row['timeframe']
            event = row['last_event']['code'] if row['last_event'] else '—'
            values = [row['timeframe'], row['data_health'], row['trend'], row['current_signal'],
                      row['monitoring_state'], display_time(row['last_bar']), display_time(row['last_scan']),
                      event, row['snapshot_id'][:10] if row['snapshot_id'] else None,
                      row['dataset_version'][:12] if row['dataset_version'] else None]
            cells = ''.join('<td>' + escape(str(value or '—')) + '</td>' for value in values)
            if row['blockers']:
                cells += '</tr><tr><td colspan="11" class="muted">阻断：' + escape(', '.join(row['blockers'])) + '</td>'
            rows.append('<tr><td><a href="' + link + '">' + escape(row['asset']) + '</a></td>' + cells + '</tr>')
        body = '<h1>趋势策略监控</h1><p class="muted">数据、状态与通知审计 · ' + escape(model['as_of']) + ' · 表格时间为北京时间</p>'
        body += links()
        body += ('<p>当前信号受数据验证、新鲜度与扫描健康门禁约束；监控状态保留上次决定。'
                 '筛选条件写入链接，可直接复制分享。</p>')
        if model['repository_error']:
            body += '<p class="blocked">Repository unavailable: ' + escape(model['repository_error']) + '</p>'
        body += '<div class="stats">'
        for label, value in (
                ('目标', summary['targets']), ('当前显示', summary['selected']), ('阻断', summary['blocked']),
                ('通知队列', sum(summary['notification_queue'].values())),
                ('回放任务', summary['replay'].get('total', 0))):
            body += '<span>' + escape(label) + ' <b>' + str(int(value)) + '</b></span>'
        body += '</div>'
        body += filter_form(spec, summary)
        body += ('<p class="muted">显示 ' + str(summary['selected']) + ' / ' + str(summary['targets'])
                 + ' 个目标；排序字段：' + escape(SORT_LABELS.get(spec['sort'], '默认（配置顺序）')) + '</p>')
        body += '<div class="scroll"><table><thead><tr>' + ''.join('<th>' + title + '</th>' for title in headers)
        body += '</tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'
        body += '<h2>通知队列</h2><p><a href="/r/notifications">查看投递明细</a></p><pre>'
        body += escape(json.dumps(model['notification_queue'], ensure_ascii=False, indent=2)) + '</pre>'
        body += '<h2>最近回放</h2><p><a href="/r/replay">查看全部回放</a></p>'
        body += replay_table(reader.replays(limit=5), compact=True)
        body += '<h2>系统健康</h2><pre>' + escape(json.dumps(model['system_health'], ensure_ascii=False, indent=2)) + '</pre>'
        return page('趋势监控', body)

    @app.get('/r/', response_class=HTMLResponse)
    def home_alias():
        return RedirectResponse('/', status_code=307)

    def targetpage(symbol: str, frame: str):
        model = detail_or_404(symbol, frame)
        body = '<p><a href="/">← 返回监控列表</a></p>'
        body += '<h1>' + escape(model['asset'] + ' / ' + model['timeframe']) + '</h1>'
        body += ('<p>Data Health: ' + escape(model['data_health']) + ' · Current Signal: '
                 + escape(model['current_signal']) + ' · Monitoring State: ' + escape(model['monitoring_state']) + '</p>')
        body += links()
        for title, value in [('阻断原因', model['blockers']), ('Decision Snapshot', model['snapshot']),
                              ('状态', model['state']), ('Quality Report / Manifest', model['metadata']),
                              ('事件与通知历史', model['events']), ('运行历史', model['run_history'])]:
            body += '<h2>' + title + '</h2><pre>' + escape(json.dumps(value, ensure_ascii=False, indent=2)) + '</pre>'
        return page(model['asset'] + '监控详情', body)

    @app.get('/targets/{symbol}/{frame}', response_class=HTMLResponse)
    def target_html(symbol: str, frame: str):
        return targetpage(symbol, frame)

    @app.get('/r/targets/{symbol}/{frame}', response_class=HTMLResponse)
    def target_html_v1(symbol: str, frame: str):
        return targetpage(symbol, frame)

    @app.get('/r/notifications', response_class=HTMLResponse)
    def notificationspage(status: str | None = None, symbol: str | None = None, limit: int = 100):
        try:
            rows = reader.notifications(status=status, symbol=symbol, limit=limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        query = urlencode({key: value for key, value in
                           (('status', status), ('symbol', symbol)) if value})
        body = '<h1>通知投递明细</h1>' + links()
        body += ('<p class="muted">Outbox 状态、重试次数、下次重试时间与最后一次错误。'
                 '明细不包含完整账户上下文。</p>')
        body += ('<form class="filters" method="get" action="/r/notifications">'
                 '<label>状态 <select name="status"><option value="">全部</option>'
                 + ''.join('<option' + (' selected' if value == status else '') + '>' + value + '</option>'
                           for value in ('PENDING', 'SENDING', 'DELIVERED', 'FAILED'))
                 + '</select></label>'
                 '<label>代码 <input type="text" name="symbol" value="' + escape(symbol or '') + '"></label>'
                 '<button type="submit">应用筛选</button>'
                 '<a href="/r/notifications">清空</a></form>')
        body += ('<p class="muted">共 ' + str(len(rows)) + ' 条' + ('（链接：?' + escape(query) + '）' if query else '') + '</p>')
        body += ('<div class="scroll"><table><thead><tr><th>状态</th><th>代码</th><th>周期</th><th>事件</th>'
                 '<th>严重度</th><th>重试</th><th>下次重试</th><th>最后错误</th><th>投递时间</th><th>通知ID</th>'
                 '</tr></thead><tbody>')
        for row in rows:
            link = '/r/notifications/' + quote(str(row['notification_id']), safe='')
            body += ('<tr><td>' + status_pill(row.get('status')) + '</td>'
                     '<td>' + escape(str(row.get('symbol') or '—')) + '</td>'
                     '<td>' + escape(str(row.get('timeframe') or '—')) + '</td>'
                     '<td>' + escape(str(row.get('code') or '—')) + '</td>'
                     '<td>' + escape(str(row.get('severity') or '—')) + '</td>'
                     '<td>' + str(int(row.get('attempts') or 0)) + '</td>'
                     '<td>' + escape(str(row.get('next_attempt') or '—')) + '</td>'
                     '<td>' + escape(str(row.get('last_error') or '—')) + '</td>'
                     '<td>' + escape(str(row.get('delivered_at') or '—')) + '</td>'
                     '<td><a href="' + link + '">' + escape(str(row['notification_id'])[:10]) + '</a></td></tr>')
        if not rows:
            body += '<tr><td colspan="10" class="muted">当前筛选没有记录</td></tr>'
        body += '</tbody></table></div>'
        return page('通知投递明细', body)

    @app.get('/r/notifications/{notification_id}', response_class=HTMLResponse)
    def notificationpage(notification_id: str):
        row = reader.notification(notification_id)
        if row is None:
            raise HTTPException(404, '未找到通知记录')
        attempts = row.pop('attempts', [])
        body = '<p><a href="/r/notifications">← 返回投递列表</a></p>'
        body += '<h1>通知 ' + escape(str(row['notification_id'])[:12]) + '</h1>' + links()
        body += ('<div class="stats"><span>状态 <b>' + escape(str(row.get('status') or '—')) + '</b></span>'
                 '<span>重试 <b>' + str(int(row.get('attempts') or 0)) + '</b></span>'
                 '<span>代码 <b>' + escape(str(row.get('symbol') or '—')) + '</b></span></div>')
        body += '<h2>通知记录</h2><pre>' + escape(json.dumps(row, ensure_ascii=False, indent=2)) + '</pre>'
        body += '<h2>投递尝试</h2>'
        if attempts:
            body += ('<div class="scroll"><table><thead><tr><th>次</th><th>状态</th><th>开始</th>'
                     '<th>结束</th><th>HTTP</th><th>错误</th></tr></thead><tbody>')
            for item in attempts:
                body += ('<tr><td>' + str(int(item.get('attempt') or 0)) + '</td><td>'
                         + escape(str(item.get('status') or '—')) + '</td><td>'
                         + escape(str(item.get('started_at') or '—')) + '</td><td>'
                         + escape(str(item.get('finished_at') or '—')) + '</td><td>'
                         + escape(str(item.get('http_status') or '—')) + '</td><td>'
                         + escape(str(item.get('error') or '—')) + '</td></tr>')
            body += '</tbody></table></div>'
        else:
            body += '<p class="muted">尚无投递尝试记录。</p>'
        return page('通知详情', body)

    def replaypage(session_id: str):
        row = reader.replay_session(session_id)
        if row is None:
            raise HTTPException(404, '未找到回放会话')
        body = '<p><a href="/r/replay">← 返回回放列表</a></p>'
        body += '<h1>回放 ' + escape(row['symbol'] + ' / ' + row['timeframe']) + '</h1>' + links()
        body += ('<div class="stats">'
                 '<span>状态 <b>' + escape(row['status']) + '</b></span>'
                 '<span>进度 <b>' + str(int(row.get('bar_index') or 0)) + ' / ' + str(int(row.get('total_bars') or 0)) + '</b></span>'
                 '<span>已比较 <b>' + _count(row.get('compared_bars')) + '</b></span>'
                 '<span>差异 <b>' + _count(row.get('mismatch_count')) + '</b></span>'
                 '<span>未比较 <b>' + _count(row.get('uncompared_bars')) + '</b></span></div>')
        body += progress_bar(row.get('bar_index'), row.get('total_bars'))
        body += '<h2>会话记录</h2><pre>' + escape(json.dumps(row, ensure_ascii=False, indent=2)) + '</pre>'
        body += ('<p class="muted">回放只比较同一 bar 上的策略意图与模拟账户输入，'
                 '不代表成交、收益或真实持仓。逐 bar 决策保存在该回放事件库中。</p>')
        return page('回放详情', body)

    @app.get('/r/replay', response_class=HTMLResponse)
    def replay_home(limit: int = 50):
        try:
            rows = reader.replays(limit=limit)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        body = '<h1>历史回放</h1>' + links()
        body += ('<p class="muted">逐 bar 回放进度与对比结果。运行中的会话按每 10 根更新进度，'
                 '完成后写入比较差异。</p>')
        body += replay_table(rows, compact=False)
        return page('历史回放', body)

    @app.get('/r/replay/{session_id}', response_class=HTMLResponse)
    def replay_detailpage(session_id: str):
        return replaypage(session_id)

    return app

