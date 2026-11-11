"""Dependency-free interactive Chinese HTML renderer for daily report bundles.

The renderer intentionally performs formatting and chart interaction only. All
strategy states, breakout assessments and entry conclusions are calculated by
the application layer before this module receives the serialized bundle.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Protocol

from .report_scripts import _JS
from .report_styles import _CSS, _META_CSS

if TYPE_CHECKING:

    class ReportBundleLike(Protocol):
        """Structural stand-in for the application's report DTO.

        The renderer only needs ``to_dict``; describing that shape locally keeps
        presentation independent of the application package.
        """

        def to_dict(self) -> Mapping[str, Any]:
            """Return the serializable report payload."""


def write_instrument_report(
    bundle: ReportBundleLike | Mapping[str, Any],
    path: str | Path,
) -> Path:
    # The presentation layer accepts a serialized report bundle and does not
    # need to import its application DTO at runtime.  This keeps renderer
    # composition independent of application-package initialization order.
    to_dict = getattr(bundle, "to_dict", None)
    payload = to_dict() if callable(to_dict) else dict(bundle)
    snapshot = {
        "report_date": str(payload.get("generated_at", ""))[:10],
        "timeframe": payload.get("timeframe", "D1"),
        "symbols": [
            {
                "symbol": payload.get("symbol", "?"),
                "run_status": "updated",
                "report_bundle": payload,
            }
        ],
        "summary": payload.get("summary", {}),
    }
    return _write(render_daily_dashboard(snapshot), path)


def write_daily_dashboard(snapshot: Mapping[str, Any], path: str | Path) -> Path:
    return _write(render_daily_dashboard(snapshot), path)


def render_daily_dashboard(snapshot: Mapping[str, Any]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, default=str)
    payload = payload.replace("</", "<\\/")
    title = f"趋势策略每日分析报告 {snapshot.get('report_date', '')}".strip()
    rows = [row for row in snapshot.get("symbols", []) if isinstance(row, Mapping)]
    meta_html = _daily_meta(snapshot, rows)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html_text(title)}</title>
<style>{_CSS}{_META_CSS}</style>
</head>
<body>
<header class="topbar">
  <div><div class="eyebrow">INV TREND · 中文金融分析日报</div><h1>{_html_text(title)}</h1></div>
  <label class="selector-wrap" for="symbolSelect">分析标的 <select id="symbolSelect"></select></label>
</header>
<dl class="meta">{meta_html}</dl>
<main>
  <section id="identity" class="identity" aria-label="标的和数据身份"></section>
  <section id="dataStatusPanel" class="panel data-panel" aria-label="数据状态与就绪性"></section>
  <section id="screeningPanel" class="panel data-panel" aria-label="策略筛选"></section>
  <section id="decisionPanel" class="decision-grid" aria-label="最终趋势与执行状态"></section>

  <section class="panel evidence-panel">
    <div class="section-head"><div><span class="kicker">可追溯解释</span><h2>最终决策证据链</h2></div><p class="section-note">0–100 为规则证据强度，不是胜率或盈利概率。</p></div>
    <div id="evidenceSummary" class="evidence-summary"></div>
    <div class="table-wrap"><table class="evidence-table"><thead><tr><th>指标</th><th>方向</th><th>有效性</th><th>强度</th><th>权重</th><th>贡献</th><th>首次触发</th><th>持续</th><th>解释</th><th>图表</th></tr></thead><tbody id="evidenceRows"></tbody></table></div>
  </section>

  <section class="panel lifecycle-panel">
    <div class="section-head"><div><span class="kicker">单指标生命周期</span><h2>指标多空证据矩阵</h2></div><p class="section-note">方向、首次触发、持续、增强/减弱与失效均来自服务端同一投影。</p></div>
    <div class="table-wrap"><table class="lifecycle-table"><thead><tr><th>指标 / 周期</th><th>可用性</th><th>多空方向</th><th>生命周期</th><th>证据强度</th><th>首次触发</th><th>持续时间</th><th>变化</th><th>失效条件</th><th>操作</th></tr></thead><tbody id="lifecycleRows"></tbody></table></div>
    <div id="lifecycleDetails" class="lifecycle-details"></div>
  </section>

  <section class="panel breakout-panel">
    <div class="section-head"><div><span class="kicker">正式信号</span><h2>正式收盘确认突破</h2></div><p class="section-note">严格按收盘价确认；盘中越轨不会进入交易决策或通知。</p></div>
    <div class="table-wrap"><table class="breakout-table"><thead><tr>
      <th>时间</th><th>当前价格</th><th>突破类型</th><th>突破对象</th><th>突破前状态</th><th>突破后状态</th>
      <th>趋势判断</th><th>入场方向</th><th>信号强度</th><th>触发依据</th><th>未满足条件</th><th>结论</th><th>图表</th>
    </tr></thead><tbody id="breakoutRows"></tbody></table></div>
    <div class="subsection-head"><div><span class="kicker">观察证据</span><h3>盘中越轨观察</h3></div><p class="section-note">使用同一份已准备 Donchian 特征，不在 HTML 中重新计算。</p></div>
    <div class="table-wrap"><table class="observation-table"><thead><tr>
      <th>时间</th><th>周期</th><th>状态</th><th>盘中高/低</th><th>通道上/下轨</th><th>收盘价</th><th>越轨幅度</th><th>说明</th><th>图表</th>
    </tr></thead><tbody id="turtleObservationRows"></tbody></table></div>
  </section>

  <section id="chartPanel" class="panel chart-panel">
    <div class="section-head"><div><span class="kicker">图表联动</span><h2>价格、突破、异常与指标</h2></div><div id="hoverInfo" class="hover-info">移动鼠标查看同一时间点；点击突破标记可定位表格。</div></div>
    <div class="chart-toolbar">
      <label for="windowSize">可视窗口 <select id="windowSize"><option value="30">30 根</option><option value="60">60 根</option><option value="90" selected>90 根</option><option value="180">180 根</option><option value="0">全部</option></select></label>
      <label class="check-control"><input id="showAllObservations" type="checkbox"> 显示全部盘中观察</label>
    </div>
    <div class="range-row"><label for="rangeStart">窗口位置</label><input id="rangeStart" type="range" min="0" value="0"><span id="rangeLabel"></span></div>
    <div class="legend" id="priceLegend"></div><canvas id="priceChart" class="chart-large" height="330" aria-label="价格与突破图"></canvas>
    <div class="chart-grid">
      <section class="chart-card"><h3>MACD 动量与柱体</h3><div class="legend" id="macdLegend"></div><canvas id="macdChart" class="chart-small" height="165" aria-label="MACD 动量图"></canvas></section>
      <section class="chart-card"><h3>ADX / DMI 趋势强度</h3><div class="legend" id="trendLegend"></div><canvas id="trendChart" class="chart-small" height="165" aria-label="趋势质量图"></canvas></section>
      <section class="chart-card"><h3>ATR 波动分位</h3><div class="legend" id="atrLegend"></div><canvas id="atrChart" class="chart-small" height="165" aria-label="ATR 波动分位图"></canvas></section>
      <section class="chart-card"><h3>相对成交量确认</h3><div class="legend" id="volumeLegend"></div><canvas id="volumeChart" class="chart-small" height="165" aria-label="相对成交量图"></canvas></section>
      <section class="chart-card wide"><h3>价格异常强度（前一完整 K 线 ATR 倍数）</h3><div class="legend" id="anomalyLegend"></div><canvas id="anomalyChart" class="chart-small" height="165" aria-label="异常强度图"></canvas></section>
    </div>
  </section>

  <section class="panel"><div class="section-head"><div><span class="kicker">触发依据</span><h2>策略条件明细</h2></div><div><div id="conditionStats" class="hover-info"></div><div class="rule-actions"><button type="button" data-rule-view="open">展开全部</button><button type="button" data-rule-view="close">收起全部</button><button type="button" data-rule-view="unmet">仅看未满足</button><button type="button" data-rule-view="all">显示全部</button></div></div></div><div id="ruleList" class="rules"></div></section>
  <section class="panel"><div class="section-head"><div><span class="kicker">指标详情</span><h2>多维趋势检查</h2></div></div><div class="table-wrap"><table class="indicator-table"><thead><tr><th>策略维度</th><th>状态</th><th>动量/方向</th><th>零轴状态</th><th>本周期新交叉</th><th>数值与判定依据</th></tr></thead><tbody id="indicatorRows"></tbody></table></div></section>
  <section class="split">
    <section class="panel"><div class="section-head"><div><span class="kicker">其他事件</span><h2>均线与 MACD 事件</h2></div></div><div class="table-wrap"><table><thead><tr><th>时间</th><th>事件</th><th>方向</th><th>价格</th></tr></thead><tbody id="signalRows"></tbody></table></div></section>
    <section class="panel"><div class="section-head"><div><span class="kicker">异常</span><h2>异常阶段</h2></div><p class="section-note">仅用于报告审查，不触发正式通知。</p></div><div class="table-wrap"><table><thead><tr><th>起止时间</th><th>类型</th><th>级别</th><th>状态</th><th>持续</th><th>极值/阈值</th></tr></thead><tbody id="anomalyEpisodeRows"></tbody></table></div><details class="raw-anomalies"><summary>查看逐日异常证据</summary><div class="table-wrap"><table><thead><tr><th>时间</th><th>类型</th><th>级别</th><th>依据</th></tr></thead><tbody id="anomalyRows"></tbody></table></div></details></section>
  </section>
  <details class="panel"><summary>状态变更与技术说明</summary><div class="details-body"><div class="table-wrap"><table><thead><tr><th>时间</th><th>状态</th><th>变更前</th><th>变更后</th><th>原因</th></tr></thead><tbody id="transitionRows"></tbody></table></div><div class="table-wrap"><table><thead><tr><th>模块</th><th>修改内容</th><th>原因</th><th>效果</th></tr></thead><tbody id="changeRows"></tbody></table></div></div></details>
  <details class="panel"><summary>机器可读 ReportBundle</summary><pre id="rawJson"></pre></details>
</main>
<footer>图表、突破表、条件、异常和策略状态均来自同一份 ReportBundle。HTML 只做中文展示和联动，不重新计算交易逻辑；本报告不构成投资建议。</footer>
<script id="snapshot" type="application/json">{payload}</script>
<script>{_JS}</script>
</body></html>"""


def _write(document: str, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    return target


def _html_text(value: Any) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _daily_meta(snapshot: Mapping[str, Any], rows: list[Mapping[str, Any]]) -> str:
    """Render only the identity fields the snapshot actually carries."""

    def unique(values) -> list[str]:
        return sorted({str(value) for value in values if value})

    providers = unique(
        (row.get("data_update_result") or {}).get("provider") for row in rows
    )
    versions = unique(
        (row.get("data_update_result") or {}).get("dataset_version")
        or (row.get("report_bundle") or {}).get("data_version")
        for row in rows
    )
    frames = unique(row.get("timeframe") for row in rows) or ["D1"]
    fields = (
        ("报告日期", snapshot.get("report_date") or "—"),
        ("周期", ",".join(frames)),
        ("标的数", len(rows)),
        ("数据来源", ",".join(providers) or "—"),
        ("数据集版本", ",".join(versions) or "—"),
    )
    return "".join(
        f"<div><dt>{_html_text(label)}</dt><dd>{_html_text(str(value))}</dd></div>"
        for label, value in fields
    )


__all__ = ["render_daily_dashboard", "write_daily_dashboard", "write_instrument_report"]
