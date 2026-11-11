"""Render the US top-100 market thermometer panel; presentation only.

Consumes the snapshot payload's ``market`` block (see
:mod:`inv_trend.application.market_panels.市场聚合`) plus the temperature
history file.  Self-contained HTML with embedded JSON.
"""

from __future__ import annotations

from html import escape

from .公共 import BASE_META_KEYS, render_document, render_meta

_TEMPERATURE_FORMULA = "温度分 = 0.4×MACD多头占比 + 0.3×EMA55上方占比 + 0.3×净突破占比（多头突破 − 空头突破，按只数），钳位 0–100。"


def render_market_thermometer(payload: dict, meta: dict | None = None, history: list | None = None) -> str:
    """Assemble the thermometer from the shared base plus its history series."""

    return render_document(
        _TEMPLATE,
        data=payload,
        history=history or [],
        placeholders={
            "%META%": render_meta(meta, BASE_META_KEYS),
            "%FORMULA%": escape(_TEMPERATURE_FORMULA),
        },
    )


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>美股前100市场温度面板</title>
<style>
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
  table { width:100%; border-collapse:collapse; background:var(--card); }
  th, td { padding:7px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; font-variant-numeric:tabular-nums; }
  th { font-size:13px; color:var(--muted); }
  .bars { display:grid; gap:6px; }
  .bars .row { display:grid; grid-template-columns:130px 1fr 60px; align-items:center; gap:10px; font-size:13px; }
  .bars .rail { height:12px; background:#eee9de; border-radius:999px; overflow:hidden; }
  .bars .rail i { display:block; height:100%; }
  canvas { height:220px; }
  .controls { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:8px 0; }
  .legend { display:flex; gap:14px; font-size:12px; color:var(--muted); flex-wrap:wrap; }
  .legend i { display:inline-block; width:14px; height:4px; border-radius:2px; margin-right:4px; vertical-align:middle; }

  .health, .cards { grid-template-columns:repeat(auto-fit,minmax(min(100%, 148px),1fr)); }
  .temp { grid-template-columns:minmax(160px,220px) minmax(0,1fr); min-width:0; }
  @media (max-width:1024px) { .temp { grid-template-columns:1fr; } }
  @media (max-width:760px) {
    .bars .row { grid-template-columns:1fr auto; }
    .bars .rail { grid-column:1 / -1; }
  }
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
    <div><b class="big" id="tempBig">—</b><div class="temp-meta"><span id="tempBand" class="badge">—</span></div><div class="gauge" role="img" aria-labelledby="tempBand"><i id="tempGauge"></i></div></div>
    <div><div class="cards" id="breadth"></div><p class="formula">%FORMULA%</p></div>
  </div>

  <h2>评级分布（日报链口径 A/B/C）</h2>
  <div class="bars" id="ratings"></div>

  <h2>波动状态与突破计数</h2>
  <div class="cards" id="volatility"></div>
  <div class="tablewrap" tabindex="0" aria-label="突破计数，可横向滚动"><table style="margin-top:10px">
    <thead><tr><th>方向 / 周期</th><th>20 日</th><th>55 日</th></tr></thead>
    <tbody id="breakouts"></tbody>
  </table></div>

  <h2>温度历史（按收盘日）</h2>
  <div class="controls">
    <button type="button" class="win on" data-n="60">60 日</button>
    <button type="button" class="win" data-n="120">120 日</button>
    <button type="button" class="win" data-n="250">250 日</button>
    <span class="legend">
      <span><i style="background:var(--ink)"></i>温度分</span>
      <span><i style="background:var(--long)"></i>A 级多头只数</span>
      <span><i style="background:var(--short)"></i>A 级空头只数</span>
      <span><i style="background:var(--mix)"></i>净突破（只）</span>
    </span>
  </div>
  <canvas id="historyCanvas" tabindex="0" aria-label="温度历史曲线"></canvas>
  <p class="chart-tip" id="historyTip" role="status"></p>
  <p class="note" id="historyNote"></p>
</div>
<noscript><p class="empty" role="status">需要启用 JavaScript 才能切换历史窗口。</p></noscript>
<script>
const DATA = %DATA%;
const HISTORY = %HISTORY%;
const market = DATA.market || {};
const HEALTH_KEYS = [["rows", "标的"], ["failed", "数据失败"], ["incomplete", "预热不足"], ["triggered_count", "已触发（全部评级）"], ["triggered_ab_count", "已触发 A/B 级（可执行）"], ["near_count", "临近触发"]];
const RATING_META = [
  ["A_long", "A 级多头", C.longDeep], ["A_short", "A 级空头", C.short],
  ["B_long", "B 级多头", C.longPale], ["B_short", "B 级空头", C.shortMid],
  ["C_long", "C 级多头", C.soft], ["C_short", "C 级空头", C.soft],
  ["CONFLICT", "族间冲突", C.mix], ["NONE", "无方向", C.neutral],
];
let window_ = 60;

function renderHealth(){
  const health = market.data_health || {};
  const boxes = HEALTH_KEYS.map(([key, label]) => {
    const value = (key === "triggered_count" || key === "triggered_ab_count" || key === "near_count")
      ? market[key]
      : health[key];
    const shown = value !== undefined && value !== null ? value : "—";
    const warn = (key === "failed" || key === "incomplete") && shown > 0;
    return '<div class="box' + (warn ? " warn" : "") + '"><b>' + shown + (warn ? ' <span class="flag">异常</span>' : '') + '</b><span>' + label + '</span></div>';
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
  const band = (score === undefined || score === null) ? "暂无" : score >= 60 ? "偏热" : score >= 40 ? "温和偏热" : score >= 20 ? "温和偏冷" : "偏冷";
  const bandEl = document.getElementById("tempBand");
  if (bandEl) bandEl.textContent = band;
  gauge.title = "温度 " + (score === undefined || score === null ? "—" : score) + "，" + band;
  gauge.style.background = score >= 60 ? C.longDeep : score >= 40 ? C.longMid : score >= 20 ? C.shortMid : C.short;
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
  const ctx = fitCanvas(canvas, 440);
  const w = canvas.width, h = canvas.height;
  const records = HISTORY.slice(-window_);
  lastRecords = records;
  document.getElementById("historyNote").textContent =
    records.length ? "最近 " + records.length + " 个收盘日（" + records[0].as_of + " → " + records[records.length - 1].as_of + "）" : "暂无历史记录：温度历史随每日收盘刷新累积。";
  if (records.length < 2) return;
  const series = [
    { values: records.map(r => r.temperature_score), color: C.ink, width: 4 },
    { values: records.map(r => (r.rating_dist ? (r.rating_dist.A_long || 0) : null)), color: C.long, width: 2 },
    { values: records.map(r => (r.rating_dist ? (r.rating_dist.A_short || 0) : null)), color: C.short, width: 2 },
    { values: records.map(r => {
        const b = r.breakout_counts;
        return b ? (b.long_20 || 0) + (b.long_55 || 0) - (b.short_20 || 0) - (b.short_55 || 0) : null;
      }), color: C.mix, width: 2 },
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
let lastRecords = [];
const historyCanvas = document.getElementById("historyCanvas");
const historyTip = document.getElementById("historyTip");
historyCanvas.addEventListener("mousemove", event => {
  if (!lastRecords.length) return;
  const rect = historyCanvas.getBoundingClientRect();
  const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / Math.max(rect.width, 1)));
  const record = lastRecords[Math.round(ratio * (lastRecords.length - 1))];
  const breaks = record.breakout_counts || {};
  const net = (breaks.long_20 || 0) + (breaks.long_55 || 0) - (breaks.short_20 || 0) - (breaks.short_55 || 0);
  const ratings = record.rating_dist || {};
  historyTip.textContent = record.as_of + " · 温度 " + record.temperature_score + " · A多 " + (ratings.A_long || 0) + " · A空 " + (ratings.A_short || 0) + " · 净突破 " + net;
});
bindButtonGroup(".win", btn => { window_ = Number(btn.dataset.n); drawHistory(); });
renderHealth();
renderTemperature();
renderRatings();
renderVolatility();
installBackToTop();
drawHistory();
onResize(drawHistory);
</script>
</body>
</html>
"""
