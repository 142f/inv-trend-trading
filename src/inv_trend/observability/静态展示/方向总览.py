"""Render the all-symbols direction-skew overview board; presentation only.

Consumes the same payload as :mod:`快照看板` (the US top-100 snapshot JSON) and
derives every direction vote in the browser from stored indicator fields, so the
Python side stays free of any second computation of signals.
"""

from __future__ import annotations

import json
from html import escape

_SYSTEMS = (
    ("ema_10_20", "EMA 10/20"),
    ("ema_10_55", "EMA 10/55"),
    ("ema_20_55", "EMA 20/55"),
    ("sma", "SMA 排列"),
    ("macd", "MACD"),
    ("di", "DMI ±DI"),
    ("donchian_20", "唐奇安 20 位置"),
    ("donchian_55", "唐奇安 55 位置"),
)

_RULES = (
    "EMA 三对：快线在慢线上方为多，下方为空（与指标看板同源字段）。",
    "SMA 排列：直接取数据内的 sma_bias（多 / 空 / 混）。",
    "MACD：dif 在 dea 上方为多，下方为空。",
    "DMI：+DI &gt; −DI 为多，反之为空；ADX 只是趋势强度，不改变投票。",
    "唐奇安位置：收盘 ≥ 上沿为多，≤ 下沿为空，其余为中；边界取数据内存储的当根窗口值，是位置判定而非突破信号。",
    "偏度分 = 多 +1 / 空 −1 / 中或混 0 的八票求和，范围 −8…+8；强多 ≥ +6，强空 ≤ −6。",
)

_META_KEYS = (
    ("provider", "数据来源"),
    ("data_cutoff", "数据截止"),
    ("price_basis", "价格口径"),
    ("config_hash", "配置哈希"),
    ("content_hash", "内容哈希"),
    ("script_git_commit", "脚本提交"),
)


def render_direction_overview(payload: dict, meta: dict | None = None) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    header = meta or {}
    meta_html = "".join(
        f"<div><dt>{escape(label)}</dt><dd>{escape(str(header.get(key, '—')))}</dd></div>"
        for key, label in _META_KEYS
    )
    systems_head = "".join(
        f'<th class="sys" data-sys="{name}">{label}</th>' for name, label in _SYSTEMS
    )
    rules_html = "".join(f"<li>{rule}</li>" for rule in _RULES)
    template = _TEMPLATE.replace("%SYSTEMS_HEAD%", systems_head)
    template = template.replace("%RULES%", rules_html)
    template = template.replace("%META%", meta_html)
    template = template.replace("%DATA%", data)
    return template


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>美股前100方向偏度总览</title>
<style>
  :root { color-scheme: light; --ink:#1c1915; --muted:#5c564c; --line:#e4ddd0; --paper:#f6f3ec; --card:#fffdf8;
          --long:#b3372b; --long-soft:#e8c9c4; --short:#1e7d46; --short-soft:#c5dfd0; --mix:#8a5a12; --neutral:#9aa3ad; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 "Segoe UI","Microsoft YaHei",sans-serif; color:var(--ink); background:var(--paper); }
  .wrap { max-width:1280px; margin:0 auto; padding:24px 18px 48px; }
  h1 { margin:0 0 6px; font-size:26px; }
  .lede { color:var(--muted); margin:0 0 14px; }
  .meta { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:6px 18px; margin:0 0 14px;
          padding:10px 14px; background:var(--card); border:1px solid var(--line); border-radius:10px; font-size:13px; }
  .meta dt { color:var(--muted); }
  .meta dd { margin:0; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
  h2 { font-size:18px; margin:22px 0 8px; }
  .dist { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; margin:0 0 10px; }
  .bucket { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
  .bucket b { display:block; font-size:22px; font-variant-numeric:tabular-nums; }
  .bucket span { color:var(--muted); font-size:13px; }
  .strip { display:flex; height:14px; border-radius:999px; overflow:hidden; border:1px solid var(--line); margin:0 0 4px; }
  .strip div { flex:0 0 auto; }
  .bar-controls { position:sticky; top:0; z-index:2; background:rgba(246,243,136,0); }
  .bar-inner { position:sticky; top:0; background:rgba(246,243,236,.96); padding:10px 0 12px; border-bottom:1px solid var(--line); }
  .controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  input, select, button { font:inherit; border:1px solid #d5cec0; background:var(--card); border-radius:999px; padding:7px 12px; }
  button { cursor:pointer; }
  button.on { background:#0c5c56; color:#f4fffc; border-color:#0c5c56; }
  .count { font-variant-numeric:tabular-nums; color:var(--muted); }
  table { width:100%; border-collapse:separate; border-spacing:0; background:var(--card); }
  th, td { padding:7px 8px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }
  thead th { position:sticky; top:var(--thead-top, 56px); background:var(--card); z-index:1; font-size:13px; color:var(--muted); box-shadow:0 1px 0 var(--line); }
  th[data-k] { cursor:pointer; }
  th.sys { font-size:11px; line-height:1.25; max-width:64px; }
  tr.item { cursor:pointer; }
  tr.item:hover { background:#f3f0e8; }
  td.num { font-variant-numeric:tabular-nums; }
  .cell { display:inline-block; min-width:34px; text-align:center; border-radius:6px; padding:2px 6px; font-size:12px; font-weight:700; }
  .cell.多 { background:var(--long); color:#fff; }
  .cell.空 { background:var(--short); color:#fff; }
  .cell.中, .cell.混 { background:#eee9de; color:var(--mix); }
  .cell.weak { opacity:.38; }
  .score { display:flex; align-items:center; gap:8px; }
  .score .rail { position:relative; flex:0 0 120px; height:10px; border-radius:999px; background:#eee9de; }
  .score .rail i { position:absolute; top:0; bottom:0; border-radius:999px; }
  .score b { font-variant-numeric:tabular-nums; min-width:26px; text-align:right; }
  .多 { color:var(--long); font-weight:700; }
  .空 { color:var(--short); font-weight:700; }
  .混, .平, .中 { color:var(--mix); font-weight:700; }
  .badge { display:inline-block; border-radius:999px; padding:1px 8px; font-size:11px; background:#eee9de; color:var(--muted); }
  .badge.hot { background:#f8efd8; color:#8a5a12; font-weight:700; }
  .detail td { white-space:normal; background:#f8f6ef; }
  .detail .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:6px 18px; }
  .detail dt { color:var(--muted); font-size:13px; }
  .detail dd { margin:0 0 6px; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
  canvas { width:100%; height:150px; background:#fff; border:1px solid var(--line); border-radius:8px; }
  .note { color:var(--muted); font-size:13px; }
  .note ul { margin:4px 0 0; padding-left:20px; }
  .warm { color:var(--mix); font-weight:700; }
</style>
</head>
<body>
<div class="wrap">
  <h1>QQQ+SPY 前100 · 全部参与标的方向偏度总览</h1>
  <dl class="meta">%META%</dl>
  <p class="lede" id="lede"></p>

  <h2>市场分布（八票偏度分档）</h2>
  <div class="strip" id="strip"></div>
  <p class="note" id="stripNote"></p>
  <div class="dist" id="dist"></div>

  <h2>逐票方向矩阵</h2>
  <div class="bar-inner">
    <div class="controls">
      <input id="q" placeholder="搜索代码（前缀）" aria-label="搜索代码">
      <select id="fund"><option value="">全部基金</option><option>QQQ</option><option>SPY</option><option value="both">两边都有</option></select>
      <select id="bucket"><option value="">全部档位</option><option>强多</option><option>偏多</option><option>中性</option><option>偏空</option><option>强空</option></select>
      <button type="button" id="adx" aria-pressed="false">只看 ADX≥25</button>
      <button type="button" id="vol" aria-pressed="false">只看放量</button>
      <button type="button" id="warm" aria-pressed="false">只看预热不足</button>
      <button type="button" id="reset">清空筛选</button>
      <span class="count" id="count"></span>
    </div>
  </div>
  <div class="tablewrap">
  <table>
    <thead><tr>
      <th data-k="symbol">代码</th><th data-k="funds">基金</th><th data-k="close">收盘</th>
      <th data-k="bias">EMA 判</th>
      %SYSTEMS_HEAD%
      <th data-k="_score">偏度分</th><th data-k="adx">ADX</th><th data-k="relative_volume">相对量</th><th>预热</th>
    </tr></thead>
    <tbody id="body"></tbody>
  </table>
  </div>

  <h2>口径说明（展示推导，非交易信号）</h2>
  <div class="note"><ul>%RULES%</ul></div>
  <h2>预热不足</h2>
  <ul id="incomplete" class="note"></ul>
  <h2>没有算出指标</h2>
  <ul id="fails" class="note"></ul>
</div>
<script>
const DATA = %DATA%;
const SYSTEMS = [["ema_10_20","EMA 10/20"],["ema_10_55","EMA 10/55"],["ema_20_55","EMA 20/55"],["sma","SMA 排列"],["macd","MACD"],["di","DMI ±DI"],["donchian_20","唐奇安 20 位置"],["donchian_55","唐奇安 55 位置"]];
const state = { fund:"", bucket:"", adx:false, vol:false, warm:false, q:"", sort:"_score", desc:true, open:"" };
document.getElementById("lede").textContent =
  DATA.universe + "。成功 " + DATA.rows.length + " 只，失败 " + DATA.failed.length +
  "，预热不足 " + (DATA.incomplete || []).length + "。截止 " + DATA.data_cutoff +
  "。八票偏度分把每只股票在各方向系统上的投票求和；颜色沿用本页口径：多＝红、空＝绿、中／混＝灰。仅指标展示，非投资建议。";

function vote(v){ return v === "多" ? 1 : v === "空" ? -1 : 0; }
function diVote(row){
  if (row.plus_di === null || row.plus_di === undefined || row.minus_di === null || row.minus_di === undefined) return 0;
  if (row.plus_di > row.minus_di) return 1;
  if (row.plus_di < row.minus_di) return -1;
  return 0;
}
function dcVote(row, period){
  const c = row.channel; if (!c) return 0;
  const hi = period === 20 ? c.high_20 : c.high_55;
  const lo = period === 20 ? c.low_20 : c.low_55;
  const close = row.close;
  if (close === null || close === undefined || hi === null || hi === undefined || lo === null || lo === undefined) return 0;
  if (close >= hi) return 1;
  if (close <= lo) return -1;
  return 0;
}
function votes(row){
  return [
    { name:"ema_10_20", raw:row.pairs["10_20"], v:vote(row.pairs["10_20"]) },
    { name:"ema_10_55", raw:row.pairs["10_55"], v:vote(row.pairs["10_55"]) },
    { name:"ema_20_55", raw:row.pairs["20_55"], v:vote(row.pairs["20_55"]) },
    { name:"sma", raw:row.sma_bias, v:vote(row.sma_bias) },
    { name:"macd", raw:row.macd, v:vote(row.macd) },
    { name:"di", raw:diVote(row) > 0 ? "多" : diVote(row) < 0 ? "空" : "中", v:diVote(row) },
    { name:"donchian_20", raw:dcVote(row,20) > 0 ? "多" : dcVote(row,20) < 0 ? "空" : "中", v:dcVote(row,20) },
    { name:"donchian_55", raw:dcVote(row,55) > 0 ? "多" : dcVote(row,55) < 0 ? "空" : "中", v:dcVote(row,55) },
  ];
}
function score(row){ return votes(row).reduce((sum, item) => sum + item.v, 0); }
function bucketOf(score){
  if (score >= 6) return "强多";
  if (score >= 1) return "偏多";
  if (score <= -6) return "强空";
  if (score <= -1) return "偏空";
  return "中性";
}
const BUCKETS = ["强多","偏多","中性","偏空","强空"];
const BUCKET_COLORS = { "强多":"#8d2b1f", "偏多":"#d9918a", "中性":"#b9b2a4", "偏空":"#7fb394", "强空":"#1e7d46" };
function enriched(){
  return DATA.rows.map(row => { const rowVotes = votes(row);
    return Object.assign({}, row, { _votes: rowVotes, _score: rowVotes.reduce((s, item) => s + item.v, 0), _bucket: bucketOf(rowVotes.reduce((s, item) => s + item.v, 0)) });
  });
}
const ROWS = enriched();

function drawDistribution(){
  const counts = {}; BUCKETS.forEach(b => counts[b] = 0);
  ROWS.forEach(row => counts[row._bucket] += 1);
  const total = ROWS.length || 1;
  document.getElementById("strip").innerHTML = BUCKETS.map(b =>
    '<div style="width:' + (counts[b] * 100 / total) + '%;background:' + BUCKET_COLORS[b] + '"></div>').join("");
  document.getElementById("stripNote").textContent =
    "每格宽度 = 该档只数占比（共 " + ROWS.length + " 只）。档位：强多 ≥ +6，偏多 +1…+5，中性 0，偏空 −5…−1，强空 ≤ −6。";
  document.getElementById("dist").innerHTML = BUCKETS.map(b =>
    '<div class="bucket"><b style="color:' + BUCKET_COLORS[b] + '">' + counts[b] + '</b><span>' + b + '</span></div>').join("");
}

function show(row){
  if (state.q && row.symbol.indexOf(state.q) !== 0) return false;
  if (state.bucket && row._bucket !== state.bucket) return false;
  if (state.fund === "both" && row.funds.indexOf(",") < 0) return false;
  if (state.fund && state.fund !== "both" && row.funds.indexOf(state.fund) < 0) return false;
  if (state.adx && !(row.adx >= 25)) return false;
  if (state.vol && !(row.relative_volume >= 1.5)) return false;
  if (state.warm && row.warmup_ok) return false;
  return true;
}
function val(row, key){
  const v = row[key];
  return (v === null || v === undefined || v === "") ? (state.desc ? -Infinity : Infinity) : v;
}
function cell(rowVotes, name, adxWeak){
  const item = rowVotes.find(v => v.name === name);
  const weak = adxWeak && (name === "di" || name === "donchian_20" || name === "donchian_55");
  return '<span class="cell ' + item.raw + (weak ? " weak" : "") + '" title="' + name + '">' + item.raw + '</span>';
}
function scoreBar(score){
  const rail = document.createElement("div");
  const width = Math.abs(score) * 100 / 8;
  const side = score >= 0 ? "left:50%" : "right:50%";
  const color = score > 0 ? "#b3372b" : score < 0 ? "#1e7d46" : "#b9b2a4";
  return '<span class="score"><span class="rail"><i style="' + side + ';width:' + (width / 2) + '%;background:' + color + '"></i></span><b>' + (score > 0 ? "+" : "") + score + '</b></span>';
}
function draw(canvas, spark){
  const ctx = canvas.getContext("2d");
  const w = canvas.width = canvas.clientWidth * 2, h = canvas.height = 300;
  ctx.clearRect(0,0,w,h);
  const series = [spark.close, spark.ema10, spark.ema20, spark.ema55];
  const nums = series.flat().filter(v => v !== null);
  if (!nums.length) return;
  const min = Math.min.apply(null, nums), max = Math.max.apply(null, nums);
  const colors = ["#1c1915", "#0c5c56", "#8a5a12", "#8d2b1f"];
  series.forEach((line, index) => {
    ctx.beginPath(); ctx.strokeStyle = colors[index]; ctx.lineWidth = index ? 2 : 3;
    let started = false;
    line.forEach((v, i) => {
      if (v === null) { started = false; return; }
      const x = (i / Math.max(line.length - 1, 1)) * (w - 16) + 8;
      const y = h - 12 - ((v - min) / Math.max(max - min, 1e-9)) * (h - 24);
      if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
    });
    ctx.stroke();
  });
}
function detail(row){
  const position55 = (row.channel && row.close !== null && row.channel.high_55 !== undefined && row.channel.high_55 !== row.channel.low_55)
    ? Math.round((row.close - row.channel.low_55) / (row.channel.high_55 - row.channel.low_55) * 100) : null;
  const voteRows = row._votes.map(item => {
    return "<div><dt>" + (SYSTEMS.find(s => s[0] === item.name) || ["", item.name])[1] + '</dt><dd class="' + item.raw + '">' + item.raw + "（票 " + (item.v > 0 ? "+1" : item.v < 0 ? "−1" : "0") + "）</dd></div>";
  }).join("");
  return '<tr class="detail"><td colspan="15"><dl class="grid">' + voteRows +
    '<div><dt>原始 / 复权收盘</dt><dd>' + row.close + " / " + row.adjusted_close + "（因子 " + row.adjustment_factor + "）</dd></div>" +
    '<div><dt>ADX · +DI / −DI</dt><dd>' + row.adx + " · " + row.plus_di + " / " + row.minus_di + "</dd></div>" +
    '<div><dt>MACD dif / dea / 柱</dt><dd>' + row.dif + " / " + row.dea + " / " + row.histogram + "</dd></div>" +
    '<div><dt>唐奇安 55 通道内位置</dt><dd>' + (position55 === null ? "—" : position55 + "%") + "（低 " + (row.channel ? row.channel.low_55 : "—") + " / 高 " + (row.channel ? row.channel.high_55 : "—") + "）</dd></div>" +
    '<div><dt>成交量 / 相对量</dt><dd>' + row.volume + "（" + row.relative_volume + "× 前20日均量）</dd></div>" +
    '<div><dt>预热</dt><dd>' + (row.warmup_ok ? "足够" : "预热不足") + "，" + row.bar_count + " 根</dd></div>" +
    '</dl><canvas id="c-' + row.symbol.replace(".", "_") + '" aria-label="收盘与EMA"></canvas></td></tr>';
}
function render(){
  const rows = ROWS.filter(show).sort((a, b) => {
    const av = val(a, state.sort), bv = val(b, state.sort);
    if (av < bv) return state.desc ? 1 : -1;
    if (av > bv) return state.desc ? -1 : 1;
    return a.symbol < b.symbol ? -1 : 1;
  });
  document.getElementById("count").textContent = rows.length + " / " + ROWS.length;
  document.getElementById("body").innerHTML = rows.map(row => {
    const open = state.open === row.symbol;
    const adxWeak = !(row.adx >= 20);
    const main = '<tr class="item" data-s="' + row.symbol + '">' +
      '<td><b>' + row.symbol + '</b></td><td>' + row.funds + '</td>' +
      '<td class="num">' + (row.close ?? "—") + '</td>' +
      '<td class="' + row.bias + '">' + row.bias + '</td>' +
      SYSTEMS.map(s => "<td>" + cell(row._votes, s[0], adxWeak) + "</td>").join("") +
      '<td>' + scoreBar(row._score) + '</td>' +
      '<td class="num">' + (row.adx ?? "—") + (row.adx >= 25 ? ' <span class="badge hot">强趋势</span>' : "") + '</td>' +
      '<td class="num">' + (row.relative_volume ?? "—") + (row.relative_volume >= 1.5 ? ' <span class="badge hot">放量</span>' : "") + '</td>' +
      '<td>' + (row.warmup_ok ? '<span class="badge">足够</span>' : '<span class="warm">预热不足</span>') + '</td></tr>';
    return main + (open ? detail(row) : "");
  }).join("");
  rows.forEach(row => {
    if (state.open !== row.symbol) return;
    const canvas = document.getElementById("c-" + row.symbol.replace(".", "_"));
    if (canvas) draw(canvas, row.spark);
  });
  document.querySelectorAll("tr.item").forEach(tr => tr.addEventListener("click", () => {
    state.open = state.open === tr.dataset.s ? "" : tr.dataset.s;
    render();
  }));
}
document.getElementById("q").addEventListener("input", e => { state.q = e.target.value.trim().toUpperCase(); render(); });
document.getElementById("fund").addEventListener("change", e => { state.fund = e.target.value; render(); });
document.getElementById("bucket").addEventListener("change", e => { state.bucket = e.target.value; render(); });
document.getElementById("adx").addEventListener("click", e => { state.adx = !state.adx; e.target.classList.toggle("on", state.adx); e.target.setAttribute("aria-pressed", String(state.adx)); render(); });
document.getElementById("vol").addEventListener("click", e => { state.vol = !state.vol; e.target.classList.toggle("on", state.vol); e.target.setAttribute("aria-pressed", String(state.vol)); render(); });
document.getElementById("warm").addEventListener("click", e => { state.warm = !state.warm; e.target.classList.toggle("on", state.warm); e.target.setAttribute("aria-pressed", String(state.warm)); render(); });
document.getElementById("reset").addEventListener("click", () => {
  Object.assign(state, { fund:"", bucket:"", adx:false, vol:false, warm:false, q:"", open:"" });
  document.getElementById("q").value = "";
  document.getElementById("fund").value = "";
  document.getElementById("bucket").value = "";
  ["adx","vol","warm"].forEach(id => { const el = document.getElementById(id); el.classList.remove("on"); el.setAttribute("aria-pressed","false"); });
  render();
});
document.querySelectorAll("th[data-k]").forEach(th => th.addEventListener("click", () => {
  if (state.sort === th.dataset.k) state.desc = !state.desc; else { state.sort = th.dataset.k; state.desc = false; }
  render();
}));
document.getElementById("fails").innerHTML = DATA.failed.map(item => "<li>" + item.symbol + "（" + item.funds + "）：" + item.error + "</li>").join("") || "<li>没有</li>";
document.getElementById("incomplete").innerHTML = (DATA.incomplete || []).map(item => "<li>" + item.symbol + "：" + item.bar_count + " 根，" + item.reason + "，预热不足</li>").join("") || "<li>没有</li>";
function syncTheadTop(){
  const bar = document.querySelector(".bar-inner");
  if (bar) document.documentElement.style.setProperty("--thead-top", bar.offsetHeight + 1 + "px");
}
window.addEventListener("resize", syncTheadTop);
drawDistribution();
syncTheadTop();
render();
</script>
</body>
</html>
"""
