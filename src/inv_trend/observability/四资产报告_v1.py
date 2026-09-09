"""Standalone audit report generated from persisted SQLite evidence."""
from html import escape
import json
from pathlib import Path

from inv_trend.storage.组合研究_v1 import PortfolioRepository

FIELDS = ("CAGR", "volatility", "MDD", "Sharpe", "Sortino", "Calmar", "Cost", "turnover", "OOS_Windows", "Positive_Windows")


def fmt(value, key=""):
    if value is None:
        return "N/A"
    if isinstance(value, str):
        return value
    if key in ("CAGR", "volatility", "MDD", "total_return", "time_in_market"):
        return f"{value:.2%}"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def answers(report):
    summary, bench = report["summary"], report["benchmarks"]
    active = {k: v for k, v in summary.items() if not k.endswith("_risk_matched")}
    btc, equal = bench["BTC Buy & Hold"], bench["Passive Equal Weight"]
    better_btc = [k for k, v in active.items() if v["CAGR"] > btc["CAGR"]]
    better_equal = [k for k, v in active.items() if v["CAGR"] > equal["CAGR"]]
    matched = [k for k, v in active.items() if v["CAGR"] > summary[k+"_risk_matched"]["CAGR"] and
               (v["Sharpe"] or 0) > (summary[k+"_risk_matched"]["Sharpe"] or 0)]
    rejected = [k for k, v in report["acceptance"].items() if v["status"] == "REJECTED"]
    survived = [k for k, v in report["acceptance"].items() if v["stress_survival"]]
    def names(xs):
        return ", ".join(xs) if xs else "无"
    return [
        f"Q1 四资产是否优于单持 BTC：研究假设下 CAGR 更高者：{names(better_btc)}。BTC CAGR={btc['CAGR']:.2%}；收益、回撤须分别比较，不能归纳为全面优于。",
        f"Q2 是否优于四资产 Buy & Hold：研究假设下 CAGR 更高者：{names(better_equal)}。等权被动 CAGR={equal['CAGR']:.2%}。",
        f"Q3 是否优于风险匹配被动：同时超过训练期校准被动的 CAGR、Sharpe 者：{names(matched)}。OOS 实现波动并非完全相同；未使用完整 OOS 波动事后缩放收益。",
        "Q4 优势来源：账户禁止借款，排除融资杠杆来源；择时、配置、风险控制仍存在交互，当前对照不构成完整因果归因，不能给出各因素贡献百分比。",
        "Q5 真正样本外优势：没有已证明者。这里只是 replayed_oos；历史修订、会话、FX 与合同未认证，paper_forward 尚未运行。",
        f"Q6 应淘汰策略：本轮预设数值门禁判为 REJECTED：{names(rejected)}。保留全部实验，不调参救活。其余仅为 CANDIDATE，不能 Accepted。",
        f"Q7 Core+Tactical：在本次回放中持仓时间 {active['core_tactical']['time_in_market']:.2%}，Momentum {active['momentum']['time_in_market']:.2%}。结构上保留核心风险敞口，但是否解决长期强趋势离场损失仍需趋势分段配对归因。",
        f"Q8 成本与延迟恶化：通过本次数值压力门禁者：{names(survived)}。20% worse execution 定义为滑点幅度增大20%，不是资产成交价恶化20%。",
        "Q9 高相关是否失效：已执行 Crypto、Metals、全资产相关矩阵冲击的配置敏感性回放；它只改变风险估计，不能替代相关性突变的联合收益路径压力验证。正式结论仍未成立。",
        "Q10 是否具备 paper trading 条件：否。历史合同、金属日历及 bid/ask、汇率与数据 vintage 门禁尚未通过，合成全额出资账户不能替代真实 CFD 执行。",
    ]


def export_report(study_id, *, data_root="data", output_root="docs/reviews", evidence=None):
    repo = PortfolioRepository(data_root)
    report = repo.read_report(study_id)
    evidence = evidence or {}
    rows = []
    for label, metrics in {**report["benchmarks"], **report["summary"]}.items():
        status = report["acceptance"].get(label, {}).get("status", "BENCHMARK")
        rows.append([study_id, label, "BTC/ETH/XAU/XAG" if " Buy & Hold" not in label else label.split()[0],
                     *[fmt(metrics.get(k), k) for k in FIELDS], status])
    headers = ["Version", "Strategy", "Universe", "CAGR", "Vol", "MDD", "Sharpe", "Sortino", "Calmar", "Cost", "Turnover", "OOS Windows", "Positive Windows", "Stress Result / Status"]
    md = [f"# 四资产组合研究验收：{study_id}", "", "**研究假设模式；正式验收未通过；禁止据此宣称可交易优势。**", "",
          "这是研究假设，不是正式交易所历史规格。", "",
          f"权威记录：`data/metadata/研究目录_v3.sqlite3`；study_id=`{study_id}`。",
          f"参数组合 {report['distinct_parameters']}；训练实验 {report['trials']}；失败 {report['failed_trials']}；5 个滚动窗口。",
          f"运行耗时 {report['elapsed_seconds']:.1f}s；缓存命中 {report['cache_hits']}。", "",
          "## 对比表", "", "|"+"|".join(headers)+"|", "|"+"|".join(["---"]*len(headers))+"|"]
    md.extend("|"+"|".join(r)+"|" for r in rows)
    md.extend(["", "费用为 fees+slippage+funding；滑点通过成交价扣除，Cost展示不再从权益重复扣减。",
               "买入持有基准保留5%现金缓冲，其余按首次已知权重购买，随后不再平衡；Inverse Vol首次权重见账本。",
               "Sharpe/Sortino采用零无风险利率；所有资产在UTC日历日估值并以365.25年化。", "",
               "## 风险匹配", "", "|Active|Active CAGR|Active Vol|Passive CAGR|Passive Vol|Active Sharpe|Passive Sharpe|", "|---|---|---|---|---|---|---|"])
    for family in report["acceptance"]:
        a, b = report["summary"][family], report["summary"][family+"_risk_matched"]
        md.append("|"+"|".join([family, fmt(a["CAGR"],"CAGR"), fmt(a["volatility"],"volatility"), fmt(b["CAGR"],"CAGR"), fmt(b["volatility"],"volatility"), fmt(a["Sharpe"]), fmt(b["Sharpe"])])+"|")
    md += ["", "被动目标仅用对应训练窗口校准并在 OOS 前锁定；表中公开实现波动差异，不能把近似匹配说成完全相同风险。", "",
           "## 压力测试", "", "|Strategy|Scenario|CAGR|Vol|MDD|Sharpe|Cost|", "|---|---|---|---|---|---|---|"]
    for family, review in report["acceptance"].items():
        for scenario, m in review["stress_metrics"].items():
            md.append("|"+"|".join([family, scenario, *[fmt(m[k],k) for k in ("CAGR","volatility","MDD","Sharpe","Cost")]])+"|")
    md += ["", "volatility_shock 与 *_corr_* 是估计矩阵敏感性，不是联合市场价格路径冲击。", "", "## 必答问题", ""] + [q+"\n" for q in answers(report)]
    best = max((v["Sharpe"] or 0) for k,v in report["summary"].items() if not k.endswith("_risk_matched"))
    scores = {"正确性": 6 if evidence.get("passed") else 0, "收益": float(np_clip(best*3, 0, 10)),
              "回撤": 5, "稳定性": 4, "鲁棒性": 2, "性能": 7, "代码质量": 6 if evidence.get("passed") else 0,
              "可解释性": 7}
    md += ["## Review 与评分", "", "评分为工程研究评审判断；收益分参考最高回放Sharpe，其他分为审慎人工判断，不构成策略准入。",
           "硬门禁失败不能由总分抵消。数据 lineage、执行合同：FAIL；paper_forward：NOT_RUN。", "",
           "|维度|0–10|", "|---|---|"] + [f"|{k}|{v:.1f}|" for k,v in scores.items()]
    md += ["", "测试证据：`"+json.dumps(evidence,ensure_ascii=False)+"`。", "",
           "本轮发现并保留的限制：", ""] + ["- "+x for x in report["protocol"]["limitations"]]
    md += ["", "Robustness："+report["robustness"], "",
           "下一轮仅补齐真实数据/合同、完整市场压力与归因证据；不得依据本轮 OOS 修改参数网格以追求跑赢。",
           "保留全部 trial、参数邻域、失败项、订单、成交、权益、风险、锁参、窗口与报告。数据库为权威，HTML/MD为其导出。", ""]
    markdown = "\n".join(md)
    def table(h, rs):
        return "<div class='scroll'><table><thead><tr>"+"".join("<th>"+escape(x)+"</th>" for x in h)+"</tr></thead><tbody>"+"".join("<tr>"+"".join("<td>"+escape(str(x))+"</td>" for x in r)+"</tr>" for r in rs)+"</tbody></table></div>"
    trials = repo.store.rows("SELECT window_id,experiment_id,family,parameters,metrics,status,error FROM parameter_trials WHERE study_id=? ORDER BY window_id,experiment_id",(study_id,))
    detail = [[r["window_id"],r["experiment_id"],r["family"],r["status"],r["error"] or "",r["parameters"],r["metrics"]] for r in trials]
    html = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>四资产组合研究验收</title>
<style>body{font:15px/1.7 system-ui,sans-serif;margin:32px;color:#172b3a;background:#f4f7fa}h1{font-size:28px}.warning{padding:16px;background:#fff0d6;border-left:5px solid #b15d00}table{border-collapse:collapse;background:white;width:100%}th,td{padding:8px 12px;border-bottom:1px solid #d8e0e8;text-align:left;white-space:nowrap}th{background:#17344b;color:white;position:sticky;top:0}.scroll{overflow:auto;max-height:70vh;margin:20px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:20px}input{padding:10px;width:min(90%,420px)}details{margin-top:20px}</style>
<h1>BTC / ETH / XAU / XAG 组合研究</h1><p class="warning">研究假设模式 · 正式验收 FAIL · 不具备 paper trading 条件<br>这是研究假设，不是正式交易所历史规格。</p>"""
    html += "<p>"+escape(study_id)+" · 160参数 / 800训练实验 / 5个replayed OOS窗口</p>"+table(headers,rows)
    html += "<h2>十项结论</h2>"+"".join("<p>"+escape(q)+"</p>" for q in answers(report))
    html += "<details><summary>完整验收、风险匹配与压力表</summary><pre>"+escape(markdown)+"</pre></details>"
    html += "<details><summary>全部实验，包括失败与非最佳参数</summary><label>筛选实验 <input id='filter' placeholder='策略、年份或参数'></label><div id='trials'>"+table(["Window","ID","Family","Status","Error","Parameters","Metrics"],detail)+"</div></details>"
    html += "<details><summary>参数邻域与锁参证据</summary><pre>"+escape(json.dumps({"neighborhoods":report["neighborhoods"],"locks":report["selections"]},ensure_ascii=False,indent=2))+"</pre></details>"
    html += "<script>document.getElementById('filter').addEventListener('input',function(){const q=this.value.toLowerCase();document.querySelectorAll('#trials tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));});</script></html>"
    out = Path(output_root)
    out.mkdir(parents=True,exist_ok=True)
    for suffix, content in (("md",markdown),("html",html)):
        name = study_id + "_验收." + suffix
        path = out / name
        if path.exists():
            raise FileExistsError("historical report cannot be overwritten: "+str(path))
        repo.report(study_id,name,content)
        path.write_text(content,encoding="utf-8")
    with repo.store.connect() as db:
        db.executemany("INSERT INTO review_scores VALUES(?,?,?,?)",[(study_id,"验收_v1",k,v) for k,v in scores.items()])
    repo.record("reviews",(study_id,"验收_v1"),dict(scores=scores,evidence=evidence,formal_status="FAIL"))
    return markdown


def np_clip(value, low, high):
    return min(high, max(low, value))
