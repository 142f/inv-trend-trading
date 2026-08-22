"""Dependency-free, self-contained HTML/SVG daily report."""

from __future__ import annotations

from html import escape
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping

from .report_renderer import write_daily_dashboard


def write_daily_report(snapshot: Mapping[str, Any], path: str | Path) -> Path:
    if any(
        isinstance(row, Mapping) and isinstance(row.get("report_bundle"), Mapping)
        for row in snapshot.get("symbols", [])
    ):
        return write_daily_dashboard(snapshot, path)

    # Backward-compatible renderer for schema-v3 snapshots created before
    # ReportBundle was introduced.
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    summary = snapshot.get("summary", {})
    cards = "".join(
        f'<div class="card"><b>{escape(str(key))}</b><strong>{value}</strong></div>'
        for key, value in summary.items()
    )
    sections = "".join(_symbol_section(row) for row in snapshot.get("symbols", []))
    embedded = escape(json.dumps(snapshot, ensure_ascii=False, allow_nan=False))
    document = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>趋势量化日报 {escape(str(snapshot.get('report_date', '')))}</title>
<style>{_CSS}</style></head><body>
<header><h1>趋势量化 · 每日市场扫描</h1><p>{escape(str(snapshot.get('report_date', '')))} · D1 · {escape(str(snapshot.get('configuration', {}).get('strategy_version', '')))}</p></header>
<main><section class="cards">{cards}</section>{sections}</main>
<details><summary>机器可读快照</summary><pre>{embedded}</pre></details>
<footer>本报告为离线静态文件；指标只使用完整日线，不构成投资建议。</footer>
</body></html>"""
    target.write_text(document, encoding="utf-8")
    return target


def _symbol_section(row: Mapping[str, Any]) -> str:
    chart = row.get("chart", [])
    scan = row.get("scan", {})
    indicators = scan.get("indicators", {}) if isinstance(scan, Mapping) else {}
    strategy_checks = scan.get("strategy_checks", {}) if isinstance(scan, Mapping) else {}
    freshness = row.get("freshness", {})
    badge = escape(str(row.get("run_status", "failed")))
    table = _indicator_table(indicators)
    strategy_table = _strategy_table(strategy_checks)
    return f"""<section class="symbol"><div class="title"><h2>{escape(str(row.get('symbol', '?')))}</h2><span class="badge {badge}">{badge}</span></div>
<p class="meta">latest {escape(str(row.get('data', {}).get('latest_complete_bar', '-')))} · version {escape(str(row.get('data', {}).get('dataset_version', '-')))} · freshness {escape(str(freshness.get('status', '-')))} · replay {escape(str(row.get('cursor', {}).get('bars_replayed', 0)))}</p>
{_price_svg(chart)}{_macd_svg(chart)}{table}{strategy_table}</section>"""


def _strategy_table(checks: Mapping[str, Any]) -> str:
    """Render report-only strategy dimensions independently of legacy signals."""
    if not isinstance(checks, Mapping):
        return ""
    rows = []
    for name in (
        "sma_alignment", "ema_trend", "macd_summary", "trend_quality",
        "volatility", "volume", "rating",
    ):
        values = checks.get(name, {})
        if not isinstance(values, Mapping):
            continue
        status = values.get("status") or values.get("grade") or "-"
        direction = values.get("direction") or "-"
        detail = values.get("event") or values.get("state") or values.get("score") or "-"
        rows.append(
            "<tr>" + "".join(
                f"<td>{escape(str(value if value is not None else '-'))}</td>"
                for value in (name, status, direction, detail)
            ) + "</tr>"
        )
    macd = checks.get("macd", {})
    if isinstance(macd, Mapping):
        for timeframe, values in macd.items():
            if not isinstance(values, Mapping):
                continue
            rows.append(
                "<tr>" + "".join(
                    f"<td>{escape(str(value if value is not None else '-'))}</td>"
                    for value in (
                        f"macd_{timeframe}",
                        values.get("status"),
                        values.get("zero_axis"),
                        values.get("event") or values.get("bar_end"),
                    )
                ) + "</tr>"
            )
    if not rows:
        return ""
    return (
        "<h3>Strategy checks</h3><table><thead><tr>"
        "<th>Check</th><th>Status</th><th>Direction</th><th>Detail</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _indicator_table(indicators: Mapping[str, Any]) -> str:
    rows = []
    for name, values in indicators.items():
        if not isinstance(values, Mapping):
            continue
        rows.append(
            "<tr>" + "".join(
                f"<td>{escape(str(value if value is not None else '-'))}</td>"
                for value in (name, values.get("status"), values.get("direction"), values.get("event"))
            ) + "</tr>"
        )
    return "<table><thead><tr><th>指标</th><th>状态</th><th>方向</th><th>事件</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def _price_svg(rows: Iterable[Mapping[str, Any]]) -> str:
    data = list(rows)
    series = [
        ("close", "#f8fafc"), ("channel_high_20", "#38bdf8"),
        ("channel_low_20", "#38bdf8"), ("channel_high_55", "#a78bfa"),
        ("channel_low_55", "#a78bfa"), ("sma_10", "#22c55e"),
        ("sma_20", "#f59e0b"),
    ]
    return _line_svg(data, series, "价格 / Donchian / SMA", 270)


def _macd_svg(rows: Iterable[Mapping[str, Any]]) -> str:
    return _line_svg(list(rows), [("dif", "#22c55e"), ("dea", "#f59e0b")], "MACD DIF / DEA", 150)


def _line_svg(
    rows: list[Mapping[str, Any]], series: list[tuple[str, str]], title: str, height: int
) -> str:
    width, pad = 1000, 30
    finite = [float(row[key]) for row in rows for key, _ in series if _finite(row.get(key))]
    if not rows or not finite:
        return f'<div class="empty">{escape(title)}：数据不足</div>'
    low, high = min(finite), max(finite)
    span = high - low or 1.0
    inner_w, inner_h = width - 2 * pad, height - 2 * pad
    paths = []
    for key, color in series:
        points = []
        for index, row in enumerate(rows):
            if not _finite(row.get(key)):
                continue
            x = pad + index * inner_w / max(1, len(rows) - 1)
            y = pad + (high - float(row[key])) * inner_h / span
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


_CSS = """
:root{color-scheme:dark;background:#08111f;color:#dbeafe;font:14px system-ui,sans-serif}body{margin:0}header,main,details,footer{max-width:1180px;margin:auto;padding:22px}header{background:linear-gradient(120deg,#102a43,#172554)}h1{margin:0 0 6px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(125px,1fr));gap:10px}.card,.symbol{background:#101d30;border:1px solid #263b55;border-radius:12px;padding:14px}.card strong{display:block;font-size:24px;margin-top:8px}.symbol{margin-top:18px}.title{display:flex;align-items:center;justify-content:space-between}.badge{padding:4px 9px;border-radius:999px;background:#334155}.badge.updated,.badge.unchanged{background:#14532d}.badge.failed,.badge.blocked,.badge.stale{background:#7f1d1d}.meta,small{color:#94a3b8}figure{margin:16px 0}svg{width:100%;background:#07101c;border-radius:8px}polyline{fill:none;stroke-width:2;vector-effect:non-scaling-stroke}.axis{stroke:#475569}table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:8px;border-bottom:1px solid #263b55}.empty{padding:28px;color:#94a3b8;background:#07101c}pre{white-space:pre-wrap;overflow:auto}footer{color:#64748b}
"""
