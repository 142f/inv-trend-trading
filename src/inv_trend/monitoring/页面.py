"""Server-rendered HTML for the monitoring gateway.

Pages are plain strings with no script: filters are query parameters and every
value is escaped here.  Keeping this module separate from the reader/routes
means the rendering contract can be reviewed without touching SQL or FastAPI.
"""
from __future__ import annotations

from html import escape
from urllib.parse import quote

import pandas as pd

SORT_LABELS = {
    'symbol': '代码', 'timeframe': '周期', 'health': '数据健康', 'state': '监控状态',
    'trend': '趋势', 'last_scan': '最近扫描', 'last_bar': '最后一根', 'signal': '当前信号',
}

_CSS = (
    'body{font:15px system-ui;margin:0;background:#f5f7fb;color:#14233b}'
    'main{box-sizing:border-box;width:100%;min-width:0;max-width:1500px;margin:auto;padding:28px}'
    'p{overflow-wrap:anywhere}a{color:#175aba}'
    'table{border-collapse:collapse;width:100%;background:white}'
    'th,td{padding:10px;text-align:left;border-bottom:1px solid #e0e6ee;vertical-align:top;white-space:nowrap}'
    'th{font-size:12px;color:#52647b}h1{font-size:26px}'
    '.scroll{overflow:auto;border:1px solid #e0e6ee;border-radius:10px}'
    'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:18px;border:1px solid #e0e6ee}'
    '.muted{color:#52647b}.blocked{color:#a52d26}'
    '.stats{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}'
    '.stats span{background:white;border:1px solid #e0e6ee;border-radius:999px;padding:5px 12px;font-size:13px}'
    '.stats b{font-variant-numeric:tabular-nums}'
    '.filters{display:flex;flex-wrap:wrap;gap:10px;align-items:end;background:white;'
    'border:1px solid #e0e6ee;border-radius:10px;padding:14px;margin:14px 0}'
    '.filters label{display:flex;flex-direction:column;gap:4px;font-size:12px;color:#52647b}'
    '.filters input,.filters select{font:inherit;padding:6px 8px;border:1px solid #c9d4e2;'
    'border-radius:6px;background:#fff}'
    '.filters input[type=text]{width:150px}'
    '.filters button{font:inherit;padding:7px 14px;border:0;border-radius:6px;background:#175aba;'
    'color:#fff;cursor:pointer}.filters a{align-self:center;font-size:13px}'
    '.pill{padding:2px 8px;border-radius:999px;font-size:12px;font-weight:700;'
    'background:#eef2f8;color:#33465f}.pill.RUNNING{background:#fff3d6;color:#8a5a12}'
    '.pill.SUCCEEDED{background:#e2f4e9;color:#1f6b45}.pill.FAILED{background:#fbe6e2;color:#8d2b1f}'
    '.bar{height:6px;background:#eef2f8;border-radius:999px;overflow:hidden;min-width:90px}'
    '.bar i{display:block;height:100%;background:#175aba}'
    '@media(max-width:600px){main{padding:16px}h1{font-size:23px}.filters input[type=text]{width:110px}}'
)


def page(title: str, body: str) -> str:
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>' + escape(title) + '</title><style>' + _CSS + '</style><main>'
            + body + '</main></html>')


def display_time(value) -> str:
    return pd.Timestamp(value).tz_convert('Asia/Shanghai').strftime('%m-%d %H:%M') if value else '—'


def status_pill(value) -> str:
    label = escape(str(value or '—'))
    return '<span class="pill ' + label + '">' + label + '</span>'


def progress_bar(index, total) -> str:
    total = max(int(total or 0), 1)
    done = max(0, min(int(index or 0), total))
    return ('<div class="bar" title="' + str(done) + '/' + str(total) + '"><i style="width:'
            + str(round(done * 100 / total)) + '%"></i></div>')


def links(extra: str = '') -> str:
    return ('<p><a href="/">监控列表</a> · <a href="/r/notifications">通知投递</a> · '
            '<a href="/r/replay">历史回放</a>' + extra + '</p>')


def filter_form(spec: dict, summary: dict) -> str:
    def options(values, selected):
        return ''.join('<option value="' + escape(str(item)) + '"'
                       + (' selected' if str(item) == selected else '') + '>'
                       + escape(str(item)) + '</option>' for item in values)

    healths = sorted(summary['by_health'])
    states = sorted(summary['by_state'])
    return ('<form class="filters" method="get" action="/">'
            '<label>代码前缀 <input type="text" name="symbol" value="' + escape(spec['symbol']) + '"></label>'
            '<label>数据健康 <select name="health"><option value="">全部</option>'
            + options(healths, spec['health']) + '</select></label>'
            '<label>监控状态 <select name="state"><option value="">全部</option>'
            + options(states, spec['state']) + '</select></label>'
            '<label>趋势 <select name="trend"><option value="">全部</option>'
            + options(['UP', 'DOWN', 'UNKNOWN'], spec['trend']) + '</select></label>'
            '<label>排序 <select name="sort"><option value="">默认</option>'
            + ''.join('<option value="' + key + '"' + (' selected' if key == spec['sort'] else '')
                      + '>' + escape(label) + '</option>' for key, label in SORT_LABELS.items())
            + '</select></label>'
            '<label>方向 <select name="desc"><option value="0"' + (' selected' if spec['desc'] == '0' else '')
            + '>升序</option><option value="1"' + (' selected' if spec['desc'] == '1' else '')
            + '>降序</option></select></label>'
            '<label>只看阻断 <input type="checkbox" name="blocked" value="1"'
            + (' checked' if spec['blocked'] == '1' else '') + '></label>'
            '<button type="submit">应用筛选</button><a href="/">清空</a></form>')


def replay_table(rows, *, compact: bool) -> str:
    if not rows:
        return '<p class="muted">暂无回放会话。</p>'
    body = ('<div class="scroll"><table><thead><tr><th>开始</th><th>代码</th><th>周期</th><th>版本</th>'
            '<th>状态</th><th>进度</th><th>已比较</th><th>差异</th>'
            + ('' if compact else '<th>错误</th>') + '</tr></thead><tbody>')
    for row in rows:
        link = '/r/replay/' + quote(str(row['session_id']), safe='')
        body += ('<tr><td><a href="' + link + '">' + escape(display_time(row.get('started_at')))
                 + '</a></td><td>' + escape(str(row.get('symbol') or '—'))
                 + '</td><td>' + escape(str(row.get('timeframe') or '—'))
                 + '</td><td>' + escape(str(row.get('dataset_version') or '—')[:12])
                 + '</td><td>' + status_pill(row.get('status'))
                 + '</td><td>' + progress_bar(row.get('bar_index'), row.get('total_bars'))
                 + ' ' + str(int(row.get('bar_index') or 0)) + '/' + str(int(row.get('total_bars') or 0))
                 + '</td><td>' + str(int(row.get('compared_bars') or 0))
                 + '</td><td>' + str(int(row.get('mismatch_count') or 0))
                 + '</td>' + ('' if compact else '<td>' + escape(str(row.get('error') or '—')) + '</td>')
                 + '</tr>')
    return body + '</tbody></table></div>'
