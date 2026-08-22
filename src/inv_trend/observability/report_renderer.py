"""Dependency-free interactive HTML renderer for report bundles.

The renderer consumes only serialized report data.  It never recalculates a
strategy condition, score, signal or anomaly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from inv_trend.application.daily_models import InstrumentReportBundle


def write_instrument_report(
    bundle: InstrumentReportBundle | Mapping[str, Any],
    path: str | Path,
) -> Path:
    payload = bundle.to_dict() if isinstance(bundle, InstrumentReportBundle) else dict(bundle)
    snapshot = {
        "report_date": str(payload.get("generated_at", ""))[:10],
        "timeframe": payload.get("timeframe", "D1"),
        "symbols": [{"symbol": payload.get("symbol", "?"), "report_bundle": payload}],
        "summary": payload.get("summary", {}),
    }
    return _write(render_daily_dashboard(snapshot), path)


def write_daily_dashboard(snapshot: Mapping[str, Any], path: str | Path) -> Path:
    return _write(render_daily_dashboard(snapshot), path)


def render_daily_dashboard(snapshot: Mapping[str, Any]) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False, default=str)
    payload = payload.replace("</", "<\\/")
    title = f"趋势策略可解释报告 {snapshot.get('report_date', '')}"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_html_text(title)}</title>
<style>{_CSS}</style>
</head>
<body>
<header class="topbar">
  <div><div class="eyebrow">INV TREND · AUDITABLE REPORT</div><h1>{_html_text(title)}</h1></div>
  <div class="selector-wrap"><label for="symbolSelect">标的</label><select id="symbolSelect"></select></div>
</header>
<main>
  <section id="identity" class="identity"></section>
  <section id="summaryCards" class="cards"></section>

  <section id="dataNoticePanel" class="panel notice-panel">
    <div class="section-head"><div><span class="kicker">PROVENANCE</span><h2>数据与验证范围</h2></div></div>
    <div id="dataNotice" class="notice-text"></div>
  </section>

  <section class="panel">
    <div class="section-head"><div><span class="kicker">CHANGE LOG</span><h2>本次修改内容、理由与效果</h2></div></div>
    <div class="table-wrap"><table><thead><tr><th>模块</th><th>修改内容</th><th>理由</th><th>修改后效果</th></tr></thead><tbody id="changeRows"></tbody></table></div>
  </section>

  <section class="panel chart-panel">
    <div class="section-head"><div><span class="kicker">LINKED CHARTS</span><h2>价格、指标、信号与异常联动</h2></div><div id="hoverInfo" class="hover-info">移动鼠标查看同一时间点</div></div>
    <div class="range-row"><label>显示起点</label><input id="rangeStart" type="range" min="0" value="0"><span id="rangeLabel"></span></div>
    <div class="legend" id="priceLegend"></div><canvas id="priceChart" height="300"></canvas>
    <div class="legend" id="macdLegend"></div><canvas id="macdChart" height="170"></canvas>
    <div class="legend" id="trendLegend"></div><canvas id="trendChart" height="170"></canvas>
    <div class="legend" id="volLegend"></div><canvas id="volChart" height="170"></canvas>
  </section>

  <section class="panel">
    <div class="section-head"><div><span class="kicker">EXPLAINABILITY</span><h2>策略条件明细</h2></div><div id="conditionStats" class="hover-info"></div></div>
    <div id="ruleList" class="rules"></div>
  </section>

  <section class="split">
    <section class="panel"><div class="section-head"><div><span class="kicker">SIGNALS</span><h2>信号</h2></div></div><div class="table-wrap"><table><thead><tr><th>时间</th><th>指标/类型</th><th>方向</th><th>价格</th></tr></thead><tbody id="signalRows"></tbody></table></div></section>
    <section class="panel"><div class="section-head"><div><span class="kicker">ANOMALIES</span><h2>异常点</h2></div></div><div class="table-wrap"><table><thead><tr><th>时间</th><th>类型</th><th>级别</th><th>依据</th></tr></thead><tbody id="anomalyRows"></tbody></table></div></section>
  </section>

  <section class="panel"><div class="section-head"><div><span class="kicker">STATE</span><h2>策略状态变化</h2></div></div><div class="table-wrap"><table><thead><tr><th>时间</th><th>状态</th><th>之前</th><th>之后</th><th>原因</th></tr></thead><tbody id="transitionRows"></tbody></table></div></section>

  <details class="panel"><summary>机器可读 ReportBundle</summary><pre id="rawJson"></pre></details>
</main>
<footer>图表、条件明细、信号、异常和状态变化均来自同一 ReportBundle；HTML 不重新计算策略逻辑。</footer>
<script id="snapshot" type="application/json">{payload}</script>
<script>{_JS}</script>
</body></html>"""


def _write(document: str, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding="utf-8")
    return target


def _html_text(value: Any) -> str:
    text = str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_CSS = r"""
:root{color-scheme:dark;--bg:#071019;--panel:#0d1824;--panel2:#111f2e;--line:#24364a;--text:#e7eef7;--muted:#8ca0b6;--ok:#4ade80;--bad:#fb7185;--warn:#fbbf24;--info:#60a5fa;font:14px Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#122235 0,#071019 42%);color:var(--text)}.topbar{position:sticky;top:0;z-index:5;display:flex;justify-content:space-between;align-items:end;gap:24px;padding:24px max(24px,calc((100vw - 1320px)/2));background:rgba(7,16,25,.92);backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}h1,h2{margin:0}.eyebrow,.kicker{font-size:11px;letter-spacing:.16em;color:#67e8f9;font-weight:700}.topbar h1{margin-top:6px;font-size:26px}.selector-wrap{display:flex;align-items:center;gap:9px;color:var(--muted)}select,input{accent-color:#67e8f9;background:#0a1420;color:var(--text);border:1px solid var(--line);border-radius:8px;padding:8px}main{max-width:1320px;margin:auto;padding:24px}.identity{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}.pill{padding:7px 10px;border:1px solid var(--line);border-radius:999px;background:#0b1723;color:var(--muted)}.pill b{color:var(--text)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(135px,1fr));gap:10px;margin-bottom:18px}.card{background:linear-gradient(160deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:14px;padding:14px}.card span{color:var(--muted);font-size:12px}.card strong{display:block;font-size:24px;margin-top:7px}.panel{background:rgba(13,24,36,.94);border:1px solid var(--line);border-radius:16px;padding:18px;margin:18px 0;box-shadow:0 16px 50px rgba(0,0,0,.16)}.section-head{display:flex;justify-content:space-between;align-items:end;gap:16px;margin-bottom:14px}.section-head h2{font-size:18px;margin-top:4px}.hover-info{color:var(--muted);font-variant-numeric:tabular-nums}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:680px}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:#a8bed4;font-size:12px}td{color:#d9e4ef}canvas{display:block;width:100%;background:#08131f;border:1px solid #1f3348;border-radius:10px;margin:6px 0 15px}.legend{display:flex;gap:14px;flex-wrap:wrap;color:var(--muted);font-size:12px}.legend i{width:18px;height:3px;border-radius:3px;display:inline-block;margin-right:5px;vertical-align:middle}.range-row{display:flex;align-items:center;gap:10px;margin:8px 0 12px;color:var(--muted)}.range-row input{flex:1;padding:0}.rules{display:grid;gap:9px}.rule{border:1px solid var(--line);border-radius:12px;background:#0a1521}.rule summary{cursor:pointer;display:flex;align-items:center;gap:10px;padding:12px;list-style:none}.rule summary::-webkit-details-marker{display:none}.rule-title{font-weight:700;flex:1}.badge{font-size:11px;padding:3px 7px;border-radius:999px;border:1px solid var(--line)}.badge.true{color:var(--ok);border-color:#166534;background:#082b18}.badge.false{color:var(--bad);border-color:#7f1d1d;background:#2c0d13}.badge.na{color:var(--warn);border-color:#713f12;background:#291b06}.conditions{padding:0 12px 12px;overflow:auto}.conditions table{min-width:1000px}.split{display:grid;grid-template-columns:1fr 1fr;gap:18px}.split .panel{margin:0}pre{max-height:500px;overflow:auto;white-space:pre-wrap;color:#b9cce0}summary{cursor:pointer}footer{max-width:1320px;margin:0 auto;padding:0 24px 32px;color:#6f849b}@media(max-width:900px){.split{grid-template-columns:1fr}.topbar{position:static;align-items:flex-start;flex-direction:column}.section-head{align-items:flex-start;flex-direction:column}main{padding:14px}}
"""

_JS = r"""
const SNAPSHOT=JSON.parse(document.getElementById('snapshot').textContent);
const rows=(SNAPSHOT.symbols||[]).filter(x=>x.report_bundle);
const select=document.getElementById('symbolSelect');
rows.forEach((r,i)=>{const o=document.createElement('option');o.value=i;o.textContent=r.symbol||r.report_bundle.symbol||('?'+i);select.appendChild(o)});
let current=null,hoverIndex=null,startIndex=0;
const palette={close:'#e2e8f0',channel_high_20:'#38bdf8',channel_low_20:'#38bdf8',channel_high_55:'#a78bfa',channel_low_55:'#a78bfa',sma_10:'#4ade80',sma_20:'#fbbf24',ema_144:'#fb7185',ema_169:'#f97316',dif:'#4ade80',dea:'#fbbf24',adx:'#e2e8f0',plus_di:'#4ade80',minus_di:'#fb7185',atr_percentile:'#60a5fa',relative_volume:'#c084fc'};
function esc(v){return String(v??'—').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]))}
function fmt(v,d=4){if(v===null||v===undefined||Number.isNaN(Number(v)))return '—';const n=Number(v);return Math.abs(n)>=1000?n.toLocaleString(undefined,{maximumFractionDigits:2}):n.toFixed(d).replace(/0+$/,'').replace(/\.$/,'')}
function badge(v){return `<span class="badge ${v===true?'true':v===false?'false':'na'}">${v===true?'TRUE':v===false?'FALSE':'N/A'}</span>`}
function load(index){current=rows[index]?.report_bundle||{};hoverIndex=null;startIndex=0;const series=current.series||[];const slider=document.getElementById('rangeStart');slider.max=Math.max(0,series.length-25);slider.value=0;renderAll();}
function renderAll(){renderIdentity();renderCards();renderNotice();renderChanges();renderRules();renderTables();renderRaw();renderCharts();}
function renderIdentity(){const b=current;document.getElementById('identity').innerHTML=[['symbol',b.symbol],['instrument',b.instrument_id],['timeframe',b.timeframe],['dataset version',b.dataset_version],['result id',b.result_id],['hash',(b.result_hash||'').slice(0,18)+'…']].map(x=>`<span class="pill">${esc(x[0])}: <b>${esc(x[1])}</b></span>`).join('')}
function renderCards(){const s=current.summary||{};const latest=current.latest_bar||{};const rating=current.strategy_snapshot?.strategy_checks?.rating||{};const winRate=(s.win_rate===null||s.win_rate===undefined)?'—（需成交回测）':fmt(Number(s.win_rate)*100,1)+'%';const pairs=[['最新收盘',fmt(latest.close,2)],['策略评级',rating.grade||'—'],['方向',rating.direction||'—'],['综合分',fmt(rating.score,2)],['信号',s.signals??0],['触发规则',`${s.triggered_rules??0}/${s.rules??0}`],['条件通过',`${s.conditions_passed??0}/${s.conditions??0}`],['异常',s.anomalies??0],['胜率',winRate]];document.getElementById('summaryCards').innerHTML=pairs.map(([k,v])=>`<div class="card"><span>${esc(k)}</span><strong>${esc(v)}</strong></div>`).join('')}
function renderNotice(){const panel=document.getElementById('dataNoticePanel');const text=current.data_notice||current.summary?.data_notice||'';if(text){panel.style.display='block';document.getElementById('dataNotice').textContent=text}else{panel.style.display='none'}}
function renderChanges(){document.getElementById('changeRows').innerHTML=(current.change_log||[]).map(x=>`<tr><td><code>${esc(x.module)}</code></td><td>${esc(x.change)}</td><td>${esc(x.reason)}</td><td>${esc(x.effect)}</td></tr>`).join('')||'<tr><td colspan="4">无修改记录</td></tr>'}
function renderRules(){const rules=current.rule_evaluations||[];let p=0,f=0,n=0;for(const r of rules)for(const c of (r.conditions||[])){if(c.passed===true)p++;else if(c.passed===false)f++;else n++}document.getElementById('conditionStats').textContent=`通过 ${p} · 未通过 ${f} · 不可用 ${n}`;document.getElementById('ruleList').innerHTML=rules.map(r=>`<details class="rule" ${r.triggered?'open':''}><summary><span>${badge(r.triggered)}</span><span class="rule-title">${esc(r.name)}</span><span class="hover-info">${esc(r.direction||r.outcome||'')}</span></summary><div class="conditions"><table><thead><tr><th>条件</th><th>实际值</th><th>阈值/比较对象</th><th>运算</th><th>满足</th><th>相对位置</th><th>影响/权重</th><th>时间</th><th>价格</th></tr></thead><tbody>${(r.conditions||[]).map(c=>`<tr><td>${esc(c.name)}</td><td>${esc(fmtAny(c.actual_value))}</td><td>${esc(fmtAny(c.reference_value))}</td><td><code>${esc(c.operator)}</code></td><td>${badge(c.passed)}</td><td>${esc(c.relative_position)}</td><td>${esc(c.impact)} / ${fmt(c.weight,2)}</td><td>${esc(shortTime(c.timestamp))}</td><td>${fmt(c.price,2)}</td></tr>`).join('')}</tbody></table></div></details>`).join('')||'<div class="hover-info">无规则结果</div>'}
function fmtAny(v){if(Array.isArray(v))return '['+v.map(x=>fmtAny(x)).join(', ')+']';if(v&&typeof v==='object')return JSON.stringify(v);return typeof v==='number'?fmt(v):String(v??'—')}
function shortTime(t){return t?String(t).replace('T',' ').replace('+00:00','Z'): '—'}
function renderTables(){const signals=current.signals||[];document.getElementById('signalRows').innerHTML=signals.map(s=>`<tr><td>${esc(shortTime(s.signal_time||s.timestamp))}</td><td>${esc(s.indicator_name||s.indicator||s.signal_type||s.event)}</td><td>${esc(s.direction)}</td><td>${fmt(s.trigger_price??s.price,2)}</td></tr>`).join('')||'<tr><td colspan="4">当前可视范围无信号</td></tr>';const anomalies=current.anomalies||[];document.getElementById('anomalyRows').innerHTML=anomalies.map(a=>`<tr><td>${esc(shortTime(a.timestamp))}</td><td>${esc(a.anomaly_type)}</td><td>${esc(a.severity)}</td><td>${esc(a.summary)}</td></tr>`).join('')||'<tr><td colspan="4">无异常点</td></tr>';const tr=current.state_transitions||[];document.getElementById('transitionRows').innerHTML=tr.map(x=>`<tr><td>${esc(shortTime(x.timestamp))}</td><td>${esc(x.state_name)}</td><td>${esc(fmtAny(x.from_state))}</td><td>${esc(fmtAny(x.to_state))}</td><td>${esc(x.reason)}</td></tr>`).join('')||'<tr><td colspan="5">最新一根未发生监控状态切换</td></tr>'}
function renderRaw(){document.getElementById('rawJson').textContent=JSON.stringify(current,null,2)}
function legend(id,series){document.getElementById(id).innerHTML=series.map(k=>`<span><i style="background:${palette[k]||'#fff'}"></i>${esc(k)}</span>`).join('')}
function setupCanvas(id){const c=document.getElementById(id),dpr=window.devicePixelRatio||1,w=c.clientWidth,h=Number(c.getAttribute('height'));if(c.width!==Math.floor(w*dpr)||c.height!==Math.floor(h*dpr)){c.width=Math.floor(w*dpr);c.height=Math.floor(h*dpr)}const ctx=c.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);return {c,ctx,w,h}}
function renderCharts(){const rows=(current.series||[]).slice(startIndex);document.getElementById('rangeLabel').textContent=rows.length?`${shortTime(rows[0].timestamp)} → ${shortTime(rows[rows.length-1].timestamp)}`:'无数据';const price=['close','channel_high_20','channel_low_20','channel_high_55','channel_low_55','sma_10','sma_20','ema_144','ema_169'];const macd=['dif','dea'];const trend=['adx','plus_di','minus_di'];const vol=['atr_percentile','relative_volume'];legend('priceLegend',price);legend('macdLegend',macd);legend('trendLegend',trend);legend('volLegend',vol);draw('priceChart',rows,price,{markers:true,candles:true});draw('macdChart',rows,macd,{});draw('trendChart',rows,trend,{});draw('volChart',rows,vol,{transform:(k,v)=>k==='atr_percentile'?v*100:v});if(hoverIndex!==null&&rows[hoverIndex]){const r=rows[hoverIndex];document.getElementById('hoverInfo').textContent=`${shortTime(r.timestamp)} · close ${fmt(r.close,2)} · ADX ${fmt(r.adx,2)} · ATR% ${fmt((r.atr_percentile??0)*100,1)} · RelVol ${fmt(r.relative_volume,2)}`}else document.getElementById('hoverInfo').textContent='移动鼠标查看同一时间点'}
function draw(id,rows,keys,opt){const {c,ctx,w,h}=setupCanvas(id);ctx.clearRect(0,0,w,h);const pad={l:48,r:16,t:14,b:26};ctx.strokeStyle='#24364a';ctx.fillStyle='#8095aa';ctx.font='11px system-ui';ctx.lineWidth=1;const values=[];for(const r of rows)for(const k of keys){const raw=r[k];if(raw!==null&&Number.isFinite(Number(raw)))values.push(opt.transform?opt.transform(k,Number(raw)):Number(raw))}if(opt.candles){for(const r of rows)for(const k of ['open','high','low','close']){const v=Number(r[k]);if(Number.isFinite(v))values.push(v)}}if(!values.length){ctx.fillText('数据不足',pad.l,pad.t+16);return}let min=Math.min(...values),max=Math.max(...values);if(min===max){min-=1;max+=1}const range=max-min;min-=range*.06;max+=range*.06;const iw=w-pad.l-pad.r,ih=h-pad.t-pad.b;const x=i=>pad.l+(rows.length<=1?0:i/(rows.length-1))*iw;const y=v=>pad.t+(max-v)/(max-min)*ih;if(opt.candles&&rows.length){const body=Math.max(1,Math.min(7,iw/Math.max(1,rows.length)*.58));rows.forEach((r,i)=>{const o=Number(r.open),hi=Number(r.high),lo=Number(r.low),cl=Number(r.close);if(![o,hi,lo,cl].every(Number.isFinite))return;const xx=x(i),up=cl>=o;ctx.strokeStyle=up?'#4ade80':'#fb7185';ctx.fillStyle=ctx.strokeStyle;ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(xx,y(hi));ctx.lineTo(xx,y(lo));ctx.stroke();const top=Math.min(y(o),y(cl)),height=Math.max(1,Math.abs(y(o)-y(cl)));ctx.fillRect(xx-body/2,top,body,height)})}for(let j=0;j<4;j++){const yy=pad.t+j*ih/3;ctx.strokeStyle='#172a3d';ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(w-pad.r,yy);ctx.stroke();const val=max-j*(max-min)/3;ctx.fillStyle='#8095aa';ctx.fillText(fmt(val,2),4,yy+4)}for(const k of keys){ctx.strokeStyle=palette[k]||'#fff';ctx.lineWidth=k==='close'?2:1.25;ctx.beginPath();let started=false;rows.forEach((r,i)=>{let v=r[k];if(v===null||!Number.isFinite(Number(v))){started=false;return}v=opt.transform?opt.transform(k,Number(v)):Number(v);const xx=x(i),yy=y(v);if(!started){ctx.moveTo(xx,yy);started=true}else ctx.lineTo(xx,yy)});ctx.stroke()}if(opt.markers){const byTime=new Map(rows.map((r,i)=>[String(r.timestamp),i]));for(const s of (current.signals||[])){const i=byTime.get(String(s.signal_time||s.timestamp));if(i===undefined)continue;const p=Number(s.trigger_price??rows[i].close);if(!Number.isFinite(p))continue;ctx.fillStyle=(String(s.direction).toLowerCase().includes('short'))?'#fb7185':'#4ade80';ctx.beginPath();ctx.arc(x(i),y(p),4,0,Math.PI*2);ctx.fill()}for(const a of (current.anomalies||[])){const i=byTime.get(String(a.timestamp));if(i===undefined)continue;const p=Number(a.price??rows[i].close);if(!Number.isFinite(p))continue;ctx.fillStyle='#fbbf24';ctx.fillRect(x(i)-3,y(p)-3,6,6)}}if(hoverIndex!==null&&rows[hoverIndex]){ctx.strokeStyle='#94a3b8';ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(x(hoverIndex),pad.t);ctx.lineTo(x(hoverIndex),h-pad.b);ctx.stroke();ctx.setLineDash([])}c.onmousemove=e=>{const rect=c.getBoundingClientRect();const idx=Math.round(((e.clientX-rect.left-pad.l)/Math.max(1,iw))*(rows.length-1));hoverIndex=Math.max(0,Math.min(rows.length-1,idx));renderCharts()};c.onmouseleave=()=>{hoverIndex=null;renderCharts()}}
select.onchange=()=>load(Number(select.value));document.getElementById('rangeStart').oninput=e=>{startIndex=Number(e.target.value);hoverIndex=null;renderCharts()};window.addEventListener('resize',()=>renderCharts());if(rows.length)load(0);else{document.getElementById('identity').textContent='快照中没有 report_bundle。'}
"""


__all__ = ["render_daily_dashboard", "write_daily_dashboard", "write_instrument_report"]
