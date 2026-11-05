"""Render the fixed US top-100 snapshot board; presentation only."""

from __future__ import annotations

import json
from html import escape


def render_snapshot_page(payload: dict, meta: dict | None = None) -> str:
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    banner = '<p class="banner show" id="banner">盘中快照</p>' if payload.get("partial") else ""
    header = meta or {}
    meta_html = "".join(
        f"<div><dt>{escape(label)}</dt><dd>{escape(str(header.get(key, '—')))}</dd></div>"
        for key, label in (
            ("provider", "数据来源"),
            ("data_cutoff", "数据截止"),
            ("price_basis", "价格口径"),
            ("config_hash", "配置哈希"),
            ("content_hash", "内容哈希"),
            ("script_git_commit", "脚本提交"),
        )
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>美股前100指标看板</title>
<style>
  :root {{ color-scheme: light; --ink:#1c1915; --muted:#5c564c; --line:#e4ddd0; --paper:#f6f3ec; --card:#fffdf8; --long:#1f6b45; --short:#8d2b1f; --mix:#8a5a12; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font:15px/1.5 "Segoe UI","Microsoft YaHei",sans-serif; color:var(--ink); background:var(--paper); }}
  .wrap {{ max-width:1180px; margin:0 auto; padding:24px 18px 48px; }}
  h1 {{ margin:0 0 6px; font-size:28px; }}
  .lede {{ color:var(--muted); margin:0 0 14px; }}
  .bar {{ position:sticky; top:0; background:rgba(246,243,236,.95); padding:10px 0 12px; border-bottom:1px solid var(--line); }}
  .controls {{ display:flex; flex-wrap:wrap; gap:8px; align-items:center; }}
  input, select, button {{ font:inherit; border:1px solid #d5cec0; background:var(--card); border-radius:999px; padding:7px 12px; }}
  button {{ cursor:pointer; }}
  button.on {{ background:#0c5c56; color:#f4fffc; border-color:#0c5c56; }}
  table {{ width:100%; border-collapse:collapse; background:var(--card); }}
  th, td {{ padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; white-space:nowrap; }}
  th {{ cursor:pointer; font-size:13px; color:var(--muted); }}
  tr.item {{ cursor:pointer; }}
  tr.item:hover {{ background:#f3f7f6; }}
  .多 {{ color:var(--long); font-weight:700; }}
  .空 {{ color:var(--short); font-weight:700; }}
  .混, .平 {{ color:var(--mix); font-weight:700; }}
  .detail td {{ white-space:normal; background:#f8faf8; }}
  canvas {{ width:100%; height:160px; background:#fff; border:1px solid var(--line); border-radius:8px; }}
  .count {{ font-variant-numeric:tabular-nums; }}
  .banner {{ display:none; margin:0 0 10px; padding:6px 12px; border-radius:999px; background:#f8efd8; color:#8a5a12; font-weight:700; }}
  .banner.show {{ display:inline-block; }}
  .meta {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:6px 18px; margin:0 0 14px; padding:10px 14px; background:var(--card); border:1px solid var(--line); border-radius:10px; font-size:13px; }}
  .meta dt {{ color:var(--muted); }}
  .meta dd {{ margin:0; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }}
  .wrap {{ overflow-x:auto; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>QQQ+SPY 前100成分 / 163 只</h1>
  {banner}
  <dl class="meta">{meta_html}</dl>
  <p class="lede" id="lede"></p>
  <div class="bar">
    <div class="controls">
      <input id="q" placeholder="搜索代码（前缀）" aria-label="搜索代码">
      <select id="fund"><option value="">全部基金</option><option>QQQ</option><option>SPY</option><option value="both">两边都有</option></select>
      <select id="bias"><option value="">全部方向</option><option value="多">三对 EMA 多头</option><option value="空">三对 EMA 空头</option><option value="混">方向混合</option></select>
      <button type="button" id="adx" aria-pressed="false">只看 ADX≥25</button>
      <button type="button" id="vol" aria-pressed="false">只看放量</button>
      <button type="button" id="reset">清空筛选</button>
      <span class="count" id="count"></span>
    </div>
  </div>
  <table>
    <thead><tr>
      <th data-k="symbol">代码</th><th data-k="funds">基金</th><th data-k="qqq_rank">QQQ</th><th data-k="spy_rank">SPY</th>
      <th data-k="close">原始收盘</th><th data-k="adjusted_close">复权收盘</th><th data-k="bias">EMA</th><th data-k="sma_bias">SMA</th><th data-k="macd">MACD</th>
      <th data-k="adx">ADX</th><th data-k="relative_volume">相对前20日均量</th><th data-k="as_of">日期</th>
    </tr></thead>
    <tbody id="body"></tbody>
  </table>
  <h2>指标不完整</h2>
  <ul id="incomplete"></ul>
  <h2>没有算出指标</h2>
  <ul id="fails"></ul>
</div>
<script>
const DATA = {data};
const state = {{ fund:"", bias:"", adx:false, vol:false, q:"", sort:"symbol", desc:false, open:"" }};
const body = document.getElementById("body");
document.getElementById("lede").textContent = DATA.universe + "。Yahoo research-only。price_basis=" + DATA.price_basis + "。截止 " + DATA.data_cutoff + "。成功 " + DATA.rows.length + "，失败 " + DATA.failed.length + "，预热不足 " + (DATA.incomplete || []).length + "。三对 EMA 都在快线之上为多，都在之下为空。仅指标结果，非投资建议。";
function warmText(row, key) {{
  if (!row.warmup_ok && (key === "144" || key === "169")) return "预热不足";
  const value = row.ema[key];
  return value === null || value === undefined ? "—" : value;
}}
function show(row) {{
  if (state.q && row.symbol.indexOf(state.q) !== 0) return false;
  if (state.fund === "both" && row.funds.indexOf(",") < 0) return false;
  if (state.fund && state.fund !== "both" && row.funds.indexOf(state.fund) < 0) return false;
  if (state.bias && row.bias !== state.bias) return false;
  if (state.adx && !(row.adx >= 25)) return false;
  if (state.vol && !(row.relative_volume >= 1.5)) return false;
  return true;
}}
function val(row, key) {{
  const v = row[key];
  return v === null || v === undefined || v === "" ? (state.desc ? -Infinity : Infinity) : v;
}}
function draw(canvas, spark) {{
  const ctx = canvas.getContext("2d");
  const w = canvas.width = canvas.clientWidth * 2, h = canvas.height = 320;
  ctx.clearRect(0,0,w,h);
  const series = [spark.close, spark.ema10, spark.ema20, spark.ema55];
  const nums = series.flat().filter(v => v !== null);
  if (!nums.length) return;
  const min = Math.min.apply(null, nums), max = Math.max.apply(null, nums);
  const colors = ["#1c1915", "#0c5c56", "#8a5a12", "#8d2b1f"];
  series.forEach((line, index) => {{
    ctx.beginPath(); ctx.strokeStyle = colors[index]; ctx.lineWidth = index ? 2 : 3;
    let started = false;
    line.forEach((v, i) => {{
      if (v === null) {{ started = false; return; }}
      const x = (i / Math.max(line.length - 1, 1)) * (w - 16) + 8;
      const y = h - 12 - ((v - min) / Math.max(max - min, 1e-9)) * (h - 24);
      if (!started) {{ ctx.moveTo(x, y); started = true; }} else ctx.lineTo(x, y);
    }});
    ctx.stroke();
  }});
}}
function render() {{
  const rows = DATA.rows.filter(show).sort((a, b) => {{
    const av = val(a, state.sort), bv = val(b, state.sort);
    if (av < bv) return state.desc ? 1 : -1;
    if (av > bv) return state.desc ? -1 : 1;
    return a.symbol < b.symbol ? -1 : 1;
  }});
  document.getElementById("count").textContent = rows.length + " / " + DATA.rows.length;
  body.innerHTML = rows.map(row => {{
    const open = state.open === row.symbol;
    const main = `<tr class="item" data-s="${{row.symbol}}"><td>${{row.symbol}}</td><td>${{row.funds}}</td><td>${{row.qqq_rank || "—"}}</td><td>${{row.spy_rank || "—"}}</td><td>${{row.close ?? "—"}}</td><td>${{row.adjusted_close ?? "—"}}</td><td class="${{row.bias}}">${{row.bias || "—"}} ${{row.pairs["10_20"]}}/${{row.pairs["10_55"]}}/${{row.pairs["20_55"]}}</td><td class="${{row.sma_bias}}">${{row.sma_bias || "—"}}</td><td class="${{row.macd}}">${{row.macd || "—"}}</td><td>${{row.adx ?? "—"}}</td><td>${{row.relative_volume ?? "—"}}</td><td>${{row.as_of}}</td></tr>`;
    const smaText = [5, 10, 20, 55, 120].map(period => {{
      const value = row.sma[String(period)];
      if (period === 120 && !row.warmup_ok) return "预热不足";
      return value === null || value === undefined ? "—" : value;
    }}).join(" / ");
    const extra = open ? `<tr class="detail"><td colspan="12">
      <dl class="meta">
        <div><dt>${{row.symbol}} 原始 / 复权收盘</dt><dd>${{row.close ?? "—"}} / ${{row.adjusted_close ?? "—"}}（因子 ${{row.adjustment_factor ?? "—"}}）</dd></div>
        <div><dt>SMA 5/10/20/55/120</dt><dd>${{smaText}}</dd></div>
        <div><dt>EMA 10/20/55</dt><dd>${{warmText(row,"10")}} / ${{warmText(row,"20")}} / ${{warmText(row,"55")}}</dd></div>
        <div><dt>EMA 144/169</dt><dd>${{warmText(row,"144")}} / ${{warmText(row,"169")}}</dd></div>
        <div><dt>ADX · +DI / −DI</dt><dd>${{row.adx ?? "—"}} · ${{row.plus_di ?? "—"}} / ${{row.minus_di ?? "—"}}</dd></div>
        <div><dt>ATR · MACD 柱</dt><dd>${{row.atr ?? "—"}} · ${{row.histogram ?? "—"}}</dd></div>
        <div><dt>通道 20 日 高 / 低</dt><dd>${{row.channel?.high_20 ?? "—"}} / ${{row.channel?.low_20 ?? "—"}}</dd></div>
        <div><dt>通道 55 日 高 / 低</dt><dd>${{row.channel?.high_55 ?? "—"}} / ${{row.channel?.low_55 ?? "—"}}</dd></div>
        <div><dt>成交量 / 前20日均量</dt><dd>${{row.volume ?? "—"}} / ${{row.volume_sma_20 ?? "—"}}（相对 ${{row.relative_volume ?? "—"}}）</dd></div>
        <div><dt>预热</dt><dd>${{row.warmup_ok ? "足够" : "预热不足"}}，${{row.bar_count}} 根</dd></div>
      </dl>
      <canvas id="c-${{row.symbol.replace(".","_")}}" aria-label="收盘与EMA"></canvas>
    </td></tr>` : "";
    return main + extra;
  }}).join("");
  rows.forEach(row => {{
    if (state.open !== row.symbol) return;
    const canvas = document.getElementById("c-" + row.symbol.replace(".","_"));
    if (canvas) draw(canvas, row.spark);
  }});
  body.querySelectorAll("tr.item").forEach(tr => tr.addEventListener("click", () => {{
    state.open = state.open === tr.dataset.s ? "" : tr.dataset.s;
    render();
  }}));
}}
document.getElementById("q").addEventListener("input", e => {{ state.q = e.target.value.trim().toUpperCase(); render(); }});
document.getElementById("fund").addEventListener("change", e => {{ state.fund = e.target.value; render(); }});
document.getElementById("bias").addEventListener("change", e => {{ state.bias = e.target.value; render(); }});
document.getElementById("adx").addEventListener("click", e => {{ state.adx = !state.adx; e.target.classList.toggle("on", state.adx); e.target.setAttribute("aria-pressed", String(state.adx)); render(); }});
document.getElementById("vol").addEventListener("click", e => {{ state.vol = !state.vol; e.target.classList.toggle("on", state.vol); e.target.setAttribute("aria-pressed", String(state.vol)); render(); }});
document.getElementById("reset").addEventListener("click", () => {{
  Object.assign(state, {{ fund:"", bias:"", adx:false, vol:false, q:"", open:"" }});
  document.getElementById("q").value = "";
  document.getElementById("fund").value = "";
  document.getElementById("bias").value = "";
  const adx = document.getElementById("adx"), vol = document.getElementById("vol");
  adx.classList.remove("on"); vol.classList.remove("on");
  adx.setAttribute("aria-pressed", "false"); vol.setAttribute("aria-pressed", "false");
  render();
}});
document.querySelectorAll("th").forEach(th => th.addEventListener("click", () => {{
  if (state.sort === th.dataset.k) state.desc = !state.desc; else {{ state.sort = th.dataset.k; state.desc = false; }}
  render();
}}));
document.getElementById("fails").innerHTML = DATA.failed.map(item => `<li>${{item.symbol}}（${{item.funds}}）：${{item.error}}</li>`).join("") || "<li>没有</li>";
document.getElementById("incomplete").innerHTML = (DATA.incomplete || []).map(item => `<li>${{item.symbol}}：${{item.bar_count}} 根，${{item.reason}}，预热不足</li>`).join("") || "<li>没有</li>";
render();
</script>
</body>
</html>
"""
