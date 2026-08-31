"""Server-side report projection and self-contained HTML rendering."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from html import escape
import json
import math
from typing import Any, Mapping, Sequence

from .models import BacktestBatchResult


REPORT_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class BacktestReportModel:
    schema_version: str
    batch_schema_version: str
    basic_information: Mapping[str, Any]
    assumptions: Mapping[str, Any]
    ranking: tuple[Mapping[str, Any], ...]
    best_combination_id: str | None
    stable_combination_ids: tuple[str, ...]
    charts: Mapping[str, Any]
    conclusion: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_report_model(
    batch: BacktestBatchResult | Mapping[str, Any],
) -> BacktestReportModel:
    raw = batch.to_dict() if isinstance(batch, BacktestBatchResult) else dict(batch)
    combinations = [dict(item) for item in raw.get("combinations", [])]
    source = dict(raw.get("source", {}))
    plan = dict(raw.get("plan", {}))
    best_id = raw.get("best_combination_id")
    stable_ids = tuple(str(item) for item in raw.get("stable_combination_ids", []))
    ranking = tuple(_ranking_row(item) for item in combinations)
    selected = sorted(
        combinations,
        key=lambda item: (
            item.get("rank") is None,
            item.get("rank") if item.get("rank") is not None else 10**9,
            str(item.get("combination_id", "")),
        ),
    )[:10]
    best = next(
        (item for item in combinations if item.get("combination_id") == best_id),
        selected[0] if selected else None,
    )
    charts = {
        "equity_series": [
            {
                "combination_id": item.get("combination_id"),
                "points": item.get("equity_curve", []),
            }
            for item in selected
        ],
        "drawdown_series": [
            {
                "combination_id": item.get("combination_id"),
                "points": item.get("drawdown_curve", []),
            }
            for item in selected
        ],
        "risk_return": [
            {
                "combination_id": item.get("combination_id"),
                "max_drawdown": _metric(item.get("validation_metrics", {}), "max_drawdown"),
                "annualized_return": _metric(
                    item.get("validation_metrics", {}), "annualized_return"
                ),
                "qualified": bool(item.get("qualified")),
            }
            for item in combinations
        ],
        "parameter_heatmap": _heatmap(combinations),
        "fold_stability": [
            {
                "combination_id": item.get("combination_id"),
                "folds": [
                    {
                        "fold": fold.get("window", {}).get("fold"),
                        "annualized_return": _metric(
                            fold.get("validation_metrics", {}), "annualized_return"
                        ),
                        "sharpe_ratio": _metric(
                            fold.get("validation_metrics", {}), "sharpe_ratio"
                        ),
                    }
                    for fold in item.get("folds", [])
                ],
            }
            for item in selected
        ],
        "trade_distribution": _trade_distribution((best or {}).get("trades", [])),
        "side_contribution": _side_contribution(best),
    }
    instruments = list(source.get("instruments", []))
    basic = {
        "run_id": raw.get("run_id"),
        "report_date": raw.get("report_date"),
        "strategy_id": source.get("strategy_id", plan.get("strategy_id", "turtle")),
        "strategy_version": source.get("strategy_version", "unknown"),
        "code_version": source.get("code_version", "unknown"),
        "symbols": [item.get("symbol") for item in instruments],
        "timeframes": sorted({str(item.get("timeframe", "")) for item in instruments}),
        "data_versions": {
            str(item.get("symbol")): item.get("dataset_version") for item in instruments
        },
        "source_run_id": source.get("source_run_id"),
        "source_hash": source.get("source_hash"),
        "result_hash": raw.get("result_hash"),
        "generated_at": raw.get("generated_at"),
        "validation_status": raw.get("validation_status"),
        "combination_count": len(combinations),
    }
    assumptions = {
        "initial_equity": plan.get("initial_equity"),
        "cash_model": plan.get("cash_model"),
        "liquidate_at_end": plan.get("liquidate_at_end"),
        "parameter_space": plan.get("parameter_space", {}),
        "constraints": plan.get("constraints", []),
        "validation": plan.get("validation", {}),
        "ranking": plan.get("ranking", {}),
        "comparison_assumptions_hash": raw.get("comparison_assumptions_hash"),
    }
    conclusion = dict(raw.get("conclusion") or _legacy_conclusion(raw, best))
    return BacktestReportModel(
        schema_version=REPORT_SCHEMA_VERSION,
        batch_schema_version=str(raw.get("schema_version", "1")),
        basic_information=basic,
        assumptions=assumptions,
        ranking=ranking,
        best_combination_id=None if best_id is None else str(best_id),
        stable_combination_ids=stable_ids,
        charts=charts,
        conclusion=conclusion,
    )


def render_backtest_html(
    batch: BacktestBatchResult | Mapping[str, Any] | BacktestReportModel,
) -> str:
    model = batch if isinstance(batch, BacktestReportModel) else build_report_model(batch)
    data = json.dumps(
        model.to_dict(), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).replace("</", "<\\/")
    rows = "".join(_ranking_html(item, model.best_combination_id) for item in model.ranking)
    issues = model.conclusion.get("key_issues", [])
    issue_html = "".join(
        f"<li><b>{escape(str(item.get('code', 'ISSUE')))}</b>"
        f"<span>{escape(str(item.get('message', '')))}</span></li>"
        for item in issues
    ) or "<li><span>未发现额外关键问题。</span></li>"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>统一策略回测报告</title><style>{_CSS}</style></head><body><main>
<header><div><span class="eyebrow">STRATEGY BACKTEST · V2</span><h1>统一策略回测与参数比较报告</h1>
<p>指标、排名、稳定区间、风险和结论均由服务端同一份批次结果生成；浏览器只负责展示。</p></div>
<div class="status"><b>{escape(str(model.conclusion.get('status', 'UNKNOWN')))}</b>
<span>候选：{escape(str(model.best_combination_id or '未确认'))}</span></div></header>
<section><h2>回测基本信息</h2><div class="cards" id="summary"></div><div class="panel"><dl id="lineage"></dl></div></section>
<section><h2>策略、参数与交易假设</h2><div class="grid"><div class="panel"><h3>参数空间</h3><pre id="parameters"></pre></div>
<div class="panel"><h3>验证与固定假设</h3><pre id="assumptions"></pre></div></div></section>
<section><div class="section-title"><div><span>组合对比</span><h2>收益、回撤与稳定性</h2></div>
<label>叠加组合 <select id="curveSelect" multiple size="1"></select></label></div>
<div class="grid"><div class="panel wide"><h3>收益曲线</h3><canvas id="equityChart"></canvas></div>
<div class="panel"><h3>回撤曲线</h3><canvas id="drawdownChart"></canvas></div>
<div class="panel"><h3>样本外风险收益</h3><canvas id="scatterChart"></canvas></div>
<div class="panel"><h3>参数热力图</h3><canvas id="heatmapChart"></canvas></div>
<div class="panel"><h3>分折稳定性</h3><canvas id="foldChart"></canvas></div></div></section>
<section><h2>核心指标与超参数横向比较</h2><div class="table-wrap"><table><thead><tr>
<th>排名</th><th>组合</th><th>状态</th><th>综合分</th><th>总收益</th><th>年化收益</th><th>最大回撤</th>
<th>Sharpe</th><th>胜率</th><th>盈亏比</th><th>交易数</th><th>留出收益</th><th>风险</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<section><h2>多空表现与交易分布</h2><div class="grid"><div class="panel"><h3>多头 / 空头贡献</h3><canvas id="sideChart"></canvas></div>
<div class="panel"><h3>交易盈亏分布</h3><canvas id="tradeChart"></canvas></div></div></section>
<section><h2>最终回测结论和关键问题</h2><div class="grid"><div class="panel conclusion"><p>{escape(str(model.conclusion.get('summary', '暂无结论。')))}</p>
<p>稳定组合：{escape(', '.join(model.stable_combination_ids) or '未形成稳定区间')}</p></div>
<div class="panel"><ul class="issues">{issue_html}</ul></div></div></section>
<section><h2>组合完整证据</h2><div class="panel"><pre id="details">点击排名表中的组合查看参数、指标、分折和不可用原因。</pre></div></section>
</main><script id="reportData" type="application/json">{data}</script><script>{_JS}</script></body></html>"""


def _ranking_row(item: Mapping[str, Any]) -> Mapping[str, Any]:
    metrics = dict(item.get("metrics", {}))
    validation = dict(item.get("validation_metrics", {}))
    holdout = dict(item.get("holdout_metrics", {}))
    return {
        "rank": item.get("rank"), "combination_id": item.get("combination_id"),
        "status": item.get("status"), "qualified": item.get("qualified"),
        "score": item.get("score"), "parameters": item.get("parameters", {}),
        "metrics": _canonical_metrics(metrics),
        "validation_metrics": _canonical_metrics(validation),
        "holdout_metrics": _canonical_metrics(holdout),
        "long_metrics": item.get("long_metrics", {}),
        "short_metrics": item.get("short_metrics", {}),
        "folds": item.get("folds", []), "unavailable": item.get("unavailable", {}),
        "overfit_risk": item.get("overfit_risk", {}),
        "execution_result_hash": item.get("execution_result_hash"),
    }


def _canonical_metrics(values: Mapping[str, Any]) -> Mapping[str, Any]:
    aliases = {
        "annualized_return": "cagr", "sharpe_ratio": "sharpe",
        "sortino_ratio": "sortino", "mar_ratio": "mar",
    }
    out = dict(values)
    for canonical, legacy in aliases.items():
        if canonical not in out and legacy in out:
            out[canonical] = out[legacy]
    return out


def _metric(values: Mapping[str, Any], name: str) -> float | None:
    canonical = _canonical_metrics(values)
    try:
        value = float(canonical.get(name))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _heatmap(combinations: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    varying = [
        key for key in sorted({key for item in combinations for key in item.get("parameters", {})})
        if len({json.dumps(item.get("parameters", {}).get(key), sort_keys=True) for item in combinations}) > 1
    ]
    if len(varying) < 2:
        return {"x_parameter": None, "y_parameter": None, "cells": []}
    x_name, y_name = varying[:2]
    grouped: dict[tuple[str, str], list[float]] = {}
    raw_values: dict[tuple[str, str], tuple[Any, Any]] = {}
    for item in combinations:
        params = item.get("parameters", {})
        x_value, y_value = params.get(x_name), params.get(y_name)
        key = (json.dumps(x_value, sort_keys=True), json.dumps(y_value, sort_keys=True))
        raw_values[key] = (x_value, y_value)
        try:
            score = float(item.get("score"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(score):
            grouped.setdefault(key, []).append(score)
    cells = [
        {"x": raw_values[key][0], "y": raw_values[key][1], "score": sum(scores) / len(scores)}
        for key, scores in sorted(grouped.items()) if scores
    ]
    return {"x_parameter": x_name, "y_parameter": y_name, "cells": cells}


def _trade_distribution(trades: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    pnl = [_finite(item.get("pnl")) for item in trades]
    holding = [_finite(item.get("holding_bars")) for item in trades]
    return {
        "pnl": _histogram([item for item in pnl if item is not None], 12),
        "holding_bars": _histogram([item for item in holding if item is not None], 12),
        "trade_count": len(trades),
    }


def _histogram(values: Sequence[float], bins: int) -> list[Mapping[str, Any]]:
    if not values:
        return []
    low, high = min(values), max(values)
    if low == high:
        return [{"start": low, "end": high, "count": len(values)}]
    width = (high - low) / bins
    counts = [0] * bins
    for value in values:
        index = min(bins - 1, int((value - low) / width))
        counts[index] += 1
    return [
        {"start": low + index * width, "end": low + (index + 1) * width, "count": count}
        for index, count in enumerate(counts)
    ]


def _side_contribution(best: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if not best:
        return []
    return [
        {"side": "多头", **dict(best.get("long_metrics", {}))},
        {"side": "空头", **dict(best.get("short_metrics", {}))},
    ]


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _legacy_conclusion(
    raw: Mapping[str, Any], best: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    best_id = raw.get("best_combination_id")
    if best_id is None:
        return {
            "status": "NO_CONFIRMED_CANDIDATE",
            "summary": "旧格式结果没有已确认的候选参数。",
            "key_issues": [{
                "code": "LEGACY_RESULT", "severity": "INFO",
                "message": "该报告由历史 v1 批次只读转换，未重新执行回测。",
            }],
        }
    return {
        "status": "CANDIDATE_CONFIRMED",
        "summary": "候选参数来自历史批次；报告转换没有改变原始回测结果。",
        "risk_level": (best or {}).get("overfit_risk", {}).get("level", "UNKNOWN"),
        "key_issues": [{
            "code": "LEGACY_RESULT", "severity": "INFO",
            "message": "该报告由历史 v1 批次只读转换，未重新执行回测。",
        }],
    }


def _ranking_html(item: Mapping[str, Any], best_id: str | None) -> str:
    metrics = item.get("metrics", {})
    holdout = item.get("holdout_metrics", {})
    klass = "best" if item.get("combination_id") == best_id else ""
    cells = [
        item.get("rank"), item.get("combination_id"), item.get("status"),
        _fmt(item.get("score")), _pct(metrics.get("total_return")),
        _pct(metrics.get("annualized_return")), _pct(metrics.get("max_drawdown")),
        _fmt(metrics.get("sharpe_ratio")), _pct(metrics.get("win_rate")),
        _fmt(metrics.get("payoff_ratio")), metrics.get("trade_count"),
        _pct(holdout.get("total_return")), item.get("overfit_risk", {}).get("level"),
    ]
    rendered = "".join(f"<td>{escape(str(value if value is not None else '不可用'))}</td>" for value in cells)
    return f"<tr class='{klass}' data-id='{escape(str(item.get('combination_id', '')))}'>{rendered}</tr>"


def _fmt(value: Any) -> str:
    number = _finite(value)
    return "不可用" if number is None else f"{number:.3f}"


def _pct(value: Any) -> str:
    number = _finite(value)
    return "不可用" if number is None else f"{number * 100:.2f}%"


_CSS = """
:root{color-scheme:dark;--bg:#07111d;--panel:#0d1b2a;--line:#20364b;--text:#e5edf7;--muted:#91a5b8;--cyan:#38d5e8;--green:#45d483;--red:#ff647c;--amber:#f5b942}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 8% 0,#10304d 0,transparent 35%),var(--bg);color:var(--text);font:14px/1.55 Inter,"Segoe UI",sans-serif}main{max-width:1540px;margin:auto;padding:28px}header,.section-title{display:flex;justify-content:space-between;gap:24px;align-items:end}h1{font-size:34px;margin:6px 0}h2{font-size:23px;margin:28px 0 13px}h3{margin:0 0 10px}.eyebrow,.section-title span{color:var(--cyan);font-weight:700;letter-spacing:.12em}.status{display:grid;gap:5px;padding:14px 18px;border:1px solid var(--line);border-radius:12px;background:#0b1826}.status span,p{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px;margin:12px 0}.card,.panel{background:linear-gradient(145deg,#0f2031,#0a1724);border:1px solid var(--line);border-radius:13px;padding:15px}.card b{display:block;font-size:23px}.card span,dt{color:var(--muted)}dl{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px;margin:0}dd{margin:0;overflow-wrap:anywhere}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.wide{grid-column:1/-1}canvas{width:100%;height:300px;display:block}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse;background:#0a1724}th,td{padding:10px 12px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}th:nth-child(2),td:nth-child(2){text-align:left}tr:hover{background:#10253a}tr.best{box-shadow:inset 3px 0 var(--green)}pre{white-space:pre-wrap;max-height:430px;overflow:auto;color:#c5d5e7}select{background:#0b1826;color:var(--text);border:1px solid var(--line);padding:7px}.issues{display:grid;gap:9px;padding-left:18px}.issues li span{display:block;color:var(--muted)}.conclusion p:first-child{font-size:18px;color:var(--text)}@media(max-width:850px){main{padding:16px}.grid{grid-template-columns:1fr}header,.section-title{display:block}.wide{grid-column:auto}}
"""


_JS = r"""
(()=>{'use strict';const m=JSON.parse(document.getElementById('reportData').textContent),rank=m.ranking,charts=m.charts,best=m.best_combination_id,colors=['#38d5e8','#45d483','#f5b942','#ff647c','#9f7aea','#60a5fa','#fb923c','#f472b6','#a3e635','#c084fc'];const q=id=>document.getElementById(id),fmt=(v,p=false)=>v==null?'不可用':p?(Number(v)*100).toFixed(2)+'%':String(v),cards=[['组合数',m.basic_information.combination_count],['验证状态',m.basic_information.validation_status],['候选最优',best||'未确认'],['稳定组合',m.stable_combination_ids.length],['结果哈希',String(m.basic_information.result_hash||'').slice(0,12)]];q('summary').innerHTML=cards.map(x=>`<div class="card"><span>${x[0]}</span><b>${x[1]}</b></div>`).join('');q('lineage').innerHTML=Object.entries(m.basic_information).filter(([k])=>!['combination_count','validation_status'].includes(k)).map(([k,v])=>`<div><dt>${k}</dt><dd>${typeof v==='object'?JSON.stringify(v):v??'不可用'}</dd></div>`).join('');q('parameters').textContent=JSON.stringify(m.assumptions.parameter_space,null,2);q('assumptions').textContent=JSON.stringify({...m.assumptions,parameter_space:undefined},null,2);const select=q('curveSelect');charts.equity_series.forEach((s,i)=>{const o=document.createElement('option');o.value=s.combination_id;o.textContent=s.combination_id;o.selected=i<3;select.appendChild(o)});function chosen(){const ids=new Set([...select.selectedOptions].map(o=>o.value));return ids.size?ids:new Set(charts.equity_series.slice(0,1).map(x=>x.combination_id))}function setup(c){const r=c.getBoundingClientRect(),d=devicePixelRatio||1;if(r.width<2||r.height<2)return null;c.width=Math.round(r.width*d);c.height=Math.round(r.height*d);const x=c.getContext('2d');x.setTransform(d,0,0,d,0,0);x.clearRect(0,0,r.width,r.height);return{x,w:r.width,h:r.height}}function grid(s){const{x,w,h}=s;x.strokeStyle='#20364b';x.lineWidth=1;for(let i=1;i<5;i++){x.beginPath();x.moveTo(42,i*h/5);x.lineTo(w-10,i*h/5);x.stroke()}}function lineChart(id,series,key){const s=setup(q(id));if(!s)return;grid(s);const rows=series.filter(z=>chosen().has(z.combination_id)&&z.points.length),vals=rows.flatMap(z=>z.points.map(p=>Number(p[key])).filter(Number.isFinite));if(!vals.length)return;const lo=Math.min(...vals),hi=Math.max(...vals),span=hi-lo||1;rows.forEach((row,j)=>{s.x.strokeStyle=colors[j%colors.length];s.x.lineWidth=1.7;s.x.beginPath();row.points.forEach((p,i)=>{const px=44+i*(s.w-58)/Math.max(1,row.points.length-1),py=10+(hi-Number(p[key]))*(s.h-28)/span;i?s.x.lineTo(px,py):s.x.moveTo(px,py)});s.x.stroke()})}function bars(id,rows,valueKey,labelKey){const s=setup(q(id));if(!s||!rows.length)return;grid(s);const vals=rows.map(r=>Number(r[valueKey])||0),mx=Math.max(...vals.map(Math.abs),1),bw=(s.w-50)/rows.length;rows.forEach((r,i)=>{const v=vals[i],bh=Math.abs(v)*(s.h-55)/mx,px=42+i*bw;s.x.fillStyle=v>=0?'#45d483':'#ff647c';s.x.fillRect(px,v>=0?s.h/2-bh:s.h/2,Math.max(2,bw-3),bh);s.x.fillStyle='#91a5b8';if(rows.length<15)s.x.fillText(String(r[labelKey]??i),px,s.h-10)})}function scatter(){const s=setup(q('scatterChart')),rows=charts.risk_return.filter(r=>r.max_drawdown!=null&&r.annualized_return!=null);if(!s||!rows.length)return;grid(s);const mx=Math.max(...rows.map(r=>Math.abs(r.max_drawdown)),.01),ys=rows.map(r=>r.annualized_return),lo=Math.min(...ys,0),hi=Math.max(...ys,.01);rows.forEach(r=>{const px=35+Math.abs(r.max_drawdown)*(s.w-55)/mx,py=10+(hi-r.annualized_return)*(s.h-35)/(hi-lo||1);s.x.fillStyle=r.combination_id===best?'#45d483':r.qualified?'#38d5e8':'#66788a';s.x.beginPath();s.x.arc(px,py,r.combination_id===best?6:4,0,Math.PI*2);s.x.fill()})}function heat(){const s=setup(q('heatmapChart')),h=charts.parameter_heatmap,c=h.cells;if(!s||!c.length){if(s){s.x.fillStyle='#91a5b8';s.x.fillText('至少需要两个变化参数',20,30)}return}const xs=[...new Set(c.map(x=>JSON.stringify(x.x)))],ys=[...new Set(c.map(x=>JSON.stringify(x.y)))],mx=Math.max(...c.map(x=>Number(x.score)||0),1),cw=(s.w-70)/xs.length,ch=(s.h-45)/ys.length;c.forEach(v=>{const xi=xs.indexOf(JSON.stringify(v.x)),yi=ys.indexOf(JSON.stringify(v.y));s.x.fillStyle=`rgba(56,213,232,${.12+.82*(Number(v.score)||0)/mx})`;s.x.fillRect(55+xi*cw,10+yi*ch,cw-2,ch-2)});s.x.fillStyle='#91a5b8';s.x.fillText(h.x_parameter,55,s.h-8);s.x.save();s.x.translate(12,s.h-30);s.x.rotate(-Math.PI/2);s.x.fillText(h.y_parameter,0,0);s.x.restore()}function fold(){const row=charts.fold_stability.find(x=>x.combination_id===best)||charts.fold_stability[0],rows=row?row.folds:[];bars('foldChart',rows,'annualized_return','fold')}function side(){bars('sideChart',charts.side_contribution,'pnl','side')}function trades(){bars('tradeChart',charts.trade_distribution.pnl,'count','start')}function draw(){lineChart('equityChart',charts.equity_series,'equity');lineChart('drawdownChart',charts.drawdown_series,'drawdown');scatter();heat();fold();side();trades()}select.addEventListener('change',draw);document.querySelectorAll('tbody tr').forEach(tr=>tr.addEventListener('click',()=>{const c=rank.find(x=>x.combination_id===tr.dataset.id);q('details').textContent=JSON.stringify(c,null,2)}));let raf=0,retries=0;function schedule(){cancelAnimationFrame(raf);raf=requestAnimationFrame(()=>{draw();if(q('equityChart').getBoundingClientRect().width<2&&retries++<8)setTimeout(schedule,80)})}new ResizeObserver(schedule).observe(document.querySelector('main'));document.addEventListener('visibilitychange',()=>{if(!document.hidden)schedule()},{passive:true});window.addEventListener('pageshow',schedule,{passive:true});schedule()})();
"""


__all__ = [
    "BacktestReportModel", "REPORT_SCHEMA_VERSION", "build_report_model",
    "render_backtest_html",
]
