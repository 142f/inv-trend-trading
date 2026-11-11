"""Render the US top-100 investment decision desk; presentation only.

All scoring comes pre-derived in ``row["decision"]`` (see
:mod:`inv_trend.application.market_panels.决策推导`); this module only
presents zones, cards and details.  Data stays embedded so the page is
self-contained behind the read-only preview server.
"""

from __future__ import annotations

from html import escape
from typing import Any, Mapping

from .公共 import DESK_META_KEYS, fmt_money, fmt_pct, render_document, render_meta

_DISCLAIMER = (
    "决策支持研究工具，非投资建议；数据为 Yahoo research-only 日线收盘口径；"
    "页面不连接券商，下单由用户手动执行。建议股数按单笔风险预算 = 权益×risk_pct ÷ (止损倍数×ATR) 向下取整。"
)


def render_decision_desk(payload: dict, meta: dict | None = None) -> str:
    """Assemble the desk from the shared base plus its server-rendered budget."""

    return render_document(
        _TEMPLATE,
        data=payload,
        placeholders={
            "%META%": render_meta(meta, DESK_META_KEYS),
            "%DISCLAIMER%": escape(_DISCLAIMER),
            "%PORTFOLIO%": _portfolio_section(payload),
        },
    )


def _portfolio_section(payload: Mapping[str, Any]) -> str:
    """Server-rendered portfolio budget and today's executable tickets.

    The table is rendered in Python so the page is readable without JavaScript;
    the copy/CSV buttons only enhance it.  A payload without a portfolio block
    (older snapshots, unit fixtures) renders nothing at all.
    """

    portfolio = payload.get("portfolio")
    if not isinstance(portfolio, Mapping):
        return ""
    allocations = [item for item in portfolio.get("allocations") or [] if isinstance(item, Mapping)]
    if not allocations:
        return ""
    totals = portfolio.get("totals") or {}
    long_totals = totals.get("long") or {}
    short_totals = totals.get("short") or {}
    caps = portfolio.get("caps") or {}
    boxes = [
        (str(totals.get("positions", 0)), "可执行标的"),
        (fmt_money(totals.get("notional")), "组合名义（$）"),
        (f"{float(totals.get('leverage') or 0):.2f}×", f"杠杆（上限 {caps.get('max_total_leverage', '—')}×）"),
        (fmt_money(totals.get("risk_amount")), f"1N 风险合计（上限 {fmt_pct(float(caps.get('max_total_1n_risk_pct', 0)) * 100)}）"),
        (fmt_pct(totals.get("risk_pct")), "风险占权益"),
        (f"{long_totals.get('positions', 0)} · {fmt_money(long_totals.get('notional'))}", f"多头（风险上限 {fmt_pct(float(caps.get('max_direction_1n_risk_pct', 0)) * 100)}）"),
        (f"{short_totals.get('positions', 0)} · {fmt_money(short_totals.get('notional'))}", "空头"),
    ]
    summary = "".join(
        f'<div class="pf-box"><b>{escape(value)}</b><span>{escape(label)}</span></div>'
        for value, label in boxes
    )
    hits = portfolio.get("caps_hit") or []
    hits_html = "".join(f"<li>{escape(str(item.get('detail')))}</li>" for item in hits if isinstance(item, Mapping))
    rows_html = []
    for index, item in enumerate(allocations, 1):
        side = str(item.get("side"))
        side_label = "做多" if side == "long" else "做空"
        side_cls = "多" if side == "long" else "空"
        qty = int(item.get("qty") or 0)
        suggested = int(item.get("suggested_qty") or qty)
        qty_text = str(qty) if not item.get("capped") else f"{qty}（建议 {suggested}）"
        ticket = f"{item.get('symbol')}\t{side_label}\t{qty}\t{fmt_money(item.get('price'))}\t{fmt_money(item.get('stop'))}"
        rows_html.append(
            "<tr data-ticket=\"" + escape(ticket) + "\">"
            f"<td>{index}</td><td><b>{escape(str(item.get('symbol')))}</b></td>"
            f'<td class="{side_cls}">{side_label}</td>'
            f"<td>{escape(str(item.get('grade')))}</td>"
            f"<td class='num'>{qty_text}</td>"
            f"<td class='num'>{fmt_money(item.get('price'))}</td>"
            f"<td class='num'>{fmt_money(item.get('stop'))}</td>"
            f"<td class='num'>{fmt_money(item.get('risk_amount'))}</td>"
            f"<td class='num'>{fmt_money(item.get('notional'))}</td>"
            f'<td><button type="button" class="copy" data-copy="' + escape(ticket) + '">复制</button></td>'
            "</tr>"
        )
    caps_html = (
        f'<div class="pf-caps"><b>预算裁剪</b><ul>{hits_html}</ul></div>' if hits_html else ''
    )
    table_head = (
        '<h3>今日可执行清单（按优先级，股数已按组合上限裁剪）</h3>'
        '<div class="pf-table-wrap"><table class="pf-table"><thead><tr>'
        '<th>#</th><th>代码</th><th>方向</th><th>评级</th><th>股数</th><th>参考价</th>'
        '<th>止损</th><th>单笔风险($)</th><th>名义($)</th><th>票据</th>'
        '</tr></thead><tbody>'
    )
    return (
        '<section class="pf" id="portfolio">'
        '<div class="pf-head"><h2>组合执行预算</h2>'
        '<div class="pf-actions"><button type="button" id="copy-all">复制全部票据</button>'
        '<button type="button" id="export-csv">导出 CSV</button></div></div>'
        f'<div class="pf-grid">{summary}</div>'
        + caps_html
        + table_head
        + "".join(rows_html)
        + '</tbody></table></div>'
        '<p class="note">票据格式：代码 / 方向 / 股数 / 参考价 / 止损，制表符分隔，可直接粘贴到表格或券商下单窗口。'
        '股数为按单笔风险预算与组合上限裁剪后的可执行数量；未列入的触发标的表示预算已用尽。</p>'
        '</section>'
    )


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>美股前100投资决策面板</title>
<style>
  .summary { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:8px; margin:0 0 12px; }
  .summary .box { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
  .summary b { display:block; font-size:22px; font-variant-numeric:tabular-nums; }
  .summary span { color:var(--muted); font-size:13px; }
  .summary .box.warn b { color:var(--long); }
  button.tab.on { background:var(--ink); border-color:var(--ink); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(min(100%, 340px),1fr)); gap:10px; margin-top:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px; cursor:pointer; }
  .card:hover { border-color:#b9b2a4; }
  .card .head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .card .head b.sym { font-size:17px; }
  .badge.dir-long { background:var(--long); color:#fff; font-weight:700; }
  .badge.dir-short { background:var(--short); color:#fff; font-weight:700; }
  .badge.dir-none { background:#eee9de; color:var(--mix); font-weight:700; }
  .badge.gradeA { background:var(--long-deep); color:#fff; font-weight:700; }
  .badge.gradeB { background:var(--long-mid); color:#fff; font-weight:700; }
  .badge.hot { background:var(--hot); color:var(--mix); font-weight:700; }
  .rows { margin:8px 0 0; font-size:13px; display:grid; gap:3px; }
  .rows .kv { display:flex; justify-content:space-between; gap:10px; }
  .rows .kv span { color:var(--muted); }
  .rows .kv b { font-variant-numeric:tabular-nums; }
  .votes { display:flex; gap:5px; margin-top:6px; flex-wrap:wrap; }
  .votes .cell { min-width:30px; text-align:center; border-radius:6px; padding:1px 6px; font-size:11px; font-weight:700; background:#eee9de; color:var(--mix); }
  .votes .cell.long { background:var(--long); color:#fff; }
  .votes .cell.short { background:var(--short); color:#fff; }
  .detail { margin-top:10px; border-top:1px dashed var(--line); padding-top:10px; }
  .detail .grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:4px 18px; font-size:13px; }
  .detail dt { color:var(--muted); font-size:12px; }
  .detail dd { margin:0 0 5px; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
  canvas { height:130px; margin-top:8px; }
  .empty { color:var(--muted); padding:24px 0; text-align:center; }
  .legend { display:flex; gap:12px; flex-wrap:wrap; margin-top:8px; font-size:12px; color:var(--muted); }
  .fail-list { margin:4px 0 0; padding-left:18px; }
  .pf { margin:0 0 16px; padding:12px 14px 6px; background:var(--card); border:1px solid var(--line); border-radius:12px; }
  .pf-head { display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap; }
  .pf-head h2 { margin:0; font-size:19px; }
  .pf-actions { display:flex; gap:8px; }
  .pf-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; margin:10px 0; }
  .pf-box { background:var(--paper); border:1px solid var(--line); border-radius:10px; padding:8px 10px; }
  .pf-box b { display:block; font-size:18px; font-variant-numeric:tabular-nums; }
  .pf-box span { color:var(--muted); font-size:12px; }
  .pf-caps { background:var(--hot); border-radius:8px; padding:6px 10px; margin:0 0 10px; font-size:12px; }
  .pf-caps ul { margin:4px 0 0; padding-left:18px; }
  .pf h3 { margin:6px 0 8px; font-size:15px; }
  .pf-table-wrap { overflow:auto; max-width:100%; }
  .pf-table { width:100%; border-collapse:collapse; font-size:13px; }
  .pf-table th, .pf-table td { border-bottom:1px solid var(--line); padding:5px 8px; text-align:left; white-space:nowrap; }
  .pf-table th { color:var(--muted); font-weight:600; }
  .pf-table td.num { text-align:right; font-variant-numeric:tabular-nums; }
  .pf-table tr:hover { background:var(--paper); }
  .pf-table .copy { padding:3px 10px; font-size:12px; }

  .summary, .pf-grid { grid-template-columns:repeat(auto-fit,minmax(min(100%, 160px),1fr)); }
  .grid { grid-template-columns:repeat(auto-fill,minmax(min(100%, 320px),1fr)); }
</style>
</head>
<body>
<div class="wrap">
  <h1>QQQ+SPY 前100 · 投资决策面板</h1>
  <dl class="meta">%META%</dl>
  <p class="disclaimer">%DISCLAIMER%</p>
  <div class="summary" id="summary"></div>
  %PORTFOLIO%
  <div class="bar-inner">
    <div class="controls">
      <button type="button" class="tab on" data-zone="triggered">已触发</button>
      <button type="button" class="tab" data-zone="near">临近触发</button>
      <button type="button" class="tab" data-zone="watch">观察仓</button>
      <button type="button" class="tab" data-zone="all">全部</button>
      <input id="q" placeholder="搜索代码（前缀）" aria-label="搜索代码">
      <select id="fund"><option value="">全部基金</option><option>QQQ</option><option>SPY</option><option value="both">两边都有</option></select>
      <select id="dir"><option value="">全部方向</option><option value="long">多头</option><option value="short">空头</option><option value="none">无方向</option></select>
      <button type="button" id="adx" aria-pressed="false">只看 ADX≥25</button>
      <button type="button" id="reset">清空筛选</button>
      <span class="count" id="count"></span>
    </div>
  </div>
  <div class="grid" id="grid"></div>
  <h2>数据失败</h2>
  <ul id="fails" class="note fail-list"></ul>
  <h2>预热不足</h2>
  <ul id="incomplete" class="note fail-list"></ul>
</div>
<noscript><p class="empty" role="status">筛选和展开需要 JavaScript。组合执行清单不依赖脚本。</p></noscript>
<script>
const DATA = %DATA%;
const FAMILY_LABELS = { turtle:"海龟", sma:"SMA", ema:"EMA", macd:"MACD" };
const EXIT_LABELS = { long_fast:"多头快退出 (10日低)", long_slow:"多头慢退出 (20日低)", short_fast:"空头快退出 (10日高)", short_slow:"空头慢退出 (20日高)" };
const state = { zone:"triggered", fund:"", dir:"", adx:false, q:"", open:"" };

function votesOf(row){ const d = row.decision || {}; return d.family_votes || {}; }
function entriesOf(row){ return (row.decision || {}).breakouts || []; }
function gradeOf(row){ return (row.decision || {}).rating ? row.decision.rating.grade : "NONE"; }
function dirOf(row){ const d = (row.decision || {}).rating; return d ? d.direction : null; }
function isTriggered(row){ return entriesOf(row).some(item => item.triggered); }
function isNear(row){
  if (isTriggered(row)) return false;
  return entriesOf(row).some(item => !item.triggered && item.distance_pct !== null && item.distance_pct > 0 && item.distance_pct <= 2);
}
function zoneOf(row){
  const grade = gradeOf(row);
  const strong = grade === "A" || grade === "B";
  if (isTriggered(row)) return strong ? "triggered" : "all";
  if (isNear(row)) return "near";
  return strong ? "watch" : "all";
}
function primaryEntries(row){
  const direction = dirOf(row);
  const side = direction === "short" ? "short" : "long";
  return entriesOf(row).filter(item => item.direction === side);
}
function minPositiveDistance(row){
  const values = entriesOf(row)
    .filter(item => !item.triggered && item.distance_pct !== null && item.distance_pct > 0)
    .map(item => item.distance_pct);
  return values.length ? Math.min.apply(null, values) : null;
}
function show(row){
  if (state.zone !== "all" && zoneOf(row) !== state.zone) return false;
  if (state.q && row.symbol.indexOf(state.q) !== 0) return false;
  if (state.fund === "both" && row.funds.indexOf(",") < 0) return false;
  if (state.fund && state.fund !== "both" && row.funds.indexOf(state.fund) < 0) return false;
  if (state.dir === "none" && dirOf(row) !== null) return false;
  if ((state.dir === "long" || state.dir === "short") && dirOf(row) !== state.dir) return false;
  if (state.adx && !(row.adx >= 25)) return false;
  return true;
}
function sortRows(rows){
  const zone = state.zone;
  return rows.slice().sort((a, b) => {
    if (zone === "triggered") return Math.abs(primaryDistance(a)) - Math.abs(primaryDistance(b));
    if (zone === "near") return (minPositiveDistance(a) ?? 1e9) - (minPositiveDistance(b) ?? 1e9);
    const sa = (a.decision || {}).rating ? (a.decision.rating.score || 0) : 0;
    const sb = (b.decision || {}).rating ? (b.decision.rating.score || 0) : 0;
    return sb - sa;
  });
}
function primaryDistance(row){
  const values = primaryEntries(row).filter(item => item.distance_pct !== null).map(item => item.distance_pct);
  return values.length ? Math.min.apply(null, values.map(Math.abs)) : 1e9;
}
function dirBadge(row){
  const direction = dirOf(row);
  if (direction === "long") return '<span class="badge dir-long">做多</span>';
  if (direction === "short") return '<span class="badge dir-short">做空</span>';
  return '<span class="badge dir-none">无方向</span>';
}
function gradeBadge(row){
  const grade = gradeOf(row);
  const cls = grade === "A" ? "gradeA" : grade === "B" ? "gradeB" : "";
  const score = (row.decision || {}).rating ? row.decision.rating.score : null;
  return '<span class="badge ' + cls + '">' + grade + (score === null ? "" : " " + score + "分") + '</span>';
}
function fmt(value){ return value === null || value === undefined ? "—" : value; }
function kv(label, value){ return '<div class="kv"><span>' + label + '</span><b>' + value + '</b></div>'; }
function votesHtml(row){
  const votes = votesOf(row);
  return '<div class="votes">' + Object.keys(FAMILY_LABELS).map(name => {
    const v = votes[name];
    return '<span class="cell ' + (v || "") + '" title="' + FAMILY_LABELS[name] + '">' + FAMILY_LABELS[name] + " " + (v === "long" ? "多" : v === "short" ? "空" : v === "conflict" ? "冲突" : "无") + '</span>';
  }).join("") + '</div>';
}
function primaryLines(row){
  const decision = row.decision || {};
  const direction = dirOf(row);
  if (direction === null) return kv("触发价 / 距离", "—（无方向）");
  const stops = decision.stops || {};
  const qty = (decision.suggested_qty || {})[direction];
  const exits = decision.exits || {};
  const lines = primaryEntries(row).map(item =>
    kv((direction === "long" ? "突破 " : "跌破 ") + item.period + " 日通道",
       fmt(item.level) + "（" + (item.triggered ? '<span class="' + (direction === "long" ? "多" : "空") + '">已触发</span>' : "差 " + fmt(item.distance_pct) + "%") + "）")
  ).join("");
  return lines
    + kv("止损价（" + (direction === "long" ? "下方" : "上方") + "）", fmt(direction === "long" ? stops.long : stops.short))
    + kv("建议股数", fmt(qty))
    + kv("退出通道", fmt(direction === "long" ? exits.long_fast : exits.short_fast) + " / " + fmt(direction === "long" ? exits.long_slow : exits.short_slow));
}
function detail(row){
  const decision = row.decision || {};
  const rating = decision.rating || {};
  const volatility = decision.volatility || {};
  const trend = decision.trend_quality || {};
  const volume = decision.volume || {};
  const entries = entriesOf(row).map(item =>
    kv(item.direction === "long" ? "做多触发 " + item.period + " 日高" : "做空触发 " + item.period + " 日低",
       fmt(item.level) + "（" + (item.triggered ? "已触发" : "距离 " + fmt(item.distance_pct) + "%") + "）")).join("");
  const exits = decision.exits || {};
  const channel = row.channel || {};
  const adjustments = (rating.quality_adjustments || []).length
    ? rating.quality_adjustments.join("、")
    : "无";
  return '<div class="detail">'
    + '<dl class="grid2">'
    + kv("原始 / 复权收盘", fmt(row.close) + " / " + fmt(row.adjusted_close))
    + kv("ADX · +DI / −DI", fmt(row.adx) + " · " + fmt(row.plus_di) + " / " + fmt(row.minus_di))
    + kv("ATR / ATR 分位", fmt(row.atr) + " / " + fmt(volatility.percentile) + "（" + (volatility.state || "—") + "）")
    + kv("相对量 / 确认", fmt(volume.relative_volume ?? row.relative_volume) + "（" + (volume.confirmed ? "≥1.5 确认" : "未确认") + "）")
    + kv("趋势质量 ADX 确认", trend.confirmed ? "确认" : "未确认（ADX " + fmt(trend.adx) + "）")
    + kv("质量调整", adjustments)
    + entries
    + kv(EXIT_LABELS.long_fast, fmt(exits.long_fast)) + kv(EXIT_LABELS.long_slow, fmt(exits.long_slow))
    + kv(EXIT_LABELS.short_fast, fmt(exits.short_fast)) + kv(EXIT_LABELS.short_slow, fmt(exits.short_slow))
    + kv("唐奇安 10 高/低", fmt(channel.high_10) + " / " + fmt(channel.low_10))
    + kv("唐奇安 20 高/低", fmt(channel.high_20) + " / " + fmt(channel.low_20))
    + kv("唐奇安 55 高/低", fmt(channel.high_55) + " / " + fmt(channel.low_55))
    + kv("MACD dif / dea / 柱", fmt(row.dif) + " / " + fmt(row.dea) + " / " + fmt(row.histogram))
    + kv("数据截止", fmt(row.as_of))
    + '</dl><p class="legend"><span><i style="background:#1c1915"></i>收盘</span><span><i class="swatch long"></i>MACD 柱 ≥ 0</span><span><i class="swatch short"></i>MACD 柱 &lt; 0</span></p><canvas id="c-' + row.symbol.replace(".", "_") + '" aria-label="最近10日收盘与MACD柱"></canvas></div>';
}
function drawTimeline(canvas, timeline){
  const ctx = fitCanvas(canvas, 260);
  const w = canvas.width, h = canvas.height;
  if (!timeline || !timeline.length) return;
  const closes = timeline.map(item => item.close).filter(v => v !== null);
  const hists = timeline.map(item => item.histogram).filter(v => v !== null);
  if (!closes.length) return;
  const min = Math.min.apply(null, closes), max = Math.max.apply(null, closes);
  const pad = 14, hist_h = 70;
  ctx.beginPath(); ctx.strokeStyle = C.ink; ctx.lineWidth = 3;
  timeline.forEach((item, i) => {
    if (item.close === null) return;
    const x = pad + i * (w - pad * 2) / Math.max(timeline.length - 1, 1);
    const y = h - hist_h - 8 - ((item.close - min) / Math.max(max - min, 1e-9)) * (h - hist_h - 24);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  const hmax = Math.max.apply(null, hists.map(Math.abs).concat([1e-9]));
  const zero = h - 10;
  timeline.forEach((item, i) => {
    if (item.histogram === null) return;
    const x = pad + i * (w - pad * 2) / Math.max(timeline.length - 1, 1);
    const bh = Math.abs(item.histogram) / hmax * (hist_h - 12);
    ctx.fillStyle = item.histogram >= 0 ? C.long : C.short;
    ctx.fillRect(x - 4, item.histogram >= 0 ? zero - bh : zero, 8, bh);
  });
}
function renderSummary(){
  const rows = DATA.rows;
  const zones = { triggered: 0, near: 0, watch: 0 };
  rows.forEach(row => { const z = zoneOf(row); if (zones[z] !== undefined) zones[z] += 1; });
  const failed = DATA.failed.length;
  const incomplete = (DATA.incomplete || []).length;
  const total = rows.length;
  const rest = Math.max(0, total - zones.triggered - zones.near - zones.watch);
  document.getElementById("summary").innerHTML =
    '<div class="box"><b>' + zones.triggered + '</b><span>已触发（A/B 级，可执行）</span></div>'
    + '<div class="box"><b>' + zones.near + '</b><span>临近触发（≤2%）</span></div>'
    + '<div class="box"><b>' + zones.watch + '</b><span>观察仓（A/B 级未触发）</span></div>'
    + '<div class="box"><b>' + rest + '</b><span>其余（未评级/未触发）</span></div>'
    + '<div class="box"><b>' + total + '</b><span>标的合计</span></div>'
    + '<div class="box' + (failed ? " warn" : "") + '"><b>' + failed + (failed ? ' <span class="flag">异常</span>' : '') + '</b><span>数据失败</span></div>'
    + '<div class="box"><b>' + incomplete + '</b><span>预热不足</span></div>';
}
function render(){
  const rows = sortRows(DATA.rows.filter(show));
  document.getElementById("count").textContent = rows.length + " / " + DATA.rows.length;
  const grid = document.getElementById("grid");
  grid.innerHTML = rows.length ? rows.map(row => {
    const open = state.open === row.symbol;
    return '<div class="card" data-s="' + row.symbol + '" tabindex="0" role="button" aria-expanded="' + open + '">'
      + '<div class="head"><b class="sym">' + row.symbol + '</b>' + dirBadge(row) + gradeBadge(row)
      + '<span class="badge">' + row.funds + '</span>'
      + ((row.adx >= 25) ? '<span class="badge hot">强趋势</span>' : '')
      + ((row.relative_volume >= 1.5) ? '<span class="badge hot">放量</span>' : '')
      + ((PF_BY[row.symbol] && PF_BY[row.symbol].capped) ? '<span class="badge hot">组合裁剪</span>' : '')
      + (!row.warmup_ok ? '<span class="badge hot">预热不足</span>' : '')
      + '</div>'
      + '<div class="rows">' + kv("收盘", fmt(row.close)) + primaryLines(row) + '</div>'
      + votesHtml(row)
      + (open ? detail(row) : '')
      + '</div>';
  }).join("") : '<div class="empty" role="status">本区没有符合条件的标的。</div>';
  rows.forEach(row => {
    if (state.open !== row.symbol) return;
    const canvas = document.getElementById("c-" + row.symbol.replace(".", "_"));
    if (canvas && row.timeline10) {
      drawTimeline(canvas, row.timeline10);
      bindChartTip(canvas, ["收盘", "MACD柱"], [row.timeline10.map(item => item.close), row.timeline10.map(item => item.histogram)]);
    }
  });
  bindActivate(grid, ".card", card => {
    state.open = state.open === card.dataset.s ? "" : card.dataset.s;
    render();
  });
}
bindButtonGroup(".tab", btn => { state.zone = btn.dataset.zone; render(); });
document.getElementById("q").addEventListener("input", e => { state.q = e.target.value.trim().toUpperCase(); render(); });
document.getElementById("fund").addEventListener("change", e => { state.fund = e.target.value; render(); });
document.getElementById("dir").addEventListener("change", e => { state.dir = e.target.value; render(); });
document.getElementById("adx").addEventListener("click", e => { state.adx = !state.adx; togglePressed(e.target, state.adx); render(); });
document.getElementById("reset").addEventListener("click", () => {
  Object.assign(state, { fund:"", dir:"", adx:false, q:"", open:"" });
  document.getElementById("q").value = "";
  document.getElementById("fund").value = "";
  document.getElementById("dir").value = "";
  const adx = document.getElementById("adx");
  adx.classList.remove("on"); adx.setAttribute("aria-pressed", "false");
  render();
});
renderList(document.getElementById("fails"), DATA.failed, item => "<li>" + item.symbol + "（" + item.funds + "）：" + item.error + "</li>");
renderList(document.getElementById("incomplete"), DATA.incomplete || [], item => "<li>" + item.symbol + "：" + item.bar_count + " 根，" + item.reason + "，预热不足</li>");
function copyText(text){
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
  }
  return Promise.resolve(fallbackCopy(text));
}
function fallbackCopy(text){
  const area = document.createElement("textarea");
  area.value = text; area.style.position = "fixed"; area.style.opacity = "0";
  document.body.appendChild(area); area.select();
  try { document.execCommand("copy"); } finally { document.body.removeChild(area); }
}
const PF = DATA.portfolio || null;
const PF_BY = {};
if (PF) { (PF.allocations || []).forEach(item => { PF_BY[item.symbol] = item; }); }
document.querySelectorAll(".pf-table .copy").forEach(button => button.addEventListener("click", () => {
  copyText(button.dataset.copy || "");
  const label = button.textContent; button.textContent = "已复制";
  setTimeout(() => { button.textContent = label; }, 1200);
}));
const copyAll = document.getElementById("copy-all");
if (copyAll) copyAll.addEventListener("click", () => {
  const rows = (PF && PF.allocations) || [];
  const header = "代码\t方向\t股数\t参考价\t止损";
  const body = rows.map(item => [item.symbol, item.side === "long" ? "做多" : "做空", item.qty, item.price, item.stop].join("\t"));
  copyText([header].concat(body).join("\\n"));
});
const exportCsv = document.getElementById("export-csv");
if (exportCsv) exportCsv.addEventListener("click", () => {
  const rows = (PF && PF.allocations) || [];
  const head = ["symbol","side","grade","qty","suggested_qty","price","stop","risk_amount","notional","funds"];
  const lines = [head.join(",")].concat(rows.map(item => head.map(key => {
    const value = item[key];
    return value === null || value === undefined ? "" : String(value);
  }).join(",")));
  const blob = new Blob(["\\ufeff" + lines.join("\\n")], {type:"text/csv;charset=utf-8"});
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "美股前100可执行清单_" + (DATA.as_of || "latest") + ".csv";
  document.body.appendChild(link); link.click(); document.body.removeChild(link);
  URL.revokeObjectURL(link.href);
});
renderSummary();
render();
onResize(render);
</script>
</body>
</html>
"""
