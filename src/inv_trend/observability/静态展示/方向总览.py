"""Render the all-symbols direction-skew overview board; presentation only.

Consumes the same payload as :mod:`快照看板` (the US top-100 snapshot JSON) and
derives every direction vote in the browser from stored indicator fields, so the
Python side stays free of any second computation of signals.
"""

from __future__ import annotations

from .公共 import BASE_META_KEYS, render_document, render_meta

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
    "格子上的 * 表示 ADX &lt; 20：DMI 与唐奇安仍按原票计分，只是标出趋势偏弱。",
)


def render_direction_overview(payload: dict, meta: dict | None = None) -> str:
    """Assemble the page from the shared base plus this page's own vote matrix."""

    systems_head = "".join(
        f'<th class="sys" data-sys="{name}">{label}</th>' for name, label in _SYSTEMS
    )
    rules_html = "".join(f"<li>{rule}</li>" for rule in _RULES)
    return render_document(
        _TEMPLATE,
        data=payload,
        placeholders={
            "%SYSTEMS_HEAD%": systems_head,
            "%RULES%": rules_html,
            "%META%": render_meta(meta, BASE_META_KEYS),
        },
    )


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>美股前100方向偏度总览</title>
<style>
  .dist { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:8px; margin:0 0 10px; }
  .bucket { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:10px 12px; }
  .bucket b { display:block; font-size:22px; font-variant-numeric:tabular-nums; }
  .bucket span { color:var(--muted); font-size:13px; }
  .strip { display:flex; height:14px; border-radius:999px; overflow:hidden; border:1px solid var(--line); margin:0 0 4px; }
  .strip div { flex:0 0 auto; }
  table { width:100%; border-collapse:separate; border-spacing:0; background:var(--card); }
  th, td { padding:7px 8px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }
  thead th { position:sticky; top:0; background:var(--card); z-index:1; font-size:13px; color:var(--muted); box-shadow:0 1px 0 var(--line); }
  th[data-k] { cursor:pointer; }
  th.sys { font-size:11px; line-height:1.25; max-width:64px; }
  tr.item { cursor:pointer; }
  tr.item:hover { background:#f3f0e8; }
  td.num { font-variant-numeric:tabular-nums; }
  .cell { display:inline-block; min-width:34px; text-align:center; border-radius:6px; padding:2px 6px; font-size:12px; font-weight:700; }
  .cell.多 { background:var(--long); color:#fff; }
  .cell.空 { background:var(--short); color:#fff; }
  .cell.中, .cell.混 { background:#eee9de; color:var(--mix); }
  .cell.weak::after { content:"*"; margin-left:1px; }
  .score { display:flex; align-items:center; gap:8px; }
  .score .rail { position:relative; flex:0 0 120px; height:10px; border-radius:999px; background:#eee9de; }
  .score .rail i { position:absolute; top:0; bottom:0; border-radius:999px; }
  .score b { font-variant-numeric:tabular-nums; min-width:26px; text-align:right; }
  .badge.hot { background:var(--hot); color:var(--mix); font-weight:700; }
  .detail td { white-space:normal; background:#f8f6ef; }
  .detail .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(230px,1fr)); gap:6px 18px; }
  .detail dt { color:var(--muted); font-size:13px; }
  .detail dd { margin:0 0 6px; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
  canvas { height:150px; }
  .note ul { margin:4px 0 0; padding-left:20px; }
  .warm { color:var(--mix); font-weight:700; }
  .legend { display:flex; gap:14px; flex-wrap:wrap; align-items:center; margin:0 0 10px; font-size:12px; color:var(--muted); }

  .dist { grid-template-columns:repeat(auto-fit,minmax(min(100%, 140px),1fr)); }
</style>
</head>
<body>
<div class="wrap">
  <h1>QQQ+SPY 前100 · 全部参与标的方向偏度总览</h1>
  <dl class="meta">%META%</dl>
  <p class="lede" id="lede"></p>
  <p class="legend"><span><i class="swatch long"></i>多</span><span><i class="swatch short"></i>空</span><span><i class="swatch mix"></i>中 / 混</span><span>* 趋势偏弱，仍计票</span></p>

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
    <tbody id="body"><tr class="empty-row"><td colspan="16"><div class="empty" role="status">正在整理矩阵…</div></td></tr></tbody>
  </table>
  </div>

  <h2>口径说明（展示推导，非交易信号）</h2>
  <div class="note"><ul>%RULES%</ul></div>
  <h2>预热不足</h2>
  <ul id="incomplete" class="note"></ul>
  <h2>没有算出指标</h2>
  <ul id="fails" class="note"></ul>
</div>
<noscript><p class="empty" role="status">需要启用 JavaScript 才能筛选、排序和展开明细。</p></noscript>
<script>
const DATA = %DATA%;
const SYSTEMS = [["ema_10_20","EMA 10/20"],["ema_10_55","EMA 10/55"],["ema_20_55","EMA 20/55"],["sma","SMA 排列"],["macd","MACD"],["di","DMI ±DI"],["donchian_20","唐奇安 20 位置"],["donchian_55","唐奇安 55 位置"]];
const state = { fund:"", bucket:"", adx:false, vol:false, warm:false, q:"", sort:"_score", desc:true, open:"" };
const PREF_KEY = "direction";
Object.assign(state, loadPrefs(PREF_KEY));
function persistFilters(){
  savePrefs(PREF_KEY, { fund:state.fund, bucket:state.bucket, adx:state.adx, vol:state.vol, warm:state.warm, q:state.q, sort:state.sort, desc:state.desc });
}
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
const BUCKET_COLORS = { "强多":C.longDeep, "偏多":C.longPale, "中性":C.neutral, "偏空":C.shortMid, "强空":C.short };
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
    '<div title="' + b + ' ' + counts[b] + '只" style="width:' + (counts[b] * 100 / total) + '%;background:' + BUCKET_COLORS[b] + '"></div>').join("");
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
  const label = (SYSTEMS.find(s => s[0] === name) || ["", name])[1];
  const note = weak ? "，趋势偏弱仍计票" : "";
  return '<span class="cell ' + item.raw + (weak ? " weak" : "") + '" title="' + label + " " + item.raw + note + '">' + item.raw + '</span>';
}
function scoreBar(score){
  const width = Math.abs(score) * 100 / 8;
  const side = score >= 0 ? "left:50%" : "right:50%";
  const color = score > 0 ? C.long : score < 0 ? C.short : C.neutral;
  return '<span class="score"><span class="rail"><i style="' + side + ';width:' + (width / 2) + '%;background:' + color + '"></i></span><b>' + (score > 0 ? "+" : "") + score + '</b></span>';
}
function detail(row){
  const position55 = (row.channel && row.close !== null && row.channel.high_55 !== undefined && row.channel.high_55 !== row.channel.low_55)
    ? Math.round((row.close - row.channel.low_55) / (row.channel.high_55 - row.channel.low_55) * 100) : null;
  const voteRows = row._votes.map(item => {
    return "<div><dt>" + (SYSTEMS.find(s => s[0] === item.name) || ["", item.name])[1] + '</dt><dd class="' + item.raw + '">' + item.raw + "（票 " + (item.v > 0 ? "+1" : item.v < 0 ? "−1" : "0") + "）</dd></div>";
  }).join("");
  return '<tr class="detail"><td colspan="16"><dl class="grid">' + voteRows +
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
  setAriaSort(document.querySelectorAll("th[data-k]"), state.sort, state.desc);
  persistFilters();
  document.getElementById("body").innerHTML = rows.length ? rows.map(row => {
    const open = state.open === row.symbol;
    const adxWeak = !(row.adx >= 20);
    const main = '<tr class="item" data-s="' + row.symbol + '" tabindex="0" role="button" aria-expanded="' + open + '">' +
      '<td><b>' + row.symbol + '</b></td><td>' + row.funds + '</td>' +
      '<td class="num">' + (row.close ?? "—") + '</td>' +
      '<td class="' + row.bias + '">' + row.bias + '</td>' +
      SYSTEMS.map(s => "<td>" + cell(row._votes, s[0], adxWeak) + "</td>").join("") +
      '<td>' + scoreBar(row._score) + '</td>' +
      '<td class="num">' + (row.adx ?? "—") + (row.adx >= 25 ? ' <span class="badge hot">强趋势</span>' : "") + '</td>' +
      '<td class="num">' + (row.relative_volume ?? "—") + (row.relative_volume >= 1.5 ? ' <span class="badge hot">放量</span>' : "") + '</td>' +
      '<td>' + (row.warmup_ok ? '<span class="badge">足够</span>' : '<span class="warm">预热不足</span>') + '</td></tr>';
    return main + (open ? detail(row) : "");
  }).join("") : '<tr class="empty-row"><td colspan="16"><div class="empty" role="status">没有符合筛选的标的</div></td></tr>';
  rows.forEach(row => {
    if (state.open !== row.symbol) return;
    const canvas = document.getElementById("c-" + row.symbol.replace(".", "_"));
    if (canvas && row.spark) {
      drawSpark(canvas, row.spark, 300);
      bindChartTip(canvas, ["收盘", "EMA10", "EMA20", "EMA55"], [row.spark.close, row.spark.ema10, row.spark.ema20, row.spark.ema55]);
    }
  });
  bindActivate(document, "tr.item", tr => {
    state.open = state.open === tr.dataset.s ? "" : tr.dataset.s;
    render();
  });
}
document.getElementById("q").addEventListener("input", e => { state.q = e.target.value.trim().toUpperCase(); render(); });
document.getElementById("fund").addEventListener("change", e => { state.fund = e.target.value; render(); });
document.getElementById("bucket").addEventListener("change", e => { state.bucket = e.target.value; render(); });
document.getElementById("adx").addEventListener("click", e => { state.adx = !state.adx; togglePressed(e.target, state.adx); render(); });
document.getElementById("vol").addEventListener("click", e => { state.vol = !state.vol; togglePressed(e.target, state.vol); render(); });
document.getElementById("warm").addEventListener("click", e => { state.warm = !state.warm; togglePressed(e.target, state.warm); render(); });
document.getElementById("reset").addEventListener("click", () => {
  Object.assign(state, { fund:"", bucket:"", adx:false, vol:false, warm:false, q:"", open:"" });
  document.getElementById("q").value = "";
  document.getElementById("fund").value = "";
  document.getElementById("bucket").value = "";
  ["adx","vol","warm"].forEach(id => togglePressed(document.getElementById(id), false));
  render();
});
bindActivate(document, "th[data-k]", th => {
  if (state.sort === th.dataset.k) state.desc = !state.desc; else { state.sort = th.dataset.k; state.desc = false; }
  render();
});
renderList(document.getElementById("fails"), DATA.failed, item => "<li>" + item.symbol + "（" + item.funds + "）：" + item.error + "</li>");
renderList(document.getElementById("incomplete"), DATA.incomplete || [], item => "<li>" + item.symbol + "：" + item.bar_count + " 根，" + item.reason + "，预热不足</li>");
drawDistribution();
function syncControls(){
  document.getElementById("q").value = state.q;
  document.getElementById("fund").value = state.fund;
  document.getElementById("bucket").value = state.bucket;
  ["adx","vol","warm"].forEach(id => togglePressed(document.getElementById(id), state[id]));
}
syncControls();
installBackToTop();
render();
onResize(() => {
  const row = ROWS.find(item => item.symbol === state.open);
  const canvas = row && document.getElementById("c-" + row.symbol.replace(".", "_"));
  if (!canvas || !row.spark) return;
  drawSpark(canvas, row.spark, 300);
});
</script>
</body>
</html>
"""
