"""Render the US top-100 market thermometer panel; presentation only.

Consumes the snapshot payload's ``market`` block (see
:mod:`inv_trend.application.market_panels.市场聚合`) plus the temperature
history file.  Self-contained HTML with embedded JSON.
"""

from __future__ import annotations

import json
from html import escape

_META_KEYS = (
    ("provider", "数据来源"),
    ("data_cutoff", "数据截止"),
    ("price_basis", "价格口径"),
    ("config_hash", "配置哈希"),
    ("content_hash", "内容哈希"),
    ("script_git_commit", "脚本提交"),
)

_TEMPERATURE_FORMULA = "温度分 = 0.4×MACD多头占比 + 0.3×EMA55上方占比 + 0.3×净突破占比（多头突破 − 空头突破，按只数），钳位 0–100。"


def render_market_thermometer(payload: dict, meta: dict | None = None, history: list | None = None) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    history_json = json.dumps(history or [], ensure_ascii=False).replace("</", "<\\/")
    header = meta or {}
    meta_html = "".join(
        f"<div><dt>{escape(label)}</dt><dd>{escape(str(header.get(key, '—')))}</dd></div>"
        for key, label in _META_KEYS
    )
    template = _TEMPLATE.replace("%META%", meta_html)
    template = template.replace("%FORMULA%", escape(_TEMPERATURE_FORMULA))
    template = template.replace("%HISTORY%", history_json)
    template = template.replace("%DATA%", data)
    return template


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>美股前100市场温度面板</title>
<style>
  :root { color-scheme: light; --ink:#1c1915; --muted:#5c564c; --line:#e4ddd0; --paper:#f6f3ec; --card:#fffdf8;
          --long:#b3372b; --short:#1e7d46; --mix:#8a5a12; --hot:#f8efd8; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 "Segoe UI","Microsoft YaHei",sans-serif; color:var(--ink); background:var(--paper); }
  .wrap { max-width:1180px; margin:0 auto; padding:24px 18px 48px; }
  h1 { margin:0 0 6px; font-size:26px; }
  h2 { font-size:18px; margin:22px 0 8px; }
  .meta { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:6px 18px; margin:0 0 12px;
          padding:10px 14px; background:var(--card); border:1px solid var(--line); border-radius:10px; font-size:13px; }
  .meta dt { color:var(--muted); }
  .meta dd { margin:0; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
  .disclaimer { color:var(--mix); background:var(--hot); border-radius:10px; padding:8px 14px; margin:0 0 14px; font-size:13px; }
  .health { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; margin:0 0 12px; }
  .health .box { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:9px 12px; font-size:13px; }
  .health b { font-size:17px; font-variant-numeric:tabular-nums; display:block; }
  .health .warn b { color:var(--long); }
  .health span { color:var(--muted); }
  .temp { display:grid; grid-template-columns:220px 1fr; gap:16px; align-items:center; background:var(--card);
          border:1px solid var(--line); border-radius:12px; padding:16px 18px; }
  .temp b.big { font-size:52px; font-variant-numeric:tabular-nums; line-height:1; }
  .temp .gauge { height:16px; border-radius:999px; overflow:hidden; background:#eee9de; margin-top:8px; }
  .temp .gauge i { display:block; height:100%; }
  .temp .formula { color:var(--muted); font-size:13px; margin-top:8px; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; margin:10px 0 0; }
  .cards .box { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
  .cards b { display:block; font-size:20px; font-variant-numeric:tabular-nums; }
  .cards span { color:var(--muted); font-size:13px; }
  table { width:100%; border-collapse:separate; border-spacing:0; background:var(--card); }
  th, td { padding:7px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; font-variant-numeric:tabular-nums; }
  th { font-size:13px; color:var(--muted); }
  .bars { display:grid; gap:6px; }
  .bars .row { display:grid; grid-template-columns:130px 1fr 60px; align-items:center; gap:10px; font-size:13px; }
  .bars .rail { height:12px; background:#eee9de; border-radius:999px; overflow:hidden; }
  .bars .rail i { display:block; height:100%; }
  canvas { width:100%; height:220px; background:#fff; border:1px solid var(--line); border-radius:8px; }
  .controls { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:8px 0; }
  button { font:inherit; border:1px solid #d5cec0; background:var(--card); border-radius:999px; padding:6px 12px; cursor:pointer; }
  button.on { background:#0c5c56; color:#f4fffc; border-color:#0c5c56; }
  .legend { display:flex; gap:14px; font-size:12px; color:var(--muted); flex-wrap:wrap; }
  .legend i { display:inline-block; width:14px; height:4px; border-radius:2px; margin-right:4px; vertical-align:middle; }
  .多 { color:var(--long); font-weight:700; }
  .空 { color:var(--short); font-weight:700; }
  .note { color:var(--muted); font-size:13px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>QQQ+SPY 前100 · 市场温度面板</h1>
  <dl class="meta">%META%</dl>
  <p class="disclaimer">决策支持研究工具，非投资建议；数据为 Yahoo research-only 日线收盘口径。回答"当前市场环境适不适合顺趋势交易"。</p>

  <h2>数据新鲜度</h2>
  <div class="health" id="health"></div>

  <h2>市场温度</h2>
  <div class="temp">
    <div><b class="big" id="tempBig">—</b><div class="gauge"><i id="tempGauge"></i></div></div>
    <div><div class="cards" id="breadth"></div><p class="formula">%FORMULA%</p></div>
  </div>

  <h2>评级分布（日报链口径 A/B/C）</h2>
  <div class="bars" id="ratings"></div>

  <h2>波动状态与突破计数</h2>
  <div class="cards" id="volatility"></div>
  <table style="margin-top:10px">
    <thead><tr><th>方向 / 周期</th><th>20 日</th><th>55 日</th></tr></thead>
    <tbody id="breakouts"></tbody>
  </table>

  <h2>温度历史（按收盘日）</h2>
  <div class="controls">
    <button type="button" class="win on" data-n="60">60 日</button>
    <button type="button" class="win" data-n="120">120 日</button>
    <button type="button" class="win" data-n="250">250 日</button>
    <span class="legend">
      <span><i style="background:#1c1915"></i>温度分</span>
      <span><i style="background:#b3372b"></i>A 级多头只数</span>
      <span><i style="background:#1e7d46"></i>A 级空头只数</span>
      <span><i style="background:#8a5a12"></i>净突破（只）</span>
    </span>
  </div>
  <canvas id="historyCanvas"></canvas>
  <p class="note" id="historyNote"></p>
</div>
<script>
const DATA = %DATA%;
const HISTORY = %HISTORY%;
const market = DATA.market || {};
const HEALTH_KEYS = [["rows", "标的"], ["failed", "数据失败"], ["incomplete", "预热不足"], ["triggered_count", "今日已触发"], ["near_count", "临近触发"]];
const RATING_META = [
  ["A_long", "A 级多头", "#8d2b1f"], ["A_short", "A 级空头", "#1e7d46"],
  ["B_long", "B 级多头", "#d9918a"], ["B_short", "B 级空头", "#7fb394"],
  ["C_long", "C 级多头", "#e3d9c6"], ["C_short", "C 级空头", "#e3d9c6"],
  ["CONFLICT", "族间冲突", "#8a5a12"], ["NONE", "无方向", "#b9b2a4"],
];
let window_ = 60;

function renderHealth(){
  const health = market.data_health || {};
  const boxes = HEALTH_KEYS.map(([key, label]) => {
    const value = (key === "triggered_count" || key === "near_count")
      ? market[key]
      : health[key];
    const shown = value !== undefined && value !== null ? value : "—";
    const warn = (key === "failed" || key === "incomplete") && shown > 0;
    return '<div class="box' + (warn ? " warn" : "") + '"><b>' + shown + '</b><span>' + label + '</span></div>';
  }).join("")
    + '<div class="box"><b>' + (DATA.partial ? "盘中快照" : "收盘口径") + '</b><span>数据模式</span></div>'
    + '<div class="box"><b>' + (health.sources || []).join(" / ") + "</b><span>数据源</span></div>";
  document.getElementById("health").innerHTML = boxes;
}
function renderTemperature(){
  const score = market.temperature_score;
  document.getElementById("tempBig").textContent = score === undefined ? "—" : score;
  const gauge = document.getElementById("tempGauge");
  gauge.style.width = (score === undefined ? 0 : Math.max(0, Math.min(100, score))) + "%";
  gauge.style.background = score >= 60 ? "#8d2b1f" : score >= 40 ? "#c07a45" : score >= 20 ? "#7fb394" : "#1e7d46";
  const breadth = market.breadth || {};
  const items = [
    ["MACD 多头占比", breadth.macd_long_pct], ["EMA55 上方占比", breadth.above_ema55_pct],
    ["SMA20 上方占比", breadth.above_sma20_pct], ["ADX≥25 占比", breadth.adx_strong_pct],
    ["中位 ADX", market.median_adx], ["中位相对量", market.median_relative_volume],
  ];
  document.getElementById("breadth").innerHTML = items.map(([label, value]) =>
    '<div class="box"><b>' + (value === undefined || value === null ? "—" : value) + '</b><span>' + label + '</span></div>').join("");
}
function renderRatings(){
  const dist = market.rating_dist || {};
  const total = Object.values(dist).reduce((a, b) => a + b, 0) || 1;
  document.getElementById("ratings").innerHTML = RATING_META.map(([key, label, color]) => {
    const value = dist[key] || 0;
    return '<div class="row"><span>' + label + '</span><div class="rail"><i style="width:' + (value * 100 / total) + '%;background:' + color + '"></i></div><b>' + value + '</b></div>';
  }).join("");
}
function renderVolatility(){
  const counts = market.volatility_counts || {};
  document.getElementById("volatility").innerHTML =
    ["normal", "low", "high", "unavailable"].map(key => {
      const label = { normal: "波动正常", low: "波动低迷", high: "波动过高", unavailable: "不可用" }[key];
      return '<div class="box"><b>' + (counts[key] || 0) + '</b><span>' + label + '</span></div>';
    }).join("");
  const breaks = market.breakout_counts || {};
  document.getElementById("breakouts").innerHTML =
    '<tr><td><span class="多">多头突破（收盘破上沿）</span></td><td>' + (breaks.long_20 || 0) + '</td><td>' + (breaks.long_55 || 0) + '</td></tr>'
    + '<tr><td><span class="空">空头突破（收盘破下沿）</span></td><td>' + (breaks.short_20 || 0) + '</td><td>' + (breaks.short_55 || 0) + '</td></tr>';
}
function drawHistory(){
  const canvas = document.getElementById("historyCanvas");
  const ctx = canvas.getContext("2d");
  const w = canvas.width = canvas.clientWidth * 2, h = canvas.height = 440;
  ctx.clearRect(0, 0, w, h);
  const records = HISTORY.slice(-window_);
  document.getElementById("historyNote").textContent =
    records.length ? "最近 " + records.length + " 个收盘日（" + records[0].as_of + " → " + records[records.length - 1].as_of + "）" : "暂无历史记录：温度历史随每日收盘刷新累积。";
  if (records.length < 2) return;
  const series = [
    { values: records.map(r => r.temperature_score), color: "#1c1915", width: 4 },
    { values: records.map(r => (r.rating_dist ? (r.rating_dist.A_long || 0) : null)), color: "#b3372b", width: 2 },
    { values: records.map(r => (r.rating_dist ? (r.rating_dist.A_short || 0) : null)), color: "#1e7d46", width: 2 },
    { values: records.map(r => {
        const b = r.breakout_counts;
        return b ? (b.long_20 || 0) + (b.long_55 || 0) - (b.short_20 || 0) - (b.short_55 || 0) : null;
      }), color: "#8a5a12", width: 2 },
  ];
  const nums = series.flatMap(s => s.values).filter(v => v !== null && v !== undefined);
  const min = Math.min.apply(null, nums), max = Math.max.apply(null, nums);
  const pad = 16;
  series.forEach(line => {
    ctx.beginPath(); ctx.strokeStyle = line.color; ctx.lineWidth = line.width * 2;
    let started = false;
    line.values.forEach((v, i) => {
      if (v === null || v === undefined) { started = false; return; }
      const x = pad + i * (w - pad * 2) / Math.max(line.values.length - 1, 1);
      const y = h - 16 - ((v - min) / Math.max(max - min, 1e-9)) * (h - 32);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
}
document.querySelectorAll(".win").forEach(btn => btn.addEventListener("click", () => {
  window_ = Number(btn.dataset.n);
  document.querySelectorAll(".win").forEach(item => item.classList.toggle("on", item === btn));
  drawHistory();
}));
renderHealth();
renderTemperature();
renderRatings();
renderVolatility();
drawHistory();
</script>
</body>
</html>
"""
