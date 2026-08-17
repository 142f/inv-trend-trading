"""Self-contained HTML summary for a multi-asset backtest result."""
from __future__ import annotations
from html import escape
import json
from pathlib import Path
from typing import Any, Mapping

def write_backtest_report(result: Any, path: str | Path, *, strategy_version: str, comparison: Mapping[str, Any] | None = None) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    equity = result.equity_curve
    values = [float(value) for value in equity.tolist()]
    curve = _polyline(values, 1000, 280)
    peaks = []
    peak = float("-inf")
    for value in values:
        peak = max(peak, value)
        peaks.append(value / peak - 1 if peak else 0)
    drawdown = _polyline(peaks, 1000, 160)
    metrics = "".join(f"<div class='card'><b>{escape(str(key))}</b><strong>{float(value):.4g}</strong></div>" for key, value in result.metrics.items())
    trades = "".join(f"<tr><td>{escape(str(row.get('symbol','-')))}</td><td>{escape(str(row.get('entry_time','-')))}</td><td>{escape(str(row.get('exit_time','-')))}</td><td>{float(row.get('pnl',0)):.4g}</td></tr>" for row in result.trades.to_dict("records")[-200:])
    compare = "" if comparison is None else f"<h2>legacy / corrected 差异</h2><pre>{escape(json.dumps(comparison, ensure_ascii=False, indent=2))}</pre>"
    html = f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'><title>回测报告</title><style>{_CSS}</style></head><body><main><h1>多资产海龟回测</h1><p>strategy: {escape(strategy_version)} · liquidation policy: explicit config</p><section class='cards'>{metrics}</section><h2>净值</h2><svg viewBox='0 0 1000 280'><polyline points='{curve}'/></svg><h2>回撤</h2><svg viewBox='0 0 1000 160'><polyline points='{drawdown}'/></svg><h2>交易时间轴（最近 200）</h2><table><tr><th>品种</th><th>入场</th><th>出场</th><th>盈亏</th></tr>{trades}</table>{compare}</main></body></html>"""
    target.write_text(html, encoding="utf-8")
    return target

def _polyline(values: list[float], width: int, height: int) -> str:
    if not values:
        return ""
    low, high = min(values), max(values)
    span = high - low or 1
    return " ".join(f"{index * width / max(1,len(values)-1):.1f},{(high-value)*height/span:.1f}" for index, value in enumerate(values))

_CSS="""body{margin:0;background:#08111f;color:#dbeafe;font:14px system-ui}main{max-width:1160px;margin:auto;padding:24px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.card{background:#101d30;border:1px solid #263b55;border-radius:10px;padding:12px}.card strong{display:block;font-size:22px;margin-top:7px}svg{width:100%;background:#07101c}polyline{fill:none;stroke:#38bdf8;stroke-width:2;vector-effect:non-scaling-stroke}table{width:100%;border-collapse:collapse}td,th{padding:8px;border-bottom:1px solid #263b55;text-align:left}pre{white-space:pre-wrap}"""
