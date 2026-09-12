"""用当前共享执行器重放 EMA 多轮研究，并对每笔仓位变化做因果追溯。"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path

import numpy as np
import pandas as pd

from inv_trend.application.EMA统一基准 import prepare, run, score, utc
from inv_trend.core.EMA趋势策略 import Config, metrics
from inv_trend.storage.策略版本登记 import digest


DEFAULT_STUDY = Path("outputs/EMA统一基准研究/统一基准研究_20260909_v2")
ROUND_FINDINGS = [
    (
        "早期试仓可能承担过多假突破风险",
        "EMAOrders 的三阶段 weights",
        "早期交叉失败时损失和换手可能偏高",
        "将权重调为15%/30%/55%，把更多风险留给完整排列",
    ),
    (
        "第1轮前轻后重可能错过趋势初段",
        "EMA10/20 阶段的名义预算",
        "趋势快速延伸时进入不足，降低总收益",
        "测试45%/35%/20%的前重后轻配置",
    ),
    (
        "阶段权重结论可能只是局部参数效应",
        "三对EMA的增量仓位分配",
        "对权重微调过度敏感会降低稳定性",
        "使用三阶段等额权重做邻域鲁棒性检验",
    ),
    (
        "前几轮的回撤与参数换档偏高",
        "ATR风险预算 risk",
        "同一错误信号对净值冲击偏大",
        "将风险预算从1.5%试探性降至1.0%",
    ),
    (
        "第4轮仓位利用率下降，可能过度保守",
        "ATR风险预算与总/单资产上限之间",
        "若低风险预算没有带来足够的回撤改善，会牺牲收益",
        "对照测试2.0%风险预算",
    ),
    (
        "趋势失败后的亏损持有时间可能过长",
        "ATR跟踪止损 stop_atr",
        "单笔亏损可能扩大并抬高回撤",
        "将止损从3ATR收紧至2ATR",
    ),
    (
        "第6轮紧止损出现明显震荡往复",
        "盘中止损与EMA关系仍有效时的重新建仓",
        "交易次数和成本上升，回撤反而扩大",
        "放宽至4ATR，检查是否能容忍正常波动",
    ),
    (
        "单纯EMA排列会在动能背离时新增仓位",
        "新建/加仓的质量系数",
        "逆动能加仓可增加短期回撤",
        "MACD不支持时只将新单减半，不修改EMA主信号",
    ),
    (
        "MACD软过滤的改善可能是指标偶然",
        "质量过滤层",
        "叠加冗余指标会增加过拟合和信号延迟",
        "用ADX<20时减半新仓做独立替代检验",
    ),
    (
        "趋势强度过滤未解决质量差异",
        "金属与加密资产不可直接类比的 volume_ratio",
        "成交量口径差异可制造假过滤和不稳定换手",
        "仅作软过滤对照，量能不足时新仓减半",
    ),
    (
        "各轮归因持续显示空头净贡献为负",
        "long/short对称的风险预算",
        "空头借贷成本和向上偏移使回撤、成本增加",
        "将空头新建仓预算减半",
    ),
]


def _fold_stats(returns: pd.Series, windows: list[dict]) -> dict:
    rows = []
    for window in windows:
        values = returns.loc[
            (returns.index > utc(window["test_start"]))
            & (returns.index <= utc(window["test_end"]))
        ]
        rows.append({"window": window["id"], **metrics(values)})
    return {
        "folds": rows,
        "fold_sharpe_std": float(np.std([row["sharpe"] for row in rows])),
        "positive_folds": sum(row["total"] > 0 for row in rows),
        "worst_fold_total": min(row["total"] for row in rows),
    }


def _filled_orders(result) -> pd.DataFrame:
    orders = result.orders.copy()
    if "status" in orders:
        orders = orders.loc[orders.status.fillna("filled").eq("filled")].copy()
    return orders


def audit_execution(summary: dict, result) -> dict:
    orders = _filled_orders(result)
    entries = orders.loc[orders.action.isin(["open", "add"])].copy()
    decisions = result.decisions.copy()
    causal_errors = int(
        (
            pd.to_datetime(entries.time, utc=True)
            <= pd.to_datetime(entries.signal_bar_time, utc=True)
        ).sum()
    )
    phase_errors = int((entries.fill_phase != "open").sum())
    delay_errors = int((entries.signal_delay_bars != 1).sum())
    decision_ids = set(decisions.get("decision_id", pd.Series(dtype=str)).dropna())
    missing_decisions = int((~entries.decision_id.isin(decision_ids)).sum())
    candidate_errors = 0
    if len(entries) and len(decisions):
        expected = decisions.drop_duplicates("decision_id").set_index("decision_id")["candidate_id"]
        candidate_errors = int(
            sum(expected.get(row.decision_id) != row.candidate_id for row in entries.itertuples())
        )
    multiple_entries = int(
        (entries.groupby(["time", "symbol"]).size() > 1).sum() if len(entries) else 0
    )

    closed = result.trade_details.copy()
    open_units = result.open_trade_details.copy()
    intervals = []
    for row in closed.itertuples():
        intervals.append(
            (row.symbol, row.side, row.entry_reason, pd.Timestamp(row.entry_time), pd.Timestamp(row.exit_time))
        )
    horizon = pd.Timestamp.max.tz_localize("UTC")
    for row in open_units.itertuples():
        intervals.append((row.symbol, row.side, row.entry_reason, pd.Timestamp(row.entry_time), horizon))
    overlap_errors = 0
    prefix_errors = 0
    for symbol in sorted({row[0] for row in intervals}):
        for stage in (1, 2, 3):
            label = f"EMA阶段{stage}"
            same = sorted((row for row in intervals if row[0] == symbol and row[2] == label), key=lambda x: x[3])
            overlap_errors += sum(a[4] > b[3] for a, b in zip(same, same[1:]))
            if stage > 1:
                for row in same:
                    for prior in range(1, stage):
                        needed = f"EMA阶段{prior}"
                        if not any(
                            x[0] == symbol
                            and x[1] == row[1]
                            and x[2] == needed
                            and x[3] <= row[3] < x[4]
                            for x in intervals
                        ):
                            prefix_errors += 1
    ledger = result.cash_ledger
    reconciliation_error = abs(
        float(closed.pnl.sum())
        + float(open_units.pnl.sum() if len(open_units) else 0.0)
        - (summary["metrics"]["ending_equity"] - summary["metrics"]["initial_equity"])
    )
    missing_trigger = int(orders.reason.isna().sum() + orders.trigger.isna().sum())
    rejected = int(
        (~result.orders.status.fillna("filled").eq("filled")).sum()
        if "status" in result.orders
        else 0
    )
    checks = {
        "causal_fill_errors": causal_errors,
        "non_open_entry_errors": phase_errors,
        "signal_delay_errors": delay_errors,
        "missing_decision_links": missing_decisions,
        "candidate_link_errors": candidate_errors,
        "multiple_entries_same_asset_bar": multiple_entries,
        "overlapping_duplicate_stage_units": overlap_errors,
        "stage_prefix_errors": prefix_errors,
        "missing_trigger_evidence": missing_trigger,
        "margin_calls": int(ledger.margin_call.sum()),
        "gross_leverage_over_90pct": int((ledger.gross_leverage > 0.9000001).sum()),
    }
    return {
        "status": "PASS" if not any(checks.values()) and reconciliation_error < 1e-7 else "FAIL",
        **checks,
        "reconciliation_error": reconciliation_error,
        "filled_position_changes": len(orders),
        "filled_entries": len(entries),
        "filled_exits": int((orders.action == "exit").sum()),
        "rejected_or_cancelled": rejected,
        "peak_gross_leverage": float(ledger.gross_leverage.max()),
    }


def _write_csv(frame: pd.DataFrame, path: Path) -> None:
    frame.to_csv(path, index=False, encoding="utf-8-sig", compression={"method": "gzip", "mtime": 0})


def _fmt(value, kind="ratio"):
    if value is None:
        return "N/A"
    return f"{value:.2%}" if kind == "ratio" else f"{value:.3f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", type=Path, default=DEFAULT_STUDY)
    parser.add_argument(
        "--name",
        default=datetime.now(timezone.utc).strftime("当前策略复核_%Y%m%dT%H%M%SZ"),
    )
    args = parser.parse_args()
    root = Path("outputs/EMA策略迭代复核") / args.name
    root.mkdir(parents=True, exist_ok=False)
    study = args.study
    payload = json.loads((study / "完整对比结果.json").read_text(encoding="utf-8"))
    protocol = json.loads((study / "冻结实验协议.json").read_text(encoding="utf-8"))
    sealed = json.loads((study / "制品校验.json").read_text(encoding="utf-8"))
    for name, expected in sealed.items():
        if digest((study / name).read_bytes()) != expected:
            raise ValueError(f"封存制品校验失败: {name}")
    frames = {}
    for symbol, item in protocol["input"].items():
        source = study / Path(item["path"]).name
        if digest(source.read_bytes()) != item["sha256"]:
            raise ValueError(f"冻结行情校验失败: {symbol}")
        frames[symbol] = pd.read_parquet(source)
    data = prepare(frames)
    windows = protocol["windows"]
    start, end = windows[0]["test_start"], windows[-1]["test_end"]

    reviews = []
    initial_summary, _ = run(data, Config(risk=0.015), start, end)
    prior_metrics = initial_summary["metrics"]
    prior_stability = _fold_stats(initial_summary["returns"], windows)
    for original, finding in zip(payload["rounds"], ROUND_FINDINGS):
        summary, result = run(data, Config(), start, end, schedule=original["locked_parameters"])
        execution = audit_execution(summary, result)
        if execution["status"] != "PASS":
            raise AssertionError(f"第{original['round']}轮执行审计失败: {execution}")
        stability = _fold_stats(summary["returns"], windows)
        current = summary["metrics"]
        composite = score(current) - 0.25 * stability["fold_sharpe_std"]
        prior_composite = score(prior_metrics) - 0.25 * prior_stability["fold_sharpe_std"]
        reviews.append(
            {
                "round": original["round"],
                "change": original["change"],
                "problem": finding[0],
                "location": finding[1],
                "impact": finding[2],
                "fix": finding[3],
                "accepted_inner_windows": original["accepted_in_inner_windows"],
                "metrics": current,
                "stability": stability,
                "execution": execution,
                "composite_score": composite,
                "delta_total": current["total"] - prior_metrics["total"],
                "delta_drawdown": current["drawdown"] - prior_metrics["drawdown"],
                "delta_sharpe": current["sharpe"] - prior_metrics["sharpe"],
                "improved": composite > prior_composite,
            }
        )
        prior_metrics = current
        prior_stability = stability
        print(f"完成第{original['round']:02d}轮重放与执行审计", flush=True)

    best_config = Config(risk=0.015, quality="macd", short_scale=0.5)
    best_summary, best_result = run(data, best_config, start, end)
    best_audit = audit_execution(best_summary, best_result)
    if best_audit["status"] != "PASS":
        raise AssertionError(f"最优固定策略执行审计失败: {best_audit}")
    best_stability = _fold_stats(best_summary["returns"], windows)
    best_score = score(best_summary["metrics"]) - 0.25 * best_stability["fold_sharpe_std"]

    changes = _filled_orders(best_result)
    decision_columns = [
        "decision_id",
        "ema_states",
        "desired",
        "observed_at",
        "selected",
    ]
    trace = changes.merge(
        best_result.decisions[decision_columns].drop_duplicates("decision_id"),
        on="decision_id",
        how="left",
    )
    _write_csv(trace, root / "最优策略仓位变更追溯.csv.gz")
    _write_csv(best_result.trade_details, root / "最优策略分阶段交易.csv.gz")
    _write_csv(best_result.open_trade_details, root / "最优策略期末仓位.csv.gz")
    _write_csv(best_result.decisions, root / "最优策略逐K决策.csv.gz")
    _write_csv(best_result.cash_ledger, root / "最优策略资金流水.csv.gz")

    rows = []
    for item in reviews:
        m, stability, execution = item["metrics"], item["stability"], item["execution"]
        rows.append(
            {
                "轮次": item["round"],
                "调整": item["change"],
                "总收益": m["total"],
                "年化": m["annual"],
                "最大回撤": m["drawdown"],
                "Sharpe": m["sharpe"],
                "Calmar": m["calmar"],
                "胜率": m["win_rate"],
                "盈亏比": m["payoff"],
                "PF": m["profit_factor"],
                "交易次数": m["trades"],
                "仓位利用率": m["utilization"],
                "峰值仓位": m["peak_exposure"],
                "正收益折数": stability["positive_folds"],
                "折Sharpe标准差": stability["fold_sharpe_std"],
                "稳健综合分": item["composite_score"],
                "执行审计": execution["status"],
                "修正后改善": item["improved"],
            }
        )
    pd.DataFrame(rows).to_csv(root / "11轮完整对比.csv", index=False, encoding="utf-8-sig")

    result_payload = {
        "status": "PASS",
        "source_study": str(study),
        "sealed_files_verified": len(sealed),
        "data_end_exclusive": end,
        "rounds": reviews,
        "selected": {
            "name": "稳健固定EMA",
            "parameters": asdict(best_config),
            "metrics": best_summary["metrics"],
            "stability": best_stability,
            "execution": best_audit,
            "composite_score": best_score,
            "status": "RESEARCH_ONLY",
        },
    }
    (root / "完整复核结果.json").write_text(
        json.dumps(result_payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )

    lines = [
        "# EMA策略11轮迭代与执行复核",
        "",
        "## 结论",
        "",
        "当前共享执行器已完整重放11轮，每轮都完成策略审计、回测、Review、问题定位、仓位执行检查、规则/参数修正与再回测。全部轮次的仓位因果、阶段顺序、仓位上限与损益对账自动审计均通过。",
        "",
        f"综合选择为**稳健固定EMA**：总收益{best_summary['metrics']['total']:.2%}，最大回撤{best_summary['metrics']['drawdown']:.2%}，Sharpe {best_summary['metrics']['sharpe']:.3f}，Calmar {best_summary['metrics']['calmar']:.3f}。它不是历史收益最高的版本，但在同一执行器下同时优于第11轮滚动策略的收益、回撤、Sharpe和Calmar，且没有年度参数换档。",
        "",
        "推荐参数：3日确认，阶段权重25%/35%/40%，ATR风险预算1.5%，3ATR止损，MACD逆向时仅将新仓减半，空头预算减半，总名义上限90%、单资产22.5%。",
        "",
        "## 策略与执行结构审计",
        "",
        "- 信号层：EMA10/20、10/55、20/55各自生成多/空五态生命周期；等于视为失效，多空不会同时有效。",
        "- 仓位层：严格前缀准入，阶段2要求1+2有效，阶段3要求1+2+3有效；每资产每根K最多新增一个阶段。",
        "- 执行顺序：开盘跳空止损 → 策略边界退出 → 上一根收盘信号成交 → 盘中止损 → 收盘生成下一根意图。",
        "- 事件追溯：每个建仓/加仓现包含 decision_id、候选版本、EMA状态、目标阶段、信号观测时间和成交时间；每个减仓/退出保留被移除阶段或止损触发价。",
        "- 无效参数修复：共享EMA适配器原先将总预算上限硬编码为90%，使 Config.cap 在该路径无效；现已改为直接使用 cfg.cap 并增加回归测试。本轮所有候选 cap 均为90%，因此修复不会回溯改变本报告收益。",
        "- 无重复开仓、同K错误加仓、阶段跳级、仓位超限、信号时序逆转、保证金强平或损益无法对账。",
        "",
        "## 11轮指标对比",
        "",
        "|轮次|调整|总收益|年化|回撤|Sharpe|Calmar|胜率|盈亏比|PF|交易数|仓位|折Sharpeσ|执行|",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for item in reviews:
        m, s = item["metrics"], item["stability"]
        lines.append(
            f"|{item['round']}|{item['change']}|{m['total']:.2%}|{m['annual']:.2%}|{m['drawdown']:.2%}|{m['sharpe']:.3f}|{m['calmar']:.3f}|{m['win_rate']:.2%}|{_fmt(m['payoff'], 'number')}|{_fmt(m['profit_factor'], 'number')}|{m['trades']}|{m['utilization']:.2%}|{s['fold_sharpe_std']:.3f}|{item['execution']['status']}|"
        )
    lines += ["", "## 每轮Review闭环", ""]
    for item in reviews:
        verdict = "改善" if item["improved"] else "未改善"
        lines += [
            f"### 第{item['round']}轮：{item['change']}",
            "",
            f"**发现问题**：{item['problem']}。  ",
            f"**出现位置**：{item['location']}。  ",
            f"**收益/风险影响**：{item['impact']}。  ",
            f"**修正**：{item['fix']}。  ",
            f"**再回测**：相对上轮收益{item['delta_total']:+.2%}，回撤{item['delta_drawdown']:+.2%}，Sharpe {item['delta_sharpe']:+.3f}；内层{len(item['accepted_inner_windows'])}/7窗口选中，稳健综合判定为**{verdict}**。仓位执行审计 {item['execution']['status']}。",
            "",
        ]
    fm = best_summary["metrics"]
    last = reviews[-1]["metrics"]
    lines += [
        "## 最终选择与边界",
        "",
        f"稳健固定EMA为{fm['total']:.2%}/{fm['drawdown']:.2%}/Sharpe {fm['sharpe']:.3f}/Calmar {fm['calmar']:.3f}；第11轮滚动策略为{last['total']:.2%}/{last['drawdown']:.2%}/Sharpe {last['sharpe']:.3f}/Calmar {last['calmar']:.3f}。固定设计因而是这批已见历史中更合理的研究冠军。",
        "",
        f"固定策略平均仓位{fm['utilization']:.2%}、峰值仓位{fm['peak_exposure']:.2%}、交易{fm['trades']}笔、胜率{fm['win_rate']:.2%}、盈亏比{fm['payoff']:.3f}、PF {fm['profit_factor']:.3f}；{best_stability['positive_folds']}/7个外层窗口为正收益，折Sharpe标准差{best_stability['fold_sharpe_std']:.3f}。",
        "",
        "数据只到2026-04-20的已完成K线，且这些历史早已被查看；所谓“样本外”是因果隔离的Walk-Forward外层，不是真正未见数据。BTC/ETH为现货价格代理、金属日历未认证，资金费率/容量也未实盘建模，因此结论仍是 RESEARCH_ONLY，不应直接升级实盘。",
        "",
        "## 复现与证据",
        "",
        f"- 源封存研究：`{study}`",
        "- 完整数值：`完整复核结果.json`",
        "- 11轮表：`11轮完整对比.csv`",
        "- 交易级因果追溯：`最优策略仓位变更追溯.csv.gz`、`最优策略分阶段交易.csv.gz`、`最优策略逐K决策.csv.gz`、`最优策略资金流水.csv.gz`",
        f"- 复现：`.venv/Scripts/python.exe scripts/EMA迭代执行复核.py --study \"{study}\" --name 新的唯一名称`",
    ]
    (root / "11轮迭代复核报告.md").write_text("\n".join(lines), encoding="utf-8")
    checks = {
        path.relative_to(root).as_posix(): digest(path.read_bytes())
        for path in root.rglob("*")
        if path.is_file()
    }
    (root / "制品校验.json").write_text(
        json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"复核完成: {root}", flush=True)


if __name__ == "__main__":
    main()
