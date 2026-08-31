"""Immutable Chinese-named artifacts for strategy-backtest batches."""

from __future__ import annotations

import hashlib
from html import escape
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping

import pandas as pd

from .models import BacktestBatchResult


class BacktestArtifactWriter:
    def __init__(self, output_root: str | Path = "outputs/strategy_backtest") -> None:
        self.output_root = Path(output_root)

    def write(
        self,
        batch: BacktestBatchResult,
        *,
        lineage: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, str]:
        run_root = self.output_root / "runs" / batch.report_date / batch.run_id
        if run_root.exists():
            raise FileExistsError(f"immutable backtest run already exists: {run_root}")
        inputs = run_root / "01_输入"
        combinations_root = run_root / "02_参数组合"
        exports = run_root / "03_汇总导出"
        reports = run_root / "04_可视化报告"
        audit = run_root / "05_审计"
        for directory in (inputs, combinations_root, exports, reports, audit):
            directory.mkdir(parents=True, exist_ok=False)
        prefix = _prefix(batch)
        _write_json(inputs / "回测输入清单_v1.json", {
            "source": batch.source.to_dict(), "plan": batch.plan.to_dict(),
            "lineage": lineage,
        })
        _write_json(inputs / "参数组合展开_v1.json", {
            "combination_count": len(batch.combinations),
            "combinations": [
                {"combination_id": item.combination_id, "parameters": item.parameters}
                for item in batch.combinations
            ],
        })
        for bundle in batch.signal_bundles:
            _write_json(
                inputs / f"信号投影_{bundle.signal_parameter_hash[:12]}_v1.json",
                bundle.to_dict(),
            )
        summary_rows: list[dict[str, Any]] = []
        all_trades: list[dict[str, Any]] = []
        for item in batch.combinations:
            directory = combinations_root / item.combination_id
            directory.mkdir()
            _write_json(directory / "组合结果_v1.json", item.to_dict())
            pd.DataFrame(item.equity_curve).to_csv(
                directory / "权益曲线_v1.csv", index=False, encoding="utf-8-sig"
            )
            pd.DataFrame(item.drawdown_curve).to_csv(
                directory / "回撤曲线_v1.csv", index=False, encoding="utf-8-sig"
            )
            trade_frame = pd.DataFrame(item.trades)
            trade_frame.to_parquet(directory / "交易明细_v1.parquet", index=False)
            pd.DataFrame(item.orders).to_csv(
                directory / "订单明细_v1.csv", index=False, encoding="utf-8-sig"
            )
            summary_rows.append(_summary_row(item))
            all_trades.extend(
                {"combination_id": item.combination_id, **dict(row)} for row in item.trades
            )
        metrics_path = exports / f"{prefix}_参数组合指标_v1.csv"
        trades_path = exports / f"{prefix}_交易明细_v1.parquet"
        pd.DataFrame(summary_rows).to_csv(metrics_path, index=False, encoding="utf-8-sig")
        pd.DataFrame(all_trades).to_parquet(trades_path, index=False)
        _write_json(exports / f"{prefix}_批次结果_v1.json", batch.to_dict())
        report_path = reports / f"{prefix}_参数比较报告_v1.html"
        report_path.write_text(render_backtest_html(batch), encoding="utf-8")
        manifest = {
            "schema_version": "1", "run_id": batch.run_id,
            "report_date": batch.report_date, "result_hash": batch.result_hash,
            "source_hash": batch.source.source_hash,
            "comparison_assumptions_hash": batch.comparison_assumptions_hash,
            "strategy_version": batch.source.strategy_version,
            "data_versions": {item.symbol: item.dataset_version for item in batch.source.instruments},
            "generated_at": batch.generated_at,
        }
        _write_json(audit / "可复现运行清单_v1.json", manifest)
        index = {
            **manifest,
            "validation_status": batch.validation_status,
            "best_combination_id": batch.best_combination_id,
            "stable_combination_ids": list(batch.stable_combination_ids),
            "combination_count": len(batch.combinations),
            "report": str(report_path.relative_to(run_root)).replace("\\", "/"),
            "metrics": str(metrics_path.relative_to(run_root)).replace("\\", "/"),
        }
        index_path = run_root / "回测批次索引_v1.json"
        _write_json(index_path, index)
        hashes = _artifact_hashes(run_root, exclude={"文件哈希_v1.json"})
        _write_json(audit / "文件哈希_v1.json", hashes)
        self._update_latest(run_root, report_path, index_path)
        return {
            "run_directory": str(run_root.resolve()),
            "report": str(report_path.resolve()),
            "index": str(index_path.resolve()),
            "metrics": str(metrics_path.resolve()),
            "trades": str(trades_path.resolve()),
        }

    def _update_latest(self, run_root: Path, report: Path, index: Path) -> None:
        latest = self.output_root / "latest"
        latest.mkdir(parents=True, exist_ok=True)
        _atomic_copy(report, latest / "最新参数比较报告.html")
        _atomic_copy(index, latest / "最新回测批次索引.json")
        _write_json(self.output_root / "latest.json", {
            "run_directory": str(run_root.resolve()),
            "report": str(report.resolve()),
            "index": str(index.resolve()),
        })


def render_backtest_html(batch: BacktestBatchResult) -> str:
    data = json.dumps(batch.to_dict(), ensure_ascii=False, allow_nan=False).replace("</", "<\\/")
    rows = "".join(_ranking_row(item, batch.best_combination_id) for item in batch.combinations)
    status = "验证就绪" if batch.validation_status == "READY" else "历史不足，仅输出诊断"
    best = batch.best_combination_id or "没有通过独立留出验证的最优参数"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>策略参数回测比较</title><style>{_CSS}</style></head>
<body><main><header><div><span class="eyebrow">STRATEGY BACKTEST · V1</span><h1>策略参数回测与稳定性审计</h1>
<p>数据、策略证据、成交假设和评价口径均由不可变批次绑定；图表只展示服务端结果。</p></div>
<div class="status"><b>{escape(status)}</b><span>候选：{escape(best)}</span></div></header>
<section class="cards" id="summary"></section>
<section><div class="section-title"><div><span>组合比较</span><h2>样本外排名与风险收益</h2></div>
<label>显示组合 <select id="curveSelect" multiple size="1"></select></label></div>
<div class="grid"><div class="panel wide"><h3>权益曲线（全样本）</h3><canvas id="equityChart"></canvas></div>
<div class="panel"><h3>回撤曲线</h3><canvas id="drawdownChart"></canvas></div>
<div class="panel"><h3>样本外风险收益</h3><canvas id="scatterChart"></canvas></div>
<div class="panel"><h3>参数稳定性热力图</h3><canvas id="heatmapChart"></canvas></div>
<div class="panel"><h3>候选组合多空贡献</h3><canvas id="sideChart"></canvas></div></div></section>
<section><h2>参数组合排名</h2><div class="table-wrap"><table><thead><tr><th>排名</th><th>组合</th><th>状态</th><th>综合分</th><th>OOS Sharpe</th><th>OOS CAGR</th><th>OOS 最大回撤</th><th>交易数</th><th>稳定性</th><th>留出收益</th><th>过拟合</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<section class="grid"><div class="panel"><h3>参数与分折证据</h3><pre id="details">点击排名表中的组合查看完整证据。</pre></div>
<div class="panel"><h3>解释口径</h3><ul><li>综合分仅使用滚动验证期，不使用最终留出集调参。</li><li>留出未通过时不宣称最优参数。</li><li>无定义指标显示为“不可用”，不会替换成 0。</li><li>0–100 分为横向证据评分，不是盈利概率。</li></ul></div></section>
</main><script id="batchData" type="application/json">{data}</script><script>{_JS}</script></body></html>"""


def _ranking_row(item: Any, best_id: str | None) -> str:
    values = item.validation_metrics
    classes = "best" if item.combination_id == best_id else ""
    return (
        f"<tr class='{classes}' data-id='{escape(item.combination_id)}'>"
        f"<td>{item.rank or '—'}</td><td><button>{escape(item.combination_id)}</button></td>"
        f"<td>{escape(item.status)}</td><td>{_fmt(item.score)}</td>"
        f"<td>{_fmt(values.get('sharpe'))}</td><td>{_pct(values.get('cagr'))}</td>"
        f"<td>{_pct(values.get('max_drawdown'))}</td><td>{values.get('trade_count', '—')}</td>"
        f"<td>{_fmt(item.stability_score)}</td><td>{_pct(item.holdout_metrics.get('total_return'))}</td>"
        f"<td>{escape(str(item.overfit_risk.get('level', '—')))}</td></tr>"
    )


def _summary_row(item: Any) -> dict[str, Any]:
    return {
        "rank": item.rank, "combination_id": item.combination_id,
        "status": item.status, "qualified": item.qualified, "score": item.score,
        "stability_score": item.stability_score,
        **{f"full_{key}": value for key, value in item.metrics.items()},
        **{f"oos_{key}": value for key, value in item.validation_metrics.items()},
        **{f"holdout_{key}": value for key, value in item.holdout_metrics.items()},
        "parameters": json.dumps(item.parameters, ensure_ascii=False, sort_keys=True),
        "overfit_risk": item.overfit_risk.get("level"),
    }


def _prefix(batch: BacktestBatchResult) -> str:
    symbols = "_".join(item.symbol for item in batch.source.instruments)
    date = batch.report_date.replace("-", "")
    return f"{symbols}_D1_{date}_{batch.run_id}_corrected-v2"


def _fmt(value: Any) -> str:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "不可用"


def _pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "不可用"


def _write_json(path: Path, value: Any) -> None:
    encoded = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=str) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)


def _atomic_copy(source: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, target)


def _artifact_hashes(root: Path, exclude: set[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item.name not in exclude):
        values[str(path.relative_to(root)).replace("\\", "/")] = hashlib.sha256(path.read_bytes()).hexdigest()
    return values


_CSS = """
:root{color-scheme:dark;--bg:#07111d;--panel:#0d1b2a;--line:#20364b;--text:#e5edf7;--muted:#8da2b8;--cyan:#38d5e8;--green:#45d483;--red:#ff647c;--amber:#f5b942}*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#102943 0,transparent 35%),var(--bg);color:var(--text);font:14px/1.5 Inter,"Segoe UI",sans-serif}main{max-width:1540px;margin:auto;padding:28px}header,.section-title{display:flex;justify-content:space-between;gap:24px;align-items:end}h1{font-size:34px;margin:6px 0}h2{font-size:22px;margin:5px 0 16px}h3{margin:0 0 10px}.eyebrow,.section-title span{color:var(--cyan);font-weight:700;letter-spacing:.12em}.status{display:grid;gap:5px;padding:14px 18px;border:1px solid var(--line);border-radius:12px;background:#0b1826}.status span,p{color:var(--muted)}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin:25px 0}.card,.panel{background:linear-gradient(145deg,#0f2031,#0a1724);border:1px solid var(--line);border-radius:13px;padding:15px}.card b{display:block;font-size:24px}.card span{color:var(--muted)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.wide{grid-column:1/-1}canvas{width:100%;height:300px;display:block}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:12px}table{width:100%;border-collapse:collapse;background:#0a1724}th,td{padding:10px 12px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap}th:nth-child(2),td:nth-child(2){text-align:left}tr:hover{background:#10253a}tr.best{box-shadow:inset 3px 0 var(--green)}button{border:0;background:none;color:var(--cyan);cursor:pointer}pre{white-space:pre-wrap;max-height:430px;overflow:auto;color:#c5d5e7}select{background:#0b1826;color:var(--text);border:1px solid var(--line);padding:7px}@media(max-width:850px){main{padding:16px}.grid{grid-template-columns:1fr}header,.section-title{display:block}.wide{grid-column:auto}}
"""

_JS = r"""
(()=>{'use strict';const data=JSON.parse(document.getElementById('batchData').textContent);const combos=data.combinations;const best=data.best_combination_id;const cards=[['组合数',combos.length],['验证状态',data.validation_status],['候选最优',best||'未通过'],['稳定组合',data.stable_combination_ids.length],['结果哈希',data.result_hash.slice(0,12)]];document.getElementById('summary').innerHTML=cards.map(x=>`<div class="card"><span>${x[0]}</span><b>${x[1]}</b></div>`).join('');const select=document.getElementById('curveSelect');combos.slice(0,10).forEach((c,i)=>{const o=document.createElement('option');o.value=c.combination_id;o.textContent=`${c.rank||'—'} · ${c.combination_id}`;o.selected=i<3;select.appendChild(o)});const colors=['#38d5e8','#45d483','#f5b942','#ff647c','#9f7aea','#60a5fa','#fb923c','#f472b6','#a3e635','#c084fc'];function chosen(){const ids=new Set([...select.selectedOptions].map(o=>o.value));return combos.filter(c=>ids.has(c.combination_id))}function setup(canvas){const dpr=devicePixelRatio||1,r=canvas.getBoundingClientRect();canvas.width=Math.max(1,r.width*dpr);canvas.height=Math.max(1,r.height*dpr);const x=canvas.getContext('2d');x.setTransform(dpr,0,0,dpr,0,0);return{x,w:r.width,h:r.height}}function lines(id,key,valueKey){const c=document.getElementById(id),{x,w,h}=setup(c),series=chosen().map(q=>q[key]).filter(s=>s.length);x.clearRect(0,0,w,h);x.strokeStyle='#20364b';for(let i=1;i<5;i++){x.beginPath();x.moveTo(45,i*h/5);x.lineTo(w-10,i*h/5);x.stroke()}const vals=series.flat().map(r=>Number(r[valueKey])).filter(Number.isFinite);if(!vals.length)return;const lo=Math.min(...vals),hi=Math.max(...vals),span=hi-lo||1;series.forEach((s,j)=>{x.strokeStyle=colors[j%colors.length];x.lineWidth=1.6;x.beginPath();s.forEach((r,i)=>{const px=45+i*(w-60)/Math.max(1,s.length-1),py=10+(hi-Number(r[valueKey]))*(h-30)/span;i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke()})}function scatter(){const c=document.getElementById('scatterChart'),{x,w,h}=setup(c),rows=combos.filter(q=>q.qualified&&Number.isFinite(q.validation_metrics.max_drawdown)&&Number.isFinite(q.validation_metrics.cagr));x.clearRect(0,0,w,h);const xs=rows.map(q=>Math.abs(q.validation_metrics.max_drawdown)),ys=rows.map(q=>q.validation_metrics.cagr),mx=Math.max(...xs,0.01),my=Math.max(...ys,0.01),mn=Math.min(...ys,0);rows.forEach((q,i)=>{const px=35+xs[i]*(w-55)/mx,py=10+(my-ys[i])*(h-35)/(my-mn||1);x.fillStyle=q.combination_id===best?'#45d483':'#38d5e8';x.beginPath();x.arc(px,py,q.combination_id===best?6:4,0,Math.PI*2);x.fill()})}function heatmap(){const {x,w,h}=setup(document.getElementById('heatmapChart')),keys=[...new Set(combos.flatMap(c=>Object.keys(c.parameters)))].filter(k=>new Set(combos.map(c=>c.parameters[k])).size>1).slice(0,2);x.clearRect(0,0,w,h);if(keys.length<2){x.fillStyle='#8da2b8';x.fillText('至少需要两个变化参数',20,30);return}const xs=[...new Set(combos.map(c=>c.parameters[keys[0]]))],ys=[...new Set(combos.map(c=>c.parameters[keys[1]]))],cw=(w-70)/xs.length,ch=(h-45)/ys.length,max=Math.max(...combos.map(c=>Number(c.score)||0),1);ys.forEach((yv,yi)=>xs.forEach((xv,xi)=>{const rows=combos.filter(c=>c.parameters[keys[0]]===xv&&c.parameters[keys[1]]===yv),v=rows.reduce((s,c)=>s+(Number(c.score)||0),0)/Math.max(rows.length,1);x.fillStyle=`rgba(56,213,232,${.12+.82*v/max})`;x.fillRect(55+xi*cw,10+yi*ch,cw-2,ch-2)}));x.fillStyle='#8da2b8';x.fillText(keys[0],55,h-8);x.save();x.translate(12,h-30);x.rotate(-Math.PI/2);x.fillText(keys[1],0,0);x.restore()}function sides(){const {x,w,h}=setup(document.getElementById('sideChart')),c=combos.find(q=>q.combination_id===best)||combos.find(q=>q.rank===1)||combos[0];x.clearRect(0,0,w,h);if(!c)return;const vals=[Number(c.long_metrics.pnl)||0,Number(c.short_metrics.pnl)||0],mx=Math.max(...vals.map(Math.abs),1);vals.forEach((v,i)=>{const bw=w*.25,px=w*(i?.62:.18),bh=Math.abs(v)*(h-55)/mx;x.fillStyle=i?'#ff647c':'#45d483';x.fillRect(px,v>=0?h/2-bh:h/2,bw,bh);x.fillStyle='#8da2b8';x.fillText(i?'空头':'多头',px,h-15)});x.strokeStyle='#20364b';x.beginPath();x.moveTo(15,h/2);x.lineTo(w-15,h/2);x.stroke()}function draw(){lines('equityChart','equity_curve','equity');lines('drawdownChart','drawdown_curve','drawdown');scatter();heatmap();sides()}select.addEventListener('change',draw);document.querySelectorAll('tbody tr').forEach(tr=>tr.addEventListener('click',()=>{const c=combos.find(x=>x.combination_id===tr.dataset.id);document.getElementById('details').textContent=JSON.stringify({parameters:c.parameters,validation:c.validation_metrics,holdout:c.holdout_metrics,folds:c.folds,overfit:c.overfit_risk,unavailable:c.unavailable},null,2)}));let raf=0;new ResizeObserver(()=>{cancelAnimationFrame(raf);raf=requestAnimationFrame(draw)}).observe(document.querySelector('main'));draw()})();
"""


__all__ = ["BacktestArtifactWriter", "render_backtest_html"]
