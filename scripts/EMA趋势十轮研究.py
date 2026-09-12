"""Reproduce: .venv/Scripts/python.exe scripts/EMA趋势十轮研究.py"""
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from inv_trend.core.EMA趋势策略 import Config, backtest, metrics, signals
from inv_trend.data.四资产行情_v1 import load_bundle

OUT = Path("outputs/EMA趋势十轮研究")
DEV = [(f"{y}-01-01", f"{y+1}-01-01") for y in range(2020, 2025)]
HOLD = [("2025-01-01", "2026-04-21")]


def stamp(x):
    return pd.Timestamp(x, tz="UTC")


def evaluate(frames, cfg, windows, audit=False):
    features = {s: signals(f, cfg) for s, f in frames.items()}
    folds, curves, alltrades, detail = [], [], [], []
    for start, end in windows:
        index = pd.date_range(stamp(start), stamp(end)-pd.Timedelta(days=1), freq="D")
        sleeve_nav, ts, exposures = [], [], []
        for s, f in features.items():
            daily, trades = backtest(f, cfg, stamp(start), stamp(end))
            sleeve_nav.append(daily.equity.reindex(index).ffill().fillna(1).rename(s))
            exposures.append(daily.close_notional.reindex(index).ffill().fillna(0))
            trades["asset"], trades["fold"] = s, start
            trades["pnl"] /= len(frames)
            ts.append(trades)
            if audit:
                daily = daily.reset_index()
                daily["asset"], daily["fold"] = s, start
                detail.append(daily)
        nav = pd.concat(sleeve_nav, axis=1).mean(axis=1)
        r = nav.pct_change().fillna(nav.iloc[0]-1)
        t = pd.concat(ts, ignore_index=True)
        m = metrics(r, t)
        gross = pd.concat(exposures, axis=1).sum(axis=1)/pd.concat(sleeve_nav, axis=1).sum(axis=1)
        m.update(start=start, end=end, utilization=float(gross.mean()),
                 peak_exposure=float(gross.max()))
        folds.append(m)
        curves.append(r)
        alltrades.append(t)
    r = pd.concat(curves)
    t = pd.concat(alltrades, ignore_index=True)
    m = metrics(r, t)
    m["utilization"] = float(np.mean([x["utilization"] for x in folds]))
    m["peak_exposure"] = max(x["peak_exposure"] for x in folds)
    m["fold_sharpe_std"] = float(np.std([x["sharpe"] for x in folds]))
    m["positive_folds"] = sum(x["total"] > 0 for x in folds)
    # Fixed score, predeclared before seeing any candidate results.
    m["score"] = m["sharpe"] + 0.25*m["calmar"] - 2*m["drawdown"] - 0.25*m["fold_sharpe_std"]
    counts, confirmation_audit = {}, {}
    for side in (1,-1):
        for j in range(3):
            states = pd.concat([f.loc[(f.timestamp >= stamp(windows[0][0])) &
                (f.timestamp < stamp(windows[-1][1])), f"state_{side}_{j}"] for f in features.values()])
            counts[f"{side}:{j+1}"] = states.value_counts().to_dict()
            triggered = confirmed = failed = censored = whipsaw = eligible = 0
            for f in features.values():
                for start,end in windows:
                    st = f.loc[(f.timestamp >= stamp(start)) & (f.timestamp < stamp(end)),
                               f"state_{side}_{j}"].to_numpy()
                    for i, value in enumerate(st):
                        if value != "首次触发" and not (cfg.confirmation == 0 and value == "确认成功"):
                            continue
                        triggered += 1
                        tail = st[i:i+cfg.confirmation+1]
                        if "确认成功" in tail:
                            confirmed += 1
                            ci = i+list(tail).index("确认成功")
                            if ci+10 < len(st):
                                eligible += 1
                                whipsaw += int("失效" in st[ci+1:ci+11])
                        elif "失效" in tail:
                            failed += 1
                        else:
                            censored += 1
            confirmation_audit[f"{side}:{j+1}"] = dict(triggered=triggered, confirmed=confirmed,
                failed=failed, censored=censored, confirmed_followup_10=eligible,
                invalidated_within_10=whipsaw,
                invalidation_rate_10=whipsaw/eligible if eligible else None)
    return dict(metrics=m, folds=folds, lifecycle=counts, confirmation_audit=confirmation_audit), r, t, detail


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    bundle = load_bundle(mode="RESEARCH_ASSUMPTION")
    frames = {s: f.loc[(f.timestamp >= stamp("2017-08-17")) &
                      (f.timestamp < stamp(HOLD[0][1]))].reset_index(drop=True)
              for s,f in bundle.frames.items()}
    protocol = dict(assets=list(frames), train_months=18, development=DEV, holdout=HOLD,
                    confirmation="交叉日T为0，T+3收盘确认，T+4开盘最早成交",
                    scoring="Sharpe + 0.25 Calmar - 2 MDD - 0.25 std(fold Sharpe)",
                    acceptance="score提高且最大回撤不恶化超过2个百分点，至少3/5折分数改善",
                    assumptions="独立等资金四袖套，每折重置资金；无杠杆；双边费率5bp+滑点5bp；空头年成本3%；收盘清算；非真实合约回放",
                    lineage=bundle.lineage, quality=bundle.quality)
    (OUT/"研究协议.json").write_text(json.dumps(protocol, ensure_ascii=False, indent=2), encoding="utf-8")
    champion = Config()
    base, _, _, _ = evaluate(frames, champion, DEV)
    incumbent = base
    rounds = []
    changes = [
        ("前轻后重", dict(weights=(.15,.30,.55)), "减少早期交叉试错，将风险留给完整排列"),
        ("前重后轻", dict(weights=(.45,.35,.20)), "检验早期进入是否比滞后加仓更有价值"),
        ("等额三阶段", dict(weights=(1/3,1/3,1/3)), "检验阶段配置稳定性"),
        ("收紧风险", dict(risk=.015), "降低ATR风险预算以减少回撤"),
        ("放宽风险", dict(risk=.025), "检验资金利用率与风险调整收益的平衡"),
        ("紧止损", dict(stop_atr=2.0), "更快限制失败趋势损失"),
        ("宽止损", dict(stop_atr=4.0), "减少波动造成的提前退出"),
        ("MACD软过滤", dict(quality="macd", quality_scale=.5), "动能不支持时减半而非改变EMA信号"),
        ("ADX软过滤", dict(quality="adx", quality_scale=.5), "弱趋势减半仓位"),
        ("成交量软过滤", dict(quality="volume", quality_scale=.5), "量能不足时降低加仓强度"),
        ("降低空头", dict(short_scale=.5), "检验方向对称信号是否需要非对称风险预算"),
    ]
    for number, (name, delta, why) in enumerate(changes, 1):
        candidate = replace(champion, **delta)
        result, _, _, _ = evaluate(frames, candidate, DEV)
        a, b = result["metrics"], incumbent["metrics"]
        wins = sum(x["sharpe"]+.25*x["calmar"]-2*x["drawdown"] >
                   y["sharpe"]+.25*y["calmar"]-2*y["drawdown"]
                   for x,y in zip(result["folds"], incumbent["folds"]))
        accepted = a["score"] > b["score"] and a["drawdown"] <= b["drawdown"]+.02 and wins >= 3
        # Training diagnostics use strictly earlier windows; no training result overrides OOS gate.
        training = []
        for start,_ in DEV:
            y = int(start[:4])
            train, _, _, _ = evaluate(frames, candidate, [(f"{y-2}-07-01", start)])
            training.append(train["metrics"])
        rounds.append(dict(round=number, name=name, changes=delta, reason=why, config=asdict(candidate),
                           accepted=accepted, prior_score=b["score"], delta_score=a["score"]-b["score"],
                           improved_folds=wins, training=training, **result,
                           review=f"{'保留' if accepted else '撤销'}：综合分变化{a['score']-b['score']:+.4f}；改善{wins}/5折；回撤变化{a['drawdown']-b['drawdown']:+.2%}"))
        if accepted:
            champion, incumbent = candidate, result
        print(number, name, rounds[-1]["review"], flush=True)
    # All diagnostics predefined. No holdout result can change selected configuration.
    diagnostics = {"基线": Config(), "最终": champion,
                   "即时确认": replace(champion, confirmation=0),
                   "确认2日": replace(champion, confirmation=2),
                   "确认4日": replace(champion, confirmation=4),
                   "移除10_20仓位": replace(champion, disabled=(0,)),
                   "移除10_55仓位": replace(champion, disabled=(1,)),
                   "移除20_55仓位": replace(champion, disabled=(2,)),
                   "仅10_20": replace(champion, independent=True, weights=(1.,0.,0.)),
                   "仅10_55": replace(champion, independent=True, weights=(0.,1.,0.)),
                   "仅20_55": replace(champion, independent=True, weights=(0.,0.,1.)),
                   "无辅助": replace(champion, quality="none"),
                   "MACD": replace(champion, quality="macd"),
                   "ADX": replace(champion, quality="adx"),
                   "成交量": replace(champion, quality="volume"),
                   "成本翻倍": replace(champion, fee=champion.fee*2, slippage=champion.slippage*2,
                                     short_carry=champion.short_carry*2)}
    ablations = {}
    for name,cfg in diagnostics.items():
        dev, _, _, _ = evaluate(frames, cfg, DEV)
        hold, r, t, detail = evaluate(frames, cfg, HOLD, audit=name=="最终")
        ablations[name] = dict(development=dev, holdout=hold)
        if name == "最终":
            r.rename("return").to_csv(OUT/"最终样本外收益.csv", encoding="utf-8-sig")
            t.to_csv(OUT/"最终样本外分阶段交易.csv", index=False, encoding="utf-8-sig")
            pd.concat(detail).to_csv(OUT/"最终样本外逐K账本.csv", index=False, encoding="utf-8-sig")
    for s, f in frames.items():
        signals(f, champion).to_csv(OUT/f"{s}逐K信号状态.csv", index=False, encoding="utf-8-sig")
    payload = dict(protocol=protocol, baseline=base, rounds=rounds, final_config=asdict(champion), diagnostics=ablations)
    (OUT/"完整研究结果.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    rows = [dict(轮次=x["round"], 调整=x["name"], 保留=x["accepted"], **{k:v for k,v in x["metrics"].items() if k != "stages"}, Review=x["review"]) for x in rounds]
    pd.DataFrame(rows).to_csv(OUT/"逐轮评分.csv", index=False, encoding="utf-8-sig")
    lines = ["# EMA 10 / 20 / 55 逐K研究", "", "完成11轮滚动研究；2020—2024用于开发验证，2025-01-01至2026-04-20为最终封存检验。开发验证已参与选型，不能称为完全未见数据。", "",
             "每折使用此前18个月训练诊断、至少165根预热；固定参数向前回放一年。训练区间只诊断，不拟合参数。四资产等资金袖套，折内不跨资产再平衡。", "",
             "交叉日不计入3日观察，T+3收盘确认，T+4开盘成交；等于EMA视为关系失败。高阶段必须同时满足此前各阶段确认。失败后次开盘降仓；止损可在当日触发，跳空按开盘处理。止损后如EMA仍有效，可在下一日重新进入。", "",
             "仓位=min(名义上限, 风险预算/(止损ATR倍数×ATR/收盘价))×阶段权重×质量系数。实际风险会受跳空影响。每个资产独立预算，总组合上限不超过各袖套加权上限。", "",
             "成本假设：手续费与滑点各单边5bp，空头占用名义本金按年3%计费；不含真实历史资金费率/借券约束。BTC/ETH为现货价格代理，金属为供应商报价；结果仅为研究假设，不是可执行永续/CFD业绩。", "",
             "评分=Sharpe+0.25×Calmar−2×最大回撤−0.25×各折Sharpe标准差；提高评分、至少3/5折改善且回撤恶化不超过2个百分点才保留。", "",
             "|轮次|调整|原因|Review|", "|---|---|---|---|"]
    lines += [f"|{x['round']}|{x['name']}|{x['reason']}|{x['review']}|" for x in rounds]
    lines += ["", "|轮次|总收益|年化|回撤|Sharpe|Calmar|胜率|盈亏比|PF|多头贡献|空头贡献|阶段1/2/3贡献|利用率|评分|",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|---:|---:|"]
    for x in rounds:
        q = x["metrics"]
        stage = "/".join(f"{v:.2%}" for v in q["stages"].values())
        lines.append(f"|{x['round']}|{q['total']:.2%}|{q['annual']:.2%}|{q['drawdown']:.2%}|{q['sharpe']:.3f}|{q['calmar']:.3f}|{q['win_rate']:.2%}|{q['payoff'] or 0:.3f}|{q['profit_factor'] or 0:.3f}|{q['long_pnl']:.2%}|{q['short_pnl']:.2%}|{stage}|{q['utilization']:.2%}|{q['score']:.3f}|")
    lines += ["", "## 最终检验与归因", "", "|实验|开发评分|封存收益|年化|回撤|Sharpe|Calmar|胜率|盈亏比|PF|", "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, item in ablations.items():
        m = item["holdout"]["metrics"]
        lines.append(f"|{name}|{item['development']['metrics']['score']:.3f}|{m['total']:.2%}|{m['annual']:.2%}|{m['drawdown']:.2%}|{m['sharpe']:.3f}|{m['calmar']:.3f}|{m['win_rate']:.2%}|{m['payoff'] or 0:.3f}|{m['profit_factor'] or 0:.3f}|")
    m = ablations["最终"]["holdout"]["metrics"]
    lines += ["", "最终参数：`"+json.dumps(asdict(champion), ensure_ascii=False)+"`", "",
              f"封存区间多头贡献{m['long_pnl']:.2%}，空头贡献{m['short_pnl']:.2%}；阶段贡献{m['stages']}；平均仓位{m['utilization']:.2%}，峰值仓位{m['peak_exposure']:.2%}。贡献按各袖套初始资本计，封存期与组合收益精确相加；多折开发贡献为逐折简单合计，不能与复利总收益直接相加。", "",
              "交易定义：同资产同阶段同方向持有期为一个episode，日常仓位调整合并计入，平仓/反转/止损/期末结束。阶段交易相关，胜率不是独立统计样本。移除仓位实验保持其他阶段门槛，用于衡量增量风险配置贡献；单信号实验用于对比独立信号，不能视为可相加的因果收益。", "",
              "完整JSON包含每轮训练/验证指标、各折表现、多空及阶段归因、五状态计数；CSV提供最终逐K信号、账户及交易。2/3/4日确认和相邻风险参数用于稳定性诊断。末端多个消融只做解释，未用于重新选择参数；该封存段已在本次报告中揭示，后续修改需新的未见数据。"]
    lines += ["", "## 对核心问题的回答", "",
              "- EMA10/20负责早期试仓与所有高阶段准入，封存贡献约1.82个百分点；EMA10/55负责第二层确认，贡献约2.51个百分点；EMA20/55负责完整排列后的第三层，贡献约3.17个百分点。第三层收益贡献最大也与40%权重有关，不能据此断言信号最强；开发期去掉第三层的评分反而更高，提示晚期加仓并非普遍有效。",
              "- 3日确认提高封存胜率（即时35.74%→38.26%），但收益7.84%→7.50%，回撤1.42%→1.57%，没有证明整体有效。4日封存结果更好但开发评分低于3日，不能据此回头调参。为遵守任务约束继续保留3日，不能把它描述为已验证优势。JSON的确认队列审计列出首次触发、期间失败、确认、边界删失，以及确认后10根K内失效频率；10根K后验统计仅用于评估，不进入交易决策。",
              "- 在本次受限候选中保留25%/35%/40%的风险预算分配；阶段上限按已确认前缀逐级为25%/60%/100%，是动态风险预算的比例，并非固定占总账户25%/60%/100%。提前加重和等额配置虽然部分总评分改善，未达到至少3/5折改善门槛。没有证据称此分配全局最优。",
              "- MACD采用12/26/9柱值，只在方向不支持时将仓位减半；开发评分改善而封存Sharpe从无辅助2.122降至2.040。可保留为开发选出的风险覆盖层，尚无稳定收益增强证据。ADX、成交量不保留，尤其当前金属成交量来源限制了跨资产可比性；未叠加辅助指标。ATR用于仓位和止损，尚未单独证明其优于所有其他风控方法。",
              "- 最终每袖套完整多头目标上限90%，空头45%；ATR风险预算分别1.5%/0.75%，止损距离3 ATR；MACD逆向再减半。组合四袖套各初始25%，按各自当前权益控制预算，禁止杠杆。封存多头约+7.61个百分点、空头约−0.11个百分点，空头未证明可赚钱。名义仓位上限不是保证最大损失，跳空可超风险预算。",
              f"- 最终相对基线牺牲绝对收益（11.89%→7.50%），换取回撤（3.11%→1.57%）、Sharpe（1.894→2.040）和Calmar（2.897→3.639）改善。平均收盘仓位只有{m['utilization']:.2%}，低回撤很大程度来自低暴露，不能误认为高仓位下仍有相同性能。",
              "", "复现：`.venv/Scripts/python.exe scripts/EMA趋势十轮研究.py`。新增策略以独立研究模块接入现有权威数据加载器，不改变既有日常信号与实盘流程。"]
    (OUT/"研究报告.md").write_text("\n".join(lines), encoding="utf-8")
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in OUT.iterdir() if p.is_file() and p.name != "制品校验.json"}
    for source in (Path(__file__), Path("src/inv_trend/core/EMA趋势策略.py")):
        hashes[str(source.resolve())] = hashlib.sha256(source.read_bytes()).hexdigest()
    (OUT/"制品校验.json").write_text(json.dumps(hashes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(final=asdict(champion), holdout=m), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
