"""Daily HTML entry point with a Chinese legacy-snapshot compatibility view."""

from __future__ import annotations

from html import escape
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from .report_renderer import write_daily_dashboard


_STATUS = {
    "updated": "已更新",
    "unchanged": "数据未变化",
    "stale": "数据过期",
    "failed": "扫描失败",
    "blocked": "已阻塞",
    "not_run": "未扫描",
    "ready": "已就绪",
    "unavailable": "不可用",
    "long": "多头",
    "short": "空头",
    "none": "未形成",
    "conflict": "方向冲突",
    "golden_cross": "金叉",
    "death_cross": "死叉",
    "breakout_up": "向上突破",
    "breakout_down": "向下突破",
    "normal": "正常",
    "low": "偏低",
    "high": "偏高",
}


def write_daily_report(snapshot: Mapping[str, Any], path: str | Path) -> Path:
    if any(
        isinstance(row, Mapping) and isinstance(row.get("report_bundle"), Mapping)
        for row in snapshot.get("symbols", [])
    ):
        return write_daily_dashboard(snapshot, path)

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    summary = snapshot.get("summary", {})
    cards = "".join(
        f'<div class="card"><b>{escape(_summary_label(str(key)))}</b><strong>{escape(str(value))}</strong></div>'
        for key, value in summary.items()
    )
    sections = "".join(_symbol_section(row) for row in snapshot.get("symbols", []))
    embedded = escape(json.dumps(snapshot, ensure_ascii=False, allow_nan=False))
    date = escape(str(snapshot.get("report_date", "")))
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>趋势策略每日分析报告 {date}</title><style>{_CSS}</style></head><body>
<header><h1>趋势策略每日分析报告</h1><p>{date} · D1 · {escape(str(snapshot.get('configuration', {}).get('strategy_version', '—')))}</p></header>
<main><section class="notice"><b>兼容模式：</b>此历史快照不含结构化突破评估。页面只展示原始结果，不在 HTML 中重算趋势或入场逻辑；请重新运行日报生成新版报告。</section><section class="cards">{cards}</section>{sections}</main>
<details><summary>机器可读快照</summary><pre>{embedded}</pre></details><footer>指标仅使用完整 D1 K 线。本报告不构成投资建议。</footer>
</body></html>"""
    target.write_text(document, encoding="utf-8")
    return target


def _symbol_section(row: Mapping[str, Any]) -> str:
    chart = row.get("chart", [])
    scan = row.get("scan", {})
    status = str(row.get("run_status", "failed"))
    if not isinstance(scan, Mapping) or not isinstance(scan.get("indicators"), Mapping):
        reason = scan.get("reason") if isinstance(scan, Mapping) else None
        error = row.get("update", {}).get("error") if isinstance(row.get("update"), Mapping) else None
        return f"""<section class="symbol"><div class="title"><h2>{escape(str(row.get('symbol', '?')))}</h2><span class="badge failed">{escape(_text(status))}</span></div><p>无法生成分析：{escape(str(error or reason or '缺少可用数据'))}</p></section>"""
    indicators = scan.get("indicators", {})
    checks = scan.get("strategy_checks", {})
    data = row.get("data", {})
    freshness = row.get("freshness", {})
    return f"""<section class="symbol"><div class="title"><h2>{escape(str(row.get('symbol', '?')))}</h2><span class="badge {escape(status)}">{escape(_text(status))}</span></div>
<p class="meta">最新完整 K 线：{escape(str(data.get('latest_complete_bar', '—')))} · 数据版本：{escape(str(data.get('dataset_version', '—')))} · 新鲜度：{escape(_text(freshness.get('status')))}</p>
<p class="notice">此快照没有结构化“突破 → 趋势 → 入场”结果，请重新扫描后查看新版报告。</p>
{_price_svg(chart)}{_macd_svg(chart)}{_indicator_table(indicators)}{_strategy_table(checks)}</section>"""


def _indicator_table(indicators: Mapping[str, Any]) -> str:
    rows = []
    for name, value in indicators.items():
        if isinstance(value, Mapping):
            rows.append(_row(_indicator_name(str(name)), value.get("status"), value.get("direction"), value.get("event")))
    return _table("原始指标状态", ("指标", "状态", "方向", "事件"), rows)


def _strategy_table(checks: Mapping[str, Any]) -> str:
    if not isinstance(checks, Mapping):
        return ""
    rows = []
    labels = {
        "sma_alignment": "SMA 均线排列",
        "ema_trend": "EMA 长周期趋势",
        "macd_summary": "MACD 多周期汇总",
        "trend_quality": "ADX/DMI 趋势质量",
        "volatility": "ATR 波动状态",
        "volume": "相对成交量",
        "rating": "组合评级",
    }
    for name, label in labels.items():
        value = checks.get(name)
        if isinstance(value, Mapping):
            detail = value.get("event") or value.get("state") or value.get("score") or "—"
            rows.append(_row(label, value.get("status") or value.get("grade"), value.get("direction"), detail))
    for timeframe, value in (checks.get("macd") or {}).items():
        if isinstance(value, Mapping):
            rows.append(_row(f"{timeframe} MACD", value.get("status"), value.get("direction"), value.get("event") or value.get("zero_axis")))
    return _table("多维趋势检查", ("策略维度", "状态", "方向", "数值/事件"), rows)


def _table(title: str, headers: tuple[str, ...], rows: list[str]) -> str:
    if not rows:
        return ""
    head = "".join(f"<th>{escape(item)}</th>" for item in headers)
    return f"<h3>{escape(title)}</h3><div class=\"table-wrap\"><table><thead><tr>{head}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"


def _row(*values: Any) -> str:
    return "<tr>" + "".join(f"<td>{escape(_text(value))}</td>" for value in values) + "</tr>"


def _price_svg(rows: Iterable[Mapping[str, Any]]) -> str:
    series = (
        ("close", "#f8fafc"), ("channel_high_20", "#38bdf8"), ("channel_low_20", "#38bdf8"),
        ("channel_high_55", "#a78bfa"), ("channel_low_55", "#a78bfa"), ("sma_10", "#22c55e"), ("sma_20", "#f59e0b"),
    )
    return _line_svg(list(rows), series, "价格、唐奇安通道与 SMA", 270)


def _macd_svg(rows: Iterable[Mapping[str, Any]]) -> str:
    return _line_svg(list(rows), (("dif", "#22c55e"), ("dea", "#f59e0b")), "MACD（DIF / DEA）", 150)


def _line_svg(rows: list[Mapping[str, Any]], series: tuple[tuple[str, str], ...], title: str, height: int) -> str:
    width, pad = 1000, 30
    finite = [float(row[key]) for row in rows for key, _ in series if _finite(row.get(key))]
    if not rows or not finite:
        return f'<div class="empty">{escape(title)}：数据不足</div>'
    low, high = min(finite), max(finite)
    span = high - low or 1.0
    paths = []
    for key, color in series:
        points = []
        for index, row in enumerate(rows):
            if _finite(row.get(key)):
                x = pad + index * (width - 2 * pad) / max(1, len(rows) - 1)
                y = pad + (high - float(row[key])) * (height - 2 * pad) / span
                points.append(f"{x:.1f},{y:.1f}")
        if points:
            paths.append(f'<polyline points="{" ".join(points)}" stroke="{color}"/>')
    legend = " · ".join(key for key, _ in series)
    return f'<figure><figcaption>{escape(title)} <small>{escape(legend)}</small></figcaption><svg viewBox="0 0 {width} {height}" role="img"><line x1="{pad}" y1="{height-pad}" x2="{width-pad}" y2="{height-pad}" class="axis"/>{"".join(paths)}</svg></figure>'


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _text(value: Any) -> str:
    return _STATUS.get(str(value), str(value if value is not None else "—"))


def _summary_label(key: str) -> str:
    return {
        "selected": "选中标的", "updated": "已更新", "unchanged": "数据未变化", "blocked": "已阻塞",
        "stale": "数据过期", "failed": "扫描失败", "scanned": "完成扫描", "signals_detected": "检测信号",
        "signals_new": "新增信号", "signals_duplicate": "重复信号", "signals_notified": "已通知信号",
        "delivery_errors": "通知错误", "recovered_deliveries": "恢复通知", "error_count": "异常标的",
    }.get(key, key)


def _indicator_name(name: str) -> str:
    return {"turtle_20": "海龟 20 日", "turtle_55": "海龟 55 日", "sma_10_20": "SMA10/20", "macd_12_26_9": "MACD 12/26/9"}.get(name, name)


_CSS = """
:root{color-scheme:dark;background:#08111f;color:#dbeafe;font:14px system-ui,sans-serif}body{margin:0}header,main,details,footer{max-width:1180px;margin:auto;padding:22px}header{background:linear-gradient(120deg,#102a43,#172554)}h1,h2,h3{margin:0 0 8px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:10px;margin:16px 0}.card,.symbol,.notice{background:#101d30;border:1px solid #263b55;border-radius:12px;padding:14px}.card strong{display:block;font-size:24px;margin-top:8px}.symbol{margin-top:18px}.title{display:flex;align-items:center;justify-content:space-between}.badge{padding:4px 9px;border-radius:999px;background:#334155}.badge.updated,.badge.unchanged{background:#14532d}.badge.failed,.badge.blocked,.badge.stale{background:#7f1d1d}.meta,small{color:#94a3b8}.notice{color:#cbd5e1}figure{margin:16px 0}svg{width:100%;background:#07101c;border-radius:8px}polyline{fill:none;stroke-width:2;vector-effect:non-scaling-stroke}.axis{stroke:#475569}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%;min-width:620px}th,td{text-align:left;padding:8px;border-bottom:1px solid #263b55}.empty{padding:28px;color:#94a3b8;background:#07101c}pre{white-space:pre-wrap;overflow:auto}footer{color:#64748b}
"""
