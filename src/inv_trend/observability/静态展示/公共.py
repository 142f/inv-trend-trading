"""Shared, dependency-free building blocks for the four generated showcase pages.

Every page remains a single self-contained HTML document: JSON, CSS and vanilla
JS are embedded inline, with no external scripts, fonts or build tooling.  This
module holds the design tokens, responsive/print rules and JS helpers that all
four pages used to duplicate verbatim.

It imports nothing outside the standard library and stays inside the
``observability`` package, so it adds no package-level dependency edge.
"""

from __future__ import annotations

import json
from html import escape

BASE_META_KEYS: tuple[tuple[str, str], ...] = (
    ("provider", "数据来源"),
    ("data_cutoff", "数据截止"),
    ("price_basis", "价格口径"),
    ("config_hash", "配置哈希"),
    ("content_hash", "内容哈希"),
    ("script_git_commit", "脚本提交"),
)

# The decision desk shows three extra risk-budget assumptions.  The order is
# part of the rendered contract and must not be changed.
DESK_META_KEYS: tuple[tuple[str, str], ...] = (
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


def embed_json(value) -> str:
    """Serialize a payload for inline ``<script>`` without closing the tag."""

    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def render_meta(meta: dict | None, keys: tuple[tuple[str, str], ...] = BASE_META_KEYS) -> str:
    """Render the ``<dl class="meta">`` entries used by every generated page."""

    header = meta or {}
    return "".join(
        f"<div><dt>{escape(label)}</dt><dd>{escape(str(header.get(key, '—')))}</dd></div>"
        for key, label in keys
    )


def fmt_money(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):,.2f}"


def fmt_pct(value) -> str:
    if value is None:
        return "—"
    return f"{float(value):.2f}%"


# One design-token palette for all four pages.  Chinese-market convention:
# long = red, short = green, neutral = grey.  Text labels always accompany the
# colours, so colour is never the only information carrier.
BASE_CSS = """  :root {
    color-scheme: light;
    --ink:#1c1915; --muted:#5c564c; --line:#e4ddd0; --paper:#f6f3ec; --card:#fffdf8;
    --long:#b3372b; --short:#1e7d46; --mix:#8a5a12; --neutral:#9aa3ad; --hot:#f8efd8;
    --long-deep:#8d2b1f; --long-soft:#e8c9c4; --long-mid:#c07a45;
    --short-soft:#c5dfd0; --short-mid:#7fb394;
    --accent:#0c5c56; --accent-ink:#f4fffc;
    --fs-body:15px; --fs-h1:26px; --fs-h2:18px; --fs-meta:13px; --fs-small:11px;
    --sp-1:4px; --sp-2:8px; --sp-3:10px; --sp-4:14px; --sp-5:24px;
    --r-pill:999px; --r-card:12px; --r-cell:6px; --r-panel:10px;
    --shadow-card:0 1px 2px rgba(28,25,21,.04);
    --shadow-sticky:0 1px 0 var(--line);
  }
  * { box-sizing: border-box; }
  body { margin:0; font:var(--fs-body)/1.5 "Segoe UI","Microsoft YaHei",sans-serif; color:var(--ink); background:var(--paper); }
  .wrap { width:min(1440px, 100%); margin:0 auto; padding:var(--sp-5) 18px 48px; }
  h1 { margin:0 0 6px; font-size:var(--fs-h1); }
  h2 { font-size:var(--fs-h2); margin:22px 0 8px; }
  .lede, .note { color:var(--muted); font-size:var(--fs-meta); }
  .lede { margin:0 0 var(--sp-4); }
  .meta { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:6px 18px; margin:0 0 var(--sp-4);
          padding:var(--sp-3) var(--sp-4); background:var(--card); border:1px solid var(--line);
          border-radius:var(--r-panel); font-size:var(--fs-meta); box-shadow:var(--shadow-card); }
  .meta dt { color:var(--muted); }
  .meta dd { margin:0; font-variant-numeric:tabular-nums; overflow-wrap:anywhere; }
  .controls { display:flex; flex-wrap:wrap; gap:var(--sp-2); align-items:center; }
  input, select, button { font:inherit; border:1px solid #d5cec0; background:var(--card); border-radius:var(--r-pill); padding:7px 12px; }
  button { cursor:pointer; }
  button.on { background:var(--accent); color:var(--accent-ink); border-color:var(--accent); }
  button.on::before { content:"✓ "; }
  button:focus-visible, input:focus-visible, select:focus-visible, [tabindex]:focus-visible {
    outline:2px solid var(--accent); outline-offset:2px;
  }
  .count { font-variant-numeric:tabular-nums; color:var(--muted); }
  .badge { display:inline-block; border-radius:var(--r-pill); padding:1px 8px; font-size:var(--fs-small); background:#eee9de; color:var(--muted); }
  .disclaimer { color:var(--mix); background:var(--hot); border-radius:var(--r-panel);
                padding:var(--sp-2) var(--sp-4); margin:0 0 var(--sp-4); font-size:var(--fs-meta); }
  .多 { color:var(--long); font-weight:700; }
  .空 { color:var(--short); font-weight:700; }
  .混, .平, .中 { color:var(--mix); font-weight:700; }
  canvas { width:100%; height:160px; background:#fff; border:1px solid var(--line); border-radius:var(--r-cell); }
  .bar-inner { position:sticky; top:0; z-index:2; background:rgba(246,243,236,.97);
               padding:var(--sp-3) 0 12px; border-bottom:1px solid var(--line); }
  .tablewrap { max-width:100%; overflow:auto; max-height:min(70vh, 920px);
    border:1px solid var(--line); border-radius:var(--r-panel); background:var(--card); }
  .pf-table-wrap { max-width:100%; overflow:auto; }
  thead th { position:sticky; top:0; z-index:1; background:#fbf7ef; }
  th[aria-sort="ascending"]::after { content:" ↑"; }
  th[aria-sort="descending"]::after { content:" ↓"; }
  td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
  .empty { color:var(--muted); padding:28px 12px; text-align:center; }
  .flag { display:inline-block; margin-left:6px; padding:0 6px; border:1px solid var(--long); border-radius:var(--r-pill);
          color:var(--long); font-size:var(--fs-small); font-weight:700; vertical-align:2px; }
  .chart-tip { min-height:1.4em; margin:6px 0 0; color:var(--muted); font-size:var(--fs-meta); font-variant-numeric:tabular-nums; }
  .swatch { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:4px; vertical-align:-1px; }
  .swatch.long { background:var(--long); } .swatch.short { background:var(--short); } .swatch.mix { background:var(--mix); }
  /* 长表格横向滚动时首列跟随，代码列始终可读 */
  .tablewrap th:first-child, .tablewrap td:first-child {
    position:sticky; left:0; z-index:3; background:var(--card); box-shadow:1px 0 0 var(--line);
  }
  .tablewrap thead th:first-child { z-index:4; background:#fbf7ef; }
  .to-top { position:fixed; right:18px; bottom:18px; z-index:20; display:none; }
  .to-top.show { display:inline-block; }
  @media (max-width:1280px) { .wrap { padding-inline:16px; } }
  @media (max-width:1024px) {
    h1 { font-size:24px; }
    .temp { grid-template-columns:1fr; }
    .score .rail { flex-basis:88px; }
  }
  @media (max-width:760px) {
    .wrap { padding:16px 12px 32px; }
    h1 { font-size:22px; }
    .meta, .dist, .health, .cards, .summary, .pf-grid, .temp, .grid { grid-template-columns:1fr; }
    .meta { gap:4px 10px; }
    .controls { gap:6px; }
    input, select, button { padding:6px 10px; font-size:13px; }
    th, td { padding:6px; }
    .temp b.big { font-size:40px; }
    .bars .row { grid-template-columns:1fr auto; }
    .bars .rail { grid-column:1 / -1; }
    th.sys { font-size:10px; max-width:48px; }
  }
  @media print {
    body { background:#fff; }
    .wrap { max-width:none; padding:0; }
    .bar, .bar-inner, .controls, .win, .tab, button, input, select { display:none !important; }
    .card, .pf, section, .bucket, .box { break-inside:avoid; box-shadow:none; }
    .tablewrap { overflow:visible; }
    table { border-collapse:collapse; }
    thead th { position:static !important; }
    canvas { height:160px; }
  }
  @media (prefers-reduced-motion:reduce) {
    * { transition:none !important; animation:none !important; }
  }
"""

# Vanilla-JS helpers shared by the four pages.  No animation/transition is
# introduced, so screenshots stay deterministic under reduced-motion.
BASE_JS = """const C = {
  ink:"#1c1915", accent:"#0c5c56", mix:"#8a5a12", hot:"#f8efd8", neutral:"#b9b2a4",
  long:"#b3372b", longDeep:"#8d2b1f", longSoft:"#e8c9c4", longMid:"#c07a45", longPale:"#d9918a",
  short:"#1e7d46", shortSoft:"#c5dfd0", shortMid:"#7fb394", soft:"#e3d9c6"
};
const SPARK_COLORS = [C.ink, C.accent, C.mix, C.longDeep];
function fitCanvas(canvas, logicalH){
  canvas.width = canvas.clientWidth * 2;
  canvas.height = logicalH;
  const ctx = canvas.getContext("2d");
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  return ctx;
}
function onResize(redraw){
  let lastWidth = window.innerWidth, timer = null;
  window.addEventListener("resize", () => {
    if (window.innerWidth === lastWidth) return;
    lastWidth = window.innerWidth;
    if (timer) clearTimeout(timer);
    timer = setTimeout(redraw, 120);
  });
}
function renderList(el, items, format){
  if (!el) return;
  el.innerHTML = items.map(format).join("") || "<li>没有</li>";
}
function togglePressed(el, on){
  if (!el) return;
  el.classList.toggle("on", on);
  el.setAttribute("aria-pressed", String(on));
}
function setAriaSort(headers, sortKey, desc){
  headers.forEach(th => {
    const active = th.dataset.k === sortKey;
    th.setAttribute("aria-sort", active ? (desc ? "descending" : "ascending") : "none");
  });
}
function bindActivate(container, selector, onActivate){
  container.querySelectorAll(selector).forEach(node => {
    if (!node.hasAttribute("tabindex")) node.setAttribute("tabindex", "0");
    const activate = () => onActivate(node);
    node.addEventListener("click", activate);
    node.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); }
    });
  });
}
function bindButtonGroup(selector, onPick){
  const buttons = Array.from(document.querySelectorAll(selector));
  const mark = () => buttons.forEach(item => item.setAttribute("aria-pressed", String(item.classList.contains("on"))));
  mark();
  buttons.forEach(btn => btn.addEventListener("click", () => {
    buttons.forEach(item => item.classList.toggle("on", item === btn));
    mark();
    onPick(btn);
  }));
}
function bindChartTip(canvas, labels, series){
  let tip = canvas.nextElementSibling;
  if (!tip || !tip.classList || !tip.classList.contains("chart-tip")) {
    tip = document.createElement("p");
    tip.className = "chart-tip";
    tip.setAttribute("role", "status");
    canvas.insertAdjacentElement("afterend", tip);
  }
  const count = Math.max.apply(null, series.map(line => (line || []).length).concat([0]));
  const show = index => {
    const parts = labels.map((label, idx) => {
      const line = series[idx] || [];
      const value = line[index];
      return label + " " + (value === null || value === undefined ? "—" : value);
    });
    tip.textContent = "第 " + (index + 1) + " 根 · " + parts.join(" · ");
  };
  canvas.tabIndex = 0;
  canvas.addEventListener("mousemove", event => {
    if (!count) return;
    const rect = canvas.getBoundingClientRect();
    const ratio = Math.min(1, Math.max(0, (event.clientX - rect.left) / Math.max(rect.width, 1)));
    show(Math.round(ratio * (count - 1)));
  });
  canvas.addEventListener("focus", () => { if (count) show(count - 1); });
}
function prefsKey(page){ return "看板偏好:" + page; }
function loadPrefs(page){
  try { return JSON.parse(window.localStorage.getItem(prefsKey(page)) || "{}") || {}; }
  catch (error) { return {}; }
}
function savePrefs(page, value){
  try { window.localStorage.setItem(prefsKey(page), JSON.stringify(value)); } catch (error) {}
}
function installBackToTop(){
  var button = document.createElement("button");
  button.type = "button";
  button.className = "to-top";
  button.textContent = "回到顶部";
  button.setAttribute("aria-label", "回到页面顶部");
  document.body.appendChild(button);
  var scrollers = function(){
    return Array.prototype.slice.call(document.querySelectorAll(".tablewrap, .pf-table-wrap"));
  };
  var sync = function(){
    var show = window.scrollY > 400 || scrollers().some(function(node){ return node.scrollTop > 200; });
    button.classList.toggle("show", show);
  };
  // 捕获阶段监听：长表格在容器内部滚动，scroll 事件不会冒泡到 window
  document.addEventListener("scroll", sync, true);
  window.addEventListener("resize", sync, { passive:true });
  sync();
  button.addEventListener("click", function(){
    window.scrollTo(0, 0);
    scrollers().forEach(function(node){ node.scrollTop = 0; node.scrollLeft = 0; });
    var title = document.querySelector("h1");
    if (title) { title.setAttribute("tabindex", "-1"); title.focus(); }
    sync();
  });
}
function drawSpark(canvas, spark, logicalH){
  const h = logicalH;
  const ctx = fitCanvas(canvas, h);
  const w = canvas.width;
  const series = [spark.close, spark.ema10, spark.ema20, spark.ema55];
  const nums = series.flat().filter(v => v !== null);
  if (!nums.length) return;
  const min = Math.min.apply(null, nums), max = Math.max.apply(null, nums);
  series.forEach((line, index) => {
    ctx.beginPath(); ctx.strokeStyle = SPARK_COLORS[index]; ctx.lineWidth = index ? 2 : 3;
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
"""


def render_document(
    template: str,
    *,
    data=None,
    history=None,
    placeholders: dict[str, str] | None = None,
) -> str:
    """Assemble one self-contained page from its template and shared pieces.

    ``BASE_CSS`` / ``BASE_JS`` are inserted at the start of the first
    ``<style>`` / ``<script>`` block so page-specific rules can override them.
    Payload JSON is substituted last because it may itself contain a literal
    ``%...%`` token that must not be mistaken for a placeholder.
    """

    document = template
    if BASE_CSS:
        document = document.replace("<style>", "<style>\n" + BASE_CSS, 1)
    if BASE_JS:
        document = document.replace("<script>", "<script>\n" + BASE_JS, 1)
    for token, value in (placeholders or {}).items():
        document = document.replace(token, value)
    if history is not None:
        document = document.replace("%HISTORY%", embed_json(history))
    if data is not None:
        document = document.replace("%DATA%", embed_json(data))
    return document
