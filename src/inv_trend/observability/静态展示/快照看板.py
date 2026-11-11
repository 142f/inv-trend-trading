"""Render the fixed US top-100 snapshot board; presentation only."""

from __future__ import annotations

from .公共 import BASE_META_KEYS, render_document, render_meta


def render_snapshot_page(payload: dict, meta: dict | None = None) -> str:
    """Assemble the snapshot from the shared base plus its own indicator table."""

    banner = (
        '<p class="banner show" id="banner">盘中快照</p>' if payload.get("partial") else ""
    )
    return render_document(
        _TEMPLATE,
        data=payload,
        placeholders={"%META%": render_meta(meta, BASE_META_KEYS), "%BANNER%": banner},
    )


_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>美股前100指标看板</title>
<style>
  .banner { display:none; margin:0 0 10px; padding:6px 12px; border-radius:var(--r-pill); background:var(--hot); color:var(--mix); font-weight:700; }
  .banner.show { display:inline-block; }
  .bar { position:sticky; top:0; z-index:3; background:rgba(246,243,236,.96); padding:10px 0 12px; border-bottom:1px solid var(--line); }
  table { width:100%; border-collapse:separate; border-spacing:0; background:var(--card); }
  th, td { padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }
  th[data-k] { cursor:pointer; font-size:13px; color:var(--muted); }
  tr.item { cursor:pointer; }
  tr.item:hover { background:#f3f0e8; }
  .detail td { white-space:normal; background:#f8f6ef; }
  .legend { display:flex; gap:14px; flex-wrap:wrap; align-items:center; margin:0 0 10px; font-size:12px; color:var(--muted); }
  .legend i { display:inline-block; width:14px; height:4px; border-radius:2px; margin-right:4px; vertical-align:middle; }
</style>
</head>
<body>
<div class="wrap">
  <h1>QQQ+SPY 前100成分 / 163 只</h1>
  %BANNER%
  <dl class="meta">%META%</dl>
  <p class="lede" id="lede"></p>
  <p class="legend"><span><i class="swatch long"></i>多</span><span><i class="swatch short"></i>空</span><span><i class="swatch mix"></i>混 / 平</span><span>色块只作辅助，状态以文字为准</span></p>
  <div class="bar">
    <div class="controls">
      <input id="q" placeholder="搜索代码（前缀）" aria-label="搜索代码">
      <select id="fund" aria-label="基金"><option value="">全部基金</option><option>QQQ</option><option>SPY</option><option value="both">两边都有</option></select>
      <select id="bias" aria-label="方向"><option value="">全部方向</option><option value="多">三对 EMA 多头</option><option value="空">三对 EMA 空头</option><option value="混">方向混合</option></select>
      <button type="button" id="adx" aria-pressed="false">只看 ADX≥25</button>
      <button type="button" id="vol" aria-pressed="false">只看放量</button>
      <button type="button" id="reset">清空筛选</button>
      <span class="count" id="count"></span>
    </div>
  </div>
  <div class="tablewrap" tabindex="0" aria-label="指标表，列较多时可横向或纵向滚动">
  <table>
    <thead><tr>
      <th data-k="symbol">代码</th><th data-k="funds">基金</th><th class="num" data-k="qqq_rank">QQQ</th><th class="num" data-k="spy_rank">SPY</th>
      <th class="num" data-k="close">原始收盘</th><th class="num" data-k="adjusted_close">复权收盘</th><th data-k="bias">EMA</th><th data-k="sma_bias">SMA</th><th data-k="macd">MACD</th>
      <th class="num" data-k="adx">ADX</th><th class="num" data-k="relative_volume">相对前20日均量</th><th data-k="as_of">日期</th>
    </tr></thead>
    <tbody id="body"><tr class="empty-row"><td colspan="12"><div class="empty" role="status">正在整理表格…</div></td></tr></tbody>
  </table>
  </div>
  <h2>指标不完整</h2>
  <ul id="incomplete" class="note"></ul>
  <h2>没有算出指标</h2>
  <ul id="fails" class="note"></ul>
</div>
<noscript><p class="empty" role="status">需要启用 JavaScript 才能筛选、排序和展开明细。</p></noscript>
<script>
const DATA = %DATA%;
const state = { fund:"", bias:"", adx:false, vol:false, q:"", sort:"symbol", desc:false, open:"" };
document.getElementById("lede").textContent = DATA.universe + "。Yahoo research-only。price_basis=" + DATA.price_basis + "。截止 " + DATA.data_cutoff + "。成功 " + DATA.rows.length + "，失败 " + DATA.failed.length + "，预热不足 " + (DATA.incomplete || []).length + "。三对 EMA 都在快线之上为多，都在之下为空。仅指标结果，非投资建议。";
function warmText(row, key) {
  if (!row.warmup_ok && (key === "144" || key === "169")) return "预热不足";
  const value = row.ema[key];
  return value === null || value === undefined ? "—" : value;
}
function show(row) {
  if (state.q && row.symbol.indexOf(state.q) !== 0) return false;
  if (state.fund === "both" && row.funds.indexOf(",") < 0) return false;
  if (state.fund && state.fund !== "both" && row.funds.indexOf(state.fund) < 0) return false;
  if (state.bias && row.bias !== state.bias) return false;
  if (state.adx && !(row.adx >= 25)) return false;
  if (state.vol && !(row.relative_volume >= 1.5)) return false;
  return true;
}
function val(row, key) {
  const value = row[key];
  return value === null || value === undefined || value === "" ? (state.desc ? -Infinity : Infinity) : value;
}
function paint(row) {
  const canvas = document.getElementById("c-" + row.symbol.replace(".", "_"));
  if (!canvas || !row.spark) return;
  drawSpark(canvas, row.spark, 320);
  bindChartTip(canvas, ["收盘", "EMA10", "EMA20", "EMA55"], [row.spark.close, row.spark.ema10, row.spark.ema20, row.spark.ema55]);
}
function render() {
  const rows = DATA.rows.filter(show).sort((a, b) => {
    const av = val(a, state.sort), bv = val(b, state.sort);
    if (av < bv) return state.desc ? 1 : -1;
    if (av > bv) return state.desc ? -1 : 1;
    return a.symbol < b.symbol ? -1 : 1;
  });
  document.getElementById("count").textContent = rows.length + " / " + DATA.rows.length;
  setAriaSort(document.querySelectorAll("th[data-k]"), state.sort, state.desc);
  document.getElementById("body").innerHTML = rows.length ? rows.map(row => {
    const open = state.open === row.symbol;
    const main = '<tr class="item" data-s="' + row.symbol + '" tabindex="0" role="button" aria-expanded="' + open + '"><td>' + row.symbol + '</td><td>' + row.funds + '</td><td class="num">' + (row.qqq_rank || "—") + '</td><td class="num">' + (row.spy_rank || "—") + '</td><td class="num">' + (row.close ?? "—") + '</td><td class="num">' + (row.adjusted_close ?? "—") + '</td><td class="' + row.bias + '">' + (row.bias || "—") + " " + row.pairs["10_20"] + "/" + row.pairs["10_55"] + "/" + row.pairs["20_55"] + '</td><td class="' + row.sma_bias + '">' + (row.sma_bias || "—") + '</td><td class="' + row.macd + '">' + (row.macd || "—") + '</td><td class="num">' + (row.adx ?? "—") + '</td><td class="num">' + (row.relative_volume ?? "—") + '</td><td>' + row.as_of + '</td></tr>';
    const smaText = [5, 10, 20, 55, 120].map(period => {
      const value = row.sma[String(period)];
      if (period === 120 && !row.warmup_ok) return "预热不足";
      return value === null || value === undefined ? "—" : value;
    }).join(" / ");
    const legend = '<p class="legend"><span><i style="background:#1c1915"></i>收盘</span><span><i style="background:#0c5c56"></i>EMA10</span><span><i style="background:#8a5a12"></i>EMA20</span><span><i style="background:#8d2b1f"></i>EMA55</span></p>';
    const extra = open ? '<tr class="detail"><td colspan="12"><dl class="meta">'
      + '<div><dt>' + row.symbol + ' 原始 / 复权收盘</dt><dd>' + (row.close ?? "—") + ' / ' + (row.adjusted_close ?? "—") + '（因子 ' + (row.adjustment_factor ?? "—") + '）</dd></div>'
      + '<div><dt>SMA 5/10/20/55/120</dt><dd>' + smaText + '</dd></div>'
      + '<div><dt>EMA 10/20/55</dt><dd>' + warmText(row, "10") + ' / ' + warmText(row, "20") + ' / ' + warmText(row, "55") + '</dd></div>'
      + '<div><dt>EMA 144/169</dt><dd>' + warmText(row, "144") + ' / ' + warmText(row, "169") + '</dd></div>'
      + '<div><dt>ADX · +DI / −DI</dt><dd>' + (row.adx ?? "—") + ' · ' + (row.plus_di ?? "—") + ' / ' + (row.minus_di ?? "—") + '</dd></div>'
      + '<div><dt>ATR · MACD 柱</dt><dd>' + (row.atr ?? "—") + ' · ' + (row.histogram ?? "—") + '</dd></div>'
      + '<div><dt>通道 20 日 高 / 低</dt><dd>' + (row.channel?.high_20 ?? "—") + ' / ' + (row.channel?.low_20 ?? "—") + '</dd></div>'
      + '<div><dt>通道 55 日 高 / 低</dt><dd>' + (row.channel?.high_55 ?? "—") + ' / ' + (row.channel?.low_55 ?? "—") + '</dd></div>'
      + '<div><dt>成交量 / 前20日均量</dt><dd>' + (row.volume ?? "—") + ' / ' + (row.volume_sma_20 ?? "—") + '（相对 ' + (row.relative_volume ?? "—") + '）</dd></div>'
      + '<div><dt>预热</dt><dd>' + (row.warmup_ok ? "足够" : "预热不足") + '，' + row.bar_count + ' 根</dd></div>'
      + '</dl>' + legend + '<canvas id="c-' + row.symbol.replace(".", "_") + '" aria-label="收盘与EMA"></canvas></td></tr>' : "";
        return main + extra;
  }).join("") : '<tr class="empty-row"><td colspan="12"><div class="empty" role="status">没有符合筛选的标的</div></td></tr>';
  rows.forEach(row => { if (state.open === row.symbol) paint(row); });
  bindActivate(document.getElementById("body"), "tr.item", tr => {
    state.open = state.open === tr.dataset.s ? "" : tr.dataset.s;
    render();
  });
}
document.getElementById("q").addEventListener("input", e => { state.q = e.target.value.trim().toUpperCase(); render(); });
document.getElementById("fund").addEventListener("change", e => { state.fund = e.target.value; render(); });
document.getElementById("bias").addEventListener("change", e => { state.bias = e.target.value; render(); });
document.getElementById("adx").addEventListener("click", e => { state.adx = !state.adx; togglePressed(e.target, state.adx); render(); });
document.getElementById("vol").addEventListener("click", e => { state.vol = !state.vol; togglePressed(e.target, state.vol); render(); });
document.getElementById("reset").addEventListener("click", () => {
  Object.assign(state, { fund:"", bias:"", adx:false, vol:false, q:"", open:"" });
  document.getElementById("q").value = "";
  document.getElementById("fund").value = "";
  document.getElementById("bias").value = "";
  togglePressed(document.getElementById("adx"), false);
  togglePressed(document.getElementById("vol"), false);
  render();
});
bindActivate(document, "th[data-k]", th => {
  if (state.sort === th.dataset.k) state.desc = !state.desc; else { state.sort = th.dataset.k; state.desc = false; }
  render();
});
renderList(document.getElementById("fails"), DATA.failed, item => "<li>" + item.symbol + "（" + item.funds + "）：" + item.error + "</li>");
renderList(document.getElementById("incomplete"), DATA.incomplete || [], item => "<li>" + item.symbol + "：" + item.bar_count + " 根，" + item.reason + "，预热不足</li>");
onResize(() => { const row = DATA.rows.find(item => item.symbol === state.open); if (row) paint(row); });
render();
</script>
</body>
</html>
"""
