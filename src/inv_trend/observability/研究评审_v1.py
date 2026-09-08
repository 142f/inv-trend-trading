"""Pure HTML projection from a structured research result; never recalculates signals."""
from __future__ import annotations

from html import escape
import json

NAMES = {"momentum": "时间序列动量", "breakout": "通道突破", "reversion": "均值回归"}


def render_study(report: dict) -> str:
    def percent(value):
        return "—" if value is None else f"{value:.2%}"
    def number(value):
        return "—" if value is None else f"{value:.3f}"
    rows = []
    scores = []
    for family, review in report["families"].items():
        m = review["metrics"]
        rows.append("<tr>" + "".join(f"<td>{escape(str(x))}</td>" for x in
                   (NAMES[family], percent(m["total_return"]), percent(m["annualized_return"]),
                    percent(m["max_drawdown"]), number(m["sharpe_ratio"]), m["fill_count"],
                    percent(review["oos_positive_window_ratio"]), number(review["unweighted_mean"]))) + "</tr>")
        scores.append("<tr><th>" + NAMES[family] + "</th>" + "".join(
            f"<td>{value:.2f}</td>" for value in review["scores_0_to_5"].values()) + "</tr>")
    baseline = report.get("buy_hold_reference", {})
    axes = next(iter(report["families"].values()))["scores_0_to_5"].keys()
    safe_json = escape(json.dumps(report, ensure_ascii=False, indent=2))
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>量化研究评审</title>
<style>body{{font:15px/1.65 system-ui,sans-serif;background:#f4f6f8;color:#1e293b;margin:0}}
main{{max-width:1150px;margin:35px auto;padding:0 24px}}h1{{font-size:32px;margin-bottom:5px}}
section{{background:white;padding:22px 26px;margin:20px 0;border-radius:10px;border:1px solid #dbe2ea}}
.label{{font-size:13px;letter-spacing:2px;color:#64748b}}.warning{{border-left:5px solid #c77721;background:#fff8ed}}
table{{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}}td,th{{padding:10px;text-align:right;border-bottom:1px solid #e2e8f0}}th:first-child,td:first-child{{text-align:left}}th{{background:#eef2f6}}pre{{white-space:pre-wrap;word-break:break-word;font-size:12px}}.scroll{{overflow:auto}}a{{color:#155e75}}</style></head><body><main>
<div class="label">CAUSAL RESEARCH / WALK-FORWARD / AUDITABLE</div><h1>多方法滚动研究评审</h1>
<p>研究编号：{escape(report['study_id'])} · 协议 SHA256：{escape(report['protocol_hash'])}</p>
<section class="warning"><b>RESEARCH_ONLY · 不批准实盘</b><p>以下是上传历史数据上的因果回放，不是新增实盘业绩。
来源、公司行为、历史成分股及交易日历未经独立认证；2025 年起的留出区间未参与本轮实验。内部评分不是成功概率。</p></section>
<section><h2>研究规模与净收益</h2><p>独立参数 {report['distinct_parameters']} 组；训练折回放 {report['training_trials']} 次；
三类方法的样本外标的窗口 {report['oos_symbol_windows']} 个。每个标的独立 100,000 本金，等初始资金汇总，不代表共享保证金账户。</p>
<div class="scroll"><table><thead><tr><th>方法</th><th>累计收益</th><th>年化收益</th><th>最大回撤</th><th>Sharpe</th><th>成交次数</th><th>正收益窗口</th><th>综合 /5</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p>同标的被动持有参考：累计收益 {percent(baseline.get('total_return'))}，最大回撤 {percent(baseline.get('max_drawdown'))}。
参考账户在首个可成交开盘建仓、留有现金缓冲，不做日常再平衡。所有净值保留期末持仓并按收盘估值；未强制虚构收盘清仓。</p></section>
<section><h2>八维 Review</h2><div class="scroll"><table><tr><th>方法</th>{''.join('<th>'+escape(x)+'</th>' for x in axes)}</tr>{''.join(scores)}</table></div>
<p>收益：净样本外 Sharpe 的单调映射；稳定性：正收益标的窗口比例；鲁棒性：两倍成本及额外成交延迟压力窗口的正收益比例。
正确性仅对应本轮定向测试，不表示旧日报、旧数据后端和全仓门禁全部通过。统计修正为描述性区块 Bootstrap 与 Bonferroni，不冒称 DSR/PBO。</p></section>
<section><h2>方法来源与适用边界</h2><p>动量参考 Moskowitz、Ooi、Pedersen（2012）；通道突破参考 Brock、Lakonishok、LeBaron（1992）的交易区间突破；
均值回归参考 Poterba、Summers 的长期历史研究。本实现为日线、单资产、波动率缩放的研究变体；短期 Z-score 交易规则不是该均值回归论文的直接复现，更不是其盈利结论的外推。</p>
<p><a href="https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum">时间序列动量原作者资料</a> ·
<a href="https://onlinelibrary.wiley.com/doi/abs/10.1111/j.1540-6261.1992.tb04681.x">技术交易规则原论文</a> ·
<a href="https://www.nber.org/papers/w2343">均值回归 NBER 论文</a> ·
<a href="https://scholarworks.wmich.edu/math_pubs/42/">回测过拟合研究</a></p></section>
<section><details><summary><b>完整结构化评审、压力测试与评分公式</b></summary><pre>{safe_json}</pre></details></section>
</main></body></html>'''
