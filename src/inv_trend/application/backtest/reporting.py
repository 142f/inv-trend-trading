"""Server-side report projection and self-contained HTML rendering."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from html import escape
import json
import math
from importlib.resources import files
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
        "signal_series": {
            bundle.get("signal_parameter_hash"): [
                {key: event.get(key) for key in (
                    "signal_time", "symbol", "system", "direction", "signal_type",
                    "channel_value", "trigger_price", "n",
                )} for event in bundle.get("events", [])
            ] for bundle in raw.get("signal_bundles", [])
        },
        "detail_series": [
            {"combination_id": item.get("combination_id"),
             "trades": item.get("trades", []), "orders": item.get("orders", []),
             "signal_parameter_hash": item.get("signal_parameter_hash")}
            for item in selected
        ],
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
        "window_boundary_policy": plan.get(
            "window_boundary_policy", "mark_to_market"
        ),
        "parameter_space": plan.get("parameter_space", {}),
        "constraints": plan.get("constraints", []),
        "validation": plan.get("validation", {}),
        "ranking": plan.get("ranking", {}),
        "comparison_assumptions_hash": raw.get("comparison_assumptions_hash"),
        "execution": "收盘形成信号 → 下一可交易开盘执行；开盘穿越保护止损优先",
        "costs": "手续费与滑点按名义金额计入现金成本；不重复调整成交价",
        "holdout": "候选与邻域仅由验证集选择，最终留出仅作确认，不用于换选参数",
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
<title>统一策略回测报告</title><style>{_CSS}</style></head><body>
<nav aria-label="报告导航"><b>趋势研究 / 回测审计</b><a href="#overview">概览</a><a href="#performance">曲线</a><a href="#comparison">参数对比</a><a href="#ledger">交易与信号</a><a href="#methodology">方法与证据</a></nav><main>
<header><div><span class="eyebrow">STRATEGY BACKTEST · V2</span><h1>统一策略回测与参数比较报告</h1>
<p>先看样本外风险，再看收益。排名依据验证集，最终留出不参与调参。</p></div>
<div class="status"><b>{escape(str(model.conclusion.get('status', 'UNKNOWN')))}</b>
<span>候选：{escape(str(model.best_combination_id or '未确认'))}</span></div></header>
<section id="overview"><h2>回测基本信息</h2><div class="cards" id="summary"></div>
<div class="toolbar"><label>分析组合 <select id="activeSelect"></select></label><span id="activeStatus" role="status"></span></div>
<div class="cards" id="metricCards"></div><p>指标卡分别标明全样本、验证集与留出集；下方资金及回撤曲线展示全样本。</p></section>
<section id="performance"><div class="section-title"><div><span>组合对比</span><h2>收益、回撤与稳定性</h2></div>
<label>叠加组合 <select id="curveSelect" multiple size="1"></select></label></div>
<div class="toolbar"><label>开始 <input id="startDate" type="date"></label><label>结束 <input id="endDate" type="date"></label><button id="resetRange">全部历史</button><span id="rangeStatus" role="status"></span></div>
<div id="chartLegend" class="legend"></div><div class="grid"><div class="panel wide"><h3>收益曲线 · 账户权益</h3><canvas id="equityChart" aria-label="全样本权益时间序列"></canvas></div>
<div class="panel"><h3>回撤曲线</h3><canvas id="drawdownChart"></canvas></div>
<div class="panel"><h3>样本外风险收益</h3><canvas id="scatterChart"></canvas></div>
<div class="panel"><h3>参数热力图</h3><canvas id="heatmapChart"></canvas></div>
<div class="panel"><h3>分折稳定性</h3><canvas id="foldChart"></canvas></div></div></section>
<section id="comparison"><h2>核心指标与超参数横向比较</h2><p>排名来自验证集综合分；总收益、年化、回撤、Sharpe、胜率和交易数为全样本指标。点击组合联动交易记录。</p><div class="table-wrap"><table><thead><tr>
<th>排名</th><th>组合</th><th>状态</th><th>综合分</th><th>总收益</th><th>年化收益</th><th>最大回撤</th>
<th>Sharpe</th><th>胜率</th><th>盈亏比</th><th>交易数</th><th>留出收益</th><th>风险</th></tr></thead><tbody>{rows}</tbody></table></div></section>
<section><h2>多空表现与交易分布</h2><div class="grid"><div class="panel"><h3>多头 / 空头贡献</h3><canvas id="sideChart"></canvas></div>
<div class="panel"><h3>交易盈亏分布</h3><canvas id="tradeChart"></canvas></div></div></section>
<section id="ledger"><h2>交易记录与信号执行</h2><div class="panel">
<div class="toolbar"><button data-ledger="trades" aria-pressed="true">已平仓交易</button><button data-ledger="orders" aria-pressed="false">执行订单</button><button data-ledger="signals" aria-pressed="false">策略信号</button>
<label>筛选 <input id="ledgerSearch" type="search" placeholder="品种、方向、原因或状态"></label><button id="exportLedger">导出筛选 CSV</button></div>
<p id="ledgerDescription"></p><div class="table-wrap"><table><thead id="ledgerHead"></thead><tbody id="ledgerBody"></tbody></table></div>
<div class="toolbar"><button id="prevPage">上一页</button><span id="pageStatus" aria-live="polite"></span><button id="nextPage">下一页</button></div></div></section>
<section><h2>最终回测结论和关键问题</h2><div class="grid"><div class="panel conclusion"><p>{escape(str(model.conclusion.get('summary', '暂无结论。')))}</p>
<p>稳定组合：{escape(', '.join(model.stable_combination_ids) or '未形成稳定区间')}</p></div>
<div class="panel"><ul class="issues">{issue_html}</ul></div></div></section>
<section id="methodology"><h2>方法、假设与完整证据</h2><div class="panel"><dl id="lineage"></dl></div>
<div class="grid"><details class="panel"><summary>参数空间</summary><pre id="parameters"></pre></details><details class="panel"><summary>验证与固定假设</summary><pre id="assumptions"></pre></details></div>
<details class="panel"><summary>当前组合完整证据</summary><pre id="details"></pre></details></section>
</main><div id="chartTooltip" role="status" hidden></div><script id="reportData" type="application/json">{data}</script><script>{_JS}</script></body></html>"""


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


_CSS = files(__package__).joinpath("报告样式.css").read_text(encoding="utf-8")
_JS = files(__package__).joinpath("报告交互.js").read_text(encoding="utf-8")


__all__ = [
    "BacktestReportModel", "REPORT_SCHEMA_VERSION", "build_report_model",
    "render_backtest_html",
]
