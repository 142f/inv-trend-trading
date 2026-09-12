"""Immutable, common-engine benchmark and nested walk-forward research entry point."""

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import platform

import numpy as np
import pandas as pd
import yaml

from inv_trend.application.EMA统一基准 import prepare, run, utc, CAPS, score, choose_training
from inv_trend.application.研究评分_v1 import block_bootstrap
from inv_trend.core.EMA趋势策略 import Config, metrics
from inv_trend.data.四资产行情_v1 import load_bundle
from inv_trend.storage.策略版本登记 import snapshot, immutable, digest, encoded, register


def variants():
    base = Config(risk=0.015)
    changes = [
        ("前轻后重", {"weights": (0.15, 0.30, 0.55)}, "降低早期试错，检验完整排列的增量价值"),
        ("前重后轻", {"weights": (0.45, 0.35, 0.20)}, "检验早进入能否抵消较多失败信号"),
        ("等额阶段", {"weights": (1 / 3, 1 / 3, 1 / 3)}, "检查阶段权重邻域稳定性"),
        ("更低风险", {"risk": 0.01}, "减少风险使用，比较超额收益和风险调整表现"),
        ("更高风险", {"risk": 0.02}, "在相同外部仓位上限内增加风险使用"),
        ("紧止损", {"stop_atr": 2.0}, "控制失败趋势持有时间"),
        ("宽止损", {"stop_atr": 4.0}, "减少噪声止损"),
        ("MACD减半", {"quality": "macd"}, "动能不支持时减少新建仓与加仓数量"),
        ("ADX减半", {"quality": "adx"}, "弱趋势降低新风险使用"),
        ("成交量减半", {"quality": "volume"}, "检验量能过滤是否值得保留"),
        ("空头减半", {"short_scale": 0.5}, "检验方向对称信号的空头风险分配"),
    ]
    return {
        "候选00": base,
        **{f"候选{i:02d}": replace(base, **d) for i, (_, d, _) in enumerate(changes, 1)},
    }, changes


def windows():
    return [
        dict(
            id=f"WF{y}",
            train_start=f"{y - 2}-07-01",
            validation_start=f"{y - 1}-07-01",
            train_end=str((utc(f"{y}-01-01") - pd.Timedelta(days=7)).date()),
            test_start=f"{y}-01-01",
            test_end=f"{y + 1}-01-01" if y < 2026 else "2026-04-21",
        )
        for y in range(2020, 2027)
    ]


def write_result(folder, summary, result):
    folder.mkdir(parents=True, exist_ok=False)
    paths = []
    for name, frame in {
        "逐K权益": summary["equity"],
        "逐日收益": summary["returns"],
        "成交订单": result.orders,
        "分阶段成交": result.trade_details,
        "期末未平仓": result.open_trade_details,
        "交易": result.trades,
        "资金流水": result.cash_ledger,
        "逐K决策": result.decisions,
        "资产归因": result.attribution,
    }.items():
        path = folder / f"{name}.csv.gz"
        frame.to_csv(
            path, index=isinstance(frame, pd.Series), compression={"method": "gzip", "mtime": 0}
        )
        paths.append(path)
    immutable(folder / "指标.json", summary["metrics"])
    return paths + [folder / "指标.json"]


def folds(summary, wins, baseline):
    out = []
    for w in wins:
        # Return dated at next midnight belongs to previous day's completed bar.
        r = summary["returns"].loc[
            (summary["returns"].index > utc(w["test_start"]))
            & (summary["returns"].index <= utc(w["test_end"]))
        ]
        b = baseline["returns"].reindex(r.index)
        m = metrics(r)
        m["excess_buy_hold"] = m["total"] - metrics(b)["total"]
        out.append(dict(window=w["id"], **m))
    return out


def comparison(summary, references):
    m = summary["metrics"]
    return {
        name: {
            "return_difference": m["total"] - ref["metrics"]["total"],
            "annual_return_difference": m["annual"] - ref["metrics"]["annual"],
            "relative_wealth": (1 + m["total"]) / (1 + ref["metrics"]["total"]) - 1,
            "sharpe_difference": m["sharpe"] - ref["metrics"]["sharpe"],
            "drawdown_difference": m["drawdown"] - ref["metrics"]["drawdown"],
            "score_difference": score(m) - score(ref["metrics"]),
        }
        for name, ref in references.items()
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--study", default=datetime.now(timezone.utc).strftime("统一基准_%Y%m%dT%H%M%S%fZ")
    )
    parser.add_argument(
        "--inputs-from", type=Path, help="reuse verified frozen data from an earlier study"
    )
    args = parser.parse_args()
    if Path(args.study).name != args.study or args.study in (".", ".."):
        raise ValueError("study must be one directory name")
    root = Path("outputs/EMA统一基准研究") / args.study
    root.mkdir(parents=True, exist_ok=False)
    code = snapshot(".", root / "代码快照")
    if args.inputs_from:
        old_protocol = json.loads(
            (args.inputs_from / "冻结实验协议.json").read_text(encoding="utf-8")
        )
        frames, lineage = {}, {}
        for s, item in old_protocol["input"].items():
            source = args.inputs_from / Path(item["path"]).name
            if digest(source.read_bytes()) != item["sha256"]:
                raise ValueError("frozen input hash mismatch")
            frames[s] = pd.read_parquet(source)
            lineage[s] = item["data_version"]
    else:
        bundle = load_bundle(mode="RESEARCH_ASSUMPTION")
        frames = {
            s: f.loc[(f.timestamp >= utc("2017-08-17")) & (f.timestamp < utc("2026-04-21"))].copy()
            for s, f in bundle.frames.items()
        }
        lineage = bundle.lineage
    pinned = {}
    for s, f in frames.items():
        path = root / f"冻结行情_{s}.parquet"
        f.to_parquet(path, index=False)
        pinned[s] = dict(
            path=str(path), sha256=digest(path.read_bytes()), rows=len(f), data_version=lineage[s]
        )
    data = prepare(frames)
    configs, changes = variants()
    wins = windows()
    native = yaml.safe_load(Path("src/inv_trend/config/strategy.yaml").read_text(encoding="utf-8"))
    publication = json.loads(Path("config/发布状态_v2.json").read_text(encoding="utf-8"))
    protocol = dict(
        assets=list(data),
        input=pinned,
        initial_equity=100000,
        external_caps=CAPS,
        cost_bps=5,
        slippage_bps=5,
        short_carry_bps_year=300,
        financing_bps_year=0,
        symbol_cap=0.225,
        contract_multiplier=1,
        qty_step=0.000001,
        execution_engine="TurtleBacktester",
        windows=wins,
        history_previously_inspected=True,
        untouched_holdout=False,
        calendar="provider daily label / next UTC midnight close; metals calendar unverified",
        capital="one continuous 100000 account; no resets at outer fold boundaries",
        selection="each round adds one predeclared candidate; only inner validation score selects each outer-window policy",
        score="Sharpe+0.25*Calmar-2*MDD; strict improvement only, ties retain previous",
        validation="last 6 months of 18-month prefix, ending 7 days before test start; indicators use earlier warm-up",
        passive="equal initial budgets 22.5% per asset, 10% cash; next eligible open, no stops/rebalancing, terminal mark-to-market",
        cap_semantics="same entry/fill limits; existing positions can drift above entry cap; leverage margin rule shared",
        history_replay="old EMA parameters replayed with latest staged order implementation; original source archived separately; not original-engine reproduction",
        formal_status=publication,
        baseline_block="B6/frozen-v4 config rejected: entry_adx_min, entry_confirmation_bars, entry_atr_percentile_max absent from TurtleRules; local B6 summaries cannot be used as a common-data replay",
        production_enabled=False,
        code=code,
        python=platform.python_version(),
        numpy=np.__version__,
        pandas=pd.__version__,
    )
    immutable(root / "冻结实验协议.json", protocol)
    immutable(root / "候选参数.json", {k: asdict(v) for k, v in configs.items()})
    registry = root / "策略登记"
    context = dict(
        git_commit=code["git_commit"],
        code_hash=code["source_hash"],
        code_archive=code["archive"],
        data_version=digest(encoded(pinned)),
        backtest_config_hash=digest(encoded(protocol)),
        backtest_config=str(root / "冻结实验协议.json"),
        walk_forward=wins,
    )
    original_path = Path("outputs/EMA趋势十轮研究/完整研究结果.json")
    original = json.loads(original_path.read_text(encoding="utf-8"))
    original_code = json.loads(
        Path("outputs/EMA统一基准研究/历史快照/上一轮原始版本.json").read_text(encoding="utf-8")
    )
    old_context = dict(
        git_commit=original_code["git_commit"],
        code_hash=original_code["source_hash"],
        code_archive=original_code["archive"],
        data_version=original["protocol"]["lineage"],
        backtest_config=original["protocol"],
        walk_forward=original["protocol"]["development"],
        imported_historical=True,
        original_creation_time="not recorded; registry time is import time",
    )
    for old in original["rounds"]:
        register(
            registry,
            strategy_id="原始EMA历史实验",
            strategy_version=f"1.{old['round']:02d}",
            previous="按原始结果中accepted字段追溯",
            parameters=old["config"],
            context=old_context,
            summary=old["metrics"],
            differences=old["changes"],
            results=[original_path],
        )
    start, end = wins[0]["test_start"], wins[-1]["test_end"]
    reference_configs = {
        "BuyHold": ("passive", Config()),
        "当前可运行_corrected_v2": ("turtle", Config()),
        "历史EMA初版": ("ema", Config()),
        "历史EMA最终参数": ("ema", replace(Config(), risk=0.015, quality="macd", short_scale=0.5)),
        "本轮EMA初始": ("ema", configs["候选00"]),
    }
    references = {}
    baseline_rows = {}
    for name, (kind, cfg) in reference_configs.items():
        summ, result = run(data, cfg, start, end, kind=kind, turtle_rules=native["turtle"]["rules"])
        refs = write_result(root / "基准" / name, summ, result)
        references[name] = summ
        register(
            registry,
            strategy_id=name,
            strategy_version="统一重测1",
            previous=None,
            parameters=(
                {"rules": native["turtle"]["rules"], "uniform_overrides": CAPS}
                if kind == "turtle"
                else asdict(cfg)
            ),
            context=context,
            summary=summ["metrics"],
            differences="同数据、同执行器重新回放；不复用旧业绩",
            results=refs,
        )
        baseline_rows[name] = summ["metrics"]
        print("基准完成", name, round(summ["metrics"]["total"], 4), flush=True)
    immutable(
        root / "不可复现基准.json",
        dict(
            formal_version="滚动策略_v2/B6",
            historical="冻结v4",
            status="UNAVAILABLE_NOT_SUBSTITUTED",
            reason=protocol["baseline_block"],
            source="config/发布状态_v2.json",
        ),
    )
    # Compute only physically clipped inner histories. Selection sees no outer return object.
    training = {}
    for w in wins:
        prefix = {s: f.loc[f.bar_end < utc(w["test_start"])].copy() for s, f in data.items()}
        training[w["id"]] = {}
        for cid, cfg in configs.items():
            summ, _ = run(prefix, cfg, w["validation_start"], w["train_end"])
            training[w["id"]][cid] = dict(
                scope="INNER_VALIDATION",
                candidate_id=cid,
                start=w["validation_start"],
                end=w["train_end"],
                metrics=summ["metrics"],
            )
        print("内层验证完成", w["id"], flush=True)
    immutable(root / "内层验证证据.json", training)
    current = {w["id"]: "候选00" for w in wins}
    previous = references["本轮EMA初始"]
    reviews = []
    effect_cache = {}
    for i, (name, delta, why) in enumerate(changes, 1):
        cid = f"候选{i:02d}"
        accepted = []
        schedule = []
        for w in wins:
            old = current[w["id"]]
            rows = [training[w["id"]][old], training[w["id"]][cid]]
            chosen = choose_training(rows)
            if score(rows[1]["metrics"]) <= score(rows[0]["metrics"]):
                chosen = old
            if chosen != old:
                accepted.append(w["id"])
            current[w["id"]] = chosen
            schedule.append(
                dict(
                    effective_at=w["test_start"],
                    trained_through=w["train_end"],
                    candidate_id=chosen,
                    parameters=asdict(configs[chosen]),
                    previous_candidate=old,
                    selection_scope="INNER_VALIDATION",
                    training_hash=digest(encoded(rows)),
                )
            )
        folder = root / f"第{i:02d}轮_{name}"
        immutable(folder / "参数事前锁定.json", schedule)
        # Even rejected candidates are replayed and audited under the common outer conditions.
        challenger, challenger_result = run(data, configs[cid], start, end)
        challenger_files = write_result(
            folder / "新候选固定参数回放", challenger, challenger_result
        )
        register(
            registry,
            strategy_id="EMA固定候选",
            strategy_version=f"2.{i:02d}",
            previous="候选00",
            parameters=asdict(configs[cid]),
            context=context,
            summary=challenger["metrics"],
            differences=delta,
            results=challenger_files,
        )
        summ, result = run(data, configs["候选00"], start, end, schedule=schedule)
        refs = write_result(folder / "滚动策略", summ, result)
        comp = comparison(summ, {**references, "上一轮滚动策略": previous})
        fs = folds(summ, wins, references["BuyHold"])
        auxiliary = {}
        # Paired ablations on the exact same locked policy path. They never feed selection.
        mutations = {
            "即时确认": dict(confirmation=0),
            "确认2日": dict(confirmation=2),
            "确认4日": dict(confirmation=4),
            "移除阶段1": dict(disabled=(0,)),
            "移除阶段2": dict(disabled=(1,)),
            "移除阶段3": dict(disabled=(2,)),
            "仅10_20": dict(independent=True, weights=(1.0, 0.0, 0.0)),
            "仅10_55": dict(independent=True, weights=(0.0, 1.0, 0.0)),
            "仅20_55": dict(independent=True, weights=(0.0, 0.0, 1.0)),
            "无辅助": dict(quality="none"),
            "MACD": dict(quality="macd"),
            "ADX": dict(quality="adx"),
            "成交量": dict(quality="volume"),
        }
        for label, change in mutations.items():
            altered = [
                {**p, "parameters": asdict(replace(Config(**p["parameters"]), **change))}
                for p in schedule
            ]
            key = digest(
                encoded([{k: p[k] for k in ("effective_at", "parameters")} for p in altered])
            )
            if key not in effect_cache:
                a, ar = run(data, configs["候选00"], start, end, schedule=altered)
                apaths = write_result(root / "消融证据" / key, a, ar)
                effect_cache[key] = dict(
                    metrics=a["metrics"],
                    folds=folds(a, wins, references["BuyHold"]),
                    evidence=[str(p) for p in apaths],
                )
            auxiliary[label] = {
                **effect_cache[key],
                "delta_total": effect_cache[key]["metrics"]["total"] - summ["metrics"]["total"],
            }
        excess = summ["returns"] - references["BuyHold"]["returns"]
        bootstrap = block_bootstrap(excess, trials=11, seed=20260909, samples=1000)
        ds = score(summ["metrics"]) - score(previous["metrics"])
        record = dict(
            round=i,
            change=name,
            parameters_delta=delta,
            why=why,
            accepted_in_inner_windows=accepted,
            score=score(summ["metrics"]),
            score_change=ds,
            metrics=summ["metrics"],
            challenger_metrics=challenger["metrics"],
            comparisons=comp,
            folds=fs,
            locked_parameters=schedule,
            ablations=auxiliary,
            parameter_changes=sum(
                a["candidate_id"] != b["candidate_id"] for a, b in zip(schedule, schedule[1:])
            ),
            fold_sharpe_std=float(np.std([f["sharpe"] for f in fs])),
            excess_bootstrap=bootstrap,
            review=f"{why}；内层{len(accepted)}/7窗口保留新候选。外层评分变化{ds:+.4f}，表现{'改善' if ds > 0 else '恶化' if ds < 0 else '不变'}。外层结果不反向修改锁定参数。",
            promotion="未发布：历史已见、B6不可复现、尚无真正未见数据",
        )
        lifecycle = {}
        for states in result.decisions.get("ema_states", []):
            for side, values in states.items():
                for j, value in enumerate(values, 1):
                    key = f"{side}:{j}:{value}"
                    lifecycle[key] = lifecycle.get(key, 0) + 1
        record["lifecycle_counts"] = lifecycle
        immutable(folder / "Review与评分.json", record)
        register(
            registry,
            strategy_id="EMA滚动策略",
            strategy_version=f"2.WF{i:02d}",
            previous=f"2.WF{i - 1:02d}" if i > 1 else "本轮EMA初始",
            parameters=schedule,
            context=context,
            summary=summ["metrics"],
            differences=record["review"],
            results=refs + [folder / "Review与评分.json"],
        )
        reviews.append(record)
        previous = summ
        print("轮次完成", i, record["review"], flush=True)
    result_data = dict(
        protocol=str(root / "冻结实验协议.json"),
        benchmarks=baseline_rows,
        rounds=reviews,
        final=reviews[-1],
        publication="RESEARCH_ONLY",
        unavailable=["正式生产版本不存在", "B6/冻结v4缺少可运行匹配实现"],
    )
    immutable(root / "完整对比结果.json", result_data)
    # Report is generated entirely from this run's numerical artifacts.
    lines = [
        "# EMA统一基准与版本审计",
        "",
        "完成11轮。所有可运行版本使用冻结的BTC/ETH/XAU/XAG行情、10万初始资金、单边手续费/滑点各5bp、空头年成本3%、相同入场仓位上限和7个年度Walk-Forward窗口。连续账户跨窗口持仓，资金不重置。",
        "",
        "历史数据已在上一轮及此前研究中查看；本次是严格时序的历史外层回放，不是新获得的未知未来证据。每个窗口只根据之前的内层验证评分决定是否保留候选，外层成绩不反馈选型。",
        "",
        "正式生产版本不存在；发布文件所指B6和冻结v4的参数与当前TurtleRules不兼容，无法准确复现。下表current corrected-v2是当前可运行实现，不冒充B6。上一轮EMA只将参数迁移到统一引擎回放，原始代码和结果保留；不能将两种执行器的业绩差异归功于信号。",
        "",
        "|策略|总收益|年化|超额BH|回撤|Sharpe|Calmar|胜率|盈亏比|PF|仓位利用率|",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    allmetrics = {**baseline_rows, **{f"第{x['round']:02d}轮": x["metrics"] for x in reviews}}
    bh = baseline_rows["BuyHold"]

    def val(v):
        return "N/A" if v is None else f"{v:.3f}"

    for label, m in allmetrics.items():
        lines.append(
            f"|{label}|{m['total']:.2%}|{m['annual']:.2%}|{m['total'] - bh['total']:+.2%}|{m['drawdown']:.2%}|{m['sharpe']:.3f}|{m['calmar']:.3f}|{m['win_rate']:.2%}|{val(m['payoff'])}|{val(m['profit_factor'])}|{m['utilization']:.2%}|"
        )
    lines += ["", "## 每轮Review", ""] + [
        f"- 第{x['round']}轮，{x['change']}：{x['review']}" for x in reviews
    ]
    last = reviews[-1]
    m = last["metrics"]
    lines += [
        "",
        "## 最终回答",
        "",
        f"最终相对Buy & Hold收益差为{m['total'] - bh['total']:+.2%}；相对当前可运行corrected-v2为{m['total'] - baseline_rows['当前可运行_corrected_v2']['total']:+.2%}；相对上一轮EMA最终参数统一重测为{m['total'] - baseline_rows['历史EMA最终参数']['total']:+.2%}。这是累计收益百分点差，不是年化alpha。",
        f"多头贡献{m['long_pnl']:.2%}，空头贡献{m['short_pnl']:.2%}；EMA10/20、10/55、20/55阶段贡献分别{m['stages']['1']:.2%}、{m['stages']['2']:.2%}、{m['stages']['3']:.2%}，包含期末未平仓浮盈亏，精确相加到账户总收益。所有版本统一期末估值，不制造无行情日的平仓。胜率以已平仓分笔为口径，各阶段相关，不是独立样本；BuyHold没有已平仓分笔，其胜率/PF不适用。",
        f"平均仓位{m['utilization']:.2%}，BH为{bh['utilization']:.2%}，所以风险和收益必须同时看，低回撤不能单独证明alpha。共同仓位约束是下单/成交上限：总90%，单资产22.5%；持有期间价格变化可漂移，统一执行引擎的保证金规则适用于全部策略。",
        "EMA阶段仅按已确认前缀建仓，每资产每根K最多加一阶段；失效时下一真实开盘只退出失效阶段，ATR跟踪止损作用于该资产共同持仓。辅助系数只调整新增单位数量，不对旧单位逐日再平衡。此次模块化执行语义与上一轮袖套每日调整不同，旧参数重测和原始结果分开保存。",
        f"样本外各窗口Sharpe标准差{last['fold_sharpe_std']:.3f}，参数换档{last['parameter_changes']}次。超额日收益20日区块Bootstrap结果：`{json.dumps(last['excess_bootstrap'], ensure_ascii=False)}`。这些检验仅描述已见历史，不能修复历史选择偏差。",
        "没有条件证明新策略优于正式/B6策略，也不能证明已知历史上的优势会在真正未知未来成立；本轮不升级正式版本。下一次验证必须使用冻结后新增、此前未查看且四资产齐全的数据，并取得B6匹配代码或明确指定正式基准。",
        "",
        "## 配对消融（最终参数路径冻结）",
        "",
        "|实验|总收益|相对最终收益变化|回撤|Sharpe|",
        "|---|---:|---:|---:|---:|",
    ]
    for label, a in last["ablations"].items():
        q = a["metrics"]
        lines.append(
            f"|{label}|{q['total']:.2%}|{a['delta_total']:+.2%}|{q['drawdown']:.2%}|{q['sharpe']:.3f}|"
        )
    lines += [
        "",
        "3日确认是否值得、辅助指标是否改善、各阶段是否增加收益，请用上表同时比较收益/回撤/Sharpe；不能仅因确认通过或高阶段盈利就称有效。所有11轮都有相同消融与基准字段，详见各轮Review JSON。",
        "",
        "## 追溯与复现",
        "",
        "每个固定候选及每轮滚动策略均登记strategy_id/version、父版本、代码哈希/Git HEAD、配置、冻结数据哈希、执行配置、窗口、结果摘要、创建时间与差异；登记文件使用排他创建，重复study名称拒绝覆盖。源代码归档包含脏工作区真实字节，不能仅凭Git HEAD复现。",
        "",
        "行情和历史规格为研究代理：BTC/ETH现货价格模拟可做空合约，USDT/USD=1，金属交易时段和真实借贷/资金费率未认证，成交量容量未模拟。Buy & Hold使用同资金、同成本，在共同入场上限内一次建仓，10%现金；非100%投资、不定期再平衡。",
        "",
        "复现命令：`.venv/Scripts/python.exe scripts/EMA统一基准研究.py --study 新的唯一研究名`。此次冻结行情保存在本研究目录；复现前核对协议中的源数据哈希，若数据头改变应创建新研究。",
    ]
    (root / "统一基准研究报告.md").write_text("\n".join(lines), encoding="utf-8")
    pd.DataFrame(
        [
            dict(
                version=k,
                **{x: y for x, y in v.items() if x != "stages"},
                excess_buy_hold=v["total"] - bh["total"],
            )
            for k, v in allmetrics.items()
        ]
    ).to_csv(root / "统一指标对照.csv", index=False, encoding="utf-8-sig")
    immutable(
        root / "制品校验.json",
        {
            p.relative_to(root).as_posix(): digest(p.read_bytes())
            for p in root.rglob("*")
            if p.is_file()
        },
    )
    print("研究完成", root, flush=True)


if __name__ == "__main__":
    main()
