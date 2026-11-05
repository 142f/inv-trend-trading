"""Render the US top-100 investment decision desk; presentation only.

All scoring comes pre-derived in ``row["decision"]`` (see
:mod:`inv_trend.application.market_panels.决策推导`); this module only
presents zones, cards and details.  Data stays embedded so the page is
self-contained behind the read-only preview server.
"""

from __future__ import annotations

import json
from html import escape

_META_KEYS = (
    ("provider", "数据来源"),
    ("data_cutoff", "数据截止"),
    ("price_basis", "价格口径"),
    ("equity", "权益假设"),
    ("risk_pct", "单笔风险预算"),
    ("stop_n", "止损倍数 (×ATR)"),
    ("config_hash", "配置哈希"),
    ("content_hash", "内容哈希"),
    ("script_git_commit", "脚本提交"),
)

_DISCLAIMER = (
    "决策支持研究工具，非投资建议；数据为 Yahoo research-only 日线收盘口径；"
    "页面不连接券商，下单由用户手动执行。建议股数按单笔风险预算 = 权益×risk_pct ÷ (止损倍数×ATR) 向下取整。"
)


def render_decision_desk(payload: dict, meta: dict | None = None) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    header = meta or {}
    meta_html = "".join(
        f"<div><dt>{escape(label)}</dt><dd>{escape(str(header.get(key, '—')))}</dd></div>"
        for key, label in _META_KEYS
    )
    template = _TEMPLATE.replace("%META%", meta_html)
    template = template.replace("%DISCLAIMER%", escape(_DISCLAIMER))
    template = template.replace("%DATA%", data)
    return template


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>美股前100投资决策面板</title>
<style>
  :root { color-scheme: light; --ink:#1c1915; --muted:#5c564c; --line:#e4ddd0; --paper:#f6f3ec; --card:#fffdf8;
          --long:#b3372b; --short:#1e7d46; --mix:#8a5a12; --neutral:#9aa3ad; --hot:#f8efd8; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 "Segoe UI","Microsoft YaHei",sans-serif; color:var(--ink); background:var(--paper); }
  .wrap { max-width:1280px; margin:0 auto; padding:24px 18px 48px; }
  h1 { margin:0 0 6px; font-size:26px; }
  .meta { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:6px 18px; margin:0 0 12px;
          padding:10px 14px; background:var(--card); border:1px solid var(--line); border-radius:10px; font-size:13px; }
  .meta dt { color:var(--muted); }
  .meta dd { margin:0; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
  .disclaimer { color:var(--mix); background:var(--hot); border-radius:10px; padding:8px 14px; margin:0 0 14px; font-size:13px; }
  .summary { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:8px; margin:0 0 12px; }
  .summary .box { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
  .summary b { display:block; font-size:22px; font-variant-numeric:tabular-nums; }
  .summary span { color:var(--muted); font-size:13px; }
  .summary .box.warn b { color:var(--long); }
  .bar-inner { position:sticky; top:0; z-index:2; background:rgba(246,243,236,.97); padding:10px 0 12px; border-bottom:1px solid var(--line); }
  .controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  input, select, button { font:inherit; border:1px solid #d5cec0; background:var(--card); border-radius:999px; padding:7px 12px; }
  button { cursor:pointer; }
  button.on { background:#0c5c56; color:#f4fffc; border-color:#0c5c56; }
  button.tab.on { background:#1c1915; border-color:#1c1915; }
  .count { font-variant-numeric:tabular-nums; color:var(--muted); }
  .grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:10px; margin-top:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px; cursor:pointer; }
  .card:hover { border-color:#b9b2a4; }
  .card .head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .card .head b.sym { font-size:17px; }
  .badge { display:inline-block; border-radius:999px; padding:1px 8px; font-size:11px; background:#eee9de; color:var(--muted); }
  .badge.dir-long { background:var(--long); color:#fff; font-weight:700; }
  .badge.dir-short { background:var(--short); color:#fff; font-weight:700; }
  .badge.dir-none { background:#eee9de; color:var(--mix); font-weight:700; }
  .badge.gradeA { background:#8d2b1f; color:#fff; font-weight:700; }
  .badge.gradeB { background:#c07a45; color:#fff; font-weight:700; }
  .badge.hot { background:var(--hot); color:var(--mix); font-weight:700; }
  .rows { margin:8px 0 0; font-size:13px; display:grid; gap:3px; }
  .rows .kv { display:flex; justify-content:space-between; gap:10px; }
  .rows .kv span { color:var(--muted); }
  .rows .kv b { font-variant-numeric:tabular-nums; }
  .多 { color:var(--long); font-weight:700; }
  .空 { color:var(--short); font-weight:700; }
  .混, .平, .中 { color:var(--mix); font-weight:700; }
  .votes { display:flex; gap:5px; margin-top:6px; flex-wrap:wrap; }
  .votes .cell { min-width:30px; text-align:center; border-radius:6px; padding:1px 6px; font-size:11px; font-weight:700; background:#eee9de; color:var(--mix); }
  .votes .cell.long { background:var(--long); color:#fff; }
  .votes .cell.short { background:var(--short); color:#fff; }
  .detail { margin-top:10px; border-top:1px dashed var(--line); padding-top:10px; }
  .detail .grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:4px 18px; font-size:13px; }
  .detail dt { color:var(--muted); font-size:12px; }
  .detail dd { margin:0 0 5px; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
  canvas { width:100%; height:130px; background:#fff; border:1px solid var(--line); border-radius:8px; margin-top:8px; }
  .note { color:var(--muted); font-size:13px; }
  .empty { color:var(--muted); padding:24px 0; text-align:center; }
  .fail-list { margin:4px 0 0; padding-left:18px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>QQQ+SPY 前100 · 投资决策面板</h1>
  <dl class="meta">%META%</dl>
  <p class="disclaimer">%DISCLAIMER%</p>
  <div class="summary" id="summary"></div>
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
    + '</dl><canvas id="c-' + row.symbol.replace(".", "_") + '" aria-label="最近10日收盘与MACD柱"></canvas></div>';
}
function drawTimeline(canvas, timeline){
  const ctx = canvas.getContext("2d");
  const w = canvas.width = canvas.clientWidth * 2, h = canvas.height = 260;
  ctx.clearRect(0, 0, w, h);
  if (!timeline || !timeline.length) return;
  const closes = timeline.map(item => item.close).filter(v => v !== null);
  const hists = timeline.map(item => item.histogram).filter(v => v !== null);
  if (!closes.length) return;
  const min = Math.min.apply(null, closes), max = Math.max.apply(null, closes);
  const pad = 14, hist_h = 70;
  ctx.beginPath(); ctx.strokeStyle = "#1c1915"; ctx.lineWidth = 3;
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
    ctx.fillStyle = item.histogram >= 0 ? "#b3372b" : "#1e7d46";
    ctx.fillRect(x - 4, item.histogram >= 0 ? zero - bh : zero, 8, bh);
  });
}
function renderSummary(){
  const rows = DATA.rows;
  const zones = { triggered: 0, near: 0, watch: 0 };
  rows.forEach(row => { const z = zoneOf(row); if (zones[z] !== undefined) zones[z] += 1; });
  const failed = DATA.failed.length;
  const incomplete = (DATA.incomplete || []).length;
  document.getElementById("summary").innerHTML =
    '<div class="box"><b>' + zones.triggered + '</b><span>已触发（A/B 级）</span></div>'
    + '<div class="box"><b>' + zones.near + '</b><span>临近触发（≤2%）</span></div>'
    + '<div class="box"><b>' + zones.watch + '</b><span>观察仓</span></div>'
    + '<div class="box' + (failed ? " warn" : "") + '"><b>' + failed + '</b><span>数据失败</span></div>'
    + '<div class="box"><b>' + incomplete + '</b><span>预热不足</span></div>';
}
function render(){
  const rows = sortRows(DATA.rows.filter(show));
  document.getElementById("count").textContent = rows.length + " / " + DATA.rows.length;
  const grid = document.getElementById("grid");
  grid.innerHTML = rows.length ? rows.map(row => {
    const open = state.open === row.symbol;
    return '<div class="card" data-s="' + row.symbol + '">'
      + '<div class="head"><b class="sym">' + row.symbol + '</b>' + dirBadge(row) + gradeBadge(row)
      + '<span class="badge">' + row.funds + '</span>'
      + ((row.adx >= 25) ? '<span class="badge hot">强趋势</span>' : '')
      + ((row.relative_volume >= 1.5) ? '<span class="badge hot">放量</span>' : '')
      + (!row.warmup_ok ? '<span class="badge hot">预热不足</span>' : '')
      + '</div>'
      + '<div class="rows">' + kv("收盘", fmt(row.close)) + primaryLines(row) + '</div>'
      + votesHtml(row)
      + (open ? detail(row) : '')
      + '</div>';
  }).join("") : '<div class="empty">本区没有符合条件的标的。</div>';
  rows.forEach(row => {
    if (state.open !== row.symbol) return;
    const canvas = document.getElementById("c-" + row.symbol.replace(".", "_"));
    if (canvas) drawTimeline(canvas, row.timeline10);
  });
  grid.querySelectorAll(".card").forEach(card => card.addEventListener("click", () => {
    state.open = state.open === card.dataset.s ? "" : card.dataset.s;
    render();
  }));
}
document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => {
  state.zone = tab.dataset.zone;
  document.querySelectorAll(".tab").forEach(item => item.classList.toggle("on", item === tab));
  render();
}));
document.getElementById("q").addEventListener("input", e => { state.q = e.target.value.trim().toUpperCase(); render(); });
document.getElementById("fund").addEventListener("change", e => { state.fund = e.target.value; render(); });
document.getElementById("dir").addEventListener("change", e => { state.dir = e.target.value; render(); });
document.getElementById("adx").addEventListener("click", e => { state.adx = !state.adx; e.target.classList.toggle("on", state.adx); e.target.setAttribute("aria-pressed", String(state.adx)); render(); });
document.getElementById("reset").addEventListener("click", () => {
  Object.assign(state, { fund:"", dir:"", adx:false, q:"", open:"" });
  document.getElementById("q").value = "";
  document.getElementById("fund").value = "";
  document.getElementById("dir").value = "";
  const adx = document.getElementById("adx");
  adx.classList.remove("on"); adx.setAttribute("aria-pressed", "false");
  render();
});
document.getElementById("fails").innerHTML = DATA.failed.map(item => "<li>" + item.symbol + "（" + item.funds + "）：" + item.error + "</li>").join("") || "<li>没有</li>";
document.getElementById("incomplete").innerHTML = (DATA.incomplete || []).map(item => "<li>" + item.symbol + "：" + item.bar_count + " 根，" + item.reason + "，预热不足</li>").join("") || "<li>没有</li>";
renderSummary();
render();
</script>
</body>
</html>
"""
